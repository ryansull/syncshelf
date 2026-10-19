"""A Goodreads RSS run end to end against the fakes (SPEC §3.4a.4, §3.4a.6, §3.4a.7,
§3.4a.12): the first complete run is the map-only backfill and pushes nothing; a later rating
change reaches Hardcover; an idle run makes no Hardcover calls."""

from collections.abc import AsyncIterator
from pathlib import Path

import anyio
import pytest

from syncshelf.sync import runs

from fakes.hardcover import read_row
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


async def test_a_change_hardcover_cant_take_fails_alone_and_the_run_goes_on(w: World) -> None:
    w.hardcover.add_user_book(HC_USER, 9, status_id=3, rating=2.0)
    w.hardcover.add_mapping(400, 9)
    w.goodreads.add_item(GR_USER, 400, "Children of Dune", user_rating="2")
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    # Dune leaves the Hardcover shelves; a rating alone can't put it back.
    w.hardcover.user_books[HC_USER] = [
        ub for ub in w.hardcover.user_books[HC_USER] if ub["book_id"] != 7
    ]
    items = {i["book_id"]: i for i in w.goodreads.users[GR_USER].items}
    items[str(DUNE)]["user_rating"] = "5"
    items["400"]["user_rating"] = "3"
    run_id = await w.sync.run_rss("alice")
    (run_state, applied, failed, error) = await state(w, run_id)
    assert (run_state, applied, failed) == ("partial", 1, 1)
    assert error is not None and "not on the profile's Hardcover shelves" in error
    assert [ub["rating"] for ub in w.hardcover.user_books[HC_USER]] == [3.0]


async def test_overlapping_runs_take_turns_and_apply_once(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"
    async with anyio.create_task_group() as tg:
        tg.start_soon(w.sync.run_rss, "alice")
        tg.start_soon(w.sync.run_rss, "alice", "sync_now")
    assert await w.rows("SELECT count(*) FROM change_log WHERE kind = 'push'") == [(1,)]
    assert [n for n, _, _ in w.hardcover.writes] == ["update_user_book"]


async def test_a_pending_change_waits_for_a_batch_that_lists_its_row(w: World) -> None:
    for n in range(250):
        w.goodreads.add_item(GR_USER, 1000 + n, f"Book {n}")  # Dune drops to page 2
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    dune = next(i for i in w.goodreads.users[GR_USER].items if i["book_id"] == str(DUNE))
    dune["user_rating"] = "5"
    w.hardcover.fail_next("UpdateUserBook", "unavailable")
    await w.sync.run_rss("alice")  # sees the change; the write fails
    w.clock.advance(3600)
    w.goodreads.fail_next("server_error", page=2)  # Dune's page is missing this time
    run_id = await w.sync.run_rss("alice")
    assert (await state(w, run_id))[:3] == ("partial", 0, 0)
    assert await w.rows(
        "SELECT pending_value FROM baseline_fields WHERE source_id = ? AND field = 'rating'",
        str(DUNE),
    ) == [("5",)]


async def test_a_stale_shared_link_queues_the_row(tmp_path: Path) -> None:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 7)
        w.hardcover.add_mapping(DUNE, 7)
        w.goodreads.add_item(GR_USER, DUNE, "Dune")
        await w.db.write(
            lambda conn: conn.execute(
                "INSERT INTO links (source, source_id, hc_book_id, method, state, linked_at, "
                "last_seen_at) VALUES ('goodreads', ?, 5, 'gr_mapping', 'stale', 0, 0)",
                (str(DUNE),),
            )
        )
        await w.sync.run_rss("alice")
        assert await w.rows("SELECT state, queue_reason FROM source_rows") == [("queued", "stale")]
        assert await w.rows("SELECT hc_book_id, state FROM links") == [(5, "stale")]


async def test_the_backfill_finds_a_shelved_book_hardcover_has_merged(tmp_path: Path) -> None:
    async with world(tmp_path) as w:
        # Shelved as book 100, which Hardcover merged into 200; the mapping names 100 too.
        w.hardcover.add_user_book(HC_USER, 100, book={"canonical_id": 200})
        w.hardcover.add_mapping(DUNE, 100)
        w.goodreads.add_item(GR_USER, DUNE, "Dune")
        await w.sync.run_rss("alice")
        assert await w.rows("SELECT state, hc_book_id FROM source_rows") == [("linked", 200)]


async def test_a_library_syncshelf_cant_store_defers_the_run(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"
    w.hardcover.add_user_book(HC_USER, 9, status_id=9)  # a status the mirror refuses
    run_id = await w.sync.run_rss("alice")
    assert await state(w, run_id) == (
        "deferred",
        0,
        0,
        "Hardcover sent data SyncShelf couldn't store (IntegrityError)",
    )
    assert w.hardcover.writes == []


async def test_a_re_read_with_no_open_read_is_always_a_new_read(w: World) -> None:
    dune = w.hardcover.user_books[HC_USER][0]
    dune["user_book_reads"] = [read_row(1, "2026-09-20", "2026-10-10", 70)]
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    items = {i["book_id"]: i for i in w.goodreads.users[GR_USER].items}
    items[str(DUNE)]["user_shelves"] = "currently-reading"
    await w.sync.run_rss("alice")  # opens a read on 2026-10-15, the default added date
    # That read is removed in Hardcover before the book is finished again.
    dune["user_book_reads"] = [r for r in dune["user_book_reads"] if r["finished_at"]]
    w.clock.advance(3600)
    items[str(DUNE)]["user_shelves"] = ""
    await w.sync.run_rss("alice")
    # 2026-10-15 is within 14 days of the old finish, but a re-read is never adopted.
    finishes = sorted(r["finished_at"][:10] for r in dune["user_book_reads"])
    assert finishes == ["2026-10-10", "2026-10-15"]


async def test_a_failed_edition_lookup_holds_only_the_reads(w: World) -> None:
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    items = {i["book_id"]: i for i in w.goodreads.users[GR_USER].items}
    items[str(DUNE)]["user_rating"] = "5"
    items[str(DUNE)]["user_read_at"] = "Fri, 2 Oct 2026 00:00:00 +0000"  # a correction read
    w.hardcover.user_books[HC_USER][0]["user_book_reads"] = [read_row(1, None, "2026-01-01", 70)]
    w.hardcover.fail_next("EditionFormats", "unavailable")
    run_id = await w.sync.run_rss("alice")
    (run_state, applied, failed, _) = await state(w, run_id)
    assert (run_state, applied, failed) == ("partial", 1, 1)
    assert w.hardcover.user_books[HC_USER][0]["rating"] == 5.0
    assert await w.rows("SELECT field FROM baseline_fields WHERE pending_since IS NOT NULL") == [
        ("date_read",)
    ]


async def test_a_link_edition_of_another_book_is_not_written(tmp_path: Path) -> None:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 7, status_id=1)
        w.hardcover.add_mapping(DUNE, 7)
        w.hardcover.editions[90] = {"book_id": 9, "reading_format_id": 4}  # not Dune's
        w.goodreads.add_item(GR_USER, DUNE, "Dune", shelf="to-read")
        await w.sync.run_rss("alice")
        await w.db.write(lambda conn: conn.execute("UPDATE links SET hc_edition_id = 90"))
        w.clock.advance(3600)
        w.goodreads.users[GR_USER].items[0]["user_shelves"] = ""
        w.goodreads.users[GR_USER].items[0]["user_read_at"] = "Fri, 2 Oct 2026 00:00:00 +0000"
        await w.sync.run_rss("alice")
        (read,) = w.hardcover.user_books[HC_USER][0]["user_book_reads"]
        assert (read["finished_at"][:10], read["edition_id"]) == ("2026-10-02", None)
