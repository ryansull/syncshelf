"""The per-profile mirror (SPEC §3.3.4) against ``FakeHardcover``."""

import json
import sqlite3
from typing import Any

import anyio
import pytest

from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.db import Database
from syncshelf.profiles import sync_profiles
from syncshelf.sync.limiter import HardcoverLimiter
from syncshelf.sync.mirror import Freshness, Mirror, NoMirror

from clock import FakeClock
from fakes.hardcover import URL, FakeHardcover

TTL = 600
TOKEN = "fake-hardcover-token"

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(TOKEN, user_id=42)
    return fake


@pytest.fixture
async def mirror(db: Database, fake: FakeHardcover, clock: FakeClock) -> Mirror:
    await db.write(sync_profiles, ["alice"], int(clock.time()))
    client = HardcoverClient(
        URL,
        TOKEN,
        gate=HardcoverLimiter("alice", reserve=1000, clock=clock),
        transport=fake.transport,
    )
    return Mirror(db, {"alice": client}, ttl=TTL, clock=clock.time)


def library_fetches(fake: FakeHardcover) -> int:
    return sum(op == "Library" for op, _ in fake.requests)


def rows(db_conn: sqlite3.Connection, sql: str) -> list[tuple[Any, ...]]:
    return db_conn.execute(sql).fetchall()


async def query(db: Database, sql: str) -> list[tuple[Any, ...]]:
    return await db.read(rows, sql)


# --- freshness ----------------------------------------------------------------------------


async def test_the_first_read_fetches_and_stamps_as_of(
    mirror: Mirror, fake: FakeHardcover, clock: FakeClock
) -> None:
    assert await mirror.fresh("alice") == Freshness(int(clock.time()))
    assert library_fetches(fake) == 1


async def test_within_the_ttl_the_copy_is_reused(
    mirror: Mirror, fake: FakeHardcover, clock: FakeClock
) -> None:
    first = await mirror.fresh("alice")
    clock.advance(TTL - 1)
    assert await mirror.fresh("alice") == first
    assert library_fetches(fake) == 1


async def test_past_the_ttl_the_copy_is_refreshed(
    mirror: Mirror, fake: FakeHardcover, clock: FakeClock
) -> None:
    await mirror.fresh("alice")
    clock.advance(TTL)
    assert (await mirror.fresh("alice")).as_of == int(clock.time())
    assert library_fetches(fake) == 2


async def test_concurrent_readers_share_one_refresh(mirror: Mirror, fake: FakeHardcover) -> None:
    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(mirror.fresh, "alice")
    assert library_fetches(fake) == 1


async def test_a_failed_refresh_serves_the_old_copy_flagged_stale(
    mirror: Mirror, db: Database, fake: FakeHardcover, clock: FakeClock
) -> None:
    first = await mirror.fresh("alice")
    clock.advance(TTL)
    fake.fail_next("Library", "unavailable")
    stale = await mirror.fresh("alice")
    assert stale.as_of == first.as_of
    assert stale.stale is True
    assert stale.stale_reason == "Hardcover returned HTTP 503"
    assert await query(db, "SELECT as_of, last_attempt_at, last_error FROM mirror_state") == [
        (first.as_of, int(clock.time()), "Hardcover returned HTTP 503")
    ]


async def test_a_success_clears_the_last_error(
    mirror: Mirror, db: Database, fake: FakeHardcover, clock: FakeClock
) -> None:
    await mirror.fresh("alice")
    clock.advance(TTL)
    fake.fail_next("Library", "unavailable")
    await mirror.fresh("alice")
    await mirror.fresh("alice")  # the failure doesn't move as_of, so this refreshes again
    assert await query(db, "SELECT last_error FROM mirror_state") == [(None,)]


async def test_with_no_copy_yet_a_failed_refresh_is_refused(
    mirror: Mirror, db: Database, fake: FakeHardcover
) -> None:
    fake.fail_next("Me", "unavailable")
    with pytest.raises(NoMirror, match="no copy of this Hardcover library yet"):
        await mirror.fresh("alice")
    assert await query(db, "SELECT as_of, last_error FROM mirror_state") == [
        (None, "Hardcover returned HTTP 503")
    ]


# --- what is stored -----------------------------------------------------------------------


async def test_a_refresh_replaces_the_whole_copy(
    mirror: Mirror, db: Database, fake: FakeHardcover, clock: FakeClock
) -> None:
    fake.add_user_book(42, 7)
    gone = fake.add_user_book(42, 8)
    await mirror.fresh("alice")
    fake.user_books[42].remove(gone)
    clock.advance(TTL)
    await mirror.fresh("alice")
    assert await query(db, "SELECT hc_book_id FROM mirror_user_books") == [(7,)]
    assert await query(db, "SELECT hc_book_id FROM mirror_books") == [(7,)]


async def test_a_user_book_is_stored_without_its_review(
    mirror: Mirror, db: Database, fake: FakeHardcover
) -> None:
    fake.add_user_book(
        42,
        7,
        status_id=3,
        rating=4.5,
        date_added="2026-02-03",
        review_markdown="private thoughts",
        has_review=True,
        user_book_reads=[
            {
                "id": 1,
                "started_at": "2026-01-02",
                "finished_at": "2026-01-20",
                "edition_id": 71,
                "reading_format_id": 2,
            },
            {"id": 2},
        ],  # fmt: skip
        book={
            "title": "Dune",
            "pages": None,
            "cached_contributors": [
                {"author": {"id": 1, "name": "Frank Herbert"}, "contribution": "Author"},
                {"author": {"id": 2, "name": "Scott Brick"}, "contribution": "Narrator"},
            ],
            "cached_featured_series": {"series": {"id": 5, "name": "Dune"}, "position": 1.0},
            "genres": [{"tag": "Science Fiction", "count": 9}, {"tag": "Classics", "count": 3}],
            "default_audio_edition": {"id": 72, "audio_seconds": 75600},
            "default_physical_edition": {"id": 70, "pages": 412},
        },
    )
    fake.add_list(42, "Favourites", [7])
    await mirror.fresh("alice")

    assert await query(db, "SELECT * FROM mirror_user_books") == [
        ("alice", 1, 7, 3, 4.5, 70, "2026-02-03")
    ]
    assert await query(db, "SELECT * FROM mirror_reads ORDER BY hc_read_id") == [
        ("alice", 1, 1, "2026-01-02", "2026-01-20", 71, 2),
        ("alice", 2, 1, None, None, None, None),
    ]
    [book] = await query(db, "SELECT * FROM mirror_books")
    assert book[:3] == ("alice", 7, "Dune")
    assert json.loads(book[3]) == [
        {"name": "Frank Herbert", "role": None},
        {"name": "Scott Brick", "role": "Narrator"},
    ]
    assert book[4:9] == (5, "Dune", 1.0, 412, 75600)  # pages from the physical edition
    assert json.loads(book[9]) == {
        "genres": ["Science Fiction", "Classics"],
        "moods": [],
        "tags": [],
    }
    assert await query(db, "SELECT * FROM mirror_list_items") == [("alice", 2, "Favourites", 7)]
    for table in ("books", "user_books", "reads", "list_items", "state"):
        assert "private thoughts" not in str(await query(db, f"SELECT * FROM mirror_{table}"))


async def test_readers_queued_behind_a_failed_refresh_share_the_failure(
    mirror: Mirror, fake: FakeHardcover
) -> None:
    fake.fail_next("Me", "unavailable")
    outcomes: list[str] = []

    async def read() -> None:
        try:
            await mirror.fresh("alice")
            outcomes.append("ok")
        except NoMirror:
            outcomes.append("refused")

    async with anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(read)
    assert outcomes == ["refused"] * 3
    assert [op for op, _ in fake.requests] == ["Me"]


async def test_data_the_copy_cant_hold_serves_the_old_copy_stale(
    mirror: Mirror, db: Database, fake: FakeHardcover, clock: FakeClock
) -> None:
    fake.add_user_book(42, 7)
    first = await mirror.fresh("alice")
    fake.add_user_book(42, 8, rating=4.25)  # off the half-star grid: the CHECK refuses it
    clock.advance(TTL)
    stale = await mirror.fresh("alice")
    assert (stale.as_of, stale.stale) == (first.as_of, True)
    assert stale.stale_reason == "Hardcover sent data SyncShelf couldn't store (IntegrityError)"
    assert await query(db, "SELECT hc_book_id FROM mirror_user_books") == [(7,)]
    assert await query(db, "SELECT last_error FROM mirror_state") == [(stale.stale_reason,)]
