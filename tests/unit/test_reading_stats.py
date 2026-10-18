"""``reading_stats`` through the real ASGI app (SPEC §3.3.7, §3.3.8), in dev mode as alice.

The fake clock starts at 2100-01-01T00:00Z, so "this year" is 2100 and last year 2099.
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
HOUR = 3600

pytestmark = pytest.mark.anyio


def read(id: int, finished: str | None, fmt: int | None = None) -> dict[str, Any]:
    return {"id": id, "finished_at": finished, "edition_id": 1 if fmt else None,
            "reading_format_id": fmt}  # fmt: skip


def book(title: str, author: str | None = "An Author", genres: tuple[str, ...] = (), **more: Any):
    people = [{"author": {"id": 1, "name": author}, "contribution": None}] if author else []
    return {"title": title, "cached_contributors": people,
            "genres": [{"tag": g} for g in genres], **more}  # fmt: skip


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_user(hardcover_token("bob"), user_id=BOB)
    return fake


@pytest.fixture
def shelf(fake: FakeHardcover) -> FakeHardcover:
    """alice's 2099: a print book (300 pages) read twice, an audiobook (10 h), an ebook, a read
    with no format, a Both read, and things no period can count."""
    fake.add_user_book(
        ALICE,
        1,
        status_id=3,
        rating=5.0,
        user_book_reads=[read(1, "2099-02-01", 1), read(2, "2099-11-20", 1)],
        book=book("Print", "Ann Leckie", ("SF", "Space"), pages=300),
    )
    fake.add_user_book(
        ALICE,
        2,
        status_id=3,
        rating=4.0,
        user_book_reads=[read(3, "2099-03-05", 2)],
        book=book(
            "Audio",
            "Ann Leckie",
            ("SF",),
            default_audio_edition={"id": 9, "audio_seconds": 10 * HOUR},
        ),
    )
    fake.add_user_book(
        ALICE,
        3,
        status_id=3,
        user_book_reads=[read(4, "2099-03-09", 4)],
        book=book("Ebook", "N. K. Jemisin", ("Fantasy",), pages=200),
    )
    fake.add_user_book(
        ALICE,
        4,
        status_id=3,
        user_book_reads=[read(5, "2099-12-31")],
        book=book("Unknown", None, (), pages=999),
    )
    fake.add_user_book(
        ALICE,
        5,
        status_id=3,
        user_book_reads=[read(6, "2098-06-01", 3)],
        book=book("Both", pages=100, default_audio_edition={"id": 8, "audio_seconds": HOUR}),
    )
    fake.add_user_book(ALICE, 6, status_id=3, user_book_reads=[read(7, None, 1)])  # undated
    fake.add_user_book(ALICE, 7, status_id=3)  # shelved as read, no read logged
    fake.add_user_book(ALICE, 8, status_id=2, user_book_reads=[read(8, None)])  # still reading
    return fake  # fmt: skip


@pytest.fixture
def connect(tmp_path: Path, data_dir: Path, fake: FakeHardcover, clock: FakeClock) -> Any:
    @asynccontextmanager
    async def connect(alice_zone: str = "Etc/UTC") -> AsyncIterator[Client]:
        config = make_config(
            tmp_path,
            profiles=["alice", "bob"],
            profile_yaml={
                "alice": f"{{timezone: {alice_zone}}}",
                "bob": "{household_visible: true}",
            },
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


async def stats(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("reading_stats", arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def refused(mcp: Client, **arguments: Any) -> str:
    result = await mcp.call_tool("reading_stats", arguments)
    assert result.is_error
    return result.content[0].text


async def test_a_years_totals(connect: Any, shelf: FakeHardcover) -> None:
    async with connect() as mcp:
        year = await stats(mcp, period="2099")
    assert year["period"] == {"from": "2099-01-01", "to": "2099-12-31"}
    assert year["totals"] == {
        "books": 4,
        "reads": 5,
        "pages": 300 * 2 + 200,  # print twice and the ebook; the unknown-format read adds none
        "hours": 10.0,
        "unknown_format_reads": 1,
    }
    assert year["not_counted"] == {"undated_reads": 1, "read_without_reads": 1}
    assert "groups" not in year


async def test_a_both_read_counts_its_pages_and_its_hours(
    connect: Any, shelf: FakeHardcover
) -> None:
    async with connect() as mcp:
        totals = (await stats(mcp, period="2098"))["totals"]
    assert (totals["pages"], totals["hours"]) == (100, 1.0)


@pytest.mark.parametrize(
    ("period", "span"),
    [
        ("2099-03", ("2099-03-01", "2099-03-31")),
        ("2096-02", ("2096-02-01", "2096-02-29")),
        ("last_30_days", ("2099-12-03", "2100-01-01")),
        ("last_12_months", ("2099-01-02", "2100-01-01")),
        ("all", (None, None)),
        ("2099-03-01..2099-03-06", ("2099-03-01", "2099-03-06")),
    ],
)
async def test_period_forms(
    connect: Any, shelf: FakeHardcover, period: str, span: tuple[str | None, str | None]
) -> None:
    async with connect() as mcp:
        assert (await stats(mcp, period=period))["period"] == dict(
            zip(("from", "to"), span, strict=True)
        )


async def test_relative_periods_are_read_in_the_profiles_time_zone(
    connect: Any, shelf: FakeHardcover
) -> None:
    # At 2100-01-01T00:00Z it is still 2099-12-31 twelve hours west.
    async with connect("Etc/GMT+12") as mcp:
        recent = await stats(mcp, period="last_30_days")
    assert recent["period"] == {"from": "2099-12-02", "to": "2099-12-31"}


@pytest.mark.parametrize("period", ["2099-13", "yesterday", "2099-03-07..2099-03-01", ""])
async def test_a_bad_period_is_refused(connect: Any, period: str) -> None:
    async with connect() as mcp:
        assert "period" in await refused(mcp, period=period)


async def test_group_by_format_always_has_unknown(connect: Any, shelf: FakeHardcover) -> None:
    async with connect() as mcp:
        groups = (await stats(mcp, period="2099", group_by="format"))["groups"]
    assert [(g["key"], g["books"], g["reads"]) for g in groups] == [
        ("print", 1, 2),
        ("audio", 1, 1),
        ("ebook", 1, 1),
        ("unknown", 1, 1),
    ]


async def test_group_by_month_runs_in_order(connect: Any, shelf: FakeHardcover) -> None:
    async with connect() as mcp:
        groups = (await stats(mcp, period="2099", group_by="month"))["groups"]
    assert [g["key"] for g in groups] == ["2099-02", "2099-03", "2099-11", "2099-12", "unknown"]
    assert groups[-1]["reads"] == 0


@pytest.mark.parametrize(
    ("group_by", "expected"),
    [
        ("author", [("Ann Leckie", 2), ("N. K. Jemisin", 1), ("unknown", 1)]),
        ("genre", [("SF", 2), ("Space", 1), ("Fantasy", 1), ("unknown", 1)]),  # ties: more reads
        ("rating", [("5", 1), ("4", 1), ("unknown", 2)]),
    ],
)
async def test_group_by(
    connect: Any, shelf: FakeHardcover, group_by: str, expected: list[tuple[str, int]]
) -> None:
    async with connect() as mcp:
        groups = (await stats(mcp, period="2099", group_by=group_by))["groups"]
    assert [(g["key"], g["books"]) for g in groups] == expected


async def test_past_15_groups_the_rest_are_other(connect: Any, fake: FakeHardcover) -> None:
    for n in range(1, 18):
        fake.add_user_book(
            ALICE, n, status_id=3, user_book_reads=[read(n, "2099-05-01", 1)],
            book=book(f"B{n}", f"Author {n:02d}"),
        )  # fmt: skip
    async with connect() as mcp:
        groups = (await stats(mcp, period="2099", group_by="author"))["groups"]
    assert len(groups) == 17
    assert [g["key"] for g in groups[-2:]] == ["other", "unknown"]
    assert groups[-2]["books"] == 2


async def test_goal_progress_comes_live(connect: Any, shelf: FakeHardcover) -> None:
    shelf.goals[ALICE] = [
        {"metric": "book", "goal": 30, "progress": 4.0, "start_date": "2099-01-01",
         "end_date": "2099-12-31", "state": "active"}
    ]  # fmt: skip
    async with connect() as mcp:
        year = await stats(mcp, period="2099")
    assert year["goals"] == shelf.goals[ALICE]


async def test_goals_are_left_out_when_hardcover_cant_answer(
    connect: Any, shelf: FakeHardcover
) -> None:
    shelf.fail_next("Goals", "unavailable")
    async with connect() as mcp:
        year = await stats(mcp, period="2099")
    assert "goals" not in year
    assert year["totals"]["books"] == 4


async def test_household_counts_everyone_and_leaves_goals_out(
    connect: Any, shelf: FakeHardcover
) -> None:
    shelf.add_user_book(BOB, 1, status_id=3, user_book_reads=[read(20, "2099-07-01", 1)],
                        book=book("Print", pages=300))  # fmt: skip
    async with connect() as mcp:
        both = await stats(mcp, period="2099", profile="household")
    assert (both["totals"]["books"], both["totals"]["reads"]) == (5, 6)
    assert "goals" not in both
    assert all(op != "Goals" for op, _ in shelf.requests)
