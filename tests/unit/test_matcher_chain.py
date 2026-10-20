"""The matcher chain, run through ``tests/golden/matcher.yaml``'s ``chain`` cases against
FakeHardcover (SPEC §3.4a.5, §2.9 golden sets)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.sync.matcher import MatchInput, match

from fakes.hardcover import URL, FakeHardcover

pytestmark = pytest.mark.anyio

CASES = yaml.safe_load(
    (Path(__file__).parents[1] / "golden" / "matcher.yaml").read_text(encoding="utf-8")
)["chain"]


def fake_for(hardcover: dict[str, Any]) -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user("t", user_id=42)
    books = hardcover.get("books") or {}
    for book_id, fields in books.items():
        fake.add_book(int(book_id), title=fields["title"], canonical_id=fields.get("canonical"))
    for goodreads_id, rows in (hardcover.get("mappings") or {}).items():
        for row in rows:
            fake.add_mapping(goodreads_id, row["book"], title=row["title"])
    for edition in hardcover.get("editions") or []:
        keys = {k: edition[k] for k in ("isbn_13", "isbn_10") if k in edition}
        fake.add_edition(edition["id"], edition["book"], **keys)
    for doc in hardcover.get("search") or []:
        fake.add_search_doc(doc["book"], doc["title"], author_names=doc["authors"])
        if doc["book"] in books:
            fake.catalog[doc["book"]]["canonical_id"] = books[doc["book"]].get("canonical")
    return fake


def client_for(fake: FakeHardcover) -> HardcoverClient:
    return HardcoverClient(URL, "t", gate=None, transport=fake.transport)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_chain(case: dict[str, Any]) -> None:
    fake = fake_for(case["hardcover"])
    row = case["row"]
    given = MatchInput(
        row["id"], row["title"], row.get("author"), row.get("isbn13"), row.get("isbn10")
    )
    found = await match(client_for(fake), [given], accept_fuzzy=case.get("accept_fuzzy", True))
    outcome, expect = found[row["id"]], case["expect"]
    if "method" in expect:
        assert outcome.hit is not None, outcome
        assert (outcome.hit.method, outcome.hit.book_id) == (expect["method"], expect["book"])
    else:
        assert outcome.hit is None, outcome
        assert outcome.reason == expect["reason"]
        assert [c.book_id for c in outcome.candidates] == expect["candidates"]


async def test_the_id_steps_are_batched_across_rows() -> None:
    fake = fake_for(
        {
            "mappings": {"1": [{"book": 7, "title": "A"}], "2": [{"book": 8, "title": "B"}]},
            "editions": [{"id": 90, "book": 9, "isbn_13": "9780000000001"}],
            "books": {9: {"title": "C"}},
        }
    )
    rows = [
        MatchInput("1", "A", "x"),
        MatchInput("2", "B", "x"),
        MatchInput("3", "C", "x", isbn13="9780000000001"),
        MatchInput("4", "C", "x", isbn13="9780000000001"),
    ]
    outcomes = await match(client_for(fake), rows, accept_fuzzy=True)
    linked = {k: o.hit.book_id for k, o in outcomes.items() if o.hit is not None}
    assert linked == {"1": 7, "2": 8, "3": 9, "4": 9}
    assert [op for op, _ in fake.requests] == ["ByGoodreads", "ByIsbn13"]
