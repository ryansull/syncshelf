"""``resolve_match`` (SPEC §3.4a.11, §3.4a.4 rule 6, §3.4a.12): a person's answer for a row is
linked as ``human`` and its pending diff applied at once, with no run; ``None`` ignores it."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import syncshelf.tools as tools
import syncshelf.tools.matching as matching
from syncshelf.app import create_app
from syncshelf.sync.scheduler import Scheduler

from clock import FakeClock
from fakes.hardcover import FakeHardcover
from serving import hardcover_token, make_config, mcp_client, running
from sync_world import GR_USER, GR_USERS, HC_USER, HC_USERS, World, world

pytestmark = pytest.mark.anyio


def _bind(w: World, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Serve the tools from this world, as the profile the returned function names."""
    s = Scheduler(db=w.db, sync=w.sync, profiles=w.config.profiles, interval=3600, clock=w.clock)
    bound = SimpleNamespace(
        scheduler=s,
        db=w.db,
        profiles=w.config.profiles,
        hardcover=w.sync._hardcover,  # noqa: SLF001
        clock=w.clock.time,
    )
    monkeypatch.setattr(matching, "services", lambda: bound)

    async def no_attention() -> list[Any]:
        return []

    monkeypatch.setattr(tools, "caller_attention", no_attention)

    def be(profile: str) -> None:
        monkeypatch.setattr(matching, "current_profile", lambda: profile)

    be("alice")
    return be


@pytest.fixture
async def w(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 9, status_id=3)
        w.hardcover.add_mapping(900, 9, title="Book 900")
        w.hardcover.add_book(7, title="Orphan")
        w.goodreads.add_item(GR_USER, 300, "Orphan", user_rating="4")
        w.goodreads.add_item(GR_USER, 900, "Book 900")
        await w.sync.run_rss("alice")  # the backfill: 300 queued (no_hit), 900 linked to 9
        _bind(w, monkeypatch)
        yield w


async def test_a_resolved_entry_is_linked_and_pushed_with_no_run(w: World) -> None:
    answer = await matching.resolve_match("goodreads:300", 7)
    assert answer == {
        "entry_id": "goodreads:300",
        "state": "linked",
        "book_id": 7,
        "title": "Orphan",
        "shared": True,
        "pushed": ["rating", "status"],
    }
    assert await w.rows("SELECT hc_book_id, method FROM links WHERE source_id = '300'") == [
        (7, "human")
    ]
    assert await w.rows(
        "SELECT state, hc_book_id, backfill_queued FROM source_rows WHERE source_id = '300'"
    ) == [("linked", 7, 0)]
    assert await w.rows("SELECT count(*) FROM runs") == [(1,)]  # only the backfill
    log = await w.rows("SELECT kind, run_id FROM change_log WHERE source_id = '300' ORDER BY id")
    assert log[0] == ("resolve_match", None)
    assert {run_id for _, run_id in log} == {None}
    (shelved,) = [ub for ub in w.hardcover.user_books[HC_USER] if ub["book_id"] == 7]
    assert (shelved["status_id"], shelved["rating"]) == (3, 4.0)


async def test_a_merged_book_is_followed_to_the_one_it_became(w: World) -> None:
    w.hardcover.add_book(8, title="Orphan (old)", canonical_id=7)
    answer = await matching.resolve_match("goodreads:300", 8)
    assert answer["book_id"] == 7
    assert await w.rows("SELECT hc_book_id FROM source_rows WHERE source_id = '300'") == [(7,)]


async def test_a_book_hardcover_doesnt_have_is_refused(w: World) -> None:
    with pytest.raises(ToolError, match="no book 12345"):
        await matching.resolve_match("goodreads:300", 12345)
    assert await w.rows("SELECT state FROM source_rows WHERE source_id = '300'") == [("queued",)]


@pytest.mark.parametrize(
    ("entry_id", "says"),
    [
        ("goodreads:abc", "isn't an entry id"),
        ("chaptarr:1", "isn't an entry id"),
        ("abs:1", "phase 5"),
        ("goodreads:12345", "isn't one of your"),
    ],
)
async def test_an_entry_id_from_elsewhere_is_refused(w: World, entry_id: str, says: str) -> None:
    with pytest.raises(ToolError, match=says):
        await matching.resolve_match(entry_id, 7)


async def test_none_ignores_an_entry_and_a_book_revives_it(w: World) -> None:
    assert await matching.resolve_match("goodreads:300", None) == {
        "entry_id": "goodreads:300",
        "state": "ignored",
    }
    assert (await matching.unmatched_books())["entries"] == []
    w.clock.advance(3600)
    await w.sync.run_rss("alice")  # an ignored row is never queued again
    assert await w.rows("SELECT state FROM source_rows WHERE source_id = '300'") == [("ignored",)]
    assert (await matching.resolve_match("goodreads:300", 7))["state"] == "linked"
    assert await w.rows(
        "SELECT kind FROM change_log WHERE source_id = '300' AND kind IN ('ignore', 'unignore') "
        "ORDER BY id"
    ) == [("ignore",), ("unignore",)]


async def test_a_wrong_link_is_corrected_and_the_old_shelf_entry_named(w: World) -> None:
    answer = await matching.resolve_match("goodreads:900", 7)
    assert answer["note"].startswith("Book 9 still has a shelf entry")
    assert {ub["book_id"] for ub in w.hardcover.user_books[HC_USER]} >= {9}  # left in place
    assert await w.rows("SELECT hc_book_id, method FROM links WHERE source_id = '900'") == [
        (7, "human")
    ]


async def test_a_failed_push_keeps_the_link_and_the_change_pending(w: World) -> None:
    w.clock.advance(3600)
    w.goodreads.add_item(GR_USER, 400, "Nowhere", user_rating="5")
    await w.sync.run_rss("alice")  # queued after the backfill: its diff is pending
    w.hardcover.fail_next("InsertUserBook", "validation")
    answer = await matching.resolve_match("goodreads:400", 7)
    assert answer["state"] == "linked"
    assert answer["pushed"] == []
    assert answer["failed"]
    assert await w.rows("SELECT state, hc_book_id FROM source_rows WHERE source_id = '400'") == [
        ("linked", 7)
    ]
    assert await w.rows(
        "SELECT field FROM baseline_fields WHERE source_id = '400' AND pending_since IS NOT NULL "
        "ORDER BY field"
    ) == [("rating",), ("status",)]


async def test_a_link_another_profile_relies_on_stays_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with world(tmp_path, others=["bob"]) as w:
        # Goodreads' book 300 is linked to book 30, and alice's row relies on it.
        w.hardcover.add_user_book(HC_USERS["alice"], 30, status_id=3)
        w.hardcover.add_mapping(300, 30, title="Orphan")
        w.hardcover.add_book(7, title="Orphan")
        for name in ("alice", "bob"):
            w.goodreads.add_item(GR_USERS[name], 300, "Orphan")
            await w.sync.run_rss(name)
        be = _bind(w, monkeypatch)
        be("bob")
        answer = await matching.resolve_match("goodreads:300", 7)
        assert answer["shared"] is False
        assert await w.rows("SELECT hc_book_id FROM links WHERE source_id = '300'") == [(30,)]
        assert await w.rows(
            "SELECT profile, hc_book_id FROM source_rows WHERE source_id = '300' ORDER BY profile"
        ) == [("alice", 30), ("bob", 7)]


async def test_through_the_app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> None:
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
        tool = {t.name: t for t in (await mcp.list_tools()).tools}["resolve_match"]
        assert tool.annotations.read_only_hint is False
        refused = await mcp.call_tool(
            "resolve_match", {"entry_id": "goodreads:300", "hardcover_book_id": 7}
        )
        assert refused.is_error
        assert "isn't one of your" in refused.content[0].text
