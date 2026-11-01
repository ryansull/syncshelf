"""``sync_status`` for the caller's own profile (SPEC §3.4a.10): its sources, schedule,
backfill and recent runs, the mirror, the limiter, the quarantine, the unmatched queue and
gone rows; unshipped sections left out. The payload on a seeded world, then the tool
through the real ASGI app."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from syncshelf.app import create_app
from syncshelf.sync import runs
from syncshelf.sync.goodreads_session import ADVICE, record
from syncshelf.sync.limiter import HardcoverLimiter
from syncshelf.sync.runs import RunCounts
from syncshelf.tools import iso
from syncshelf.tools.sync import _limiter, status

from clock import FakeClock
from fakes.hardcover import FakeHardcover
from serving import (
    GR_PASSWORD,
    SIDECAR,
    credential_env,
    hardcover_token,
    make_config,
    mcp_client,
    running,
)
from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio

HOUR = 3600
DAY = 24 * HOUR


@pytest.fixture
async def w(tmp_path: Path) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 7, status_id=3, rating=4.0, book={"title": "Dune"})
        w.hardcover.add_mapping(100, 7, title="Dune")
        w.goodreads.add_item(GR_USER, 300, "Orphan")
        w.goodreads.add_item(GR_USER, 100, "Dune", user_rating="4")
        await w.sync.run_rss("alice")  # the backfill: Dune linked, Orphan queued
        yield w


ON = {"gr_rss": True, "gr_csv": False}
"""The RSS source only: the world's profile carries no Goodreads credentials."""


async def current(w: World, enabled: dict[str, bool] | None = None) -> dict[str, Any]:
    return await w.db.read(status, "alice", int(w.clock.time()), enabled or ON)


async def test_a_backfilled_profile(w: World) -> None:
    s = await current(w)
    source = s["sources"]["gr_rss"]
    assert source["enabled"] is True
    assert source["backfill"]["state"] == "done"
    assert (source["backfill"]["rows_linked"], source["backfill"]["rows_queued"]) == (1, 1)
    assert [r["state"] for r in source["recent_runs"]] == ["ok"]
    assert s["unmatched"] == {
        "total": 1,
        "by_reason": {"no_hit": 1},
        "oldest_queued_at": s["unmatched"]["oldest_queued_at"],
    }
    assert s["mirror"]["as_of"] is not None
    assert s["quarantine"] == {"total": 0, "items": []}
    assert s["gone"] == {"total": 0}
    assert set(s) == {"sources", "mirror", "quarantine", "unmatched", "gone"}
    # Both Goodreads sources are listed; gr_csv is off here, so it shows no next run.
    assert set(s["sources"]) == {"gr_rss", "gr_csv"}  # abs arrives in phase 5
    assert s["sources"]["gr_csv"]["enabled"] is False
    assert s["sources"]["gr_csv"]["next_due_at"] is None
    # And no goodreads_session section at all, rather than a null one (§3.4a.10).
    assert "goodreads_session" not in s


async def test_a_quarantined_change_shows_its_book_and_values(w: World) -> None:
    w.clock.advance(HOUR)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"
    w.hardcover.fail_next("UpdateUserBook", "capacity")
    await w.sync.run_rss("alice")
    (item,) = (await current(w))["quarantine"]["items"]
    assert {k: item[k] for k in ("hc_book_id", "title", "field", "old", "new", "error_class")} == {
        "hc_book_id": 7,
        "title": "Dune",
        "field": "rating",
        "old": "4",
        "new": "5",
        "error_class": "bug",
    }
    assert item["attempts"] == 1 and item["last_error"]


async def test_a_review_shows_only_its_length_and_start(w: World) -> None:
    review = "A" * 30 + " and then a great deal more that is never shown here."
    await w.db.write(
        lambda conn: conn.execute(
            "UPDATE baseline_fields SET pending_value = ?, pending_since = 1, quarantined_at = 1, "
            "attempts = 3 WHERE source_id = '100' AND field = 'review'",
            (review,),
        )
    )
    (item,) = (await current(w))["quarantine"]["items"]
    assert item["new"] == {"length": len(review), "start": review[:40]}
    assert item["old"] is None


async def test_recent_runs_are_the_last_three_and_every_failure_of_the_day(w: World) -> None:
    now = int(w.clock.time())

    def seed(conn: Any) -> None:
        for n, state in enumerate(["fetch_failed", "partial", "ok", "ok", "ok"]):
            run_id = runs.start(conn, "alice", "gr_rss", "schedule", now + n)
            runs.finish(conn, run_id, state, RunCounts(), now + n + 2)  # type: ignore[arg-type]
        old = runs.start(conn, "alice", "gr_rss", "schedule", now - 2 * DAY)
        runs.finish(conn, old, "fetch_failed", RunCounts(), now - 2 * DAY)

    await w.db.write(seed)
    shown = (await current(w))["sources"]["gr_rss"]["recent_runs"]
    assert [r["state"] for r in shown] == ["ok", "ok", "ok", "partial", "fetch_failed"]
    assert shown[0]["duration_s"] == 2


async def test_gone_rows_are_counted(w: World) -> None:
    w.clock.advance(HOUR)
    w.goodreads.users[GR_USER].items.pop(0)  # Dune leaves the feed
    await w.sync.run_rss("alice")
    assert (await current(w))["gone"] == {"total": 1}


async def test_the_schedule_shows_backoff(w: World) -> None:
    now = int(w.clock.time())
    await w.db.write(
        lambda conn: conn.execute(
            "INSERT INTO schedules (profile, source, next_due_at, last_result, "
            "consecutive_failures, backoff_until) VALUES ('alice', 'gr_rss', ?, 'fetch_failed', "
            "2, ?)",
            (now + 2 * HOUR, now + 2 * HOUR),
        )
    )
    source = (await current(w))["sources"]["gr_rss"]
    assert (source["last_result"], source["consecutive_failures"]) == ("fetch_failed", 2)
    assert source["backoff_until"] == source["next_due_at"] is not None


# --- the tool -------------------------------------------------------------------------------


async def test_the_tool_answers_for_the_callers_own_profile(
    tmp_path: Path, data_dir: Path, clock: FakeClock
) -> None:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=42)
    app = create_app(
        make_config(tmp_path),
        data_dir=data_dir,
        clock=clock.time,
        http_transport=fake.transport,
        limiter_clock=clock,
    )
    async with running(app) as http, mcp_client(http) as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
        assert tools["sync_status"].annotations.read_only_hint is True
        result = await mcp.call_tool("sync_status", {})
        assert not result.is_error, result.content
        body = result.structured_content
        assert body is not None
        assert body["profile"] == "alice"
        assert set(body) == {
            "profile",
            "now",
            "version",
            "sources",
            "mirror",
            "hardcover",
            "quarantine",
            "unmatched",
            "gone",
        }
        # A profile with no goodreads section has the source, disabled and never backfilled.
        source = body["sources"]["gr_rss"]
        assert (source["enabled"], source["backfill"]) == (False, {"state": "pending"})
        assert body["hardcover"]["limiter"] == "ok"


async def test_a_disabled_source_shows_no_next_run(w: World) -> None:
    await w.db.write(
        lambda conn: conn.execute(
            "INSERT INTO schedules (profile, source, next_due_at) VALUES ('alice', 'gr_rss', 9)"
        )
    )
    shown = await current(w, {"gr_rss": False, "gr_csv": False})
    assert shown["sources"]["gr_rss"]["next_due_at"] is None


def test_the_limiter_shows_a_pause_in_wall_clock_time(clock: FakeClock) -> None:
    from syncshelf.clients.hardcover import RateLimit

    limiter = HardcoverLimiter("alice", reserve=1000, clock=clock)
    assert _limiter(limiter) == {"limiter": "ok", "paused_until": None, "daily_remaining": None}
    limiter.observe(RateLimit(daily_remaining=900))
    assert _limiter(limiter)["limiter"] == "reserve"
    assert _limiter(limiter)["daily_remaining"] == 900


# --- the Goodreads session section (§3.4b.6, §3.4a.10) ---------------------------------------

CSV_ON = {"gr_rss": True, "gr_csv": True}
PASSWORD = GR_PASSWORD
CSV_PROFILE = (
    '{goodreads: {user_id: "1", shelf_map: {read: 3}, '
    'email: "${GR_EMAIL}", password: "${GR_PASSWORD}"}, timezone: Etc/UTC}'
)


@pytest.fixture
async def csv_w(tmp_path: Path) -> AsyncIterator[World]:
    async with world(tmp_path, csv=True) as w:
        yield w


class TestTheSessionSection:
    async def test_a_profile_with_no_session_yet_reads_as_needs_sign_in(self, csv_w: World) -> None:
        # No row means nothing has signed in, which is what needs_sign_in says.
        shown = await csv_w.db.read(status, "alice", int(csv_w.clock.time()), CSV_ON)
        assert shown["goodreads_session"] == {
            "state": "needs_sign_in",
            "since": None,
            "detail": None,
            "action": ADVICE["needs_sign_in"],
        }

    @pytest.mark.parametrize(
        "state", ["ok", "needs_sign_in", "awaiting_code", "challenged", "bad_credentials"]
    )
    async def test_every_state_is_shown_with_when_it_changed_and_what_to_do(
        self, csv_w: World, state: str
    ) -> None:
        at = int(csv_w.clock.time()) - 60
        await csv_w.db.write(record, "alice", state, at, "why it stopped")
        shown = await csv_w.db.read(status, "alice", int(csv_w.clock.time()), CSV_ON)
        session_shown = shown["goodreads_session"]
        assert session_shown["state"] == state
        assert session_shown["since"] == iso(at)
        assert session_shown["detail"] == "why it stopped"
        assert session_shown["action"] == ADVICE[state]

    async def test_the_csv_source_shows_its_own_schedule(self, csv_w: World) -> None:
        now = int(csv_w.clock.time())
        await csv_w.db.write(
            lambda conn: conn.execute(
                "INSERT INTO schedules (profile, source, next_due_at, last_run_at, "
                "last_success_at, last_result) VALUES ('alice', 'gr_csv', ?, ?, ?, 'ok')",
                (now + 5 * DAY, now - DAY, now - DAY),
            )
        )
        source = (await csv_w.db.read(status, "alice", now, CSV_ON))["sources"]["gr_csv"]
        assert source["enabled"] is True
        assert source["next_due_at"] == iso(now + 5 * DAY)
        # Separate from last_run_at, so a run that failed doesn't look like a success.
        assert source["last_run_at"] == source["last_success_at"] == iso(now - DAY)

    async def test_a_failed_export_shows_a_run_without_a_success(self, csv_w: World) -> None:
        now = int(csv_w.clock.time())
        await csv_w.db.write(
            lambda conn: conn.execute(
                "INSERT INTO schedules (profile, source, next_due_at, last_run_at, "
                "last_result) VALUES ('alice', 'gr_csv', ?, ?, 'fetch_failed')",
                (now + DAY, now - HOUR),
            )
        )
        source = (await csv_w.db.read(status, "alice", now, CSV_ON))["sources"]["gr_csv"]
        assert (source["last_run_at"], source["last_success_at"]) == (iso(now - HOUR), None)

    async def test_a_detail_never_carries_the_password(self, csv_w: World) -> None:
        # Everything that reaches a detail was scrubbed where it was made (§3.4b.2); this is
        # the check that the tool layer doesn't put one back.
        await csv_w.db.write(
            record, "alice", "bad_credentials", int(csv_w.clock.time()), "Goodreads said no"
        )
        shown = await csv_w.db.read(status, "alice", int(csv_w.clock.time()), CSV_ON)
        assert PASSWORD not in repr(shown)

    async def test_the_section_is_left_out_without_a_csv_source(self, csv_w: World) -> None:
        await csv_w.db.write(record, "alice", "challenged", int(csv_w.clock.time()), "a bot check")
        shown = await csv_w.db.read(status, "alice", int(csv_w.clock.time()), ON)
        assert "goodreads_session" not in shown  # stored, but this stack can't use it


async def test_the_tool_carries_the_session_and_its_attention(csv_app: Any) -> None:
    async with running(csv_app) as http, mcp_client(http) as mcp:
        result = await mcp.call_tool("sync_status", {})
        assert not result.is_error, result.content
        answer = result.structured_content
        assert answer is not None
        assert answer["goodreads_session"]["state"] == "needs_sign_in"
        assert [i["code"] for i in answer["attention"]] == ["goodreads_session"]
        assert set(answer["sources"]) == {"gr_rss", "gr_csv"}


@pytest.fixture
def csv_app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> Any:
    """An app whose profile has Goodreads credentials and a sidecar, so it has a CSV source."""
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=42)
    config = make_config(
        tmp_path,
        profile_yaml={"alice": CSV_PROFILE},
        browser_ws=SIDECAR,
        environ=credential_env(),
    )
    return create_app(
        config,
        data_dir=data_dir,
        clock=clock.time,
        http_transport=fake.transport,
        limiter_clock=clock,
    )
