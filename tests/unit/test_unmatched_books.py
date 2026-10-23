"""``unmatched_books`` (SPEC §3.4a.5, §3.4a.11): the caller's queue with each entry's input
as seen, its pending diff in words and its candidates; ignored entries on request; and a
refresh that matches entries again first."""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import syncshelf.tools as tools
import syncshelf.tools.matching as matching
from syncshelf.app import create_app
from syncshelf.sync.scheduler import Scheduler

from clock import FakeClock
from fakes.hardcover import FakeHardcover
from serving import hardcover_token, make_config, mcp_client, running
from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio


@pytest.fixture
async def w(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 9, status_id=3)
        w.hardcover.add_mapping(900, 9, title="Book 900")
        w.hardcover.add_search_doc(7, "Orphan", author_names=["Someone Else"], release_year=1999)
        w.goodreads.add_item(GR_USER, 300, "Orphan", user_rating="4")
        w.goodreads.add_item(GR_USER, 900, "Book 900")
        await w.sync.run_rss("alice")  # the backfill: 300 queued, with book 7 a candidate
        s = Scheduler(
            db=w.db, sync=w.sync, profiles=w.config.profiles, interval=3600, clock=w.clock
        )
        bound = SimpleNamespace(
            scheduler=s,
            db=w.db,
            profiles=w.config.profiles,
            hardcover=w.sync._hardcover,  # noqa: SLF001
            clock=w.clock.time,
        )
        monkeypatch.setattr(matching, "current_profile", lambda: "alice")
        monkeypatch.setattr(matching, "services", lambda: bound)

        async def no_attention() -> list[Any]:
            return []

        monkeypatch.setattr(tools, "caller_attention", no_attention)
        yield w


async def test_a_backfill_queued_entry(w: World) -> None:
    answer = await matching.unmatched_books()
    assert answer["total"] == 1
    (entry,) = answer["entries"]
    assert entry["entry_id"] == "goodreads:300"
    assert (entry["source"], entry["state"], entry["reason"]) == (
        "goodreads",
        "queued",
        "fuzzy_low",
    )
    assert entry["input"] == {
        "title": "Orphan",
        "author": "Ann Author",
        "isbn": None,
        "format": None,
    }
    # Nothing is pending: the backfill took Goodreads as it stood.
    assert entry["pending"] == (
        "nothing new; if the book chosen isn't on the shelves, adds it: mark Read, rate 4"
    )
    (candidate,) = entry["candidates"]
    assert candidate == {
        "book_id": 7,
        "title": "Orphan",
        "subtitle": None,
        "authors": ["Someone Else"],
        "release_year": 1999,
        "url": "https://hardcover.app/books/book-7",
        "method": "fuzzy",
        "title_score": 100.0,
        "author_score": candidate["author_score"],
    }


async def test_a_new_entrys_pending_diff_in_words(w: World) -> None:
    w.clock.advance(3600)
    w.goodreads.add_item(
        GR_USER, 400, "Nowhere", user_rating="5", user_read_at="Mon, 2 Mar 2026 00:00:00 +0000"
    )
    await w.sync.run_rss("alice")
    entries = {e["entry_id"]: e for e in (await matching.unmatched_books())["entries"]}
    assert entries["goodreads:400"]["pending"] == "would mark Read, finished 2026-03-02, rate 5"
    assert entries["goodreads:400"]["reason"] == "no_hit"


async def test_ignored_entries_only_on_request(w: World) -> None:
    await w.db.write(
        lambda conn: conn.execute(
            "UPDATE source_rows SET state = 'ignored', queue_reason = NULL, queued_at = NULL "
            "WHERE source_id = '300'"
        )
    )
    assert (await matching.unmatched_books())["entries"] == []
    (entry,) = (await matching.unmatched_books(include_ignored=True))["entries"]
    assert entry["state"] == "ignored"


async def test_a_refresh_matches_the_entries_again_first(w: World) -> None:
    # The book has since reached Hardcover's Goodreads mappings, and the shelves.
    w.hardcover.add_user_book(HC_USER, 30, status_id=3)
    w.hardcover.add_mapping(300, 30, title="Orphan")
    assert (await matching.unmatched_books(refresh=True))["total"] == 0
    assert await w.rows("SELECT state, hc_book_id FROM source_rows WHERE source_id = '300'") == [
        ("linked", 30)
    ]


async def test_candidates_still_listed_when_their_details_fail(w: World) -> None:
    w.hardcover.fail_next("BooksById", "unavailable")
    answer = await matching.unmatched_books()
    assert answer["error"]
    assert answer["entries"][0]["candidates"][0]["book_id"] == 7


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
        tool = {t.name: t for t in (await mcp.list_tools()).tools}["unmatched_books"]
        assert tool.annotations.read_only_hint is True
        result = await mcp.call_tool("unmatched_books", {})
        assert result.structured_content == {"total": 0, "entries": []}
        refused = await mcp.call_tool("unmatched_books", {"source": "abs"})
        assert refused.is_error and "phase 5" in refused.content[0].text


async def test_a_refresh_queues_a_run_for_what_it_linked(w: World) -> None:
    w.hardcover.add_user_book(HC_USER, 30, status_id=3)
    w.hardcover.add_mapping(300, 30, title="Orphan")
    scheduler = matching.services().scheduler
    await scheduler.resume()
    await matching.unmatched_books(refresh=True)
    assert scheduler.due("alice") == w.clock.time()


async def test_a_refresh_leaves_a_conflict_for_a_person(w: World) -> None:
    await w.db.write(
        lambda conn: conn.execute(
            "UPDATE source_rows SET queue_reason = 'profile_conflict' WHERE source_id = '300'"
        )
    )
    w.hardcover.add_mapping(300, 30, title="Orphan")
    (entry,) = (await matching.unmatched_books(refresh=True))["entries"]
    assert entry["reason"] == "profile_conflict"


async def test_a_refresh_never_waits_for_a_run(w: World) -> None:
    async with w.sync.lock("alice"):
        answer = await matching.unmatched_books(refresh=True)
    assert "a sync is running" in answer["error"]
    assert answer["total"] == 1


async def test_a_refresh_that_fails_still_lists_the_entries(w: World) -> None:
    w.hardcover.fail_next("Library", "unavailable")
    w.hardcover.fail_next("ByGoodreads", "unavailable")
    answer = await matching.unmatched_books(refresh=True)
    assert answer["total"] == 1 and len(answer["entries"]) == 1
