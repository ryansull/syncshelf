"""The write procedure against the real Hardcover (SPEC §3.3.10, Live): **only** on
``HARDCOVER_TOKEN_TEST``, a throwaway account that belongs to neither profile, and only with
``SYNCSHELF_LIVE_WRITE=1`` on top of the live gate. Each test starts from the book being off
that account's shelves and takes it off again (journal included), whatever happens.

They confirm what §3.3.6 left to verify here: what a status-3 insert with ``user_date`` alone
creates (rule 3, recorded as a fixture), and that the rule 4 reconciliation leaves no read
Hardcover invented."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from textwrap import dedent
from typing import Any

import anyio
import pytest
from mcp import Client
from record import FIXTURES, HARDCOVER_URL, HARDCOVER_VERSION
from test_hardcover_live import profile_tokens

from syncshelf.app import create_app
from syncshelf.clients.hardcover import (
    DELETE_JOURNALS,
    DELETE_USER_BOOK,
    INSERT_USER_BOOK,
    JOURNAL,
    USER_BOOK,
    HardcoverClient,
    check_mutation,
)
from syncshelf.config import load_config
from syncshelf.sync.limiter import HardcoverLimiter

from scrub import write_fixture
from serving import DEV_TOKEN, mcp_client, running

pytestmark = [pytest.mark.live, pytest.mark.anyio]

WRITE_ENV = "SYNCSHELF_LIVE_WRITE"
BOOK = 1464466
"""A catalog book with default audio and physical editions (the read fixtures' book)."""
STARTED, FINISHED, CORRECTED = "2020-03-01", "2020-03-09", "2020-03-07"
"""Long past, so any read dated today is one Hardcover invented."""
SETTLE = 2.0
"""Seconds to let a write show before checking it (#150)."""


@pytest.fixture
async def test_account(live_env: dict[str, str]) -> AsyncIterator[HardcoverClient]:
    """The test account's client, with :data:`BOOK` off its shelves before and after."""
    if os.environ.get(WRITE_ENV) != "1":
        pytest.skip(f"live writes need {WRITE_ENV}=1 as well")
    token = live_env.get("HARDCOVER_TOKEN_TEST")
    if not token:
        pytest.fail("live writes need HARDCOVER_TOKEN_TEST in the env file")
    client = HardcoverClient(HARDCOVER_URL, token, gate=HardcoverLimiter("test", reserve=0))
    try:
        me = await client.me_id()
        for var, other in profile_tokens(live_env).items():
            profile = HardcoverClient(HARDCOVER_URL, other, gate=None)
            try:
                if await profile.me_id() == me:
                    pytest.fail(f"HARDCOVER_TOKEN_TEST is {var}'s account: refusing to write")
            finally:
                await profile.aclose()
        if await _shelf(client):
            pytest.fail(f"book {BOOK} is already on the test account: take it off first")
        yield client
    finally:
        try:
            if row := await _shelf(client):
                check_mutation(
                    (await client.execute(DELETE_USER_BOOK, {"id": row["id"]})).data,
                    "delete_user_book",
                )
            await client.execute(DELETE_JOURNALS, {"book_id": BOOK})
        finally:
            await client.aclose()


async def _settled(client: HardcoverClient) -> dict[str, Any] | None:
    """The shelf entry once the last write shows: Hardcover's reads can lag its writes by
    up to a second or so (#150)."""
    await anyio.sleep(SETTLE)
    return await _shelf(client)


async def _shelf(client: HardcoverClient) -> dict[str, Any] | None:
    result = await client.execute(USER_BOOK, {"book_id": BOOK, "user_id": await client.me_id()})
    rows = result.data.get("user_books") or []
    return rows[0] if rows else None


def _reads(row: dict[str, Any] | None) -> list[tuple[str | None, str | None]]:
    reads = sorted((row or {}).get("user_book_reads") or [], key=lambda r: r["id"])
    return [((r["started_at"] or "")[:10] or None, (r["finished_at"] or "")[:10] or None)
            for r in reads]  # fmt: skip


def _invented(row: dict[str, Any] | None) -> bool:
    """Whether a read is dated today, in UTC or here: a date nobody stated. Reads only:
    Hardcover stamps today on ``first_started_reading_date`` too, and won't clear it (#148)."""
    today = {date.today().isoformat(), datetime.now(UTC).date().isoformat()}
    dated = []
    for read in (row or {}).get("user_book_reads") or []:
        dated += [read["started_at"] or "", read["finished_at"] or ""]
    return any(d[:10] in today for d in dated)


async def test_a_read_status_insert_with_only_a_finish_date(
    test_account: HardcoverClient,
) -> None:
    result = await test_account.execute(
        INSERT_USER_BOOK,
        {"object": {"book_id": BOOK, "status_id": 3, "user_date": FINISHED,
                    "privacy_setting_id": 3}},
    )  # fmt: skip
    check_mutation(result.data, "insert_user_book")
    row = await _settled(test_account)
    write_fixture(
        FIXTURES / "hardcover" / "insert_read.json",
        {"data": {"user_books": [row]}},
        recorded_at=datetime.now(UTC).date().isoformat(),
        server_version=HARDCOVER_VERSION,
    )
    # §3.3.6 rule 3: one read, finished on that date, with no start.
    assert _reads(row) == [(None, FINISHED)]


async def test_edits_leave_only_the_dates_given(
    test_account: HardcoverClient, live_env: dict[str, str], tmp_path: Path
) -> None:
    async with _app(live_env["HARDCOVER_TOKEN_TEST"], tmp_path) as mcp:

        async def call(tool: str, **arguments: Any) -> dict[str, Any]:
            result = await mcp.call_tool(tool, {"book_id": BOOK, **arguments})
            assert not result.is_error, (tool, arguments, result.content)
            return result.structured_content or {}

        async def shelf() -> dict[str, Any]:
            row = await _settled(test_account)
            assert row is not None
            assert not _invented(row), row["user_book_reads"]
            return row

        assert (await call("update_book", status="want-to-read"))["added_to_shelf"] is True
        assert (await shelf())["privacy_setting_id"] == 3
        # Rule 4: the "today" reads Hardcover makes for these are undone.
        await call("update_book", status="currently-reading")
        assert _reads(await shelf()) == []
        await call("update_book", status="read")
        assert _reads(await shelf()) == []

        await call("update_book", started_on=STARTED, format="audio")
        row = await shelf()
        assert (row["status_id"], _reads(row)) == (2, [(STARTED, None)])
        assert row["first_started_reading_date"] == STARTED
        await call("update_book", finished_on=FINISHED)
        row = await shelf()
        assert (row["status_id"], _reads(row)) == (3, [(STARTED, FINISHED)])

        read_id = (await shelf())["user_book_reads"][0]["id"]
        await call("correct_read", read_id=read_id, finished_on=CORRECTED)
        assert _reads(await shelf()) == [(STARTED, CORRECTED)]

        preview = await call("correct_read", read_id=read_id, delete=True)
        assert _reads(await shelf()) == [(STARTED, CORRECTED)]
        await call(
            "correct_read", read_id=read_id, delete=True, confirm_token=preview["confirm_token"]
        )
        assert _reads(await shelf()) == []

        preview = await call("remove_book")
        assert preview["has_journal"] is True
        done = await call("remove_book", confirm_token=preview["confirm_token"])
        assert done["removed"] is True
    assert await _settled(test_account) is None
    journal = await test_account.execute(
        JOURNAL, {"book_id": BOOK, "user_id": await test_account.me_id()}
    )
    assert journal.data["reading_journals"] == []


async def test_the_same_review_again_writes_nothing(
    test_account: HardcoverClient, live_env: dict[str, str], tmp_path: Path
) -> None:
    review = {"book_id": BOOK, "review": "A **fine** book.\n\nWould read again."}
    async with _app(live_env["HARDCOVER_TOKEN_TEST"], tmp_path) as mcp:
        first = await mcp.call_tool("update_book", {**review, "status": "read"})
        assert not first.is_error, first.content
        await _settled(test_account)
        again = await mcp.call_tool("update_book", review)
    assert not again.is_error, again.content
    assert (again.structured_content or {})["changes"] == []


def _app(token: str, tmp_path: Path) -> Any:
    """The app in dev mode with one profile on the test account's token."""
    config_file = tmp_path / "profiles.yaml"
    config_file.write_text(
        dedent("""\
            server:
              public_url: http://localhost:8000
              auth_mode: dev
              forwarded_allow_ips: 127.0.0.1
              dev: {token: "${DEV}", profile: alice}
            services:
              hardcover_url: https://api.hardcover.app/v1/graphql
            profiles:
              alice: {hardcover_token: "${HC}", hardcover_privacy: private}
            """),
        encoding="utf-8",
    )
    config = load_config(config_file, environ={"DEV": DEV_TOKEN, "HC": token})
    data = tmp_path / "data"
    data.mkdir()

    @asynccontextmanager
    async def connected() -> AsyncIterator[Client]:
        async with running(create_app(config, data_dir=data)) as http, mcp_client(http) as mcp:
            yield mcp

    return connected()
