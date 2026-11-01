"""The sync loops: when each profile's sources run (SPEC §3.4a.9, §3.4b.5).

Each profile with a Goodreads section has one **RSS** loop, every ``cadence.rss_interval``,
and — when it also has credentials and a sidecar — one **CSV** loop, which is due five days
after its last CSV success and runs in the next 02:00–05:00 window in the profile's own
timezone (§3.4b.5). The loops are plain asyncio, supervised by :meth:`Scheduler.run_forever`,
which runs whichever loop is due, one at a time, so two profiles' Goodreads traffic (from one
IP) never overlaps, then sleeps until the next is due or a run is asked for.

**The two sources share a profile's lock,** so a CSV run and an RSS run of the same profile
never overlap either: a tick that finds the lock held skips (§2.2).

**Startup resumes, never bursts.** Each loop's next run comes from the ``schedules`` table,
plus up to :data:`JITTER` seconds. A loop overdue after downtime runs once, never once per
missed interval, and the profiles are staggered half an interval apart, as they are when
first scheduled, so their traffic never lines up.

**Backoff** follows a failed RSS fetch only (§3.4a.7): a fetch that failed, or came back
incomplete, waits 1 h, then 2 h, then 4 h, capped at 6 h, and the first success resets it. A
``deferred`` run (Hardcover unavailable or the daily reserve) gets none: the next scheduled
run tries again. **The CSV loop has no backoff curve**: the window is already a once-a-night
pace, so a failed export waits for the next window and nothing sooner (§3.4b.5).

**A dated ``/ingest`` upload counts as a CSV success** and pushes the due date out
(:meth:`Scheduler.csv_uploaded`), because the household now has a fresh export whoever
fetched it. An undated one does not, since it only fills (§3.4a.8).

A tick that finds the profile's sync lock held (a ``sync_now`` run, say) skips and logs it.
A run never kills its loop: the engine records any failure on the run's row.
"""

import asyncio
import logging
import random
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Protocol

from syncshelf.config import ProfileConfig
from syncshelf.db import Database
from syncshelf.sync.csv_schedule import due_after_failure, due_after_success, next_window_start
from syncshelf.sync.goodreads import Clock, GoodreadsSync
from syncshelf.sync.runs import RunResult, RunSource, Trigger

__all__ = [
    "BACKOFF_CAP",
    "BACKOFF_FIRST",
    "CSV",
    "JITTER",
    "RSS",
    "Scheduler",
    "backoff",
    "purge_runs",
]

log = logging.getLogger(__name__)

RSS: RunSource = "gr_rss"
CSV: RunSource = "gr_csv"
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


class _Run(Protocol):
    """What the scheduler needs of an engine run method: both sources have this shape."""

    async def __call__(
        self,
        profile: str,
        trigger: Trigger = "schedule",
        started: Callable[[int, int], None] | None = None,
        *,
        held: bool = False,
    ) -> RunResult: ...


@dataclass
class _Loop:
    profile: str
    source: RunSource
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
        interval_days: int = 5,
        window: tuple[time, time] = (time(2, 0), time(5, 0)),
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._db = db
        self._sync = sync
        self._interval = interval
        self._clock = clock
        self._interval_days = interval_days
        self._window = window
        self._jitter = jitter or (lambda: random.uniform(0, JITTER))  # noqa: S311
        self._config = profiles
        self._profiles = sorted(p for p in profiles if sync.enabled(p))
        self._csv_profiles = [p for p in self._profiles if sync.csv_enabled(p)]
        self._loops: dict[tuple[str, RunSource], _Loop] = {}
        self._wake = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    async def resume(self) -> None:
        """Load every loop's next run from ``schedules`` (§3.4a.9, §3.4b.5), creating the rows
        of loops that have none."""
        await self._resume_rss()
        await self._resume_csv()

    async def _resume_rss(self) -> None:
        """The RSS loops: a loop with no row runs soon, staggered half an interval apart, and
        one overdue after downtime runs once rather than once per missed interval."""
        now = self._clock.time()
        rows = await self._db.read(_schedules, RSS)
        for i, profile in enumerate(self._profiles):
            stagger = i * self._interval / 2
            due = rows.get(profile)
            if due is None:
                due = now + stagger
                await self._db.write(_create, profile, RSS, int(due))
            elif due <= now:
                due = now + stagger  # overdue: once, staggered, never a catch-up burst
            self._loops[profile, RSS] = _Loop(profile, RSS, due + self._jitter())

    async def _resume_csv(self) -> None:
        """The CSV loops. A loop with no row is due at once, so it runs in the next night
        window; the stored due time is kept as it stands, which is what makes a restart pick
        up where it left off rather than starting the five days again (§3.4b.5).

        The profiles are spread across the window so their exports — the slowest Goodreads
        call SyncShelf makes — don't start at the same minute.
        """
        now = self._clock.time()
        rows = await self._db.read(_schedules, CSV)
        for i, profile in enumerate(self._csv_profiles):
            due = rows.get(profile)
            if due is None:
                due = self._next_window(profile, now) + self._stagger(i)
                await self._db.write(_create, profile, CSV, int(due))
            self._loops[profile, CSV] = _Loop(profile, CSV, due + self._jitter())

    def _stagger(self, i: int) -> float:
        """How far into the window profile ``i``'s export starts."""
        start, end = self._window
        span = (end.hour - start.hour) * 3600 + (end.minute - start.minute) * 60
        return i * span / max(len(self._csv_profiles), 1)

    def _next_window(self, profile: str, now: float) -> float:
        """The start of the next night window in ``profile``'s timezone."""
        zone = self._config[profile].tzinfo()
        return next_window_start(datetime.fromtimestamp(now, zone), self._window).timestamp()

    def nudge(self, profile: str, trigger: Trigger, source: RunSource = RSS) -> None:
        """Run the profile's loop as soon as it can (``/ingest``, ``sync_now``)."""
        loop = self._loops.get((profile, source))
        if loop is None:
            return
        now = self._clock.time()
        loop.due, loop.trigger, loop.nudged = now, trigger, now
        self._wake.set()

    @property
    def sync(self) -> GoodreadsSync:
        """The engine the loops run."""
        return self._sync

    def enabled(self, profile: str) -> bool:
        """Whether the profile has an RSS loop."""
        return (profile, RSS) in self._loops

    def csv_enabled(self, profile: str) -> bool:
        """Whether the profile has a CSV loop (§3.4b.5)."""
        return (profile, CSV) in self._loops

    def running(self, profile: str) -> tuple[int, int] | None:
        """The profile's run in progress, as (run id, when it started), if one is."""
        return self._sync.running(profile)

    async def run_now(
        self, profile: str, trigger: Trigger, source: RunSource = RSS
    ) -> tuple[int, int, bool, RunSource]:
        """Start a run of ``source`` for the profile now, as a task the scheduler holds (not
        tied to the request that asked), and return (run id, when it started, True, source)
        once it has an id (§3.4a.9).

        If a run is already going, return its (id, start, False, **its own** source) instead:
        the lock is taken here, at once, so a tick or a second call can't slip in between.
        Both sources share the lock, so the run going may well be the other source's, and
        saying which it is keeps the answer from naming a source that isn't running.

        The run is recorded on the loop's schedule like any other.
        """
        lock = self._sync.lock(profile)
        if lock.locked():
            run_id, since, going = await self._going(profile)
            return run_id, since, False, going
        await lock.acquire()  # free and unwaited, so taken without yielding
        started: asyncio.Future[tuple[int, int]] = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run_now(profile, source, trigger, started, lock))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return (*await started, True, source)

    async def _going(self, profile: str) -> tuple[int, int, RunSource]:
        """The run in progress and which source it is.

        A run holds the lock for a moment before its row exists, so this waits for the row
        rather than guessing: which source is running is the part the caller can't infer.
        """
        while True:
            for source in (RSS, CSV):
                found = await self._db.read(_latest_running, profile, source)
                if found is not None:
                    return (*found, source)
            await asyncio.sleep(0)

    async def _run_now(
        self,
        profile: str,
        source: RunSource,
        trigger: Trigger,
        started: asyncio.Future[tuple[int, int]],
        lock: asyncio.Lock,
    ) -> None:
        def told(run_id: int, at: int) -> None:
            if not started.done():
                started.set_result((run_id, at))

        try:
            result = await self._source(source)(profile, trigger, told, held=True)
            next_due = await self._db.write(
                _record,
                profile,
                source,
                result,
                int(self._clock.time()),
                self._due(profile, source),
            )
            loop = self._loops.get((profile, source))
            if loop is not None and loop.nudged is None:  # a run asked for meanwhile stands
                loop.due = next_due + self._jitter()
        except Exception as exc:
            if not started.done():
                started.set_exception(exc)
            log.exception(
                "a Goodreads run asked for now failed",
                extra={"event": "run_now_failed", "profile": profile, "source": source},
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
            trigger, loop.trigger, loop.nudged = loop.trigger, "schedule", None
            # Moved on before the run, so a run or record that fails can't leave the loop
            # due, fetching again every second.
            loop.due = self._due(loop.profile, loop.source)(False) + self._jitter()
            try:
                result = await self._source(loop.source)(loop.profile, trigger)
                next_due = await self._db.write(
                    _record,
                    loop.profile,
                    loop.source,
                    result,
                    int(self._clock.time()),
                    self._due(loop.profile, loop.source),
                )
            except Exception:
                log.exception(
                    "a scheduled Goodreads run failed",
                    extra={
                        "event": "tick_failed",
                        "profile": loop.profile,
                        "source": loop.source,
                    },
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
            extra={"event": "tick_skipped", "profile": loop.profile, "source": loop.source},
        )
        now = self._clock.time()
        if loop.nudged is not None:
            loop.due = now + NUDGE_RETRY
            return
        next_due = self._due(loop.profile, loop.source)(False)
        loop.due = next_due + self._jitter()
        await self._db.write(_reschedule, loop.profile, loop.source, next_due)

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

    def due(self, profile: str, source: RunSource = RSS) -> float | None:
        """When the profile's loop next runs (epoch seconds), if it has one."""
        loop = self._loops.get((profile, source))
        return loop.due if loop is not None else None

    def _source(self, source: RunSource) -> _Run:
        """The engine method that runs ``source``."""
        return self._sync.run_csv if source == CSV else self._sync.run_rss

    def _due(self, profile: str, source: RunSource = RSS) -> Callable[[bool], int]:
        """When ``source`` next runs for ``profile``, as a function of whether the run that
        just finished succeeded. RSS is a plain interval; CSV is §3.4b.5's two rules, where
        a success waits five days and a failure only for the next night window."""
        now = int(self._clock.time())
        if source == RSS:
            return lambda _: now + self._interval
        zone = self._config[profile].tzinfo()
        return lambda succeeded: (
            due_after_success(now, zone, interval_days=self._interval_days, window=self._window)
            if succeeded
            else due_after_failure(now, zone, window=self._window)
        )

    async def csv_uploaded(self, profile: str, at: int) -> None:
        """A dated ``/ingest`` upload counts as a CSV success (§3.4a.8, §3.4b.5).

        The household now has a fresh export whoever fetched it, so the due date moves out
        five days from ``at`` — the export's own generation time, not when it was uploaded, so
        uploading an old export doesn't buy five quiet days. An undated upload never reaches
        here: it only fills.
        """
        if not self.csv_enabled(profile):
            return
        zone = self._config[profile].tzinfo()
        next_due = due_after_success(
            at, zone, interval_days=self._interval_days, window=self._window
        )
        await self._db.write(_uploaded, profile, at, next_due)
        self._loops[profile, CSV].due = next_due + self._jitter()
        log.info(
            "an uploaded export counts as a CSV success",
            extra={
                "event": "csv_success_from_upload",
                "profile": profile,
                "source": CSV,
                "next_due_at": next_due,
            },
        )


async def every(interval: float, job: Callable[[], Awaitable[None]]) -> None:
    """Run ``job`` every ``interval`` seconds of real time until cancelled (the daily
    housekeeping). Real time, not the injectable clock: a test clock's sleep moves time on,
    which would run it without pause."""
    while True:
        await asyncio.sleep(interval)
        await job()


# --- the database side --------------------------------------------------------------------


def _schedules(conn: sqlite3.Connection, source: RunSource) -> dict[str, float]:
    return dict(
        conn.execute("SELECT profile, next_due_at FROM schedules WHERE source = ?", (source,))
    )


def _create(conn: sqlite3.Connection, profile: str, source: RunSource, due: int) -> None:
    conn.execute(
        "INSERT INTO schedules (profile, source, next_due_at) VALUES (?, ?, ?) "
        "ON CONFLICT (profile, source) DO NOTHING",
        (profile, source, due),
    )


def _latest_running(
    conn: sqlite3.Connection, profile: str, source: RunSource
) -> tuple[int, int] | None:
    found = conn.execute(
        "SELECT id, started_at FROM runs WHERE profile = ? AND source = ? AND state = 'running' "
        "ORDER BY id DESC LIMIT 1",
        (profile, source),
    ).fetchone()
    return (found[0], found[1]) if found else None


def _uploaded(conn: sqlite3.Connection, profile: str, at: int, due: int) -> None:
    """Record a dated upload as the CSV source's latest success (§3.4b.5).

    ``last_run_at`` and ``last_result`` are left alone: no CSV *run* happened, and overwriting
    them would hide the last real export's outcome. The success times only ever move forward,
    so uploading an export older than the last one changes nothing.
    """
    conn.execute(
        "INSERT INTO schedules (profile, source, last_success_at, last_observed_at, next_due_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT (profile, source) DO UPDATE SET "
        "last_success_at = MAX(COALESCE(schedules.last_success_at, 0), excluded.last_success_at), "
        "last_observed_at = MAX(COALESCE(schedules.last_observed_at, 0), "
        "excluded.last_observed_at), next_due_at = excluded.next_due_at",
        (profile, CSV, at, at, due),
    )


def _reschedule(conn: sqlite3.Connection, profile: str, source: RunSource, due: int) -> None:
    conn.execute(
        "UPDATE schedules SET next_due_at = ? WHERE profile = ? AND source = ?",
        (due, profile, source),
    )


def _record(
    conn: sqlite3.Connection,
    profile: str,
    source: RunSource,
    result: RunResult,
    now: int,
    due: Callable[[bool], int],
) -> int:
    """Record a run on the loop's schedule and return its next run (§3.4a.7, §3.4a.9).

    ``due`` says when this source next runs, given whether this run succeeded; the backoff
    columns are the RSS loop's and only it widens ``next_due_at`` with them (§3.4b.5).

    A fetch that failed or came back incomplete backs off. A deferred run, or one that died
    (``aborted``), says nothing about Goodreads: the backoff stands as it was. Any other run
    resets it.
    """
    found = conn.execute(
        "SELECT consecutive_failures, backoff_until FROM schedules "
        "WHERE profile = ? AND source = ?",
        (profile, source),
    ).fetchone()
    failures, backoff_until = found if found else (0, None)
    if result.state in ("deferred", "aborted"):
        pass  # the next scheduled run tries again, any backoff still in force
    elif result.state == "fetch_failed" or not result.fetched:
        failures += 1
        backoff_until = now + backoff(failures)
    else:
        failures, backoff_until = 0, None
    succeeded = result.state in ("ok", "partial") and result.fetched
    next_due = due(succeeded)
    if source == RSS:
        next_due = max(next_due, backoff_until or 0)
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
            source,
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
