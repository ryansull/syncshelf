"""``check_books`` through the real ASGI app (SPEC §3.3.7), in dev mode as alice: each
candidate resolves to one book or to not_found, never to a nearest guess."""

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
        "The Way of Kings",
        author_names=["Brandon Sanderson"],
        has_audiobook=True,
        has_ebook=True,
        pages=1007,
        rating=4.6,
    )
    fake.add_search_doc(2, "The Name of the Wind", author_names=["Patrick Rothfuss"])
    fake.add_search_doc(3, "A Wizard of Earthsea", author_names=["Ursula K. Le Guin"])
    fake.add_search_doc(4, "Dune", author_names=["Frank Herbert"])
    fake.add_search_doc(5, "Dune", author_names=["Frank Herbert"])  # an indistinguishable twin
    fake.add_book(
        9,
        title="Hyperion",
        release_year=1989,
        cached_contributors=[
            {"author": {"id": 1, "name": "Dan Simmons"}, "contribution": "Author"}
        ],
        default_audio_edition={"id": 91},
        default_ebook_edition=None,
    )
    fake.add_user_book(ALICE, 1, status_id=1)
    fake.add_user_book(BOB, 1, status_id=3)
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


async def check(mcp: Client, *candidates: dict[str, Any], **more: Any) -> list[dict[str, Any]]:
    result = await mcp.call_tool("check_books", {"candidates": list(candidates), **more})
    assert not result.is_error, result.content
    return result.structured_content["books"]


async def test_a_close_title_and_author_is_exact(connect: Any) -> None:
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "The Way of Kings", "author": "Brandon Sanderson"})
    assert row["match"] == "exact"
    assert row["input"] == {"title": "The Way of Kings", "author": "Brandon Sanderson"}
    book = row["book"]
    assert (book["book_id"], book["has_audiobook"], book["pages"], book["status"]) == (
        1, True, 1007, "want-to-read",
    )  # fmt: skip


async def test_a_near_title_is_likely(connect: Any) -> None:
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "Name of Wind", "author": "Patrick Rothfuss"})
    assert (row["match"], row["book"]["book_id"]) == ("likely", 2)


async def test_without_an_author_a_match_is_likely_at_best(connect: Any) -> None:
    # Decided in #142: the author score is set aside, and nothing is exact without one.
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "The Way of Kings"})
    assert (row["match"], row["book"]["book_id"]) == ("likely", 1)


async def test_a_book_beats_its_graphic_novel(connect: Any, fake: FakeHardcover) -> None:
    # Decided in #138: both score 100 before the ':', but only the book's full title does.
    fake.add_search_doc(60, "A Wizard of Earthsea: A Graphic Novel",
                        author_names=["Ursula K. Le Guin", "Fred Fordham"])  # fmt: skip
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "A Wizard of Earthsea", "author": "Ursula K. Le Guin"})
    assert (row["match"], row["book"]["book_id"]) == ("exact", 3)


async def test_only_the_first_of_several_authors_is_used(connect: Any) -> None:
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "The Way of Kings",
                                  "author": "Brandon Sanderson & A. Narrator"})  # fmt: skip
    assert row["match"] == "exact"


async def test_an_id_whose_title_doesnt_fit_is_refused(connect: Any) -> None:
    async with connect() as mcp:
        [row] = await check(mcp, {"book_id": 9, "title": "Dune"})
    assert row["match"] == "not_found" and "Hyperion" in row["error"]


async def test_a_book_reads_the_same_by_title_or_id(connect: Any) -> None:
    async with connect() as mcp:
        by_title, by_id = await check(
            mcp, {"title": "The Way of Kings", "author": "Brandon Sanderson"}, {"book_id": 1}
        )
    assert by_title["book"] == by_id["book"]


async def test_an_author_hardcover_writes_differently_retries_without_the_filter(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "A Wizard of Earthsea", "author": "Ursula Le Guin"})
    assert (row["match"], row["book"]["book_id"]) == ("exact", 3)
    filters = [v["filter"] for op, v in fake.requests if op == "Search"]
    assert filters == ["author_names:=[`Ursula Le Guin`]", None]


async def test_two_equally_good_hits_are_not_found_not_a_guess(connect: Any) -> None:
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "Dune", "author": "Frank Herbert"})
    assert row == {"input": {"title": "Dune", "author": "Frank Herbert"}, "match": "not_found"}


async def test_nothing_close_is_not_found(connect: Any) -> None:
    async with connect() as mcp:
        [row] = await check(mcp, {"title": "Completely Unknown Book", "author": "Nobody"})
    assert row["match"] == "not_found" and "book" not in row


async def test_a_book_id_is_exact_when_hardcover_has_it(connect: Any) -> None:
    async with connect() as mcp:
        found, missing = await check(mcp, {"book_id": 9}, {"book_id": 999})
    assert (found["match"], found["book"]["title"], found["book"]["author"]) == (
        "exact", "Hyperion", "Dan Simmons",
    )  # fmt: skip
    assert (found["book"]["has_audiobook"], found["book"]["has_ebook"]) == (True, False)
    assert missing["match"] == "not_found"


async def test_rows_come_back_in_the_order_given(connect: Any) -> None:
    async with connect() as mcp:
        rows = await check(
            mcp,
            {"book_id": 9},
            {"title": "Unknown", "author": "Nobody"},
            {"title": "Name of Wind", "author": "Patrick Rothfuss"},
        )
    assert [r["match"] for r in rows] == ["exact", "not_found", "likely"]


async def test_household_shows_everyones_status(connect: Any) -> None:
    async with connect() as mcp:
        [row] = await check(
            mcp, {"title": "The Way of Kings", "author": "Brandon Sanderson"}, profile="household"
        )
    assert row["book"]["statuses"] == {"alice": "want-to-read", "bob": "read"}


async def test_a_candidate_hardcover_fails_on_says_so_and_the_rest_carry_on(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.fail_next("Search", "unavailable", after=1)  # whichever search goes second
    async with connect() as mcp:
        rows = await check(
            mcp,
            {"title": "The Way of Kings", "author": "Brandon Sanderson"},
            {"title": "Name of Wind", "author": "Patrick Rothfuss"},
        )
    failed = [r for r in rows if "error" in r]
    assert len(failed) == 1 and failed[0]["match"] == "not_found"
    assert failed[0]["error"] == "Hardcover returned HTTP 503"
    assert [r["match"] for r in rows if "error" not in r] in (["exact"], ["likely"])


@pytest.mark.parametrize("candidates", [[], [{"title": "x"}] * 21, [{"author": "Only An Author"}]])
async def test_bad_candidate_lists_are_refused(
    connect: Any, candidates: list[dict[str, Any]]
) -> None:
    async with connect() as mcp:
        result = await mcp.call_tool("check_books", {"candidates": candidates})
    assert result.is_error
