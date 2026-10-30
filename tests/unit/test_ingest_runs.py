"""Runs applying uploaded exports (SPEC §3.4a.8, #204): stored dated batches first, oldest
first, then RSS; the newer observation winning field by field and for presence (§3.4a.6 rule
8); a first CSV after an RSS backfill pushing nothing (rule 3); and the shelves stored exports
teach RSS (#182)."""

from pathlib import Path

import pytest

from syncshelf.sync import batches, goodreads

from fakes.goodreads_csv import ExportBook, library_export
from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio


async def upload(w: World, *books: ExportBook, ago: int | None = 0) -> int:
    """Store an export generated ``ago`` seconds before now (``None``: stale, undated)."""
    now = int(w.clock.time())
    observed = None if ago is None else now - ago
    return await w.db.write(
        batches.store, "alice", "gr_csv_ingest", now, observed, library_export(*books), len(books)
    )


async def rating(w: World, book_id: int) -> float | None:
    (shelved,) = [ub for ub in w.hardcover.user_books[HC_USER] if ub["book_id"] == book_id]
    return shelved["rating"]


@pytest.fixture
async def w(tmp_path: Path) -> World:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 7, status_id=3, rating=4.0)
        w.hardcover.add_mapping(100, 7, title="Dune")
        yield w  # type: ignore[misc]


async def backfill_by_rss(w: World) -> None:
    w.goodreads.add_item(GR_USER, 100, "Dune", user_rating="4")
    await w.sync.run_rss("alice")
    w.clock.advance(3600)


async def test_a_dated_export_can_be_the_backfill(w: World) -> None:
    await upload(w, ExportBook(100, "Dune", rating=4))
    w.goodreads.fail_next("server_error")
    result = await w.sync.run_rss("alice")
    assert result.state == "fetch_failed"  # RSS failed, but the export was applied first
    assert await w.rows("SELECT rows_linked, rows_queued FROM backfills") == [(1, 0)]
    assert await w.rows("SELECT processed_at IS NOT NULL, run_id FROM ingest_batches") == [
        (1, result.run_id)
    ]
    assert w.hardcover.writes == []


async def test_the_first_export_after_an_rss_backfill_pushes_nothing(w: World) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=4, spoiler=True, read_count=2))
    await w.sync.run_rss("alice")
    assert w.hardcover.writes == []
    assert await w.rows(
        "SELECT field, value FROM baseline_fields WHERE field IN ('spoiler', 'read_count') "
        "ORDER BY field"
    ) == [("read_count", "2"), ("spoiler", "1")]


async def test_a_newer_export_pushes_its_change(w: World) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=5))
    await w.sync.run_rss("alice")
    assert await rating(w, 7) == 5.0


async def test_an_export_older_than_rss_cant_roll_it_back(w: World) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=2), ago=7200)  # made before the backfill
    await w.sync.run_rss("alice")
    assert w.hardcover.writes == []
    assert await rating(w, 7) == 4.0


async def test_exports_are_applied_once_oldest_first(w: World) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=5), ago=60)
    await upload(w, ExportBook(100, "Dune", rating=3), ago=600)
    w.goodreads.fail_next("server_error")  # so only the exports speak
    await w.sync.run_rss("alice")
    assert await rating(w, 7) == 5.0  # the newer export wins, whatever the upload order
    writes = len(w.hardcover.writes)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"  # the feed agrees, as it would
    w.clock.advance(3600)
    await w.sync.run_rss("alice")
    assert len(w.hardcover.writes) == writes
    assert await w.rows("SELECT count(*) FROM ingest_batches WHERE processed_at IS NULL") == [(0,)]


async def test_a_stale_upload_is_not_applied(w: World) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=1), ago=None)
    await w.sync.run_rss("alice")
    assert w.hardcover.writes == []
    assert await w.rows("SELECT processed_at FROM ingest_batches") == [(None,)]


async def test_an_older_export_cant_find_a_book_rss_has_seen_since_gone(w: World) -> None:
    await backfill_by_rss(w)
    w.goodreads.add_item(GR_USER, 200, "Emma")
    await w.sync.run_rss("alice")  # RSS sees Emma now
    w.clock.advance(60)
    await upload(w, ExportBook(100, "Dune", rating=4), ago=3600)  # made before Emma was shelved
    await w.sync.run_rss("alice")
    assert await w.rows("SELECT source_id FROM source_rows WHERE gone_at IS NOT NULL") == []


async def test_a_newer_export_finds_a_removed_book_gone(w: World) -> None:
    await backfill_by_rss(w)
    w.goodreads.add_item(GR_USER, 200, "Emma")
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    await upload(w, ExportBook(100, "Dune", rating=4))  # Emma since removed on Goodreads
    w.goodreads.fail_next("server_error")  # so only the export speaks
    await w.sync.run_rss("alice")
    assert await w.rows("SELECT source_id FROM source_rows WHERE gone_at IS NOT NULL") == [("200",)]


async def test_an_export_hardcover_cant_take_waits_for_the_next_run(w: World) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=5))
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "5"  # the feed agrees, as it would
    w.clock.advance(3600)  # past the mirror's freshness
    w.hardcover.fail_next("Library", "unavailable")
    result = await w.sync.run_rss("alice")
    assert result.state == "deferred"
    assert await w.rows("SELECT processed_at FROM ingest_batches") == [(None,)]
    w.clock.advance(3600)
    await w.sync.run_rss("alice")
    assert await rating(w, 7) == 5.0


async def test_stored_exports_teach_rss_which_shelves_are_tags(w: World) -> None:
    w.goodreads.add_item(GR_USER, 100, "Dune", shelves=("owned",))
    await w.sync.run_rss("alice")
    # RSS lists only "owned": it can't tell whether that's an exclusive shelf (#182).
    assert await w.rows("SELECT count(*) FROM baseline_fields WHERE field = 'status'") == [(0,)]
    await upload(w, ExportBook(200, "Emma", shelf="to-read", shelves=("owned",)), ago=None)
    w.clock.advance(3600)
    await w.sync.run_rss("alice")
    assert await w.rows(
        "SELECT value FROM baseline_fields WHERE source_id = '100' AND field = 'status'"
    ) == [("read",)]


async def test_an_export_that_cant_be_applied_never_holds_up_rss(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=5))

    def broken(*args: object, **kwargs: object) -> None:
        raise KeyError("a bug")

    monkeypatch.setattr(goodreads, "observe_csv", broken)
    w.goodreads.users[GR_USER].items[0]["user_rating"] = "3"
    result = await w.sync.run_rss("alice")
    assert result.state == "ok" and await rating(w, 7) == 3.0  # RSS still ran
    assert await w.rows("SELECT processed_at IS NOT NULL FROM ingest_batches") == [(1,)]


async def test_an_export_from_before_a_removal_changes_nothing_on_it(w: World) -> None:
    await backfill_by_rss(w)
    w.goodreads.add_item(GR_USER, 200, "Emma")
    w.hardcover.add_user_book(HC_USER, 8, status_id=3)
    w.hardcover.add_mapping(200, 8, title="Emma")
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    w.goodreads.users[GR_USER].items.pop(0)  # Emma removed on Goodreads
    await w.sync.run_rss("alice")
    writes = len(w.hardcover.writes)
    # An export made before the removal, with Emma rated: she stays gone, and nothing is
    # pushed for her.
    await upload(w, ExportBook(100, "Dune", rating=4), ExportBook(200, "Emma", rating=5), ago=5400)
    await w.sync.run_rss("alice")
    assert len(w.hardcover.writes) == writes
    assert await w.rows("SELECT source_id FROM source_rows WHERE gone_at IS NOT NULL") == [("200",)]


async def test_a_book_an_older_export_adds_is_found_gone_by_a_newer_one(w: World) -> None:
    await backfill_by_rss(w)
    await upload(w, ExportBook(100, "Dune", rating=4), ExportBook(300, "Orphan"), ago=600)
    await upload(w, ExportBook(100, "Dune", rating=4), ago=60)  # Orphan removed since
    w.goodreads.fail_next("server_error")  # only the exports speak
    await w.sync.run_rss("alice")
    assert await w.rows("SELECT source_id FROM source_rows WHERE gone_at IS NOT NULL") == [("300",)]


async def test_the_shelf_vocabulary_is_parsed_only_when_exports_change(
    w: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    parsed: list[int] = []
    real = batches.vocabulary
    monkeypatch.setattr(batches, "vocabulary", lambda e: parsed.append(len(e)) or real(e))
    await backfill_by_rss(w)
    await w.sync.run_rss("alice")
    await upload(w, ExportBook(100, "Dune", rating=4), ago=None)
    w.clock.advance(3600)
    await w.sync.run_rss("alice")
    await w.sync.run_rss("alice")
    assert parsed == [0, 1]
