"""``get_book`` through the real ASGI app (SPEC §3.3.7, §3.3.8), in dev mode as alice."""

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


def person(name: str, role: str | None = "Author") -> dict[str, Any]:
    return {"author": {"id": hash(name) % 1000, "name": name}, "contribution": role}


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_user(hardcover_token("bob"), user_id=BOB)
    fake.add_user_book(
        ALICE,
        7,
        status_id=3,
        rating=4.5,
        date_added="2026-01-02",
        review_raw="Loved it.",
        review_has_spoilers=True,
        user_book_reads=[
            {
                "id": 1,
                "started_at": "2025-01-01",
                "finished_at": "2025-01-20",
                "edition_id": 70,
                "reading_format_id": 2,
            },
            {"id": 2},
        ],
        book={
            "title": "Dune",
            "subtitle": "Book One",
            "release_year": 1965,
            "pages": None,
            "cached_contributors": [person("Frank Herbert"), person("Jo Artist", "Illustrator")],
            "genres": [{"tag": g} for g in ("SF", "Classics", "Space", "Politics", "Desert", "X")],
            "default_audio_edition": {"id": 72, "audio_seconds": 75600},
            "default_physical_edition": {"id": 70, "pages": 412},
        },
    )
    fake.catalog[7].update(
        rating=4.2567,
        users_count=9000,
        cached_similar_book_ids=[30, 8, 20, 99],
        book_series=[{"position": 1.0, "series": {"id": 5, "name": "Dune"}}],
        warnings=[{"tag": f"W{n}"} for n in range(12)],
        narrated={
            "cached_contributors": [person("Frank Herbert"), person("Scott Brick", "Narrator")]
        },
    )
    fake.add_user_book(ALICE, 8, status_id=1, book={"title": "Dune Messiah"})
    fake.add_book(
        20,
        title="Hyperion",
        release_year=1989,
        rating=4.1,
        cached_contributors=[person("Dan Simmons")],
    )
    fake.add_book(30, title="Foundation", cached_contributors=[person("Isaac Asimov")])
    fake.add_user_book(BOB, 7, status_id=3, rating=2.0, review_raw="Bob's private note.")
    fake.add_list(ALICE, "Favourites", [7])
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


async def get_book(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("get_book", arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def library_rows(mcp: Client) -> dict[int, dict[str, Any]]:
    result = await mcp.call_tool("get_my_library", {})
    return {b["book_id"]: b for b in result.structured_content["books"]}


async def test_the_full_record(connect: Any) -> None:
    async with connect() as mcp:
        await library_rows(mcp)  # fills the mirror, which the lists come from
        record = await get_book(mcp, book=7)
    assert {k: record[k] for k in ("book_id", "title", "subtitle", "release_year")} == {
        "book_id": 7, "title": "Dune", "subtitle": "Book One", "release_year": 1965,
    }  # fmt: skip
    assert (record["pages"], record["audio_hours"], record["community_rating"]) == (412, 21.0, 4.26)
    assert record["contributors"] == [
        {"name": "Frank Herbert", "role": "Author"},
        {"name": "Jo Artist", "role": "Illustrator"},
    ]
    assert record["narrators"] == ["Scott Brick"]
    assert record["series"] == [{"series_id": 5, "name": "Dune", "position": 1.0}]
    assert record["genres"] == ["SF", "Classics", "Space", "Politics", "Desert"]  # top 5
    assert record["content_warnings"] == [f"W{n}" for n in range(10)]  # at most 10
    assert "canonical_id" not in record
    [shelf] = record["shelves"]
    assert shelf == {
        "profile": "alice",
        "status": "read",
        "rating": 4.5,
        "date_added": "2026-01-02",
        "reads": [
            {"read_id": 1, "started_on": "2025-01-01", "finished_on": "2025-01-20",
             "formats": ["audio"]},
            {"read_id": 2, "started_on": None, "finished_on": None, "formats": ["unknown"]},
        ],
        "lists": ["Favourites"],
        "review": "Loved it.",
        "review_has_spoilers": True,
    }  # fmt: skip


async def test_similar_books_keep_hardcovers_order_with_shelf_status(connect: Any) -> None:
    async with connect() as mcp:
        await library_rows(mcp)
        similar = (await get_book(mcp, book=7))["similar"]
    assert [(s["book_id"], s["title"], s["status"]) for s in similar] == [
        (30, "Foundation", None),
        (8, "Dune Messiah", "want-to-read"),
        (20, "Hyperion", None),  # 99 isn't a book Hardcover knows: left out
    ]
    assert similar[2]["author"] == "Dan Simmons"
    assert similar[2]["community_rating"] == 4.1


async def test_a_live_read_is_written_back_to_the_mirror(connect: Any, fake: FakeHardcover) -> None:
    async with connect() as mcp:
        assert (await library_rows(mcp))[7]["rating"] == 4.5
        fake.user_books[ALICE][0]["rating"] = 3.0  # changed in Hardcover itself
        await get_book(mcp, book=7)
        rows = await library_rows(mcp)  # within the TTL: served from the copy
    assert rows[7]["rating"] == 3.0
    assert sum(op == "Library" for op, _ in fake.requests) == 1


async def test_a_book_removed_in_hardcover_leaves_the_mirror(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        assert 8 in await library_rows(mcp)
        fake.user_books[ALICE] = [ub for ub in fake.user_books[ALICE] if ub["book_id"] != 8]
        record = await get_book(mcp, book=8)
        rows = await library_rows(mcp)
    assert record["shelves"] == []
    assert 8 not in rows


async def test_household_shows_each_shelf_but_only_the_callers_review(connect: Any) -> None:
    async with connect() as mcp:
        record = await get_book(mcp, book=7, profile="household")
    alice, bob = record["shelves"]
    assert (alice["profile"], alice["review"]) == ("alice", "Loved it.")
    assert (bob["profile"], bob["rating"]) == ("bob", 2.0)
    assert "review" not in bob and "review_has_spoilers" not in bob
    assert "Bob's private note" not in str(record)


async def test_an_unknown_book_is_refused(connect: Any) -> None:
    async with connect() as mcp:
        result = await mcp.call_tool("get_book", {"book": 123456})
    assert result.is_error
    assert "search_books" in result.content[0].text


async def test_a_merged_book_names_its_canonical_id(connect: Any, fake: FakeHardcover) -> None:
    fake.add_book(55, title="Dupe", canonical_id=7)
    async with connect() as mcp:
        assert (await get_book(mcp, book=55))["canonical_id"] == 7
