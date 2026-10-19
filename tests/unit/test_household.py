"""Household reads (SPEC §2.8, §3.3.4): ``profile="<name>" | "household"`` on the mirror-backed
tools, through the real ASGI app in dev mode, acting as alice."""

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

ALICE, BOB, CAROL = 42, 43, 44
TTL = 600

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_user(hardcover_token("bob"), user_id=BOB)
    fake.add_user(hardcover_token("carol"), user_id=CAROL)
    fake.add_user_book(
        ALICE, 7, status_id=3, rating=4.0, date_added="2026-01-02", review_markdown="alice's review"
    )
    fake.add_user_book(
        BOB,
        7,
        status_id=3,
        rating=2.5,
        date_added="2026-01-01",
        review_markdown="bob's private review",
    )
    fake.add_user_book(BOB, 8, status_id=1, date_added="2026-01-03")
    fake.add_user_book(CAROL, 9, status_id=1)
    return fake  # fmt: skip


@pytest.fixture
def connect(tmp_path: Path, data_dir: Path, fake: FakeHardcover, clock: FakeClock) -> Any:
    @asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        config = make_config(
            tmp_path,
            profiles=["alice", "bob", "carol"],
            profile_yaml={"bob": "{household_visible: true}"},  # carol hasn't opted in
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


async def call(mcp: Client, **arguments: Any) -> Any:
    return await mcp.call_tool("get_my_library", arguments)


async def page(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await call(mcp, **arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def test_another_profile_is_read_with_its_own_token(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        bobs = await page(mcp, profile="bob")
    assert [(b["book_id"], b["rating"]) for b in bobs["books"]] == [(8, None), (7, 2.5)]
    assert {b["profile"] for b in bobs["books"]} == {"bob"}  # not the caller's own shelf
    library_callers = {
        c for (op, _), c in zip(fake.requests, fake.callers, strict=True) if op == "Library"
    }
    assert library_callers == {BOB}


async def test_a_profile_that_hasnt_opted_in_is_refused_before_any_refresh(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        result = await call(mcp, profile="carol")
    assert result.is_error
    assert "hasn't opted in" in result.content[0].text
    assert CAROL not in fake.callers


async def test_an_unknown_profile_is_refused(connect: Any) -> None:
    async with connect() as mcp:
        result = await call(mcp, profile="dave")
    assert result.is_error
    assert "no profile named" in result.content[0].text


async def test_household_rows_say_whose_shelf_they_are_from(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        both = await page(mcp, profile="household")
    assert [(b["profile"], b["book_id"], b["rating"]) for b in both["books"]] == [
        ("bob", 8, None),
        ("alice", 7, 4.0),
        ("bob", 7, 2.5),
    ]
    assert both["total"] == 3
    assert CAROL not in fake.callers  # carol hasn't opted in, so household leaves her out


async def test_reviews_are_never_in_a_row(connect: Any) -> None:
    async with connect() as mcp:
        both = await page(mcp, profile="household")
    assert "review" not in str(both)


async def test_household_filters_and_counts_cover_every_shelf(connect: Any) -> None:
    async with connect() as mcp:
        read = await page(mcp, profile="household", status="read", limit=1)
    assert read["total"] == 2
    assert read["counts_by_status"] == {"read": 2}


async def test_a_stale_copy_for_one_profile_marks_the_answer_stale(
    connect: Any, fake: FakeHardcover, clock: FakeClock
) -> None:
    async with connect() as mcp:
        await page(mcp, profile="household")
        clock.advance(TTL)
        fake.fail_next("Library", "unavailable", user_id=ALICE)
        stale = await page(mcp, profile="household")
    assert stale["stale"] is True
    assert stale["stale_reason"] == "alice: Hardcover returned HTTP 503"


async def test_me_and_the_callers_own_name_share_a_cursor(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.add_user_book(ALICE, 10, date_added="2026-01-05")
    async with connect() as mcp:
        first = await page(mcp, profile="me", limit=1)
        second = await page(mcp, profile="alice", limit=1, cursor=first["next_cursor"])
    assert [b["book_id"] for b in first["books"] + second["books"]] == [10, 7]


async def test_the_callers_own_rows_carry_no_profile(connect: Any) -> None:
    async with connect() as mcp:
        mine = await page(mcp)
    assert all("profile" not in b for b in mine["books"])


async def test_a_cursor_is_bound_to_its_profile(connect: Any) -> None:
    async with connect() as mcp:
        first = await page(mcp, profile="household", limit=1)
        result = await call(mcp, profile="me", cursor=first["next_cursor"])
    assert result.is_error


async def test_a_profile_with_no_copy_yet_leaves_the_rest_answered_and_flagged(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.fail_next("Me", "unavailable", user_id=BOB)  # bob's first fetch fails: no copy yet
    async with connect() as mcp:
        both = await page(mcp, profile="household")
    assert [(b["profile"], b["book_id"]) for b in both["books"]] == [("alice", 7)]
    assert both["stale"] is True
    assert both["stale_reason"].startswith("bob: SyncShelf has no copy of this Hardcover library")


async def test_with_no_copy_for_anyone_asked_about_it_is_refused(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.fail_next("Me", "unavailable", user_id=BOB)
    async with connect() as mcp:
        result = await call(mcp, profile="bob")
    assert result.is_error
    assert "no copy" in result.content[0].text
