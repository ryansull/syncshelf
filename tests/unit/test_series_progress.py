"""``series_progress`` through the real ASGI app (SPEC §3.3.7), in dev mode as alice.

The series rows mirror live data (#128): translations at the same position, a novella at a
fractional one, a box set, and a row with no position.
"""

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
DUNE, DONE, FOUNDATION = 50, 60, 70

pytestmark = pytest.mark.anyio


def featured(series_id: int, name: str, position: float) -> dict[str, Any]:
    return {"cached_featured_series": {"series": {"id": series_id, "name": name},
                                       "position": position}}  # fmt: skip


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_user(hardcover_token("bob"), user_id=BOB)
    fake.add_series(
        DUNE,
        "Dune",
        [
            (1, 1, "Dune"),
            (1, 101, "Duna"),
            (1, 198, "Dune Box Set"),
            (1.5, 150, "A Novella"),
            (2, 2, "Dune Messiah"),
            (3, 3, "Children of Dune"),
            (None, 199, "Dune Omnibus"),
        ],
        users_count={1: 9000, 101: 5},
        compilation={198: True},
    )
    fake.add_series(DONE, "Done", [(1, 11, "Done One"), (2, 12, "Done Two")])
    fake.add_series(FOUNDATION, "Foundation", [(1, 21, "Foundation")])
    # alice read Dune in translation and is reading Messiah; she finished "Done".
    fake.add_user_book(ALICE, 101, status_id=3, book=featured(DUNE, "Dune", 1))
    fake.add_user_book(ALICE, 2, status_id=2, book=featured(DUNE, "Dune", 2))
    fake.add_user_book(ALICE, 11, status_id=3, book=featured(DONE, "Done", 1))
    fake.add_user_book(ALICE, 12, status_id=3, book=featured(DONE, "Done", 2))
    fake.add_user_book(BOB, 1, status_id=3, book=featured(DUNE, "Dune", 1))
    fake.add_user_book(BOB, 2, status_id=3, book=featured(DUNE, "Dune", 2))
    fake.add_user_book(BOB, 3, status_id=1, book=featured(DUNE, "Dune", 3))
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


async def progress(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("series_progress", arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def test_started_unfinished_series_with_the_next_book(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        page = await progress(mcp)
    assert page["series"] == [
        {
            "series_id": DUNE,
            "name": "Dune",
            "books": 3,  # positions 1, 2 and 3: not the novella, the box set or the omnibus
            "progress": [
                {
                    "profile": "alice",
                    "read": 1,  # the translation counts for position 1
                    "next": {
                        "position": 2.0,
                        "book_id": 2,
                        "title": "Dune Messiah",
                        "status": "currently-reading",
                    },
                }
            ],
        }
    ]  # "Done" is finished, so it isn't listed
    assert [op for op, _ in fake.requests].count("Series") == 1  # one batched request


async def test_a_named_series_gives_the_full_order(connect: Any) -> None:
    async with connect() as mcp:
        [dune] = (await progress(mcp, series="dune"))["series"]
    assert [(e["position"], e["book_id"], e["status"]) for e in dune["order"]] == [
        (1.0, 1, "read"),  # shown as the most-read edition; read via the translation
        (1.5, 150, None),  # the novella is listed but doesn't block progress
        (2.0, 2, "currently-reading"),
        (3.0, 3, None),
    ]


async def test_a_series_by_id(connect: Any) -> None:
    async with connect() as mcp:
        [done] = (await progress(mcp, series=str(DONE)))["series"]
    assert done["progress"] == [{"profile": "alice", "read": 2, "next": None}]


async def test_a_series_not_started_is_found_on_hardcover_by_exact_name(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        [found] = (await progress(mcp, series="Foundation"))["series"]
    assert (found["series_id"], found["order"][0]["status"]) == (FOUNDATION, None)
    assert "SeriesByName" in [op for op, _ in fake.requests]


async def test_an_unknown_series_is_refused(connect: Any) -> None:
    async with connect() as mcp:
        result = await mcp.call_tool("series_progress", {"series": "Nothing Like It"})
    assert result.is_error
    assert "search_books" in result.content[0].text


async def test_household_shows_each_profiles_progress(connect: Any) -> None:
    async with connect() as mcp:
        [dune] = (await progress(mcp, profile="household"))["series"]
        [ordered] = (await progress(mcp, series="Dune", profile="household"))["series"]
    assert [(p["profile"], p["read"], p["next"]["book_id"]) for p in dune["progress"]] == [
        ("alice", 1, 2),
        ("bob", 2, 3),
    ]
    assert ordered["order"][2]["statuses"] == {"alice": "currently-reading", "bob": "read"}


async def test_an_exact_name_wins_over_a_series_that_contains_it(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.add_series(80, "Legends of Dune", [(1, 31, "The Butlerian Jihad")])
    fake.add_user_book(ALICE, 31, status_id=3, book=featured(80, "Legends of Dune", 1))
    async with connect() as mcp:
        found = (await progress(mcp, series="DUNE"))["series"]
    assert [s["series_id"] for s in found] == [DUNE]


async def test_a_part_of_a_name_finds_the_users_own_series(connect: Any) -> None:
    async with connect() as mcp:
        found = (await progress(mcp, series="don"))["series"]
    assert [s["name"] for s in found] == ["Done"]


async def test_a_numeric_name_is_tried_as_a_name_when_no_id_matches(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.add_series(1999, "1632", [(1, 41, "1632")])
    async with connect() as mcp:
        [found] = (await progress(mcp, series="1632"))["series"]
    assert found["series_id"] == 1999


async def test_a_blank_series_means_every_started_series(connect: Any) -> None:
    async with connect() as mcp:
        blank = await progress(mcp, series="  ")
        default = await progress(mcp)
    assert blank["series"] == default["series"]


async def test_many_started_series_are_fetched_in_chunks(connect: Any, fake: FakeHardcover) -> None:
    for n in range(25):
        sid = 1000 + n
        fake.add_series(sid, f"S{n:02d}", [(1, 5000 + n, "One"), (2, 6000 + n, "Two")])
        fake.add_user_book(ALICE, 5000 + n, status_id=3, book=featured(sid, f"S{n:02d}", 1))
    async with connect() as mcp:
        page = await progress(mcp)
    assert [s["name"] for s in page["series"]][:3] == ["Dune", "S00", "S01"]  # by name
    assert len(page["series"]) == 26
    assert [op for op, _ in fake.requests].count("Series") == 2
