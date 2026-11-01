"""``sync_now`` and ``retry_failed`` (SPEC §3.4a.9, §3.4a.11): starting a run and reporting
one already going, the phases not yet shipped, and releasing only the caller's own
quarantined changes. The engine's side on a seeded world, then the tools' refusals through
the real ASGI app."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest

from syncshelf.app import create_app
from syncshelf.sync import identity, runs
from syncshelf.sync.scheduler import Scheduler

from clock import FakeClock
from fakes.goodreads import FakeGoodreads
from fakes.hardcover import FakeHardcover
from serving import hardcover_token, make_config, mcp_client, running
from sync_world import GR_USERS, HC_USERS, World, world

pytestmark = pytest.mark.anyio


@pytest.fixture
async def w(tmp_path: Path) -> AsyncIterator[World]:
    async with world(tmp_path, others=["bob"]) as w:
        for name in ("alice", "bob"):
            w.hardcover.add_user_book(HC_USERS[name], 7, status_id=3, rating=4.0)
            w.goodreads.add_item(GR_USERS[name], 100, "Dune", user_rating="4")
        w.hardcover.add_mapping(100, 7, title="Dune")
        for name in ("alice", "bob"):
            await w.sync.run_rss(name)
        yield w


async def quarantine(w: World, profile: str) -> int:
    def mark(conn: Any) -> int:
        conn.execute(
            "UPDATE baseline_fields SET pending_value = '5', pending_since = 1, attempts = 3, "
            "first_failed_at = 1, quarantined_at = 2 WHERE profile = ? AND field = 'rating'",
            (profile,),
        )
        return conn.execute(
            "SELECT id FROM baseline_fields WHERE profile = ? AND field = 'rating'", (profile,)
        ).fetchone()[0]

    return await w.db.write(mark)


async def test_only_the_callers_own_quarantined_changes_are_released(w: World) -> None:
    mine, theirs = await quarantine(w, "alice"), await quarantine(w, "bob")
    assert await w.db.write(identity.release_changes, "alice", [theirs]) == []
    assert await w.db.write(identity.release_changes, "alice", [mine]) == [mine]
    assert await w.rows(
        "SELECT profile, attempts, first_failed_at, quarantined_at FROM baseline_fields "
        "WHERE field = 'rating' ORDER BY profile"
    ) == [("alice", 0, None, None), ("bob", 3, 1, 2)]


async def test_all_releases_every_one_of_the_callers(w: World) -> None:
    await quarantine(w, "alice")
    await quarantine(w, "bob")
    assert len(await w.db.write(identity.release_changes, "alice", None)) == 1
    assert await w.rows(
        "SELECT count(*) FROM baseline_fields WHERE quarantined_at IS NOT NULL"
    ) == [(1,)]


def scheduler(w: World) -> Scheduler:
    return Scheduler(db=w.db, sync=w.sync, profiles=w.config.profiles, interval=3600, clock=w.clock)


async def settle(w: World) -> None:
    while w.sync.running("alice") is not None or w.sync.lock("alice").locked():
        await w.clock.sleep(0)


async def test_run_now_starts_a_run_and_says_its_id_at_once(w: World) -> None:
    s = scheduler(w)
    await s.resume()
    run_id, since, started, _ = await s.run_now("alice", "sync_now")
    assert started is True
    # The id is known while the run is still going.
    assert w.sync.running("alice") == (run_id, since)
    await settle(w)
    assert await w.rows("SELECT trigger, state FROM runs WHERE id = ?", run_id) == [
        ("sync_now", "ok")
    ]


async def test_run_now_during_a_run_reports_it_and_starts_none(w: World) -> None:
    s = scheduler(w)
    await s.resume()
    first = await s.run_now("alice", "sync_now")
    again = await s.run_now("alice", "sync_now")
    assert again == (first[0], first[1], False, "gr_rss")
    await settle(w)
    assert await w.rows("SELECT count(*) FROM runs WHERE trigger = 'sync_now'") == [(1,)]


async def test_the_scheduler_closes_runs_asked_for_now(w: World) -> None:
    s = scheduler(w)
    await s.resume()
    run_id, _, _, _ = await s.run_now("alice", "sync_now")
    await s.close()
    (state,) = (await w.rows("SELECT state FROM runs WHERE id = ?", run_id))[0]
    assert state != "running"  # closed out before the database would go


# --- the tools, called with their caller and services in place --------------------------------


@pytest.fixture
def bound(w: World, monkeypatch: pytest.MonkeyPatch) -> Scheduler:
    import syncshelf.tools.sync as tools

    s = scheduler(w)
    monkeypatch.setattr(tools, "current_profile", lambda: "alice")
    monkeypatch.setattr(
        tools, "services", lambda: SimpleNamespace(scheduler=s, db=w.db, clock=w.clock.time)
    )
    return s


async def test_sync_now_starts_a_run_then_reports_it_going(w: World, bound: Scheduler) -> None:
    from syncshelf.tools.sync import sync_now

    await bound.resume()
    first = await sync_now()
    (run,) = first["runs"]
    assert run["started"] is True and "sync_status" in first["note"]
    again = await sync_now()
    assert again["runs"][0] == {**run, "started": False}
    assert "already going" in again["note"]
    await settle(w)


async def test_retry_failed_releases_and_queues_a_run(w: World, bound: Scheduler) -> None:
    from syncshelf.tools.sync import retry_failed

    await bound.resume()
    change = await quarantine(w, "alice")
    assert await retry_failed(change) == {"released": [change], "run": "queued"}
    assert bound.due("alice") == w.clock.time()


# --- the tools through the app ----------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> Any:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=42)
    return create_app(
        make_config(tmp_path),
        data_dir=data_dir,
        clock=clock.time,
        http_transport=fake.transport,
        limiter_clock=clock,
    )


@pytest.fixture
def csv_app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> Any:
    """An app whose profile has Goodreads credentials and a sidecar, so it has a CSV source."""
    hardcover, goodreads = FakeHardcover(), FakeGoodreads(clock)
    hardcover.add_user(hardcover_token("alice"), user_id=42)
    goodreads.add_user(GR_USERS["alice"])
    body = (
        f'{{goodreads: {{user_id: "{GR_USERS["alice"]}", shelf_map: {{read: 3}}, '
        'email: "${GR_EMAIL}", password: "${GR_PASSWORD}"}, timezone: Etc/UTC}'
    )

    def route(request: Any) -> Any:
        if request.url.host == "www.goodreads.com":
            return goodreads.handle(request)
        return hardcover.handle(request)

    config = make_config(
        tmp_path,
        profile_yaml={"alice": body},
        browser_ws="ws://sidecar.invalid:3000",
        environ={"GR_EMAIL": "reader@example.com", "GR_PASSWORD": "a-test-password-never-shown"},
    )
    return create_app(
        config,
        data_dir=data_dir,
        clock=clock.time,
        http_transport=httpx2.MockTransport(route),
        limiter_clock=clock,
    )


def state(http: Any) -> Any:
    """The running app's state, for reaching the scheduler and the database in a tool test."""
    return http._transport.app.state  # noqa: SLF001


async def idle(http: Any, profile: str) -> None:
    """Wait until no Goodreads run of ``profile`` holds the lock.

    Startup resumes the RSS loop, which runs at once, and the test clock never moves on its
    own \u2014 so once that run is done the lock stays free.
    """
    scheduler = state(http).scheduler
    while scheduler.sync.lock(profile).locked():
        await asyncio.sleep(0)


async def ran(http: Any, run_id: int) -> str:
    """Which source the run with this id belongs to."""
    return await state(http).db.read(
        lambda conn: conn.execute("SELECT source FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    )


async def error(mcp: Any, tool: str, **arguments: Any) -> str:
    result = await mcp.call_tool(tool, arguments)
    assert result.is_error, result.content
    return result.content[0].text


@pytest.mark.parametrize(
    ("arguments", "says"),
    [
        ({"full": True}, "no goodreads section"),  # the section check comes first
        ({"source": "abs"}, "phase 5"),
        ({}, "no goodreads section"),  # this profile has no Goodreads sync
    ],
)
async def test_sync_now_refuses_what_it_cant_do(
    app: Any, arguments: dict[str, Any], says: str
) -> None:
    async with running(app) as http, mcp_client(http) as mcp:
        assert says in await error(mcp, "sync_now", **arguments)


async def test_retry_failed_refuses_a_change_that_isnt_the_callers(app: Any) -> None:
    async with running(app) as http, mcp_client(http) as mcp:
        assert "isn't one of your quarantined changes" in await error(
            mcp, "retry_failed", change_id=99
        )
        result = await mcp.call_tool("retry_failed", {"change_id": "all"})
        assert not result.is_error
        assert result.structured_content == {"released": [], "run": "none"}


async def test_a_full_sync_starts_a_library_export(csv_app: Any) -> None:
    # #227: before 4b this raised a phase error. It now starts the CSV source's own run.
    async with running(csv_app) as http, mcp_client(http) as mcp:
        await idle(http, "alice")  # the RSS run startup kicks off holds the shared lock
        result = await mcp.call_tool("sync_now", {"source": "goodreads", "full": True})
        assert not result.is_error, result.content
        answer = result.structured_content
        assert answer is not None
        (started,) = answer["runs"]
        assert (started["source"], started["started"]) == ("gr_csv", True)
        assert "library export" in answer["note"]
        assert await ran(http, started["run_id"]) == "gr_csv"


async def test_a_full_sync_reports_a_feed_run_already_going(csv_app: Any, clock: FakeClock) -> None:
    # Both sources share the profile's lock, so it names the run that is actually going.
    async with running(csv_app) as http, mcp_client(http) as mcp:
        await idle(http, "alice")
        scheduler = state(http).scheduler
        # A run writes its row before it is in progress, so a held lock has a row to find.
        going = await state(http).db.write(
            runs.start, "alice", "gr_rss", "schedule", int(clock.time())
        )
        async with scheduler.sync.lock("alice"):
            result = await mcp.call_tool("sync_now", {"source": "goodreads", "full": True})
        answer = result.structured_content
        assert answer is not None
        (started,) = answer["runs"]
        assert (started["source"], started["started"], started["run_id"]) == (
            "gr_rss",
            False,
            going,
        )
        assert "shelf feed" in answer["note"]


async def test_a_full_sync_without_credentials_says_what_is_missing(app: Any) -> None:
    async with running(app) as http, mcp_client(http) as mcp:
        says = await error(mcp, "sync_now", source="goodreads", full=True)
    assert "no goodreads section" in says
