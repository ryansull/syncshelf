"""``update_book`` for status, rating and review, through the real ASGI app (SPEC §3.3.6,
§3.3.9), in dev mode as alice."""

import logging
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

ALICE = 42

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_user_book(ALICE, 7, status_id=3, rating=4.0, privacy_setting_id=2)
    fake.add_book(8, title="Hyperion")
    return fake


@pytest.fixture
def connect(tmp_path: Path, data_dir: Path, fake: FakeHardcover, clock: FakeClock) -> Any:
    @asynccontextmanager
    async def connect() -> AsyncIterator[Client]:
        config = make_config(tmp_path, profile_yaml={"alice": "{hardcover_privacy: public}"})
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


async def edit(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("update_book", arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def refused(mcp: Client, **arguments: Any) -> str:
    result = await mcp.call_tool("update_book", arguments)
    assert result.is_error
    return result.content[0].text


def row(fake: FakeHardcover, book_id: int) -> dict[str, Any]:
    return next(r for r in fake.user_books[ALICE] if r["book_id"] == book_id)


async def test_the_tool_is_marked_not_destructive(connect: Any) -> None:
    async with connect() as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
    assert tools["update_book"].annotations.destructive_hint is False


async def test_an_unshelved_book_is_added_with_the_profiles_privacy(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        result = await edit(mcp, book_id=8, status="want-to-read", rating=4.5)
    assert result == {
        "book_id": 8,
        "added_to_shelf": True,
        "changes": [
            {"field": "status", "before": None, "after": "want-to-read"},
            {"field": "rating", "before": None, "after": 4.5},
        ],
    }
    added = row(fake, 8)
    assert (added["status_id"], added["rating"], added["privacy_setting_id"]) == (1, 4.5, 1)


async def test_an_unshelved_book_needs_a_status(connect: Any, fake: FakeHardcover) -> None:
    async with connect() as mcp:
        message = await refused(mcp, book_id=8, rating=4.0)
    assert "give a status" in message
    assert fake.writes == []


async def test_a_change_reports_its_prior_value_and_never_touches_privacy(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        result = await edit(mcp, book_id=7, rating=2.5, status="ignored")  # Ignored is writable
    assert result["changes"] == [
        {"field": "status", "before": "read", "after": "ignored"},
        {"field": "rating", "before": 4.0, "after": 2.5},
    ]
    [(mutation, _, written)] = fake.writes
    assert mutation == "update_user_book" and "privacy_setting_id" not in written
    assert row(fake, 7)["privacy_setting_id"] == 2


async def test_a_value_already_there_is_not_written(connect: Any, fake: FakeHardcover) -> None:
    async with connect() as mcp:
        result = await edit(mcp, book_id=7, status="read", rating=4.0)
    assert result["changes"] == []
    assert fake.writes == []


async def test_rating_0_clears_and_an_off_grid_rating_is_refused(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        cleared = await edit(mcp, book_id=7, rating=0)
        message = await refused(mcp, book_id=7, rating=4.3)
    assert cleared["changes"] == [{"field": "rating", "before": 4.0, "after": None}]
    assert "half stars" in message
    assert len(fake.writes) == 1


async def test_a_review_is_set_and_an_empty_one_clears_it_and_its_flag(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        await edit(mcp, book_id=7, review="Loved it.", review_has_spoilers=True)
        cleared = await edit(mcp, book_id=7, review="")
    assert cleared["changes"] == [
        {"field": "review", "before": "Loved it.", "after": None},
        {"field": "review_has_spoilers", "before": True, "after": False},
    ]
    assert (row(fake, 7)["review_raw"], row(fake, 7)["review_has_spoilers"]) == (None, False)


async def test_the_mirror_sees_the_edit_at_once(connect: Any, fake: FakeHardcover) -> None:
    async with connect() as mcp:
        await mcp.call_tool("get_my_library", {})  # fills the copy
        await edit(mcp, book_id=7, rating=1.5)
        page = (await mcp.call_tool("get_my_library", {})).structured_content
    assert page["books"][0]["rating"] == 1.5
    assert sum(op == "Library" for op, _ in fake.requests) == 1  # no refresh needed


async def test_hardcover_refusing_is_reported_and_nothing_changes(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.fail_next("UpdateUserBook", "validation", message="Not allowed")
    async with connect() as mcp:
        message = await refused(mcp, book_id=7, rating=3.0)
    assert "Hardcover refused" in message and "Not allowed" in message
    assert row(fake, 7)["rating"] == 4.0


async def test_nothing_to_change_is_refused(connect: Any) -> None:
    async with connect() as mcp:
        assert "Nothing to change" in await refused(mcp, book_id=7)


async def test_each_change_is_logged_with_the_review_hashed(
    connect: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="syncshelf.sync.apply"):
        async with connect() as mcp:
            await edit(mcp, book_id=7, rating=3.0, review="A secret opinion.")
    lines = [r.__dict__ for r in caplog.records if r.__dict__.get("event") == "change_applied"]
    assert [(line["field"], line["hc_book_id"]) for line in lines] == [("rating", 7), ("review", 7)]
    assert "A secret opinion" not in str(lines)
    assert lines[1]["after"].startswith("17 chars ")
