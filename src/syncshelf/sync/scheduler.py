"""The sync loops: when each profile's sources run (SPEC §3.4a.9).

Each profile with a Goodreads section has one RSS loop, every ``cadence.rss_interval``. The
loops are plain asyncio, supervised by :meth:`Scheduler.run_forever`, which runs whichever
loop is due, one at a time, so two profiles' Goodreads traffic (from one IP) never overlaps,
then sleeps until the next is due or a run is asked for.

**Startup resumes, never bursts.** Each loop's next run comes from the ``schedules`` table,
plus up to :data:`JITTER` seconds. A loop overdue after downtime runs once, never once per
missed interval, and the profiles are staggered half an interval apart, as they are when
first scheduled, so their traffic never lines up.

**Backoff** follows a failed fetch only (§3.4a.7): an RSS fetch that failed, or came back
incomplete, waits 1 h, then 2 h, then 4 h, capped at 6 h, and the first success resets it. A
``deferred`` run (Hardcover unavailable or the daily reserve) gets none: the next scheduled
run tries again.

A tick that finds the profile's sync lock held (a ``sync_now`` run, say) skips and logs it.
A run never kills its loop: the engine records any failure on the run's row.
"""

import asyncio
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from syncshelf.config import ProfileConfig
from syncshelf.db import Database
from syncshelf.sync.goodreads import Clock, GoodreadsSync
from syncshelf.sync.runs import RunResult, Trigger

__all__ = [
    "BACKOFF_CAP",
    "BACKOFF_FIRST",
    "JITTER",
    "Scheduler",
    "backoff",
    "purge_runs",
]

log = logging.getLogger(__name__)

SOURCE = "gr_rss"
JITTER = 60.0
"""Up to this many seconds are added to each loop's next run (§3.4a.9)."""
BACKOFF_FIRST = 3600
"""The first wait after a failed Goodreads fetch (seconds), doubling each time..."""
BACKOFF_CAP = 6 * 3600
"""...up to this (§3.4a.7)."""
NUDGE_RETRY = 5.0
"""Seconds before a run asked for tries again, when a run was already going."""
RUNS_KEPT = 90 * 86400
"""Runs older than this are purged; their change log rows are kept (§2.4)."""


def backoff(consecutive_failures: int) -> int:
    """The wait after this many fetch failures in a row: 1 h, 2 h, 4 h, then 6 h."""
    return min(BACKOFF_FIRST * 2 ** (consecutive_failures - 1), BACKOFF_CAP)


@dataclass
class _Loop:
    profile: str
    due: float
    """When it next runs (epoch seconds), jitter included."""
    trigger: Trigger = "schedule"
    nudged: float | None = None
    """When a run was last asked for and not yet started: it outlives a run in progress."""


class Scheduler:
    """Every profile's sync loops. ``jitter`` returns the seconds added to each next run;
    tests pass a fixed one. The daily housekeeping runs beside it, through :func:`every`."""

    def __init__(
        self,
        *,
        db: Database,
        sync: GoodreadsSync,
        profiles: Mapping[str, ProfileConfig],
        interval: int,
        clock: Clock,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._db = db
        self._sync = sync
        self._interval = interval
        self._clock = clock
        self._jitter = jitter or (lambda: random.uniform(0, JITTER))  # noqa: S311
        self._profiles = sorted(p for p in profiles if sync.enabled(p))
        self._loops: dict[str, _Loop] = {}
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    async def resume(self) -> None:
        """Load each loop's next run from ``schedules`` (§3.4a.9), creating the rows of loops
        that have none: those run soon, staggered half an interval apart."""
        now = self._clock.time()
        rows = await self._db.read(_schedules)
        for i, profile in enumerate(self._profiles):
            stagger = i * self._interval / 2
            due = rows.get(profile)
            if due is None:
                due = now + stagger
                await self._db.write(_create, profile, int(due))
            elif due <= now:
                due = now + stagger  # overdue: once, staggered, never a catch-up burst
            self._loops[profile] = _Loop(profile, due + self._jitter())

    def nudge(self, profile: str, trigger: Trigger) -> None:
        """Run the profile's loop as soon as it can (``/ingest``, ``sync_now``)."""
        loop = self._loops.get(profile)
        if loop is None:
            return
        now = self._clock.time()
        loop.due, loop.trigger, loop.nudged = now, trigger, now
        self._wake.set()

    def enabled(self, profile: str) -> bool:
        """Whether the profile has a sync loop."""
        return profile in self._loops

    def running(self, profile: str) -> tuple[int, int] | None:
        """The profile's run in progress, as (run id, when it started), if one is."""
        return self._sync.running(profile)

    async def run_now(self, profile: str, trigger: Trigger) -> tuple[int, int, bool]:
        """Start a run of the profile's source now, as a task the scheduler holds (not tied
        to the request that asked), and return (run id, when it started, True) once it has
        an id (§3.4a.9). If a run is already going, return its (id, start, False) instead:
        the lock is taken here, at once, so a tick or a second call can't slip in between.
        The run is recorded on the loop's schedule like any other."""
        lock = self._sync.lock(profile)
        if lock.locked():
            return (*await self._going(profile), False)
        await lock.acquire()  # free and unwaited, so taken without yielding
        started: asyncio.Future[tuple[int, int]] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run_now(profile, trigger, started, lock))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return (*await started, True)

    async def _going(self, profile: str) -> tuple[int, int]:
        """The run in progress: the engine's, or, in the moment before it has written its
        row, the latest one that SQLite says is running."""
        while (going := self._sync.running(profile)) is None:
            found = await self._db.read(_latest_running, profile)
            if found is not None:
                return found
            await asyncio.sleep(0)
        return going

    async def _run_now(
        self,
        profile: str,
        trigger: Trigger,
        started: asyncio.Future[tuple[int, int]],
        lock: asyncio.Lock,
    ) -> None:
        def told(run_id: int, at: int) -> None:
            if not started.done():
                started.set_result((run_id, at))

        try:
            result = await self._sync.run_rss(profile, trigger, told, held=True)
            next_due = await self._db.write(
                _record, profile, result, int(self._clock.time()), self._interval
            )
            loop = self._loops.get(profile)
            if loop is not None and loop.nudged is None:  # a run asked for meanwhile stands
                loop.due = next_due + self._jitter()
        except Exception as exc:
            if not started.done():
                started.set_exception(exc)
            log.exception(
                "a Goodreads run asked for now failed",
                extra={"event": "run_now_failed", "profile": profile},
            )
        finally:
            lock.release()
            if not started.done():  # cancelled before the run had an id
                started.cancel()

    async def close(self) -> None:
        """Cancel the runs asked for now that are still going, and wait for them to unwind,
        so they close their run rows before the database goes (at shutdown)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self) -> list[RunResult]:
        """Run every loop that is due now, one at a time, and schedule each one's next run."""
        done: list[RunResult] = []
        for loop in sorted(self._loops.values(), key=lambda lp: lp.due):
            if loop.due > self._clock.time():
                continue
            if self._sync.lock(loop.profile).locked():
                await self._skip(loop)
                continue
            started = self._clock.time()
            trigger, loop.trigger, loop.nudged = loop.trigger, "schedule", None
            # Moved on before the run, so a run or record that fails can't leave the loop
            # due, fetching again every second.
            loop.due = started + self._interval + self._jitter()
            try:
                result = await self._sync.run_rss(loop.profile, trigger)
                next_due = await self._db.write(
                    _record, loop.profile, result, int(self._clock.time()), self._interval
                )
            except Exception:
                log.exception(
                    "a scheduled Goodreads run failed",
                    extra={"event": "tick_failed", "profile": loop.profile, "source": SOURCE},
                )
                continue
            if loop.nudged is None:  # a run asked for meanwhile still runs now
                loop.due = next_due + self._jitter()
            done.append(result)
        return done

    async def _skip(self, loop: _Loop) -> None:
        """A tick that finds a run already going (§3.4a.9). A scheduled run waits an interval,
        stored so a restart agrees; one asked for waits only for the run going to end."""
        log.info(
            "a scheduled Goodreads run was skipped: one is already running",
            extra={"event": "tick_skipped", "profile": loop.profile, "source": SOURCE},
        )
        now = self._clock.time()
        if loop.nudged is not None:
            loop.due = now + NUDGE_RETRY
            return
        loop.due = now + self._interval + self._jitter()
        await self._db.write(_reschedule, loop.profile, int(now + self._interval))

    async def run_forever(self) -> None:
        """Run the loops until cancelled: each tick, then sleep until the next is due or a
        run is asked for. One tick failing never stops the loops."""
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("a scheduled Goodreads tick failed", extra={"event": "tick_failed"})
            wait = min((lp.due for lp in self._loops.values()), default=None)
            await self._sleep(None if wait is None else max(wait - self._clock.time(), 1.0))

    async def _sleep(self, seconds: float | None) -> None:
        self._wake.clear()
        waits: list[asyncio.Task[Any]] = [asyncio.create_task(self._wake.wait())]
        if seconds is not None:
            waits.append(asyncio.create_task(self._clock.sleep(seconds)))
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

    def due(self, profile: str) -> float | None:
        """When the profile's loop next runs (epoch seconds), if it has one."""
        loop = self._loops.get(profile)
        return loop.due if loop is not None else None


async def every(interval: float, job: Callable[[], Awaitable[None]]) -> None:
    """Run ``job`` every ``interval`` seconds of real time until cancelled (the daily
    housekeeping). Real time, not the injectable clock: a test clock's sleep moves time on,
    which would run it without pause."""
    while True:
        await asyncio.sleep(interval)
        await job()


# --- the database side --------------------------------------------------------------------


def _schedules(conn: sqlite3.Connection) -> dict[str, float]:
    return dict(
        conn.execute("SELECT profile, next_due_at FROM schedules WHERE source = ?", (SOURCE,))
    )


def _create(conn: sqlite3.Connection, profile: str, due: int) -> None:
    conn.execute(
        "INSERT INTO schedules (profile, source, next_due_at) VALUES (?, ?, ?) "
        "ON CONFLICT (profile, source) DO NOTHING",
        (profile, SOURCE, due),
    )


def _latest_running(conn: sqlite3.Connection, profile: str) -> tuple[int, int] | None:
    found = conn.execute(
        "SELECT id, started_at FROM runs WHERE profile = ? AND source = ? AND state = 'running' "
        "ORDER BY id DESC LIMIT 1",
        (profile, SOURCE),
    ).fetchone()
    return (found[0], found[1]) if found else None


def _reschedule(conn: sqlite3.Connection, profile: str, due: int) -> None:
    conn.execute(
        "UPDATE schedules SET next_due_at = ? WHERE profile = ? AND source = ?",
        (due, profile, SOURCE),
    )


def _record(
    conn: sqlite3.Connection, profile: str, result: RunResult, now: int, interval: int
) -> int:
    """Record a run on the loop's schedule and return its next run (§3.4a.7, §3.4a.9). A
    fetch that failed or came back incomplete backs off. A deferred run, or one that died
    (``aborted``), says nothing about Goodreads: the backoff stands as it was. Any other run
    resets it."""
    found = conn.execute(
        "SELECT consecutive_failures, backoff_until FROM schedules "
        "WHERE profile = ? AND source = ?",
        (profile, SOURCE),
    ).fetchone()
    failures, backoff_until = found if found else (0, None)
    if result.state in ("deferred", "aborted"):
        pass  # the next scheduled run tries again, any backoff still in force
    elif result.state == "fetch_failed" or not result.fetched:
        failures += 1
        backoff_until = now + backoff(failures)
    else:
        failures, backoff_until = 0, None
    next_due = max(now + interval, backoff_until or 0)
    succeeded = result.state in ("ok", "partial") and result.fetched
    conn.execute(
        "INSERT INTO schedules (profile, source, last_run_at, last_success_at, "
        "last_observed_at, next_due_at, last_result, consecutive_failures, backoff_until) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (profile, source) DO UPDATE SET "
        "last_run_at = excluded.last_run_at, "
        "last_success_at = COALESCE(excluded.last_success_at, schedules.last_success_at), "
        "last_observed_at = COALESCE(excluded.last_observed_at, schedules.last_observed_at), "
        "next_due_at = excluded.next_due_at, last_result = excluded.last_result, "
        "consecutive_failures = excluded.consecutive_failures, "
        "backoff_until = excluded.backoff_until",
        (
            profile,
            SOURCE,
            now,
            now if succeeded else None,
            result.observed_at,
            next_due,
            result.state,
            failures,
            backoff_until,
        ),
    )
    return next_due


def purge_runs(conn: sqlite3.Connection, now: int) -> int:
    """Delete runs older than 90 days (§2.4); their ``change_log`` rows keep ``run_id`` NULL.
    Returns how many went."""
    return conn.execute("DELETE FROM runs WHERE started_at < ?", (now - RUNS_KEPT,)).rowcount
