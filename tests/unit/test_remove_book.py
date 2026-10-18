"""``remove_book`` and ``correct_read(delete=true)`` through the real ASGI app (SPEC §3.3.9):
a preview and a confirm token first, the deletion only on that token over an unchanged book,
and the journal rows Hardcover leaves behind deleted too (§3.3.1 trap 4)."""

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
AUDIO = 72
EDITIONS = {"default_audio_edition": {"id": AUDIO, "audio_seconds": 36000}}

pytestmark = pytest.mark.anyio


def read(id: int, started: str | None, finished: str | None) -> dict[str, Any]:
    return {"id": id, "started_at": started, "finished_at": finished, "edition_id": AUDIO,
            "reading_format_id": 2}  # fmt: skip


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_user_book(
        ALICE,
        7,
        status_id=3,
        rating=4.5,
        review_raw="Loved it.",
        has_review=True,
        first_started_reading_date="2099-01-01",
        user_book_reads=[read(1, "2099-01-01", "2099-01-09"), read(2, "2099-05-01", "2099-05-09")],
        book={"title": "Hyperion", **EDITIONS},
    )
    fake.add_journal(ALICE, 7)
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


async def call(mcp: Client, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def refused(mcp: Client, tool: str, **arguments: Any) -> str:
    result = await mcp.call_tool(tool, arguments)
    assert result.is_error
    return result.content[0].text


def shelved(fake: FakeHardcover) -> list[int]:
    return [r["book_id"] for r in fake.user_books[ALICE]]


# --- remove_book ----------------------------------------------------------------------------


async def test_a_preview_shows_what_would_go_and_removes_nothing(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        preview = await call(mcp, "remove_book", book_id=7)
    assert preview["removed"] is False
    assert preview["confirm_token"]
    assert {k: preview[k] for k in ("title", "status", "rating", "has_review", "has_journal")} == {
        "title": "Hyperion",
        "status": "read",
        "rating": 4.5,
        "has_review": True,
        "has_journal": True,
    }
    assert preview["reads"] == [
        {"read_id": 1, "started_on": "2099-01-01", "finished_on": "2099-01-09", "format": "audio"},
        {"read_id": 2, "started_on": "2099-05-01", "finished_on": "2099-05-09", "format": "audio"},
    ]
    assert preview["linked_source_rows"] == []
    assert fake.writes == []


async def test_the_token_removes_the_book_and_its_journal(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        await call(mcp, "get_my_library")  # fills the copy
        token = (await call(mcp, "remove_book", book_id=7))["confirm_token"]
        done = await call(mcp, "remove_book", book_id=7, confirm_token=token)
        library = await call(mcp, "get_my_library")
    assert done["removed"] is True and done["confirm_token"] is None
    assert done["rating"] == 4.5 and len(done["reads"]) == 2  # what it had, for undoing
    assert shelved(fake) == []
    assert fake.journals[ALICE] == []
    assert [w[0] for w in fake.writes] == ["delete_user_book", "delete_reading_journals_for_book"]
    assert library["books"] == []


async def test_a_book_with_no_journal_skips_deleting_one(connect: Any, fake: FakeHardcover) -> None:
    fake.journals[ALICE] = []
    async with connect() as mcp:
        token = (await call(mcp, "remove_book", book_id=7))["confirm_token"]
        done = await call(mcp, "remove_book", book_id=7, confirm_token=token)
    assert done["has_journal"] is False
    assert [w[0] for w in fake.writes] == ["delete_user_book"]


async def test_a_book_changed_since_the_preview_gets_a_fresh_one(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        token = (await call(mcp, "remove_book", book_id=7))["confirm_token"]
        await call(mcp, "update_book", book_id=7, rating=2.0)
        again = await call(mcp, "remove_book", book_id=7, confirm_token=token)
        stale = await call(mcp, "remove_book", book_id=7, confirm_token=token)
    assert again["removed"] is False
    assert again["rating"] == 2.0
    assert "changed since that preview" in again["note"]
    assert again["confirm_token"] not in (None, token)
    assert "expired, used" in stale["note"]  # spent, so it can't act later either
    assert shelved(fake) == [7]


async def test_a_token_expires_after_ten_minutes(
    connect: Any, fake: FakeHardcover, clock: FakeClock
) -> None:
    async with connect() as mcp:
        token = (await call(mcp, "remove_book", book_id=7))["confirm_token"]
        clock.advance(600)
        again = await call(mcp, "remove_book", book_id=7, confirm_token=token)
    assert again["removed"] is False and "expired" in again["note"]
    assert shelved(fake) == [7]


async def test_a_token_for_another_book_removes_nothing(connect: Any, fake: FakeHardcover) -> None:
    fake.add_user_book(ALICE, 8, status_id=1)
    async with connect() as mcp:
        token = (await call(mcp, "remove_book", book_id=8))["confirm_token"]
        wrong = await call(mcp, "remove_book", book_id=7, confirm_token=token)
        right = await call(mcp, "remove_book", book_id=8, confirm_token=token)
    assert wrong["removed"] is False
    assert right["removed"] is True
    assert shelved(fake) == [7]


async def test_a_journal_hardcover_fails_to_delete_is_reported(
    connect: Any, fake: FakeHardcover
) -> None:
    fake.fail_next("DeleteJournals", "unavailable")
    async with connect() as mcp:
        token = (await call(mcp, "remove_book", book_id=7))["confirm_token"]
        done = await call(mcp, "remove_book", book_id=7, confirm_token=token)
    assert done["removed"] is True
    assert "journal history, which stays" in done["note"]
    assert shelved(fake) == []


async def test_a_book_not_on_the_shelves_cant_be_removed(connect: Any) -> None:
    async with connect() as mcp:
        assert "isn't on the shelves" in await refused(mcp, "remove_book", book_id=99)


# --- correct_read(delete=true) --------------------------------------------------------------


async def test_a_read_is_deleted_only_with_its_token(connect: Any, fake: FakeHardcover) -> None:
    async with connect() as mcp:
        preview = await call(mcp, "correct_read", book_id=7, read_id=1, delete=True)
        assert fake.writes == []
        done = await call(
            mcp,
            "correct_read",
            book_id=7,
            read_id=1,
            delete=True,
            confirm_token=preview["confirm_token"],
        )
    assert preview["deleted"] is False
    assert preview["preview"] == {
        "read_id": 1,
        "started_on": "2099-01-01",
        "finished_on": "2099-01-09",
        "format": "audio",
    }
    assert done["deleted"] is True
    assert done["changes"] == [{"field": "read", "before": preview["preview"], "after": None}]
    (row,) = fake.user_books[ALICE]
    assert [r["id"] for r in row["user_book_reads"]] == [2]
    assert row["first_started_reading_date"] == "2099-05-01"


async def test_a_read_token_doesnt_delete_another_read(connect: Any, fake: FakeHardcover) -> None:
    async with connect() as mcp:
        token = (await call(mcp, "correct_read", book_id=7, read_id=1, delete=True))[
            "confirm_token"
        ]
        other = await call(
            mcp, "correct_read", book_id=7, read_id=2, delete=True, confirm_token=token
        )
    assert other["deleted"] is False
    assert len(fake.user_books[ALICE][0]["user_book_reads"]) == 2


@pytest.mark.parametrize(
    ("arguments", "says"),
    [
        ({"delete": True, "finished_on": "2099-01-10"}, "delete goes alone"),
        ({"confirm_token": "x", "finished_on": "2099-01-10"}, "only with delete=true"),
        ({"read_id": 9, "delete": True}, "isn't one of book 7's reads"),
    ],
)
async def test_bad_deletions_are_refused(
    connect: Any, fake: FakeHardcover, arguments: dict[str, Any], says: str
) -> None:
    async with connect() as mcp:
        assert says in await refused(
            mcp, "correct_read", **{"book_id": 7, "read_id": 1, **arguments}
        )
    assert fake.writes == []


async def test_both_deletions_are_marked_destructive(connect: Any) -> None:
    async with connect() as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
    for name in ("remove_book", "correct_read"):
        annotations = tools[name].annotations
        assert annotations is not None and annotations.destructive_hint is True
