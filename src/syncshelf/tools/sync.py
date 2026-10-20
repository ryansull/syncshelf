"""``sync_status``: how a profile's syncs are doing (SPEC §3.4a.10).

The caller's own profile in full detail: each source's last result, schedule and backoff,
its backfill, its recent runs; the mirror; the Hardcover limiter; the quarantine, with what
each stuck change would do; the unmatched queue in counts; how many books a complete feed
showed gone. Sections for phases not yet shipped are left out, not null.

Every error string here was host-aliased and secret-scrubbed before it was stored (§2.7),
and a review is shown only as its length and first 40 characters.
"""

import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal, NotRequired, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from syncshelf import __version__
from syncshelf.sync.limiter import HardcoverLimiter
from syncshelf.tools import REGISTRY, current_profile, services

__all__ = ["RECENT_RUNS", "QUARANTINE_SHOWN", "status", "sync_status"]

RECENT_RUNS = 3
"""Runs shown per source, besides every run in the last day that wasn't ``ok``..."""
RECENT_CAP = 20
"""...up to this many in all."""
QUARANTINE_SHOWN = 25
"""Quarantined changes listed, newest first; ``total`` counts them all."""
REVIEW_SHOWN = 40
"""Characters of a review shown."""
DAY = 86400

SOURCES = {"gr_rss": "goodreads"}
"""The run sources shipped so far, and the identity source each belongs to."""


class Backfill(TypedDict):
    state: Literal["pending", "done"]
    done_at: NotRequired[str]
    rows_linked: NotRequired[int]
    rows_queued: NotRequired[int]


class Run(TypedDict):
    id: int
    trigger: str
    state: str
    started_at: str
    duration_s: int | None
    applied: int
    failed: int
    quarantined: int
    first_error: str | None


class Source(TypedDict):
    enabled: bool
    last_result: str | None
    last_run_at: str | None
    last_success_at: str | None
    last_observed_at: str | None
    next_due_at: str | None
    consecutive_failures: int
    backoff_until: str | None
    backfill: Backfill
    recent_runs: list[Run]


class Stuck(TypedDict):
    change_id: int
    hc_book_id: int | None
    title: str | None
    field: str
    old: Any
    new: Any
    last_error: str | None
    error_class: str | None
    first_failed_at: str | None
    attempts: int


class Status(TypedDict):
    profile: str
    now: str
    version: str
    sources: dict[str, Source]
    mirror: dict[str, Any]
    hardcover: dict[str, Any]
    quarantine: dict[str, Any]
    unmatched: dict[str, Any]
    gone: dict[str, int]


def _iso(epoch: float | None) -> str | None:
    return None if epoch is None else datetime.fromtimestamp(epoch, UTC).isoformat()


def _shown(field: str, value: str | None) -> Any:
    """A baseline value as the status shows it: a review only as its length and start."""
    if field == "review" and value is not None:
        return {"length": len(value), "start": value[:REVIEW_SHOWN]}
    return value


def status(conn: sqlite3.Connection, profile: str, now: int, enabled: bool) -> dict[str, Any]:
    """Everything ``sync_status`` shows for ``profile`` that SQLite holds."""
    return {
        "sources": {
            run_source: _source(conn, profile, run_source, identity_source, now, enabled)
            for run_source, identity_source in SOURCES.items()
        },
        "mirror": _mirror(conn, profile),
        "quarantine": _quarantine(conn, profile),
        "unmatched": _unmatched(conn, profile),
        "gone": {
            "total": conn.execute(
                "SELECT count(*) FROM source_rows WHERE profile = ? AND gone_at IS NOT NULL",
                (profile,),
            ).fetchone()[0]
        },
    }


def _source(
    conn: sqlite3.Connection,
    profile: str,
    run_source: str,
    identity_source: str,
    now: int,
    enabled: bool,
) -> Source:
    row = conn.execute(
        "SELECT last_result, last_run_at, last_success_at, last_observed_at, next_due_at, "
        "consecutive_failures, backoff_until FROM schedules WHERE profile = ? AND source = ?",
        (profile, run_source),
    ).fetchone() or (None, None, None, None, None, 0, None)
    result, run_at, success_at, observed_at, due_at, failures, backoff_until = row
    done = conn.execute(
        "SELECT done_at, rows_linked, rows_queued FROM backfills WHERE profile = ? AND source = ?",
        (profile, identity_source),
    ).fetchone()
    backfill: Backfill = (
        Backfill(
            state="done", done_at=_iso(done[0]) or "", rows_linked=done[1], rows_queued=done[2]
        )
        if done
        else Backfill(state="pending")
    )
    return Source(
        enabled=enabled,
        last_result=result,
        last_run_at=_iso(run_at),
        last_success_at=_iso(success_at),
        last_observed_at=_iso(observed_at),
        # A source taken out of the config keeps its history but runs no more.
        next_due_at=_iso(due_at) if enabled else None,
        consecutive_failures=failures,
        backoff_until=_iso(backoff_until),
        backfill=backfill,
        recent_runs=_runs(conn, profile, run_source, now),
    )


def _runs(conn: sqlite3.Connection, profile: str, source: str, now: int) -> list[Run]:
    """The last few runs, and every one in the last day that wasn't ``ok``, newest first."""
    rows = conn.execute(
        "SELECT id, trigger, state, started_at, ended_at, applied, failed, quarantined, "
        "first_error FROM runs WHERE profile = ? AND source = ? AND (id IN (SELECT id FROM runs "
        "WHERE profile = ? AND source = ? ORDER BY started_at DESC, id DESC LIMIT ?) OR "
        "(state <> 'ok' AND started_at >= ?)) ORDER BY started_at DESC, id DESC LIMIT ?",
        (profile, source, profile, source, RECENT_RUNS, now - DAY, RECENT_CAP),
    )
    return [
        Run(
            id=run_id,
            trigger=trigger,
            state=state,
            started_at=_iso(started) or "",
            duration_s=None if ended is None else ended - started,
            applied=applied,
            failed=failed,
            quarantined=quarantined,
            first_error=error,
        )
        for run_id, trigger, state, started, ended, applied, failed, quarantined, error in rows
    ]


def _mirror(conn: sqlite3.Connection, profile: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT as_of, last_attempt_at, last_error FROM mirror_state WHERE profile = ?",
        (profile,),
    ).fetchone() or (None, None, None)
    return {"as_of": _iso(row[0]), "last_attempt_at": _iso(row[1]), "last_error": row[2]}


def _quarantine(conn: sqlite3.Connection, profile: str) -> dict[str, Any]:
    total = conn.execute(
        "SELECT count(*) FROM baseline_fields WHERE profile = ? AND quarantined_at IS NOT NULL",
        (profile,),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT b.id, r.hc_book_id, m.title, b.field, b.value, b.pending_value, b.last_error, "
        "b.last_error_class, b.first_failed_at, b.attempts FROM baseline_fields b "
        "JOIN source_rows r USING (profile, source, source_id) "
        "LEFT JOIN mirror_books m ON m.profile = b.profile AND m.hc_book_id = r.hc_book_id "
        "WHERE b.profile = ? AND b.quarantined_at IS NOT NULL "
        "ORDER BY b.quarantined_at DESC, b.id DESC "
        "LIMIT ?",
        (profile, QUARANTINE_SHOWN),
    )
    items = [
        Stuck(
            change_id=change_id,
            hc_book_id=book,
            title=title,
            field=field,
            old=_shown(field, old),
            new=_shown(field, new),
            last_error=error,
            error_class=kind,
            first_failed_at=_iso(first),
            attempts=attempts,
        )
        for change_id, book, title, field, old, new, error, kind, first, attempts in rows
    ]
    return {"total": total, "items": items}


def _unmatched(conn: sqlite3.Connection, profile: str) -> dict[str, Any]:
    reasons = dict(
        conn.execute(
            "SELECT queue_reason, count(*) FROM source_rows WHERE profile = ? AND "
            "state = 'queued' GROUP BY queue_reason ORDER BY queue_reason",
            (profile,),
        ).fetchall()
    )
    oldest = conn.execute(
        "SELECT min(queued_at) FROM source_rows WHERE profile = ? AND state = 'queued'",
        (profile,),
    ).fetchone()[0]
    return {"total": sum(reasons.values()), "by_reason": reasons, "oldest_queued_at": _iso(oldest)}


def _limiter(gate: Any) -> dict[str, Any]:
    if not isinstance(gate, HardcoverLimiter):
        return {"limiter": "ok", "paused_until": None, "daily_remaining": None}
    state = gate.state()
    return {
        "limiter": state.limiter,
        "paused_until": _iso(state.paused_until),
        "daily_remaining": state.daily_remaining,
    }


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def sync_status(profile: Literal["me", "household"] = "me") -> Status:
    """How the caller's syncs are doing: use it when the user asks whether their Goodreads
    sync is working, why a book hasn't reached Hardcover, or what's stuck.

    For the caller's own profile ("me", the default) it shows each source's last result, when
    it last ran and succeeded, when it next runs, any backoff after failed fetches, whether
    its first sync (the backfill) is done with how many books were linked and queued, and its
    recent runs with their errors. Then: how current the copy of the Hardcover library is;
    the Hardcover rate limiter (ok, paused, or holding back its daily reserve); quarantined
    changes, each with its book, field, old and new values, error and attempts (release them
    with retry_failed once the cause is fixed); the unmatched queue in counts by reason (see
    unmatched_books for the books themselves); and how many books Goodreads no longer lists.
    Times are ISO 8601 UTC. A review is shown only as its length and first 40 characters.
    """
    if profile == "household":
        raise ToolError("The household health view arrives with attention items (#171).")
    caller = current_profile()
    bound = services()
    now = int(bound.clock())
    enabled = bound.profiles[caller].goodreads is not None
    detail = await bound.db.read(status, caller, now, enabled)
    gate = bound.hardcover[caller].gate if caller in bound.hardcover else None
    return Status(
        profile=caller,
        now=_iso(now) or "",
        version=__version__,
        hardcover=_limiter(gate),
        **detail,
    )
