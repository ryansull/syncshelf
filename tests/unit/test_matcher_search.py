"""The fuzzy step's search (SPEC §3.4a.5): author-filtered first, unfiltered once if that finds
nothing, at most two searches, and hits scored best first."""

import pytest

from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.sync.matcher import search_candidates

from fakes.hardcover import FakeHardcover
from serving import hardcover_token

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"))
    return fake


@pytest.fixture
def client(fake: FakeHardcover) -> HardcoverClient:
    return HardcoverClient(
        "https://hardcover.example/v1/graphql",
        hardcover_token("alice"),
        gate=None,
        transport=fake.transport,
    )


def searches(fake: FakeHardcover) -> list[str | None]:
    return [v.get("filter") for op, v in fake.requests if op == "Search"]


async def test_the_author_filter_finds_the_book_in_one_search(
    fake: FakeHardcover, client: HardcoverClient
) -> None:
    fake.add_search_doc(1, "Dune", author_names=["Frank Herbert"])
    hits = await search_candidates(client, "Dune", "Frank Herbert")
    assert [h.book_id for h in hits] == [1]
    assert searches(fake) == ["author_names:=[`Frank Herbert`]"]
    assert hits[0].scored.title == 100 and hits[0].scored.author == 100


async def test_no_filtered_hit_searches_once_more_without_the_filter(
    fake: FakeHardcover, client: HardcoverClient
) -> None:
    fake.add_search_doc(1, "Dune", author_names=["F. Herbert"])
    hits = await search_candidates(client, "Dune", "Frank Herbert")
    assert [h.book_id for h in hits] == [1]
    assert searches(fake) == ["author_names:=[`Frank Herbert`]", None]


async def test_nothing_found_costs_two_searches(
    fake: FakeHardcover, client: HardcoverClient
) -> None:
    assert await search_candidates(client, "Nothing Like It", "Nobody") == []
    assert len(searches(fake)) == 2


async def test_hits_come_back_best_first(fake: FakeHardcover, client: HardcoverClient) -> None:
    fake.add_search_doc(2, "Dune Messiah", author_names=["Frank Herbert"])
    fake.add_search_doc(1, "Dune", author_names=["Frank Herbert"])
    hits = await search_candidates(client, "Dune", "Frank Herbert")
    assert [h.book_id for h in hits] == [1, 2]
    assert hits[0].document["title"] == "Dune"


async def test_with_no_author_the_author_score_is_set_aside(
    fake: FakeHardcover, client: HardcoverClient
) -> None:
    fake.add_search_doc(1, "Dune", author_names=["Frank Herbert"])
    hits = await search_candidates(client, "Dune", None)
    assert searches(fake) == [None]
    assert hits[0].scored.author == 100
