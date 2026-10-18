"""``correct_read`` through the real ASGI app, and the write procedure's ``correct`` operation
(SPEC §3.3.6 rules 5–6, §3.3.9), against ``FakeHardcover``. Its read writes reset every field
they omit, so each edit must carry the whole read."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from syncshelf.app import create_app
from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.sync.apply import Applier, Correct, FormatNeeded, Target

from clock import FakeClock
from fakes.hardcover import FakeHardcover
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
    id: int,
    started: str | None = None,
    finished: str | None = None,
    edition: int | None = None,
    **more: Any,
) -> dict[str, Any]:
    fmt = {PHYSICAL: 1, AUDIO: 2, EBOOK: 4}.get(edition or 0)
    return {"id": id, "started_at": started, "finished_at": finished, "edition_id": edition,
            "reading_format_id": fmt, **more}  # fmt: skip


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=ALICE)
    return fake


def shelve(fake: FakeHardcover, status: int, *reads: dict[str, Any], **fields: Any) -> None:
    fake.add_user_book(
        ALICE, 7, status_id=status, user_book_reads=list(reads), book=EDITIONS, **fields
    )


def row(fake: FakeHardcover) -> dict[str, Any]:
    return next(r for r in fake.user_books[ALICE] if r["book_id"] == 7)


def reads(fake: FakeHardcover) -> list[tuple[Any, ...]]:
    """[(started, finished, edition)] for book 7, in read-id order."""
    found = sorted(row(fake)["user_book_reads"], key=lambda r: r["id"])
    return [(r["started_at"], r["finished_at"], r["edition_id"]) for r in found]


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


async def correct(mcp: Client, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool("correct_read", arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def refused(mcp: Client, **arguments: Any) -> str:
    result = await mcp.call_tool("correct_read", arguments)
    assert result.is_error
    return result.content[0].text


# --- correct_read ---------------------------------------------------------------------------


async def test_a_finish_date_is_corrected_and_the_old_one_returned(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 3, read(1, "2099-05-01", "2099-05-09", AUDIO))
    async with connect() as mcp:
        result = await correct(mcp, book_id=7, read_id=1, finished_on="2099-05-03")
    assert reads(fake) == [("2099-05-01", "2099-05-03", AUDIO)]
    assert result["changes"] == [
        {
            "field": "read",
            "before": {"read_id": 1, "started_on": "2099-05-01", "finished_on": "2099-05-09",
                       "format": "audio"},
            "after": {"read_id": 1, "started_on": "2099-05-01", "finished_on": "2099-05-03",
                      "format": "audio"},
        }
    ]  # fmt: skip
    assert row(fake)["status_id"] == 3


async def test_an_edit_keeps_every_field_it_doesnt_change(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 2, read(1, "2099-05-01", None, PHYSICAL, progress_pages=120))
    async with connect() as mcp:
        await correct(mcp, book_id=7, read_id=1, started_on="2099-04-28")
    (only,) = row(fake)["user_book_reads"]
    assert (only["started_at"], only["edition_id"], only["progress_pages"]) == (
        "2099-04-28",
        PHYSICAL,
        120,
    )


async def test_a_format_moves_the_read_to_that_default_edition(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 3, read(1, "2099-05-01", "2099-05-09", PHYSICAL))
    async with connect() as mcp:
        result = await correct(mcp, book_id=7, read_id=1, format="audio")
    assert reads(fake) == [("2099-05-01", "2099-05-09", AUDIO)]
    assert result["changes"][0]["after"]["format"] == "audio"


async def test_finishing_an_open_read_leaves_the_status_alone(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(fake, 2, read(1, "2099-05-01", None, AUDIO))
    async with connect() as mcp:
        await correct(mcp, book_id=7, read_id=1, finished_on="2099-05-09")
    assert reads(fake) == [("2099-05-01", "2099-05-09", AUDIO)]
    assert row(fake)["status_id"] == 2


async def test_the_first_start_follows_the_earliest_read(connect: Any, fake: FakeHardcover) -> None:
    shelve(
        fake,
        3,
        read(1, "2099-01-01", "2099-01-09", AUDIO),
        read(2, "2099-05-01", "2099-05-09", AUDIO),
        first_started_reading_date="2099-01-01",
    )
    async with connect() as mcp:
        await correct(mcp, book_id=7, read_id=1, started_on="2099-06-01", finished_on="2099-06-09")
    assert row(fake)["first_started_reading_date"] == "2099-05-01"


async def test_a_value_the_read_already_has_isnt_written(connect: Any, fake: FakeHardcover) -> None:
    shelve(
        fake, 3, read(1, "2099-05-01", "2099-05-09", AUDIO), first_started_reading_date="2099-05-01"
    )
    async with connect() as mcp:
        result = await correct(mcp, book_id=7, read_id=1, finished_on="2099-05-09")
    assert result["changes"] == []
    assert fake.writes == []


@pytest.mark.parametrize(
    ("arguments", "says"),
    [
        ({"read_id": 1}, "Nothing to change"),
        ({"read_id": 9, "finished_on": "2099-05-03"}, "isn't one of book 7's reads"),
        ({"read_id": 1, "started_on": "2099-05-20"}, "after it finished on 2099-05-09"),
        ({"read_id": 1, "started_on": "2099-05-20", "finished_on": "2099-05-10"}, "earlier date"),
    ],
)
async def test_bad_edits_are_refused_with_nothing_written(
    connect: Any, fake: FakeHardcover, arguments: dict[str, Any], says: str
) -> None:
    shelve(fake, 3, read(1, "2099-05-01", "2099-05-09", AUDIO))
    fake.add_user_book(ALICE, 8, status_id=3, user_book_reads=[read(9, None, "2099-03-01")])
    async with connect() as mcp:
        assert says in await refused(mcp, book_id=7, **arguments)
    assert fake.writes == []


async def test_a_book_not_on_the_shelves_has_no_reads(connect: Any) -> None:
    async with connect() as mcp:
        assert "isn't on the shelves" in await refused(
            mcp, book_id=7, read_id=1, finished_on="2099-05-03"
        )


async def test_it_is_marked_destructive(connect: Any) -> None:
    async with connect() as mcp:
        tools = {t.name: t for t in (await mcp.list_tools()).tools}
    annotations = tools["correct_read"].annotations
    assert annotations is not None and annotations.destructive_hint is True


# --- the procedure's correct operation ------------------------------------------------------


class _NoMirror:
    async def write_through(self, *args: Any) -> None:
        pass


@pytest.fixture
def applier(tmp_path: Path, fake: FakeHardcover) -> Applier:
    client = HardcoverClient(
        "https://hardcover.example/v1/graphql",
        hardcover_token("alice"),
        gate=None,
        transport=fake.transport,
    )
    profiles = make_config(tmp_path).profiles
    return Applier({"alice": client}, _NoMirror(), profiles)  # type: ignore[arg-type]


async def test_a_correction_moves_the_read_finished_on_the_old_date(
    applier: Applier, fake: FakeHardcover
) -> None:
    shelve(
        fake,
        3,
        read(1, "2099-01-01", "2099-01-09", AUDIO),
        read(2, None, "2099-05-09"),
        first_started_reading_date="2099-01-01",
    )
    await applier.apply("alice", 7, Target(read=Correct("2099-05-09", "2099-06-02")))
    assert reads(fake) == [("2099-01-01", "2099-01-09", AUDIO), (None, "2099-06-02", None)]
    assert [w[0] for w in fake.writes] == ["update_user_book_read"]


async def test_a_correction_with_a_status_change_leaves_no_invented_read(
    applier: Applier, fake: FakeHardcover
) -> None:
    shelve(fake, 4, read(1, "2099-05-01", "2099-05-09", AUDIO))
    await applier.apply("alice", 7, Target(status_id=3, read=Correct("2099-05-09", "2099-05-12")))
    assert reads(fake) == [("2099-05-01", "2099-05-12", AUDIO)]
    assert TODAY not in str(row(fake)["user_book_reads"])


async def test_a_correction_with_nothing_on_the_old_date_adopts_a_nearby_read(
    applier: Applier, fake: FakeHardcover
) -> None:
    shelve(fake, 3, read(1, "2099-05-01", "2099-05-09", AUDIO))
    applied = await applier.apply("alice", 7, Target(read=Correct("2099-02-01", "2099-05-12")))
    assert reads(fake) == [("2099-05-01", "2099-05-09", AUDIO)]
    assert applied.changes == []


async def test_a_correction_with_nothing_to_move_or_adopt_logs_a_read(
    applier: Applier, fake: FakeHardcover
) -> None:
    shelve(fake, 3, read(1, "2099-01-01", "2099-01-09", AUDIO))
    with pytest.raises(FormatNeeded):
        await applier.apply("alice", 7, Target(read=Correct("2099-02-01", "2099-05-12")))
    await applier.apply("alice", 7, Target(read=Correct("2099-02-01", "2099-05-12", "print")))
    assert reads(fake)[1] == (None, "2099-05-12", PHYSICAL)


async def test_a_correction_replayed_after_it_landed_changes_nothing(
    applier: Applier, fake: FakeHardcover
) -> None:
    shelve(fake, 3, read(1, "2099-05-01", "2099-05-09", AUDIO))
    target = Target(read=Correct("2099-05-09", "2099-05-12"))
    await applier.apply("alice", 7, target)
    fake.writes.clear()
    assert (await applier.apply("alice", 7, target)).changes == []
    assert fake.writes == []


async def test_a_first_start_that_failed_is_set_by_the_same_call_again(
    connect: Any, fake: FakeHardcover
) -> None:
    shelve(
        fake, 3, read(1, "2099-05-01", "2099-05-09", AUDIO), first_started_reading_date="2099-05-01"
    )
    fake.fail_next("UpdateUserBook", "unavailable")
    async with connect() as mcp:
        assert "Partly done" in await refused(mcp, book_id=7, read_id=1, started_on="2099-04-20")
        assert reads(fake) == [("2099-04-20", "2099-05-09", AUDIO)]
        await correct(mcp, book_id=7, read_id=1, started_on="2099-04-20")
    assert row(fake)["first_started_reading_date"] == "2099-04-20"
