"""Every case in ``tests/golden/diff.yaml`` run through the real Goodreads engine against the
fakes (SPEC §2.9 golden sets, §3.4a.6). The YAML file is the decision table."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from syncshelf.sync import batches

from fakes.goodreads_csv import ExportBook, library_export
from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio

GOLDEN = yaml.safe_load(
    (Path(__file__).parents[1] / "golden" / "diff.yaml").read_text(encoding="utf-8")
)
CASES = GOLDEN["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_case(tmp_path: Path, case: dict[str, Any]) -> None:
    async with world(tmp_path) as w:
        hardcover = case.get("hardcover") or {}
        for n, (book_id, fields) in enumerate((hardcover.get("shelf") or {}).items()):
            fields = dict(fields)
            reads = [
                {"id": 1000 * (n + 1) + i, "started_at": r[0], "finished_at": r[1],
                 "edition_id": r[2] if len(r) > 2 else None}
                for i, r in enumerate(fields.pop("reads", []))
            ]  # fmt: skip
            w.hardcover.add_user_book(
                HC_USER, int(book_id), user_book_reads=reads, **_shelf_fields(fields)
            )
        # A mapped book carries the title the feed gives it, so the title floor passes.
        titles = {
            str(i["id"]): i.get("title")
            for run in case["runs"]
            for i in [*run.get("feed", []), *run.get("export", [])]
        }
        for goodreads_id, mapping in (hardcover.get("mappings") or {}).items():
            title = titles.get(str(goodreads_id))
            if isinstance(mapping, dict):
                book, edition = mapping["book"], mapping.get("edition")
                extra = {"reading_format_id": mapping["format"]} if "format" in mapping else {}
                w.hardcover.add_mapping(goodreads_id, book, edition, title=title, **extra)
            else:
                w.hardcover.add_mapping(goodreads_id, mapping, title=title)
        for edition in hardcover.get("editions") or []:
            keys = {k: edition[k] for k in ("isbn_13", "isbn_10") if k in edition}
            w.hardcover.add_edition(edition["id"], edition["book"], **keys)
            w.hardcover.catalog[edition["book"]]["title"] = edition.get("title", "")
        for n, run in enumerate(case["runs"]):
            if n:
                w.clock.advance(3600)
            if resolve := run.get("resolve"):
                # A person's answer (resolve_match), applied at once with no run.
                writes_before = len(w.hardcover.writes)
                log_before = (await w.rows("SELECT count(*) FROM change_log"))[0][0]
                done = await w.sync.resolve("alice", str(resolve["id"]), resolve["book"])
                expect = run.get("expect") or {}
                if "resolved" in expect:
                    assert {k: getattr(done, k) for k in expect["resolved"]} == expect["resolved"]
                await _check(w, expect, None, writes_before, log_before)
                continue
            if "export" in run:
                await _upload(w, run["export"], run.get("export_ago", 0))
                if "feed" not in run:
                    w.goodreads.fail_next("server_error")  # only the export speaks
            if "feed" in run:
                _serve(w, run["feed"], run.get("filler", 0))
            if fail := run.get("fail"):
                w.goodreads.fail_next(fail["fault"], page=fail.get("page"))
            if fail := run.get("hardcover_fail"):
                for _ in range(fail.get("times", 1)):
                    w.hardcover.fail_next(fail["op"], fail["fault"])
            writes_before = len(w.hardcover.writes)
            log_before = (await w.rows("SELECT count(*) FROM change_log"))[0][0]
            run_id = (await w.sync.run_rss("alice")).run_id
            await _check(w, run.get("expect") or {}, run_id, writes_before, log_before)


def _shelf_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {{"status": "status_id"}.get(k, k): v for k, v in fields.items()}


async def _upload(w: World, export: list[dict[str, Any]], ago: int | None) -> None:
    """Store a library export made ``ago`` seconds before now (``None``: undated, stale)."""
    keys = ("rating", "shelf", "date_read", "read_count", "binding", "spoiler", "review", "isbn13")
    books = [
        ExportBook(
            int(b["id"]), b.get("title", f"Book {b['id']}"), **{k: b[k] for k in keys if k in b}
        )
        for b in export
    ]
    now = int(w.clock.time())
    observed = None if ago is None else now - ago
    payload = library_export(*books)
    await w.db.write(batches.store, "alice", "gr_csv_ingest", now, observed, payload, len(books))


def _serve(w: World, feed: list[dict[str, Any]], filler: int) -> None:
    w.goodreads.users[GR_USER].items.clear()
    # Unmatched books older than the feed's own, to push it past one page of 200.
    for n in range(filler):
        w.goodreads.add_item(GR_USER, 900_000 + n, f"Filler {n}")
    for item in reversed(feed):  # add_item puts each on top: the feed is newest first
        extra = {}
        if "rating" in item:
            extra["user_rating"] = item["rating"]
        if "read_at" in item:
            extra["user_read_at"] = item["read_at"]
        if "review" in item:
            extra["user_review"] = item["review"]
        if "added" in item:
            extra["user_date_added"] = item["added"]
        if "isbn" in item:
            extra["isbn"] = item["isbn"]
        w.goodreads.add_item(
            GR_USER,
            item["id"],
            item.get("title", f"Book {item['id']}"),
            shelf=item.get("shelf", "read"),
            shelves=tuple(item.get("shelves", ())),
            **extra,
        )


async def _check(
    w: World, expect: dict[str, Any], run_id: int | None, writes_before: int, log_before: int
) -> None:
    if "state" in expect:
        (state,) = (await w.rows("SELECT state FROM runs WHERE id = ?", run_id))[0]
        assert state == expect["state"]
    if "writes" in expect:
        sent = [[name, fields] for name, _, fields in w.hardcover.writes[writes_before:]]
        assert sent == expect["writes"]
    for book_id, fields in (expect.get("shelf") or {}).items():
        (row,) = [ub for ub in w.hardcover.user_books[HC_USER] if ub["book_id"] == int(book_id)]
        for key, value in _shelf_fields(fields).items():
            assert row[key] == value, f"book {book_id} {key}"
    for source_id, fields in (expect.get("baselines") or {}).items():
        for field, value in fields.items():
            found = await w.rows(
                "SELECT value FROM baseline_fields WHERE source_id = ? AND field = ?",
                str(source_id),
                field,
            )
            assert found == [(value,)], f"{source_id} {field} baseline"
    if "pending" in expect:
        found = await w.rows(
            "SELECT source_id, field, pending_value FROM baseline_fields "
            "WHERE pending_since IS NOT NULL"
        )
        wanted = [
            (str(source_id), field, value)
            for source_id, fields in expect["pending"].items()
            for field, value in fields.items()
        ]
        assert sorted(found) == sorted(wanted)
    if "change_log" in expect:
        found = await w.rows(
            "SELECT source_id, field, old_value, new_value FROM change_log WHERE id > ? "
            "ORDER BY id",
            log_before,
        )
        assert [list(r) for r in found] == expect["change_log"]
    for book_id, wanted in (expect.get("reads") or {}).items():
        (row,) = [ub for ub in w.hardcover.user_books[HC_USER] if ub["book_id"] == int(book_id)]
        found = [
            [(r["started_at"] or None) and r["started_at"][:10],
             (r["finished_at"] or None) and r["finished_at"][:10],
             r["edition_id"]]
            for r in sorted(
                row["user_book_reads"],
                key=lambda r: (r["started_at"] or r["finished_at"] or "", r["id"]),
            )
        ]  # fmt: skip
        assert found == wanted, f"book {book_id} reads"
    if "log" in expect:
        found = await w.rows(
            "SELECT kind, field, trigger, date_source FROM change_log WHERE id > ? ORDER BY id",
            log_before,
        )
        assert [list(r) for r in found] == expect["log"]
    for source_id, (state, reason) in (expect.get("rows") or {}).items():
        found = await w.rows(
            "SELECT state, queue_reason FROM source_rows WHERE source_id = ?", str(source_id)
        )
        assert found == [(state, reason)], f"row {source_id}"
    if "gone" in expect:
        found = await w.rows(
            "SELECT source_id FROM source_rows WHERE gone_at IS NOT NULL ORDER BY source_id"
        )
        assert [r[0] for r in found] == [str(i) for i in expect["gone"]]
    if "backfill" in expect:
        found = await w.rows(
            "SELECT rows_linked, rows_queued FROM backfills WHERE run_id = ?", run_id
        )
        assert [list(r) for r in found] == [expect["backfill"]]
