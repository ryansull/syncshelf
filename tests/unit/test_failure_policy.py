"""The failure policy, by fault injection (SPEC §3.4a.7, §3.4a.13): each error class's in-run
retries, quarantine at 24 h and 3 attempts, each release path, a timeout that landed, the
daily reserve deferring a run, and quarantined changes costing nothing."""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from syncshelf.sync import identity

from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio

DUNE = 100
HOUR = 3600


async def linked(w: World) -> None:
    w.hardcover.add_user_book(HC_USER, 7, status_id=3, rating=4.0)
    w.hardcover.add_mapping(DUNE, 7, title="Dune")
    w.goodreads.add_item(GR_USER, DUNE, "Dune", user_rating="4")
    await w.sync.run_rss("alice")  # the backfill
    w.clock.advance(HOUR)


@pytest.fixture
async def w(tmp_path: Path) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        await linked(w)
        yield w


def rate(w: World, stars: str) -> None:
    w.goodreads.users[GR_USER].items[0]["user_rating"] = stars


async def run(w: World) -> tuple[Any, ...]:
    run_id = await w.sync.run_rss("alice")
    ((row),) = await w.rows(
        "SELECT state, applied, failed, quarantined, first_error FROM runs WHERE id = ?", run_id
    )
    return row


async def rating(w: World) -> tuple[Any, ...]:
    ((row),) = await w.rows(
        "SELECT value, pending_value, attempts, first_failed_at, last_error, last_error_class, "
        "quarantined_at FROM baseline_fields WHERE source_id = ? AND field = 'rating'",
        str(DUNE),
    )
    return row


def updates(w: World) -> int:
    return sum(1 for op, _ in w.hardcover.requests if op == "UpdateUserBook")


# --- classes and in-run retries -------------------------------------------------------------


async def test_a_transient_failure_is_retried_after_2_then_8_seconds(
    w: World, caplog: pytest.LogCaptureFixture
) -> None:
    rate(w, "5")
    for _ in range(2):
        w.hardcover.fail_next("UpdateUserBook", "unavailable")
    with caplog.at_level(logging.WARNING, logger="syncshelf.sync.goodreads"):
        assert (await run(w))[:3] == ("ok", 1, 0)
    retries = [r for r in caplog.records if r.__dict__.get("event") == "change_retry"]
    assert [r.getMessage().split(";")[1].split(":")[0].strip() for r in retries] == [
        "retrying in 2.0s",
        "retrying in 8.0s",
    ]
    assert updates(w) == 3
    assert w.hardcover.user_books[HC_USER][0]["rating"] == 5.0


async def test_a_transient_failure_left_after_its_retries_stays_pending(w: World) -> None:
    rate(w, "5")
    for _ in range(3):
        w.hardcover.fail_next("UpdateUserBook", "unavailable")
    assert await run(w) == ("partial", 0, 1, 0, "Hardcover returned HTTP 503")
    value, pending, attempts, first_failed, error, kind, quarantined = await rating(w)
    assert (value, pending, attempts, kind, quarantined) == ("4", "5", 1, "transient", None)
    assert first_failed == int(w.clock.time()) and error == "Hardcover returned HTTP 503"


async def test_a_permanent_failure_is_not_retried(w: World) -> None:
    rate(w, "5")
    w.hardcover.fail_next("UpdateUserBook", "validation", message="no")
    assert (await run(w))[:3] == ("partial", 0, 1)
    assert updates(w) == 1
    assert (await rating(w))[2:6:3] == (1, "permanent")


async def test_a_bug_is_quarantined_at_once(w: World) -> None:
    rate(w, "5")
    w.hardcover.fail_next("UpdateUserBook", "capacity")
    assert (await run(w))[:4] == ("partial", 0, 1, 1)
    assert updates(w) == 1
    *_, kind, quarantined = await rating(w)
    assert kind == "bug" and quarantined == int(w.clock.time())


async def test_a_timeout_that_landed_converges_without_a_second_write(w: World) -> None:
    rate(w, "5")
    w.hardcover.fail_next("UpdateUserBook", "timeout", landed=True)
    assert (await run(w))[:3] == ("ok", 1, 0)
    # The retry found Hardcover already at 5, so it wrote nothing more.
    assert updates(w) == 1
    assert w.hardcover.user_books[HC_USER][0]["rating"] == 5.0
    assert (await rating(w))[:2] == ("5", None)


# --- quarantine --------------------------------------------------------------------------------


async def fail_permanently(w: World) -> tuple[Any, ...]:
    w.hardcover.fail_next("UpdateUserBook", "validation", message="no")
    return await run(w)


def failing(schema: Any, attempts: int, first_failed_at: int | None) -> int:
    schema.execute("INSERT INTO profiles (name, created_at) VALUES ('alice', 0)")
    schema.execute(
        "INSERT INTO source_rows (profile, source, source_id, state, first_seen_at, last_seen_at)"
        " VALUES ('alice', 'goodreads', '1', 'ignored', 0, 0)"
    )
    return schema.execute(
        "INSERT INTO baseline_fields (profile, source, source_id, field, attempts, "
        "first_failed_at) VALUES ('alice', 'goodreads', '1', 'rating', ?, ?)",
        (attempts, first_failed_at),
    ).lastrowid


T0 = 4_102_444_800


@pytest.mark.parametrize(
    ("attempts_before", "elapsed", "quarantined"),
    [
        (2, 24 * HOUR, True),  # the third attempt, 24 h after the first: quarantined
        (2, 24 * HOUR - 1, False),  # a second short
        (1, 30 * HOUR, False),  # only the second attempt, however late
        (0, 0, False),  # a first failure
    ],
)
def test_quarantine_needs_24_hours_and_3_attempts(
    schema: Any, attempts_before: int, elapsed: int, quarantined: bool
) -> None:
    with schema:
        baseline = failing(schema, attempts_before, T0 if attempts_before else None)
        now = T0 + elapsed
        found = identity.record_failure(schema, [baseline], "permanent", "no", now)
        assert (baseline in found) is quarantined


async def test_24_hours_alone_is_not_enough(w: World) -> None:
    rate(w, "5")
    await fail_permanently(w)
    w.clock.advance(30 * HOUR)
    await fail_permanently(w)  # attempt 2, long past 24 h
    assert (await rating(w))[6] is None
    w.clock.advance(HOUR)
    await fail_permanently(w)  # attempt 3
    assert (await rating(w))[6] is not None


async def quarantine(w: World) -> None:
    rate(w, "5")
    w.hardcover.fail_next("UpdateUserBook", "capacity")
    await run(w)
    w.clock.advance(HOUR)


async def test_a_quarantined_change_costs_no_hardcover_call(w: World) -> None:
    await quarantine(w)
    before = len(w.hardcover.requests)
    assert (await run(w))[:3] == ("ok", 0, 0)
    assert len(w.hardcover.requests) == before


async def test_a_new_source_value_releases_a_quarantined_change(w: World) -> None:
    await quarantine(w)
    rate(w, "3")
    assert (await run(w))[:3] == ("ok", 1, 0)
    assert w.hardcover.user_books[HC_USER][0]["rating"] == 3.0
    assert (await rating(w))[:3] == ("3", None, 0)


async def test_a_new_version_releases_every_quarantined_change_once(w: World) -> None:
    await quarantine(w)
    assert await w.db.write(identity.release_on_new_version, "9.9.9") == 1
    assert (await rating(w))[2:7:4] == (0, None)
    assert await w.rows("SELECT value FROM meta WHERE key = 'last_started_version'") == [("9.9.9",)]
    # The same version again releases nothing; the next run applies the change.
    assert await w.db.write(identity.release_on_new_version, "9.9.9") is None
    assert (await run(w))[:2] == ("ok", 1)


# --- the daily reserve -------------------------------------------------------------------


async def test_the_daily_reserve_defers_the_run_without_counting_a_failure(
    tmp_path: Path,
) -> None:
    async with world(tmp_path, reserve=1000) as w:
        await linked(w)
        rate(w, "5")
        # The library fetch leaves the day's budget under the reserve, so the write
        # procedure's first call is refused by the limiter.
        w.hardcover.daily_remaining = 999
        state, applied, failed, _, error = await run(w)
        assert (state, applied, failed) == ("deferred", 0, 0)
        assert error is not None and "reserve" in error
        assert updates(w) == 0
        assert (await rating(w))[1:3] == ("5", 0)


# --- review follow-ups ----------------------------------------------------------------------


async def two_changes(w: World) -> None:
    """Dune and a second linked book, both with a rating change pending."""
    w.hardcover.add_user_book(HC_USER, 8, status_id=3, rating=2.0)
    w.hardcover.add_mapping(200, 8, title="Dune Messiah")
    w.goodreads.add_item(GR_USER, 200, "Dune Messiah", user_rating="2")
    await w.sync.run_rss("alice")  # Messiah's first sighting is pushed: it's already 2
    w.clock.advance(HOUR)
    for item in w.goodreads.users[GR_USER].items:
        item["user_rating"] = "5"


async def test_hardcover_down_leaves_the_runs_other_changes_for_the_next_run(w: World) -> None:
    await two_changes(w)
    for _ in range(3):
        w.hardcover.fail_next("UpdateUserBook", "unavailable")
    state, applied, failed, *_ = await run(w)
    # The first change used up its retries; the second wasn't tried, nor counted.
    assert (state, applied, failed) == ("partial", 0, 1)
    assert updates(w) == 3
    assert await w.rows("SELECT count(*) FROM baseline_fields WHERE attempts > 0") == [(1,)]


async def test_a_429_the_limiter_gives_up_on_is_no_failure(w: World) -> None:
    rate(w, "5")
    for _ in range(10):  # more than the limiter retries
        w.hardcover.fail_next("UpdateUserBook", "rate_limited", retry_after=1)
    state, applied, failed, *_ = await run(w)
    assert (state, applied, failed) == ("partial", 0, 0)
    assert (await rating(w))[1:3] == ("5", 0)


async def test_a_reserve_hit_after_a_write_makes_the_run_partial(tmp_path: Path) -> None:
    async with world(tmp_path, reserve=1000) as w:
        await linked(w)
        await two_changes(w)
        # Budget for the fetch and one change, then the reserve.
        w.hardcover.daily_remaining = 1003
        state, applied, failed, *_ = await run(w)
        assert (state, applied, failed) == ("partial", 1, 0)
