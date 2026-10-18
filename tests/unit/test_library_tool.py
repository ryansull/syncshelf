"""``get_my_library`` through the real ASGI app (SPEC §3.3.7, §3.3.8), in dev mode, against
``FakeHardcover``."""

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

TTL = 600

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=42)
    return fake


@pytest.fixture
def connect(tmp_path: Path, data_dir: Path, fake: FakeHardcover, clock: FakeClock) -> Any:
    @asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        app = create_app(
            make_config(tmp_path),
            data_dir=data_dir,
            clock=clock.time,
            http_transport=fake.transport,
            limiter_clock=clock,
        )
        async with running(app) as http, mcp_client(http) as mcp:
            yield mcp

    return connect


async def library(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("get_my_library", arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def error(mcp: Client, **arguments: Any) -> str:
    result = await mcp.call_tool("get_my_library", arguments)
    assert result.is_error
    return result.content[0].text  # pyright: ignore[reportAttributeAccessIssue]


async def test_the_tool_is_listed_as_read_only(connect: Any) -> None:
    async with connect() as mcp:
        tools = {tool.name: tool for tool in (await mcp.list_tools()).tools}
    assert tools["get_my_library"].annotations.read_only_hint is True


async def test_a_row_is_compact_and_complete(
    connect: Any, fake: FakeHardcover, clock: FakeClock
) -> None:
    fake.add_user_book(
        42,
        7,
        status_id=3,
        rating=4.5,
        review_raw="never shown",
        user_book_reads=[
            {"id": 1, "finished_at": "2025-03-01", "edition_id": 70, "reading_format_id": 2},
            {"id": 2, "finished_at": "2026-01-10", "edition_id": 71, "reading_format_id": 4},
            {"id": 3},
        ],
        book={
            "title": "Dune",
            "cached_contributors": [
                {"author": {"id": 2, "name": "Scott Brick"}, "contribution": "Narrator"},
                {"author": {"id": 1, "name": "Frank Herbert"}, "contribution": None},
            ],
            "cached_featured_series": {"series": {"id": 5, "name": "Dune"}, "position": 1.0},
            "genres": [{"tag": t} for t in ("SF", "Classics", "Space", "Politics")],
        },
    )
    async with connect() as mcp:
        page = await library(mcp)
    assert page["books"] == [
        {
            "book_id": 7,
            "title": "Dune",
            "author": "Frank Herbert",
            "status": "read",
            "rating": 4.5,
            "formats": ["audio", "ebook", "unknown"],
            "last_finished": "2026-01-10",
            "read_count": 3,
            "series": {"name": "Dune", "position": 1.0},
            "genres": ["SF", "Classics", "Space"],
        }
    ]
    assert page["total"] == 1
    assert page["next_cursor"] is None
    assert "counts_by_status" not in page
    assert page["as_of"] == clock.now().isoformat()
    assert page["stale"] is False
    assert "stale_reason" not in page


async def test_a_book_with_no_reads_says_so(connect: Any, fake: FakeHardcover) -> None:
    fake.add_user_book(42, 7, status_id=3)
    async with connect() as mcp:
        [row] = (await library(mcp))["books"]
    assert (row["formats"], row["read_count"], row["last_finished"]) == ([], 0, None)
    assert row["series"] is None


async def test_a_read_in_both_formats_counts_as_audio_and_print(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.add_user_book(42, 7, user_book_reads=[{"id": 1, "edition_id": 70, "reading_format_id": 3}])
    async with connect() as mcp:
        [row] = (await library(mcp))["books"]
    assert row["formats"] == ["audio", "print"]


async def test_newest_additions_come_first_and_status_filters(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.add_user_book(42, 1, status_id=1, date_added="2026-01-01")
    fake.add_user_book(42, 2, status_id=2, date_added="2026-03-01")
    fake.add_user_book(42, 3, status_id=1, date_added="2026-02-01")
    async with connect() as mcp:
        everything = await library(mcp)
        wanted = await library(mcp, status="want-to-read")
    assert [b["book_id"] for b in everything["books"]] == [2, 3, 1]
    assert [b["book_id"] for b in wanted["books"]] == [3, 1]
    assert wanted["total"] == 2


async def test_paging_carries_totals_and_counts_for_the_whole_set(
    connect: Any, fake: FakeHardcover
) -> None:
    for book_id in range(1, 31):
        fake.add_user_book(
            42, book_id, status_id=3 if book_id <= 20 else 1, date_added=f"2026-01-{book_id:02d}"
        )
    async with connect() as mcp:
        first = await library(mcp)
        second = await library(mcp, cursor=first["next_cursor"])
    assert (len(first["books"]), first["total"]) == (25, 30)
    assert first["counts_by_status"] == {"want-to-read": 10, "read": 20}
    assert [b["book_id"] for b in second["books"]] == [5, 4, 3, 2, 1]
    assert second["next_cursor"] is None
    assert sum(fake_op == "Library" for fake_op, _ in fake.requests) == 1  # one refresh


async def test_a_cursor_that_wasnt_given_out_is_refused(connect: Any) -> None:
    async with connect() as mcp:
        message = await error(mcp, cursor="not-a-cursor")
    assert "next_cursor" in message


@pytest.mark.parametrize("limit", [0, 101])
async def test_limit_is_1_to_100(connect: Any, limit: int) -> None:
    async with connect() as mcp:
        await error(mcp, limit=limit)


async def test_a_failed_refresh_answers_from_the_old_copy_flagged_stale(
    connect: Any, fake: FakeHardcover, clock: FakeClock
) -> None:
    fake.add_user_book(42, 7)
    async with connect() as mcp:
        fresh = await library(mcp)
        clock.advance(TTL)
        fake.fail_next("Library", "unavailable")
        stale = await library(mcp)
    assert stale["books"] == fresh["books"]
    assert stale["as_of"] == fresh["as_of"]
    assert stale["stale"] is True
    assert stale["stale_reason"] == "Hardcover returned HTTP 503"


async def test_with_no_copy_and_hardcover_down_it_is_refused(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.fail_next("Me", "unavailable")
    async with connect() as mcp:
        message = await error(mcp)
    assert "no copy of this Hardcover library yet" in message
