"""Every case in ``tests/golden/diff.yaml`` run through the real Goodreads engine against the
fakes (SPEC §2.9 golden sets, §3.4a.6). The YAML file is the decision table."""

from pathlib import Path
from typing import Any

import pytest
import yaml

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
        for book_id, fields in (hardcover.get("shelf") or {}).items():
            w.hardcover.add_user_book(HC_USER, int(book_id), **_shelf_fields(fields))
        for goodreads_id, book_id in (hardcover.get("mappings") or {}).items():
            w.hardcover.add_mapping(goodreads_id, book_id)
        for n, run in enumerate(case["runs"]):
            if n:
                w.clock.advance(3600)
            _serve(w, run["feed"])
            writes_before = len(w.hardcover.writes)
            log_before = (await w.rows("SELECT count(*) FROM change_log"))[0][0]
            run_id = await w.sync.run_rss("alice")
            await _check(w, run.get("expect") or {}, run_id, writes_before, log_before)


def _shelf_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {{"status": "status_id"}.get(k, k): v for k, v in fields.items()}


def _serve(w: World, feed: list[dict[str, Any]]) -> None:
    w.goodreads.users[GR_USER].items.clear()
    for item in reversed(feed):  # add_item puts each on top: the feed is newest first
        extra = {}
        if "rating" in item:
            extra["user_rating"] = item["rating"]
        if "read_at" in item:
            extra["user_read_at"] = item["read_at"]
        if "review" in item:
            extra["user_review"] = item["review"]
        w.goodreads.add_item(
            GR_USER,
            item["id"],
            item.get("title", f"Book {item['id']}"),
            shelf=item.get("shelf", "read"),
            shelves=tuple(item.get("shelves", ())),
            **extra,
        )


async def _check(
    w: World, expect: dict[str, Any], run_id: int, writes_before: int, log_before: int
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
    if "backfill" in expect:
        found = await w.rows(
            "SELECT rows_linked, rows_queued FROM backfills WHERE run_id = ?", run_id
        )
        assert [list(r) for r in found] == [expect["backfill"]]
