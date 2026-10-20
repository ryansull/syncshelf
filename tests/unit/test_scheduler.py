"""The sync loops (SPEC §3.4a.9, §3.4a.7 backoff): resume from SQLite with jitter, overdue
once, the half-interval offset between profiles, a tick skipping while the lock is held, the
backoff curve, and runs purged after 90 days. All on the injectable clock, with no sleeps."""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from syncshelf.db import Database
from syncshelf.profiles import sync_profiles
from syncshelf.sync import runs
from syncshelf.sync.runs import RunResult, RunState, Trigger
from syncshelf.sync.scheduler import Scheduler, backoff, purge_runs

from clock import FakeClock

pytestmark = pytest.mark.anyio

HOUR = 3600
JITTER = 7.0


class FakeSync:
    """Just what the scheduler uses of GoodreadsSync, answering with ``outcomes`` in turn."""

    def __init__(self, clock: FakeClock, profiles: list[str]) -> None:
        self.clock = clock
        self.profiles = profiles
        self.locks = {p: asyncio.Lock() for p in profiles}
        self.calls: list[tuple[str, Trigger, float]] = []
        self.outcomes: list[tuple[RunState, bool]] = []

    def enabled(self, profile: str) -> bool:
        return profile in self.profiles

    def lock(self, profile: str) -> asyncio.Lock:
        return self.locks[profile]

    async def run_rss(self, profile: str, trigger: Trigger = "schedule") -> RunResult:
        self.calls.append((profile, trigger, self.clock.time()))
        state, fetched = self.outcomes.pop(0) if self.outcomes else ("ok", True)
        return RunResult(len(self.calls), state, fetched, int(self.clock.time()))


@pytest.fixture
async def db(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "syncshelf.db", snapshot_dir=tmp_path)
    await database.write(sync_profiles, ["alice", "bob"], int(clock.time()))
    try:
        yield database
    finally:
        await database.close()


def scheduler(db: Database, sync: FakeSync, clock: FakeClock) -> Scheduler:
    profiles: dict[str, Any] = {p: None for p in ["alice", "bob"]}
    return Scheduler(
        db=db,
        sync=sync,  # type: ignore[arg-type]
        profiles=profiles,
        interval=HOUR,
        clock=clock,
        jitter=lambda: JITTER,
    )


async def schedule(db: Database, profile: str = "alice") -> tuple[Any, ...]:
    return await db.read(
        lambda conn: conn.execute(
            "SELECT last_run_at, last_success_at, next_due_at, last_result, "
            "consecutive_failures, backoff_until FROM schedules WHERE profile = ?",
            (profile,),
        ).fetchone()
    )


async def test_first_schedules_are_staggered_half_an_interval(
    db: Database, clock: FakeClock
) -> None:
    sync = FakeSync(clock, ["alice", "bob"])
    s = scheduler(db, sync, clock)
    await s.resume()
    now = clock.time()
    assert (s.due("alice"), s.due("bob")) == (now + JITTER, now + HOUR / 2 + JITTER)
    assert (await schedule(db, "bob"))[2] == int(now + HOUR / 2)


async def test_a_restart_resumes_the_stored_schedule_with_jitter(
    db: Database, clock: FakeClock
) -> None:
    sync = FakeSync(clock, ["alice", "bob"])
    await scheduler(db, sync, clock).resume()
    await db.write(
        lambda conn: conn.execute(
            "UPDATE schedules SET next_due_at = ?", (int(clock.time()) + 600,)
        )
    )
    s = scheduler(db, sync, clock)
    await s.resume()
    assert s.due("alice") == clock.time() + 600 + JITTER


async def test_an_overdue_loop_runs_once_staggered_never_a_burst(
    db: Database, clock: FakeClock
) -> None:
    sync = FakeSync(clock, ["alice", "bob"])
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(30 * 24 * HOUR)  # a month down: 720 intervals missed
    s = scheduler(db, sync, clock)
    await s.resume()
    now = clock.time()
    assert (s.due("alice"), s.due("bob")) == (now + JITTER, now + HOUR / 2 + JITTER)
    clock.advance(JITTER)
    assert len(await s.tick()) == 1  # alice once; bob's turn is half an interval later
    assert [c[0] for c in sync.calls] == ["alice"]


async def test_a_tick_runs_what_is_due_and_schedules_the_next(
    db: Database, clock: FakeClock
) -> None:
    sync = FakeSync(clock, ["alice", "bob"])
    s = scheduler(db, sync, clock)
    await s.resume()
    assert await s.tick() == []  # nothing due yet
    clock.advance(JITTER)
    (result,) = await s.tick()
    now = int(clock.time())
    assert result.state == "ok" and sync.calls == [("alice", "schedule", now)]
    assert await schedule(db) == (now, now, now + HOUR, "ok", 0, None)
    assert s.due("alice") == now + HOUR + JITTER


async def test_a_tick_finding_the_lock_held_skips_and_logs(
    db: Database, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    sync = FakeSync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    async with sync.lock("alice"):
        with caplog.at_level(logging.INFO, logger="syncshelf.sync.scheduler"):
            assert await s.tick() == []
    assert sync.calls == []
    assert [r for r in caplog.records if r.__dict__.get("event") == "tick_skipped"]
    assert s.due("alice") == clock.time() + HOUR + JITTER


async def test_a_nudge_runs_the_loop_now_with_its_trigger(db: Database, clock: FakeClock) -> None:
    sync = FakeSync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    s.nudge("alice", "ingest")
    await s.tick()
    assert sync.calls == [("alice", "ingest", clock.time())]


def test_the_backoff_curve() -> None:
    assert [backoff(n) // HOUR for n in range(1, 6)] == [1, 2, 4, 6, 6]


@pytest.mark.parametrize(
    ("outcomes", "failures", "wait"),
    [
        ([("fetch_failed", False)], 1, HOUR),
        ([("fetch_failed", False)] * 3, 3, 4 * HOUR),
        ([("fetch_failed", False)] * 5, 5, 6 * HOUR),
        ([("partial", False)], 1, HOUR),  # an incomplete fetch backs off too
        ([("fetch_failed", False)] * 2 + [("ok", True)], 0, None),  # success resets
        ([("partial", True)], 0, None),  # failed changes are not a failed fetch
    ],
)
async def test_backoff_follows_failed_fetches_only(
    db: Database,
    clock: FakeClock,
    outcomes: list[tuple[RunState, bool]],
    failures: int,
    wait: int | None,
) -> None:
    sync = FakeSync(clock, ["alice"])
    sync.outcomes = list(outcomes)
    s = scheduler(db, sync, clock)
    await s.resume()
    for _ in outcomes:
        clock.advance((s.due("alice") or 0) - clock.time())
        await s.tick()
    _, _, next_due, _, consecutive, backoff_until = await schedule(db)
    now = int(clock.time())
    assert consecutive == failures
    if wait is None:
        assert backoff_until is None and next_due == now + HOUR
    else:
        assert backoff_until == now + wait and next_due == now + max(wait, HOUR)


async def test_the_loops_run_until_cancelled(db: Database, clock: FakeClock) -> None:
    sync = FakeSync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    task = asyncio.create_task(s.run_forever())
    while len(sync.calls) < 3:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    times = [c[2] for c in sync.calls[:3]]
    assert [round(b - a) for a, b in zip(times, times[1:], strict=False)] == [
        HOUR + JITTER,
        HOUR + JITTER,
    ]


async def test_runs_older_than_90_days_are_purged(db: Database, clock: FakeClock) -> None:
    now = int(clock.time())
    old = await db.write(runs.start, "alice", "gr_rss", "schedule", now - 91 * 86400)
    await db.write(runs.start, "alice", "gr_rss", "schedule", now - 89 * 86400)
    assert await db.write(purge_runs, now) == 1
    assert (
        await db.read(
            lambda conn: conn.execute("SELECT id FROM runs WHERE id = ?", (old,)).fetchall()
        )
        == []
    )


async def test_a_deferred_or_aborted_run_keeps_the_backoff_in_force(
    db: Database, clock: FakeClock
) -> None:
    sync = FakeSync(clock, ["alice"])
    sync.outcomes = [("fetch_failed", False), ("deferred", True), ("aborted", False)]
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    await s.tick()
    (*_, failures, backoff_until) = await schedule(db)
    assert (failures, backoff_until) == (1, int(clock.time()) + HOUR)
    for _ in range(2):  # a sync_now run, deferred and then aborted, inside the backoff
        s.nudge("alice", "sync_now")
        await s.tick()
        assert (await schedule(db))[4:] == (1, backoff_until)


async def test_a_run_that_raises_moves_the_loop_on(db: Database, clock: FakeClock) -> None:
    sync = FakeSync(clock, ["alice"])

    async def broken(profile: str, trigger: Trigger = "schedule") -> RunResult:
        raise RuntimeError("boom")

    sync.run_rss = broken  # type: ignore[method-assign]
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    assert await s.tick() == []
    assert s.due("alice") == clock.time() + HOUR + JITTER  # not due again at once


async def test_a_run_asked_for_during_a_run_still_happens(db: Database, clock: FakeClock) -> None:
    sync = FakeSync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    original = sync.run_rss

    async def nudged_meanwhile(profile: str, trigger: Trigger = "schedule") -> RunResult:
        s.nudge(profile, "ingest")
        return await original(profile, trigger)

    sync.run_rss = nudged_meanwhile  # type: ignore[method-assign]
    await s.tick()
    sync.run_rss = original  # type: ignore[method-assign]
    assert s.due("alice") == clock.time()
    await s.tick()
    assert [c[1] for c in sync.calls] == ["schedule", "ingest"]


async def test_a_run_asked_for_while_one_goes_waits_only_for_it(
    db: Database, clock: FakeClock
) -> None:
    sync = FakeSync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    s.nudge("alice", "ingest")
    async with sync.lock("alice"):
        await s.tick()
    assert s.due("alice") == clock.time() + 5.0
    clock.advance(5.0)
    await s.tick()
    assert [c[1] for c in sync.calls] == ["ingest"]


async def test_a_skipped_tick_is_stored(db: Database, clock: FakeClock) -> None:
    sync = FakeSync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    async with sync.lock("alice"):
        await s.tick()
    assert (await schedule(db))[2] == int(clock.time()) + HOUR
