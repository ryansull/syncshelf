"""``search_books`` through the real ASGI app (SPEC §3.3.7, §3.3.8), in dev mode as alice."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from syncshelf.app import create_app

from clock import FakeClock
from fakes.hardcover import FakeHardcover
from serving import hardcover_token, make_config, mcp_client, running

ALICE, BOB = 42, 43

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_user(hardcover_token("bob"), user_id=BOB)
    fake.add_search_doc(
        1,
        "Dune",
        subtitle="Deluxe Edition",
        author_names=["Frank Herbert"],
        genres=["Science Fiction", "Classics", "Space", "Politics"],
        release_year=1965,
        rating=4.2649,
        users_count=9000,
        pages=412,
        has_audiobook=True,
        has_ebook=True,
        featured_series={"position": 1.0, "series": {"id": 5, "name": "Dune"}},
        description="Set on the desert planet Arrakis. " * 20,
    )
    fake.add_search_doc(
        2,
        "Dune Messiah",
        author_names=["Frank Herbert"],
        genres=["Science Fiction"],
        release_year=1969,
        rating=3.9,
        users_count=5000,
    )
    fake.add_search_doc(
        3,
        "The Dispossessed",
        author_names=["Ursula K. Le Guin"],
        genres=["Science Fiction"],
        release_year=1974,
        rating=4.4,
        users_count=4000,
    )
    fake.add_search_doc(
        4,
        "A Wizard of Earthsea",
        author_names=["Ursula K. Le Guin"],
        genres=["Fantasy"],
        release_year=1968,
        rating=4.1,
        users_count=8000,
    )
    fake.add_user_book(ALICE, 2, status_id=5)  # did-not-finish still counts as shelved
    fake.add_user_book(BOB, 3, status_id=3)
    return fake  # fmt: skip


@pytest.fixture
def connect(tmp_path: Path, data_dir: Path, fake: FakeHardcover, clock: FakeClock) -> Any:
    @asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        config = make_config(
            tmp_path,
            profiles=["alice", "bob"],
            profile_yaml={"bob": "{household_visible: true}"},
        )
        app = create_app(
            config,
            data_dir=data_dir,
            clock=clock.time,
            http_transport=fake.transport,
            limiter_clock=clock,
        )
        async with running(app) as http, mcp_client(http) as mcp:
            yield mcp

    return connect


async def search(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("search_books", arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def ids(mcp: Client, **arguments: Any) -> list[int]:
    return [b["book_id"] for b in (await search(mcp, **arguments))["books"]]


async def test_a_hit_is_compact_and_annotated(connect: Any) -> None:
    async with connect() as mcp:
        page = await search(mcp, query="dune")
    [dune] = page["books"]  # Dune Messiah is shelved (did-not-finish), so it's left out
    assert {k: v for k, v in dune.items() if k != "description"} == {
        "book_id": 1,
        "title": "Dune",
        "subtitle": "Deluxe Edition",
        "author": "Frank Herbert",
        "release_year": 1965,
        "series": {"name": "Dune", "position": 1.0},
        "community_rating": 4.26,
        "pages": 412,
        "has_audiobook": True,
        "has_ebook": True,
        "genres": ["Science Fiction", "Classics", "Space"],
        "status": None,
    }
    assert len(dune["description"]) <= 281 and dune["description"].endswith("…")
    assert (page["found"], page["excluded_shelved"]) == (2, 1)


async def test_shelved_books_can_be_kept_with_their_status(connect: Any) -> None:
    async with connect() as mcp:
        page = await search(mcp, query="dune", exclude_shelved=False)
    assert [(b["book_id"], b["status"]) for b in page["books"]] == [
        (1, None),
        (2, "did-not-finish"),
    ]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"author": "Ursula K. Le Guin"}, [3, 4]),
        ({"genre": "Fantasy"}, [4]),
        ({"author": "Ursula K. Le Guin", "genre": "Science Fiction"}, [3]),
        ({"genre": "Science Fiction", "sort": "popular"}, [1, 3]),
        ({"genre": "Science Fiction", "sort": "rating"}, [3, 1]),
        ({"genre": "Science Fiction", "sort": "newest"}, [3, 1]),
        ({"author": "Le`Guin"}, []),  # a backtick can't break out of the filter
    ],
)
async def test_filters_and_sorts(
    connect: Any, arguments: dict[str, Any], expected: list[int]
) -> None:
    async with connect() as mcp:
        assert await ids(mcp, **arguments) == expected


async def test_a_filter_with_no_query_searches_everything(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        await ids(mcp, genre="Fantasy")
    [(_, variables)] = [r for r in fake.requests if r[0] == "Search"]
    assert (variables["q"], variables["filter"]) == ("*", "genres:=[`Fantasy`]")


async def test_nothing_to_search_for_is_refused(connect: Any) -> None:
    async with connect() as mcp:
        result = await mcp.call_tool("search_books", {"query": "  "})
    assert result.is_error


async def test_household_leaves_out_anyones_shelf(connect: Any) -> None:
    async with connect() as mcp:
        page = await search(mcp, genre="Science Fiction", profile="household")
    assert [b["book_id"] for b in page["books"]] == [1]
    assert page["books"][0]["statuses"] == {"alice": None, "bob": None}


async def test_more_pages_are_fetched_when_shelved_books_are_left_out(
    connect: Any, fake: FakeHardcover
) -> None:
    for n in range(100, 140):
        fake.add_search_doc(n, f"Fantasy {n}", genres=["Fantasy"])
        if n < 125:  # nearly all of the first page is already shelved
            fake.add_user_book(ALICE, n, status_id=1)
    async with connect() as mcp:
        page = await search(mcp, genre="Fantasy", limit=5)
    # Page 1 is A Wizard of Earthsea then 100–123 (shelved); page 2 starts at 124 (shelved).
    assert [b["book_id"] for b in page["books"]] == [4, 125, 126, 127, 128]
    assert page["excluded_shelved"] == 25
    assert [op for op, _ in fake.requests].count("Search") == 2


@pytest.mark.parametrize("limit", [0, 26])
async def test_limit_is_1_to_25(connect: Any, limit: int) -> None:
    async with connect() as mcp:
        result = await mcp.call_tool("search_books", {"query": "dune", "limit": limit})
    assert result.is_error
