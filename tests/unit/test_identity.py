"""Identity rules the golden diffs don't reach (SPEC §3.4a.4, §3.4a.5): method ranking on the
shared link, ignored rows staying ignored, and what a queue entry holds."""

import json
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from syncshelf.sync import identity
from syncshelf.sync.matcher import IdHit
from syncshelf.sync.observations import Observation

from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio

NOW = 4_102_444_800


def obs(source_id: str = "1") -> Observation:
    return Observation(source_id, {}, "Dune", "Frank Herbert", None)


def link(conn: sqlite3.Connection, method: str, book: int) -> tuple[int, str]:
    identity._link(conn, "goodreads", obs(), IdHit(book, None, "Dune", method), NOW)  # noqa: SLF001
    return conn.execute("SELECT hc_book_id, method FROM links").fetchone()


@pytest.mark.parametrize(
    ("first", "then", "kept"),
    [
        ("fuzzy", "isbn13", ("isbn13", 8)),  # stronger replaces
        ("isbn10", "gr_mapping", ("gr_mapping", 8)),
        ("gr_mapping", "human", ("human", 8)),
        ("isbn13", "isbn10", ("isbn13", 7)),  # equal never does
        ("gr_mapping", "fuzzy", ("gr_mapping", 7)),  # weaker never does
        ("human", "gr_mapping", ("human", 7)),  # nothing replaces a person's link
    ],
)
def test_only_a_strictly_stronger_method_replaces_a_link(
    schema: sqlite3.Connection, first: str, then: str, kept: tuple[str, int]
) -> None:
    with schema:
        link(schema, first, 7)
        book, method = link(schema, then, 8)
    assert (method, book) == kept


@pytest.fixture
async def w(tmp_path: Path) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 9, status_id=3)
        w.hardcover.add_mapping(900, 9, title="Book 900")
        w.goodreads.add_item(GR_USER, 300, "Orphan", user_rating="4")
        w.goodreads.add_item(GR_USER, 900, "Book 900")
        await w.sync.run_rss("alice")
        yield w


async def test_a_queue_entry_holds_its_reason_time_input_and_candidates(w: World) -> None:
    w.hardcover.add_search_doc(7, "Orphan", author_names=["Someone Else"])
    w.clock.advance(3600)
    w.goodreads.add_item(GR_USER, 400, "Orphan", user_rating="2")
    await w.sync.run_rss("alice")
    ((reason, queued_at, given),) = await w.rows(
        "SELECT queue_reason, queued_at, match_input FROM source_rows WHERE source_id = '400'"
    )
    assert reason == "fuzzy_low" and queued_at == int(w.clock.time())
    assert json.loads(given) == {
        "title": "Orphan",
        "authors": ["Ann Author"],
        "isbns": [],
        "asin": None,
        "gr_book_id": "400",
        "format": None,
        "year": None,
    }
    assert await w.rows(
        "SELECT rank, hc_book_id, method, title_score FROM match_candidates WHERE source_id = '400'"
    ) == [(1, 7, "fuzzy", 100.0)]


async def test_an_ignored_row_is_never_matched_or_pushed(w: World) -> None:
    await w.db.write(
        lambda conn: conn.execute(
            "UPDATE source_rows SET state = 'ignored', queue_reason = NULL, queued_at = NULL "
            "WHERE source_id = '300'"
        )
    )
    w.hardcover.add_mapping(300, 5, title="Orphan")
    w.clock.advance(3600)
    item = next(i for i in w.goodreads.users[GR_USER].items if i["book_id"] == "300")
    item["user_rating"] = "5"
    item["title"] = "Orphan (Revised)"  # a new match input would re-match a queued row
    before = len(w.hardcover.requests)
    await w.sync.run_rss("alice")
    assert len(w.hardcover.requests) == before
    assert await w.rows("SELECT state, hc_book_id FROM source_rows WHERE source_id = '300'") == [
        ("ignored", None)
    ]


async def test_a_conflict_waits_for_a_person_even_when_its_details_change(w: World) -> None:
    await w.db.write(
        lambda conn: conn.execute(
            "UPDATE source_rows SET queue_reason = 'profile_conflict' WHERE source_id = '300'"
        )
    )
    w.hardcover.add_mapping(300, 5, title="Orphan (Revised)")
    w.clock.advance(3600)
    item = next(i for i in w.goodreads.users[GR_USER].items if i["book_id"] == "300")
    item["title"] = "Orphan (Revised)"
    await w.sync.run_rss("alice")
    assert await w.rows("SELECT state, queue_reason FROM source_rows WHERE source_id = '300'") == [
        ("queued", "profile_conflict")
    ]
