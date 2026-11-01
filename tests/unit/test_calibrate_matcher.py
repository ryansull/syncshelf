"""The matcher calibration script (``tests/live/calibrate_matcher.py``, SPEC §3.4a.13), offline:
which pairs are near the thresholds, and the exact-id hint each carries."""

import sys
from datetime import date
from pathlib import Path

import pytest

from syncshelf.clients.goodreads_csv import CsvRow
from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.sync.matcher import Hit, Scored

from fakes.hardcover import FakeHardcover
from serving import hardcover_token

sys.path.insert(0, str(Path(__file__).parents[1] / "live"))
import calibrate_matcher  # noqa: E402

pytestmark = pytest.mark.anyio


def row(book_id: str = "100", title: str = "Dune", author: str = "Frank Herbert") -> CsvRow:
    return CsvRow(
        book_id, title, author, None, None, 0, "Paperback", None, date(2026, 1, 1), "read", (),
        "", False, 1,
    )  # fmt: skip


def hit(book_id: int, title: float, author: float) -> Hit:
    return Hit(book_id, Scored(title, author), {"title": f"Book {book_id}", "author_names": []})


def test_only_pairs_near_a_threshold_are_listed() -> None:
    hits = [hit(1, 100, 100), hit(2, 40, 100), hit(3, 85, 20), hit(4, 30, 85)]
    pairs = calibrate_matcher.near_pairs(row(), hits, None)
    # The exact top with a wide margin is settled; 3's title and 4's author are near.
    assert [(p["book_id"], p["rank"]) for p in pairs] == [(3, 3), (4, 4)]
    assert all(p["label"] is None and p["margin"] is None for p in pairs)


def test_a_top_candidate_with_a_narrow_margin_is_near() -> None:
    assert calibrate_matcher.near_pairs(row(), [hit(1, 100, 100), hit(2, 50, 10)], 1) == []
    pairs = calibrate_matcher.near_pairs(row(), [hit(1, 100, 100), hit(2, 90, 10)], 2)
    top = pairs[0]
    assert (top["book_id"], top["margin"], top["auto_accepts"], top["id_agrees"]) == (
        1,
        10.0,
        True,
        False,
    )


async def test_calibrate_reads_the_exact_ids_then_searches_each_book() -> None:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"))
    fake.add_mapping(100, 7, title="Dune")
    fake.add_search_doc(7, "Dunes", author_names=["Frank Herbert"])  # title 88.9
    client = HardcoverClient(
        "https://hardcover.example/v1/graphql",
        hardcover_token("alice"),
        gate=None,
        transport=fake.transport,
    )
    pairs = await calibrate_matcher.calibrate(client, [row()])
    await client.aclose()
    assert [(p["book_id"], p["id_book"], p["id_agrees"]) for p in pairs] == [(7, 7, True)]
    assert fake.writes == []


def test_an_exact_tops_margin_is_over_full_titles() -> None:
    # "Red Rising" against "Red Rising: Sons of Ares": the same before ':', not a rival (#138).
    adaptation = Hit(2, Scored(100, 100, 51.3), {"title": "Book 2", "author_names": []})
    assert calibrate_matcher.near_pairs(row(), [hit(1, 100, 100), adaptation], 1) == []
