"""The run log (SPEC §3.4a.7): one ``runs`` row per execution of one source loop.

A run's row is written ``running`` when it starts and ends as one of:

- ``ok``: fetched and applied, possibly nothing;
- ``partial``: the fetch was incomplete, or some changes failed;
- ``fetch_failed``: nothing usable was fetched;
- ``deferred``: the Hardcover fetch failed or the daily reserve was hit, so nothing applied;
- ``aborted``: the run never finished. Startup marks every row still ``running`` this way,
  since only a dead process leaves one behind.

Each unit here is a plain function on the writer connection, run inside its transaction.
"""

import sqlite3
from dataclasses import dataclass
from typing import Literal

__all__ = ["RunCounts", "RunSource", "RunState", "Trigger", "abort_running", "finish", "start"]

RunSource = Literal["gr_rss", "gr_csv", "abs"]
"""The sources that write runs. ``mirror`` and ``chaptarr_watch`` are reserved (§3.4a.7)."""
Trigger = Literal["schedule", "sync_now", "ingest", "startup"]
RunState = Literal["ok", "partial", "fetch_failed", "deferred", "aborted"]


@dataclass
class RunCounts:
    """What a run did, recorded on its row when it ends."""

    applied: int = 0
    failed: int = 0
    quarantined: int = 0
    first_error: str | None = None
    """The first error seen, already host-aliased and secret-scrubbed (§2.7)."""

    def error(self, message: str) -> None:
        """Keep ``message`` unless an earlier error was already kept."""
        if self.first_error is None:
            self.first_error = message


def start(
    conn: sqlite3.Connection, profile: str, source: RunSource, trigger: Trigger, now: int
) -> int:
    """Open a ``running`` row and return its id."""
    cursor = conn.execute(
        "INSERT INTO runs (profile, source, trigger, started_at, state) "
        "VALUES (?, ?, ?, ?, 'running')",
        (profile, source, trigger, now),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def finish(
    conn: sqlite3.Connection, run_id: int, state: RunState, counts: RunCounts, now: int
) -> None:
    """Close a run with its outcome and counts."""
    conn.execute(
        "UPDATE runs SET state = ?, ended_at = ?, applied = ?, failed = ?, quarantined = ?, "
        "first_error = ? WHERE id = ?",
        (
            state,
            now,
            counts.applied,
            counts.failed,
            counts.quarantined,
            counts.first_error,
            run_id,
        ),
    )


def abort_running(conn: sqlite3.Connection, now: int) -> int:
    """Mark every run still ``running`` as ``aborted`` (§2.2 lifespan step 4); returns how
    many there were."""
    return conn.execute(
        "UPDATE runs SET state = 'aborted', ended_at = ? WHERE state = 'running'", (now,)
    ).rowcount
