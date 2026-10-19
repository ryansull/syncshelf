"""``update_book`` with dates, through the real ASGI app (SPEC §3.3.5, §3.3.6 rules 4–6 and
9, §3.3.9), in dev mode as alice, against ``FakeHardcover``'s status traps: becoming Currently
Reading or Read makes Hardcover invent reads dated :data:`TODAY`. None may survive."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from syncshelf.app import create_app

from clock import FakeClock
from fakes.hardcover import READ_FIELDS, FakeHardcover
from serving import hardcover_token, make_config, mcp_client, running

ALICE = 42
TODAY = "2100-01-01"
PHYSICAL, AUDIO, EBOOK = 71, 72, 74
EDITIONS = {
    "default_physical_edition": {"id": PHYSICAL, "pages": 300},
    "default_audio_edition": {"id": AUDIO, "audio_seconds": 36000},
    "default_ebook_edition": {"id": EBOOK},
}

pytestmark = pytest.mark.anyio


def read(
    id: int, started: str | None = None, finished: str | None = None, edition: int | None = None
) -> dict[str, Any]:
    fmt = {PHYSICAL: 1, AUDIO: 2, EBOOK: 4}.get(edition or 0)
    return {"id": id, "started_at": started, "finished_at": finished, "edition_id": edition,
            "reading_format_id": fmt}  # fmt: skip


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    fake.add_book(8, title="Hyperion", **EDITIONS)
    return fake


def shelve(fake: FakeHardcover, status: int, *reads: dict[str, Any]) -> None:
    fake.add_user_book(ALICE, 7, status_id=status, user_book_reads=list(reads), book=EDITIONS)


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


async def edit(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("update_book", arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def refused(mcp: Client, **arguments: Any) -> str:
    result = await mcp.call_tool("update_book", arguments)
    assert result.is_error
    return result.content[0].text


def state(fake: FakeHardcover, book_id: int = 7) -> tuple[int, list[tuple[Any, ...]]]:
    """(status, [(started, finished, edition)]) for the book, in read-id order."""
    row = next(r for r in fake.user_books[ALICE] if r["book_id"] == book_id)
    reads = sorted(row["user_book_reads"], key=lambda r: r["id"])
    return row["status_id"], [(r["started_at"], r["finished_at"], r["edition_id"]) for r in reads]


def no_today(fake: FakeHardcover) -> bool:
    """No read is dated today. Reads only: the first-start date Hardcover stamps can't be
    cleared (#148)."""
    return TODAY not in str([r["user_book_reads"] for r in fake.user_books[ALICE]])


# --- no date means no read ------------------------------------------------------------------


@pytest.mark.parametrize(("status", "status_id"), [("read", 3), ("currently-reading", 2)])
async def test_a_status_without_a_date_logs_no_read(
    connect: Any, fake: FakeHardcover, status: str, status_id: int
) -> None:
    shelve(fake, 1)
    async with connect() as mcp:
        result = await edit(mcp, book_id=7, status=status)
    assert state(fake) == (status_id, [])
    assert no_today(fake)
    assert [c["field"] for c in result["changes"]] == ["status"]


async def test_read_over_an_open_read_needs_its_finish_date(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 2, read(1, "2099-12-01"))
    async with connect() as mcp:
        message = await refused(mcp, book_id=7, status="read")
    assert "date_needed" in message and "2099-12-01" in message
    assert fake.writes == []


@pytest.mark.parametrize("status", [("paused", 4), ("dnf", 5)])
async def test_paused_and_dnf_leave_reads_alone(
    connect: Any, fake: FakeHardcover, status: tuple[str, int]
) -> None:
    shelve(fake, 2, read(1, "2099-12-01", None, AUDIO))
    async with connect() as mcp:
        await edit(mcp, book_id=7, status=status[0])
    assert state(fake) == (status[1], [("2099-12-01", None, AUDIO)])


# --- finishing ------------------------------------------------------------------------------


async def test_finishing_completes_the_open_read_and_keeps_its_edition(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 2, read(1, "2099-12-01", None, AUDIO))
    async with connect() as mcp:
        result = await edit(mcp, book_id=7, finished_on="2099-12-20")
    assert state(fake) == (3, [("2099-12-01", "2099-12-20", AUDIO)])
    assert no_today(fake)
    assert result["changes"][-1] == {
        "field": "read",
        "before": {"read_id": 1, "started_on": "2099-12-01", "finished_on": None,
                   "format": "audio"},
        "after": {"read_id": 1, "started_on": "2099-12-01", "finished_on": "2099-12-20",
                  "format": "audio"},
    }  # fmt: skip


async def test_finishing_with_no_read_logs_one_in_the_formats_edition(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20", started_on="2099-12-02",
                   format="ebook")  # fmt: skip
    assert state(fake) == (3, [("2099-12-02", "2099-12-20", EBOOK)])
    assert no_today(fake)


async def test_a_new_read_with_no_known_format_is_refused(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)
    async with connect() as mcp:
        message = await refused(mcp, book_id=7, finished_on="2099-12-20")
    assert "format_needed" in message
    assert fake.writes == []


@pytest.mark.parametrize(("days", "adopted"), [(14, True), (15, False)])
async def test_a_read_within_14_days_is_adopted_not_doubled(
    connect: Any, fake: FakeHardcover, days: int, adopted: bool
) -> None:
    logged = f"2099-12-{20 - days:02d}" if days < 20 else None
    shelve(fake, 3, read(1, None, logged, PHYSICAL))
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20", format="print")
    _, reads = state(fake)
    assert len(reads) == (1 if adopted else 2)


async def test_an_undated_read_is_never_adopted(connect: Any, fake: FakeHardcover) -> None:
    shelve(fake, 3, read(1, edition=PHYSICAL))
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20", format="print")
    assert state(fake)[1] == [(None, None, PHYSICAL), (None, "2099-12-20", PHYSICAL)]


async def test_reread_logs_a_new_read_even_near_one(connect: Any, fake: FakeHardcover) -> None:
    shelve(fake, 3, read(1, None, "2099-12-18", PHYSICAL))
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20", format="audio", reread=True)
    assert state(fake)[1] == [(None, "2099-12-18", PHYSICAL), (None, "2099-12-20", AUDIO)]


# --- starting -------------------------------------------------------------------------------


async def test_starting_opens_a_read(connect: Any, fake: FakeHardcover) -> None:
    shelve(fake, 1)
    async with connect() as mcp:
        await edit(mcp, book_id=7, started_on="2099-12-05", format="audio")
    assert state(fake) == (2, [("2099-12-05", None, AUDIO)])
    assert no_today(fake)


async def test_starting_with_a_read_already_open_is_refused(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 2, read(1, "2099-12-01"))
    async with connect() as mcp:
        assert "correct_read" in await refused(mcp, book_id=7, started_on="2099-12-05",
                                                format="print")  # fmt: skip
    assert fake.writes == []


# --- a book not yet shelved -----------------------------------------------------------------


async def test_a_new_book_finished_on_a_date_gets_exactly_that_read(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        result = await edit(mcp, book_id=8, finished_on="2099-11-30", started_on="2099-11-01",
                            format="audio")  # fmt: skip
    assert state(fake, 8) == (3, [("2099-11-01", "2099-11-30", AUDIO)])
    assert result["added_to_shelf"] is True
    assert result["changes"][-1]["after"]["format"] == "audio"
    assert no_today(fake)


async def test_a_new_book_shelved_read_without_a_date_has_no_read(
    connect: Any, fake: FakeHardcover
) -> None:
    async with connect() as mcp:
        await edit(mcp, book_id=8, status="read")
    assert state(fake, 8) == (3, [])


# --- writes are whole, starts stay first, reruns converge -----------------------------------


async def test_every_read_update_is_a_full_object(connect: Any, fake: FakeHardcover) -> None:
    shelve(fake, 2, read(1, "2099-12-01", None, AUDIO))
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20")
    updates = [w for w in fake.writes if w[0] == "update_user_book_read"]
    assert updates and all(set(obj) == set(READ_FIELDS) for _, _, obj in updates)


async def test_the_first_start_date_follows_the_earliest_read(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 3, read(1, "2099-06-01", "2099-06-30", PHYSICAL))
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20", started_on="2099-01-10",
                   format="print", reread=True)  # fmt: skip
    row = next(r for r in fake.user_books[ALICE] if r["book_id"] == 7)
    assert row["first_started_reading_date"] == "2099-01-10"


async def test_a_failed_fix_up_is_completed_by_running_it_again(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)
    fake.fail_next("DeleteRead", "unavailable")  # undoing the invented read fails
    async with connect() as mcp:
        first = await refused(mcp, book_id=7, finished_on="2099-12-20", format="print")
        assert "Partly done" in first and state(fake)[0] == 3  # the status landed
        await edit(mcp, book_id=7, finished_on="2099-12-20", format="print")
    assert state(fake) == (3, [(None, "2099-12-20", PHYSICAL)])
    assert no_today(fake)


# --- what can't be asked --------------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        {"status": "want-to-read", "finished_on": "2099-12-20"},
        {"status": "read", "started_on": "2099-12-01"},
        {"started_on": "2099-12-21", "finished_on": "2099-12-20"},
        {"format": "audio"},
        {"reread": True},
    ],
)
async def test_contradictions_are_refused(
    connect: Any, fake: FakeHardcover, arguments: dict[str, Any]
) -> None:
    shelve(fake, 1)
    async with connect() as mcp:
        await refused(mcp, book_id=7, **arguments)
    assert fake.writes == []


# --- review: replays, editions, dangling reads ----------------------------------------------


async def test_a_status_write_that_fails_leaves_nothing_to_replay(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)
    fake.fail_next("UpdateUserBook", "unavailable")
    async with connect() as mcp:
        await refused(mcp, book_id=7, status="read")
        row = fake.user_books[ALICE][0]
        row["user_book_reads"].append(  # the user logs a read in Hardcover meanwhile
            {"id": 900, "started_at": "2099-05-01", "finished_at": "2099-05-09", "paused_at": None,
             "edition_id": None, "progress_pages": None, "progress_seconds": None, "edition": None}
        )  # fmt: skip
        await edit(mcp, book_id=7, rating=3.0)
    assert state(fake)[1] == [("2099-05-01", "2099-05-09", None)]


async def test_a_different_call_after_a_partial_failure_leaves_new_reads_alone(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)
    fake.fail_next("DeleteRead", "unavailable")
    async with connect() as mcp:
        await refused(mcp, book_id=7, status="read")  # partly done, never retried
        await edit(mcp, book_id=7, finished_on="2099-05-09", format="print")
    _, reads = state(fake)
    assert (None, "2099-05-09", PHYSICAL) in reads  # the new read is there, nothing deleted


async def test_a_retried_start_is_judged_against_the_reads_before_it(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)
    fake.fail_next("DeleteRead", "unavailable")
    async with connect() as mcp:
        await refused(mcp, book_id=7, started_on="2099-12-05", format="audio")
        result = await edit(mcp, book_id=7, started_on="2099-12-05", format="audio")
    assert state(fake) == (2, [("2099-12-05", None, AUDIO)])
    assert no_today(fake)
    assert result["changes"][0] == {"field": "status", "before": "want-to-read",
                                    "after": "currently-reading"}  # fmt: skip


async def test_a_read_hardcover_opened_on_another_edition_isnt_taken_as_asked(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)  # the shelf entry's own edition is neither default
    async with connect() as mcp:
        await edit(mcp, book_id=7, started_on=TODAY, format="audio")
    assert state(fake) == (2, [(TODAY, None, AUDIO)])


async def test_a_first_start_date_hardcover_stamped_is_left_without_a_null_write(
    connect: Any, fake: FakeHardcover
) -> None:
    # Hardcover can't clear it (#148), so no write is wasted trying.
    shelve(fake, 1)
    async with connect() as mcp:
        await edit(mcp, book_id=7, status="currently-reading")
    assert fake.user_books[ALICE][0]["first_started_reading_date"] == TODAY
    assert not any("first_started_reading_date" in w[2] for w in fake.writes)


async def test_a_stated_start_replaces_the_stamped_first_start_date(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 1)
    async with connect() as mcp:
        await edit(mcp, book_id=7, started_on="2099-12-05", format="audio")
    assert fake.user_books[ALICE][0]["first_started_reading_date"] == "2099-12-05"


@pytest.mark.parametrize(
    "arguments",
    [
        {"finished_on": "2099-11-20", "format": "print"},  # before the open read started
        {"finished_on": "2099-12-20", "format": "print", "reread": True},
    ],
)
async def test_a_finish_that_would_leave_a_read_open_is_refused(
    connect: Any, fake: FakeHardcover, arguments: dict[str, Any]
) -> None:
    shelve(fake, 2, read(1, "2099-12-01", None, AUDIO))
    async with connect() as mcp:
        assert "still open" in await refused(mcp, book_id=7, **arguments)
    assert fake.writes == []


async def test_an_explicit_format_wins_over_the_open_reads_edition(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 2, read(1, "2099-12-01", None, PHYSICAL))
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20", format="audio")
    assert state(fake) == (3, [("2099-12-01", "2099-12-20", AUDIO)])


async def test_a_stamped_open_read_is_finished_in_one_write(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 2, read(1, "2099-12-01", None, AUDIO))
    async with connect() as mcp:
        await edit(mcp, book_id=7, finished_on="2099-12-20")
    assert sum(w[0] == "update_user_book_read" for w in fake.writes) == 1
