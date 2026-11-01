"""The sync loops (SPEC §3.4a.9, §3.4a.7 backoff, §3.4b.5): resume from SQLite with jitter,
overdue once, the half-interval offset between profiles, a tick skipping while the lock is
held, the backoff curve, and runs purged after 90 days. Then the CSV loop: the night window
in the profile's own timezone, a restart keeping the due date, a failure waiting for the next
window, and a dated upload counting as a success. All on the injectable clock, with no
sleeps — the clock starts at midnight UTC, so :func:`when` reads as a wall clock."""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from syncshelf.db import Database
from syncshelf.profiles import sync_profiles
from syncshelf.sync import runs
from syncshelf.sync.runs import RunResult, RunState, Trigger
from syncshelf.sync.scheduler import CSV, Scheduler, backoff, purge_runs

from clock import FakeClock

pytestmark = pytest.mark.anyio

HOUR = 3600
JITTER = 7.0
DAY = 24 * HOUR
WINDOW = (time(2, 0), time(5, 0))
"""The night window a CSV export runs in (§3.4b.5), as the config's default has it."""
TOKYO = "Asia/Tokyo"


def when(day: int, hour: int, minute: int = 0) -> float:
    """That UTC wall-clock time, ``day`` days after the clock's start (itself midnight)."""
    return FakeClock.START + day * DAY + hour * HOUR + minute * 60


def jump(clock: FakeClock, target: float) -> None:
    """Move the clock to ``target``, which must not be in its past."""
    clock.advance(target - clock.time())


class _Profile:
    """Just the one thing the scheduler asks a profile's config for."""

    def __init__(self, zone: str = "Etc/UTC") -> None:
        self._zone = ZoneInfo(zone)

    def tzinfo(self) -> ZoneInfo:
        return self._zone


class FakeSync:
    """Just what the scheduler uses of GoodreadsSync, answering with ``outcomes`` in turn."""

    def __init__(self, clock: FakeClock, profiles: list[str]) -> None:
        self.clock = clock
        self.profiles = profiles
        self.locks = {p: asyncio.Lock() for p in profiles}
        self.calls: list[tuple[str, Trigger, float]] = []
        self.csv_calls: list[tuple[str, Trigger, float]] = []
        self.outcomes: list[tuple[RunState, bool]] = []
        self.csv: list[str] = []
        """Which profiles also have a CSV source; none unless a test says so."""
        self.csv_outcomes: list[tuple[RunState, bool]] = []

    def enabled(self, profile: str) -> bool:
        return profile in self.profiles

    def csv_enabled(self, profile: str) -> bool:
        return profile in self.csv

    def lock(self, profile: str) -> asyncio.Lock:
        return self.locks[profile]

    def running(self, profile: str) -> tuple[int, int] | None:
        return None

    async def run_rss(
        self,
        profile: str,
        trigger: Trigger = "schedule",
        started: Any = None,
        *,
        held: bool = False,
    ) -> RunResult:
        self.calls.append((profile, trigger, self.clock.time()))
        if started is not None:
            started(len(self.calls), int(self.clock.time()))
        state, fetched = self.outcomes.pop(0) if self.outcomes else ("ok", True)
        return RunResult(len(self.calls), state, fetched, int(self.clock.time()))

    async def run_csv(
        self,
        profile: str,
        trigger: Trigger = "schedule",
        started: Any = None,
        *,
        held: bool = False,
    ) -> RunResult:
        self.csv_calls.append((profile, trigger, self.clock.time()))
        self.calls.append((profile, trigger, self.clock.time()))
        if started is not None:
            started(len(self.calls), int(self.clock.time()))
        state, fetched = self.csv_outcomes.pop(0) if self.csv_outcomes else ("ok", True)
        return RunResult(len(self.calls), state, fetched, int(self.clock.time()))


@pytest.fixture
async def db(tmp_path: Path, clock: FakeClock) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "syncshelf.db", snapshot_dir=tmp_path)
    await database.write(sync_profiles, ["alice", "bob"], int(clock.time()))
    try:
        yield database
    finally:
        await database.close()


def scheduler(
    db: Database, sync: FakeSync, clock: FakeClock, zones: dict[str, str] | None = None
) -> Scheduler:
    places = zones or {}
    profiles: dict[str, Any] = {p: _Profile(places.get(p, "Etc/UTC")) for p in ["alice", "bob"]}
    return Scheduler(
        db=db,
        sync=sync,  # type: ignore[arg-type]
        profiles=profiles,
        interval=HOUR,
        clock=clock,
        window=WINDOW,
        jitter=lambda: JITTER,
    )


async def schedule(db: Database, profile: str = "alice", source: str = "gr_rss") -> tuple[Any, ...]:
    return await db.read(
        lambda conn: conn.execute(
            "SELECT last_run_at, last_success_at, next_due_at, last_result, "
            "consecutive_failures, backoff_until FROM schedules "
            "WHERE profile = ? AND source = ?",
            (profile, source),
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

    async def broken(profile: str, trigger: Trigger = "schedule", started: Any = None) -> RunResult:
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

    async def nudged_meanwhile(
        profile: str, trigger: Trigger = "schedule", started: Any = None
    ) -> RunResult:
        s.nudge(profile, "ingest")
        return await original(profile, trigger, started)

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


async def test_a_run_asked_for_now_returns_its_id_and_is_recorded(
    db: Database, clock: FakeClock
) -> None:
    sync = FakeSync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    assert await s.run_now("alice", "sync_now") == (1, int(clock.time()), True, "gr_rss")
    while not (await schedule(db))[0]:
        await asyncio.sleep(0)
    assert sync.calls == [("alice", "sync_now", clock.time())]
    assert (await schedule(db))[3] == "ok"


# --- the CSV loop (§3.4b.5) ----------------------------------------------------------------


def csv_sync(clock: FakeClock, profiles: list[str]) -> FakeSync:
    """A fake engine whose profiles all have a CSV source as well as RSS."""
    sync = FakeSync(clock, profiles)
    sync.csv = list(profiles)
    return sync


async def csv_schedule(db: Database, profile: str = "alice") -> tuple[Any, ...]:
    return await schedule(db, profile, "gr_csv")


async def test_a_profile_without_credentials_has_no_csv_loop(
    db: Database, clock: FakeClock
) -> None:
    s = scheduler(db, FakeSync(clock, ["alice"]), clock)
    await s.resume()
    assert s.csv_enabled("alice") is False
    assert s.due("alice", CSV) is None
    assert await csv_schedule(db) is None


async def test_a_fresh_csv_loop_is_due_in_the_next_night_window(
    db: Database, clock: FakeClock
) -> None:
    jump(clock, when(0, 13))  # the afternoon: tonight's window is ahead
    s = scheduler(db, csv_sync(clock, ["alice"]), clock)
    await s.resume()
    assert s.due("alice", CSV) == when(1, 2) + JITTER
    assert (await csv_schedule(db))[2] == int(when(1, 2))


async def test_a_fresh_loop_inside_the_window_runs_tonight(db: Database, clock: FakeClock) -> None:
    # A window the moment is already inside counts as now, so the export runs tonight rather
    # than waiting a whole day for the next one.
    jump(clock, when(0, 3, 30))
    s = scheduler(db, csv_sync(clock, ["alice"]), clock)
    await s.resume()
    assert (await csv_schedule(db))[2] == int(when(0, 2))
    clock.advance(JITTER)
    assert len(await s.tick()) == 2  # the export and the feed, one after the other


async def test_the_window_is_read_in_the_profiles_own_timezone(
    db: Database, clock: FakeClock
) -> None:
    # 13:00 UTC is 22:00 in Tokyo, so bob's next 02:00 is 17:00 UTC the same day — before
    # alice's, which is nine hours later.
    jump(clock, when(0, 13))
    s = scheduler(db, csv_sync(clock, ["alice", "bob"]), clock, zones={"bob": TOKYO})
    await s.resume()
    assert s.due("bob", CSV) == when(0, 17) + 1.5 * HOUR + JITTER
    assert s.due("alice", CSV) == when(1, 2) + JITTER


async def test_the_profiles_exports_are_spread_across_the_window(
    db: Database, clock: FakeClock
) -> None:
    # The export is the slowest Goodreads call there is, so two profiles don't start together.
    jump(clock, when(0, 13))
    s = scheduler(db, csv_sync(clock, ["alice", "bob"]), clock)
    await s.resume()
    gap = (s.due("bob", CSV) or 0) - (s.due("alice", CSV) or 0)
    assert gap == 1.5 * HOUR  # half the three-hour window


async def test_a_restart_keeps_the_stored_due_date(db: Database, clock: FakeClock) -> None:
    # The whole point of storing next_due_at: a restart must not start the five days again.
    jump(clock, when(0, 13))
    sync = csv_sync(clock, ["alice"])
    await scheduler(db, sync, clock).resume()
    stored = int(when(4, 2))
    await db.write(
        lambda conn: conn.execute(
            "UPDATE schedules SET next_due_at = ? WHERE source = 'gr_csv'", (stored,)
        )
    )
    for _ in range(3):  # three restarts in a row change nothing
        s = scheduler(db, sync, clock)
        await s.resume()
        assert s.due("alice", CSV) == stored + JITTER
        assert (await csv_schedule(db))[2] == stored


async def test_an_overdue_csv_loop_runs_once_not_once_per_interval_missed(
    db: Database, clock: FakeClock
) -> None:
    jump(clock, when(0, 13))
    sync = csv_sync(clock, ["alice"])
    await scheduler(db, sync, clock).resume()
    clock.advance(60 * DAY)  # two months down: twelve exports missed
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    await s.tick()
    assert len(sync.csv_calls) == 1
    await s.tick()  # and not again: it is now five days out, not twelve exports behind
    assert len(sync.csv_calls) == 1


async def test_a_successful_export_is_due_five_days_on_in_the_window(
    db: Database, clock: FakeClock
) -> None:
    jump(clock, when(0, 2))
    sync = csv_sync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    await s.tick()
    now = int(clock.time())
    last_run, last_success, next_due, last_result, *_ = await csv_schedule(db)
    assert (last_run, last_success, last_result) == (now, now, "ok")
    # Five days on is 02:00:07 on day 5, still inside that morning's window.
    assert next_due == int(when(5, 2))
    assert s.due("alice", CSV) == next_due + JITTER


async def test_five_days_landing_past_the_window_waits_for_the_next_night(
    db: Database, clock: FakeClock
) -> None:
    jump(clock, when(0, 6))  # after the window closed, so this export is a nudge-like one
    sync = csv_sync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    await s.run_now("alice", "sync_now", CSV)
    while not (await csv_schedule(db))[0]:
        await asyncio.sleep(0)
    # 06:00 plus five days is past that morning's window, so it is the night after.
    assert (await csv_schedule(db))[2] == int(when(6, 2))


@pytest.mark.parametrize("outcome", [("fetch_failed", False), ("ok", False)])
async def test_a_failed_export_waits_for_the_next_window_and_nothing_sooner(
    db: Database, clock: FakeClock, outcome: tuple[RunState, bool]
) -> None:
    jump(clock, when(0, 2, 30))  # inside tonight's window
    sync = csv_sync(clock, ["alice"])
    sync.csv_outcomes = [outcome]
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    await s.tick()
    _, last_success, next_due, *_ = await csv_schedule(db)
    assert last_success is None
    # Tomorrow's window: not a backoff curve, and not the rest of tonight's (§3.4b.5).
    assert next_due == int(when(1, 2))


async def test_the_csv_loop_has_no_backoff_curve(db: Database, clock: FakeClock) -> None:
    # Four failures in a row: still the next night window each time, never 1 h then 2 h.
    jump(clock, when(0, 2, 30))
    sync = csv_sync(clock, ["alice"])
    sync.csv_outcomes = [("fetch_failed", False)] * 4
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)  # already inside tonight's window, so the first export is due now
    for day in range(1, 5):
        await s.tick()
        assert (await csv_schedule(db))[2] == int(when(day, 2))
        jump(clock, s.due("alice", CSV) or 0)


async def test_a_csv_tick_finding_the_lock_held_skips_and_stores_the_wait(
    db: Database, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    # An RSS run holding the lock is the ordinary case: one Goodreads run per profile (§2.2).
    jump(clock, when(0, 2))
    sync = csv_sync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    clock.advance(JITTER)
    async with sync.lock("alice"):
        with caplog.at_level(logging.INFO, logger="syncshelf.sync.scheduler"):
            assert await s.tick() == []
    assert sync.csv_calls == []
    skipped = [r for r in caplog.records if r.__dict__.get("event") == "tick_skipped"]
    assert sorted(r.__dict__["source"] for r in skipped) == ["gr_csv", "gr_rss"]
    assert (await csv_schedule(db))[2] == int(when(1, 2))


async def test_a_csv_run_asked_for_now_runs_the_export(db: Database, clock: FakeClock) -> None:
    # This is sync_now(full=True): the export, not the feed.
    jump(clock, when(0, 13))
    sync = csv_sync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    _, _, started, ran = await s.run_now("alice", "sync_now", CSV)
    assert (started, ran) == (True, "gr_csv")
    while not (await csv_schedule(db))[0]:
        await asyncio.sleep(0)
    assert sync.csv_calls == [("alice", "sync_now", clock.time())]
    assert (await csv_schedule(db))[3] == "ok"
    # It moved the CSV loop on five days and left the RSS loop where it was.
    assert s.due("alice", CSV) == int(when(6, 2)) + JITTER
    assert s.due("alice") == when(0, 13) + JITTER


async def test_an_export_asked_for_now_reports_an_rss_run_already_going(
    db: Database, clock: FakeClock
) -> None:
    # The sources share the profile's lock, so what is going may be the other source's run.
    jump(clock, when(0, 13))
    sync = csv_sync(clock, ["alice"])
    s = scheduler(db, sync, clock)
    await s.resume()
    going = await db.write(runs.start, "alice", "gr_rss", "schedule", int(clock.time()))
    async with sync.lock("alice"):
        run_id, _, started, ran = await s.run_now("alice", "sync_now", CSV)
    # It names the RSS run that is going, not the export that was asked for.
    assert (run_id, started, ran) == (going, False, "gr_rss")
    assert sync.csv_calls == []


class TestADatedUploadCountingAsASuccess:
    """``csv_uploaded`` (§3.4a.8, §3.4b.5): the household has a fresh export either way."""

    async def test_it_pushes_the_due_date_five_days_from_the_export(
        self, db: Database, clock: FakeClock
    ) -> None:
        jump(clock, when(0, 13))
        s = scheduler(db, csv_sync(clock, ["alice"]), clock)
        await s.resume()
        generated = int(when(0, 9))  # made this morning, uploaded now
        await s.csv_uploaded("alice", generated)
        _, last_success, next_due, *_ = await csv_schedule(db)
        assert last_success == generated
        assert next_due == int(when(6, 2))  # 09:00 on day 5 is past the window: day 6
        assert s.due("alice", CSV) == next_due + JITTER

    async def test_it_leaves_the_last_real_run_alone(self, db: Database, clock: FakeClock) -> None:
        # No CSV run happened, so overwriting last_result would hide the last export's outcome.
        jump(clock, when(0, 2))
        sync = csv_sync(clock, ["alice"])
        sync.csv_outcomes = [("fetch_failed", False)]
        s = scheduler(db, sync, clock)
        await s.resume()
        clock.advance(JITTER)
        await s.tick()
        ran_at = (await csv_schedule(db))[0]
        await s.csv_uploaded("alice", int(clock.time()))
        last_run, _, _, last_result, *_ = await csv_schedule(db)
        assert (last_run, last_result) == (ran_at, "fetch_failed")

    async def test_an_export_older_than_the_last_success_moves_nothing_back(
        self, db: Database, clock: FakeClock
    ) -> None:
        jump(clock, when(10, 2))
        s = scheduler(db, csv_sync(clock, ["alice"]), clock)
        await s.resume()
        clock.advance(JITTER)
        await s.tick()
        fresh = (await csv_schedule(db))[1]
        await s.csv_uploaded("alice", int(when(0, 9)))  # a ten-day-old export
        assert (await csv_schedule(db))[1] == fresh

    async def test_a_profile_with_no_csv_loop_is_left_alone(
        self, db: Database, clock: FakeClock
    ) -> None:
        s = scheduler(db, FakeSync(clock, ["alice"]), clock)
        await s.resume()
        await s.csv_uploaded("alice", int(clock.time()))  # no row to write, and no error
        assert await csv_schedule(db) is None
