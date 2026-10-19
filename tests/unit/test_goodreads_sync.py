"""A Goodreads RSS run end to end against the fakes (SPEC §3.4a.4, §3.4a.6, §3.4a.7,
§3.4a.12): the first complete run is the map-only backfill and pushes nothing; a later rating
change reaches Hardcover; an idle run makes no Hardcover calls."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from syncshelf.sync import runs

from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio

DUNE, MESSIAH, ORPHAN = 100, 200, 300
"""Goodreads edition ids: Dune is on the profile's Hardcover shelves, Messiah is a book
Hardcover knows but the profile never shelved, and Orphan has no Goodreads mapping."""


@pytest.fixture
async def w(tmp_path: Path) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 7, status_id=3, rating=4.0)
        w.hardcover.add_mapping(DUNE, 7, 70, reading_format_id=4)
        w.hardcover.add_mapping(MESSIAH, 8)
        w.goodreads.add_item(GR_USER, ORPHAN, "Orphan")
        w.goodreads.add_item(GR_USER, MESSIAH, "Dune Messiah", user_rating="5")
        w.goodreads.add_item(GR_USER, DUNE, "Dune", user_rating="4")
        yield w


async def state(w: World, run_id: int) -> tuple[str, int, int, str | None]:
    ((row),) = await w.rows(
        "SELECT state, applied, failed, first_error FROM runs WHERE id = ?", run_id
    )
    return row


async def test_the_first_complete_run_is_the_backfill_and_pushes_nothing(w: World) -> None:
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == ("ok", 0, 0, None)
    assert w.hardcover.writes == []
    assert await w.rows("SELECT rows_linked, rows_queued, run_id FROM backfills") == [
        (1, 2, run_id)
    ]
    rows = await w.rows(
        "SELECT source_id, state, hc_book_id, hc_user_book_id, queue_reason, backfill_queued "
        "FROM source_rows ORDER BY source_id"
    )
    user_book = w.hardcover.user_books[HC_USER][0]["id"]
    assert rows == [
        (str(DUNE), "linked", 7, user_book, None, 0),
        (str(MESSIAH), "queued", None, None, "not_in_library", 1),
        (str(ORPHAN), "queued", None, None, "no_hit", 1),
    ]
    assert await w.rows("SELECT source_id, hc_book_id, hc_edition_id, method FROM links") == [
        (str(DUNE), 7, 70, "gr_mapping")
    ]
    # The book the mapping found is kept as the queued row's candidate.
    assert await w.rows("SELECT source_id, rank, hc_book_id, method FROM match_candidates") == [
        (str(MESSIAH), 1, 8, "gr_mapping")
    ]


async def test_the_backfill_takes_every_observed_value_as_the_baseline(w: World) -> None:
    observed = int(w.clock.time())  # a batch is observed when its fetch starts
    await w.sync.run_rss("alice")
    baselines = await w.rows(
        "SELECT field, value, observed_at, pending_since FROM baseline_fields "
        "WHERE source_id = ? ORDER BY field",
        str(DUNE),
    )
    assert baselines == [
        ("date_read", None, observed, None),
        ("rating", "4", observed, None),
        ("review", None, observed, None),
        ("status", "read", observed, None),
    ]


async def test_a_rating_changed_after_the_backfill_reaches_hardcover(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"
    observed = int(w.clock.time())
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == ("ok", 1, 0, None)
    assert [(name, fields) for name, _, fields in w.hardcover.writes] == [
        ("update_user_book", {"rating": 5.0})
    ]
    assert w.hardcover.user_books[HC_USER][0]["rating"] == 5.0
    (baseline,) = await w.rows(
        "SELECT value, prev_value, observed_at, pending_value, pending_since FROM baseline_fields"
        " WHERE source_id = ? AND field = 'rating'",
        str(DUNE),
    )
    assert baseline == ("5", "4", observed, None, None)
    assert await w.rows(
        "SELECT run_id, source_id, hc_book_id, kind, field, old_value, new_value FROM change_log"
    ) == [(run_id, str(DUNE), 7, "push", "rating", "4", "5")]


async def test_a_cleared_rating_clears_hardcovers(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "0"
    await w.sync.run_rss("alice")
    assert w.hardcover.user_books[HC_USER][0]["rating"] is None


async def test_an_idle_run_makes_no_hardcover_calls(w: World) -> None:
    await w.sync.run_rss("alice")
    before = len(w.hardcover.requests)
    w.clock.advance(3600)
    observed = int(w.clock.time())
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == ("ok", 0, 0, None)
    assert len(w.hardcover.requests) == before
    # It still recorded what it saw: the baselines' observations moved on.
    assert await w.rows(
        "SELECT DISTINCT observed_at FROM baseline_fields WHERE source_id = ?", str(DUNE)
    ) == [(observed,)]


async def test_a_change_on_a_queued_row_waits_and_costs_nothing(w: World) -> None:
    await w.sync.run_rss("alice")
    before = len(w.hardcover.requests)
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items[1]["user_rating"] = "3"  # Messiah, queued
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == ("ok", 0, 0, None)
    assert len(w.hardcover.requests) == before
    # Recorded on its baseline all the same, so resolving the row later applies it.
    assert await w.rows(
        "SELECT value, pending_value FROM baseline_fields WHERE source_id = ? AND field = 'rating'",
        str(MESSIAH),
    ) == [("5", "3")]


async def test_a_failed_write_leaves_the_baseline_for_the_next_run(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"
    w.hardcover.fail_next("UpdateUserBook", "validation", message="Rating refused")
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == (
        "partial",
        0,
        1,
        "update_user_book was refused: Rating refused",
    )
    assert await w.rows(
        "SELECT value, pending_value FROM baseline_fields WHERE source_id = ? AND field = 'rating'",
        str(DUNE),
    ) == [("4", "5")]
    assert await w.rows("SELECT count(*) FROM change_log") == [(0,)]
    # The next run converges.
    w.clock.advance(3600)
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == ("ok", 1, 0, None)
    assert w.hardcover.user_books[HC_USER][0]["rating"] == 5.0


async def test_a_failed_hardcover_fetch_defers_the_run(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"
    w.hardcover.fail_next("Library", "unavailable")
    run_id = await w.sync.run_rss("alice")
    assert (await state(w, run_id))[:3] == ("deferred", 0, 0)
    assert w.hardcover.writes == []


async def test_an_unusable_feed_fails_the_fetch_and_does_nothing(w: World) -> None:
    w.goodreads.fail_next("challenge")
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == (
        "fetch_failed",
        0,
        0,
        "page 1: Goodreads answered with a WAF challenge (HTTP 202)",
    )
    assert w.hardcover.requests == []
    assert await w.rows("SELECT count(*) FROM source_rows") == [(0,)]


async def test_an_incomplete_first_run_waits_for_a_complete_one(w: World) -> None:
    for n in range(250):  # two pages, so the second can fail
        w.goodreads.add_item(GR_USER, 1000 + n, f"Book {n}")
    w.goodreads.fail_next("server_error", page=2)
    run_id = await w.sync.run_rss("alice")
    assert (await state(w, run_id))[0] == "partial"
    assert w.hardcover.requests == []
    assert await w.rows("SELECT count(*) FROM backfills") == [(0,)]
    assert await w.rows("SELECT count(*) FROM source_rows") == [(0,)]


async def test_the_other_profiles_link_is_reused(tmp_path: Path) -> None:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 9)
        w.goodreads.add_item(GR_USER, DUNE, "Dune")
        # A link the other profile's backfill made; no mapping is asked for.
        await w.db.write(
            lambda conn: conn.execute(
                "INSERT INTO links (source, source_id, hc_book_id, method, linked_at, "
                "last_seen_at) VALUES ('goodreads', ?, 9, 'human', 0, 0)",
                (str(DUNE),),
            )
        )
        await w.sync.run_rss("alice")
        assert "ByGoodreads" not in w.hardcover_calls()
        assert await w.rows("SELECT state, hc_book_id FROM source_rows") == [("linked", 9)]


async def test_startup_marks_a_run_left_running_as_aborted(w: World) -> None:
    run_id = await w.db.write(runs.start, "alice", "gr_rss", "schedule", 1)
    assert await w.db.write(runs.abort_running, 2) == 1
    assert await w.rows("SELECT state, ended_at FROM runs WHERE id = ?", run_id) == [("aborted", 2)]


async def test_a_404_user_fails_the_fetch_and_concludes_nothing(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    del w.goodreads.users[GR_USER]
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == (
        "fetch_failed",
        0,
        0,
        "page 1: Goodreads has no such user (HTTP 404)",
    )
    assert await w.rows("SELECT count(*) FROM source_rows WHERE gone_at IS NOT NULL") == [(0,)]
