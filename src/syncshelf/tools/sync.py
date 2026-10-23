"""``sync_status``, ``sync_now`` and ``retry_failed``: how a profile's syncs are doing, and
starting them (SPEC §3.4a.10, §3.4a.11).

The caller's own profile in full detail: each source's last result, schedule and backoff,
its backfill, its recent runs; the mirror; the Hardcover limiter; the quarantine, with what
each stuck change would do; the unmatched queue in counts; how many books a complete feed
showed gone. Sections for phases not yet shipped are left out, not null.

Every error string here was host-aliased and secret-scrubbed before it was stored (§2.7),
and a review is shown only as its length and first 40 characters.
"""

import sqlite3
from collections.abc import Mapping
from typing import Any, Literal, NotRequired, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from syncshelf import __version__
from syncshelf.attention import AttentionItem, attention
from syncshelf.config import ProfileConfig
from syncshelf.sync.identity import release_changes
from syncshelf.sync.limiter import HardcoverLimiter
from syncshelf.tools import REGISTRY, current_profile, iso, services

__all__ = ["QUARANTINE_SHOWN", "RECENT_RUNS", "retry_failed", "status", "sync_now", "sync_status"]

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


class Health(TypedDict):
    """One profile in the household health view: codes and results, no titles or values."""

    profile: str
    attention: list[str]
    sources: dict[str, dict[str, str | None]]


class Status(TypedDict):
    """``sync_status``'s answer: the caller's full detail, or the household health view."""

    now: str
    version: str
    profile: NotRequired[str]
    attention: NotRequired[list[AttentionItem]]
    sources: NotRequired[dict[str, Source]]
    mirror: NotRequired[dict[str, Any]]
    hardcover: NotRequired[dict[str, Any]]
    quarantine: NotRequired[dict[str, Any]]
    unmatched: NotRequired[dict[str, Any]]
    gone: NotRequired[dict[str, int]]
    household: NotRequired[list[Health]]
    """With ``profile="household"``: every profile's health, in name order."""


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
        Backfill(state="done", done_at=iso(done[0]) or "", rows_linked=done[1], rows_queued=done[2])
        if done
        else Backfill(state="pending")
    )
    return Source(
        enabled=enabled,
        last_result=result,
        last_run_at=iso(run_at),
        last_success_at=iso(success_at),
        last_observed_at=iso(observed_at),
        # A source taken out of the config keeps its history but runs no more.
        next_due_at=iso(due_at) if enabled else None,
        consecutive_failures=failures,
        backoff_until=iso(backoff_until),
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
            started_at=iso(started) or "",
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
    return {"as_of": iso(row[0]), "last_attempt_at": iso(row[1]), "last_error": row[2]}


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
            first_failed_at=iso(first),
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
    return {"total": sum(reasons.values()), "by_reason": reasons, "oldest_queued_at": iso(oldest)}


def _limiter(gate: Any) -> dict[str, Any]:
    if not isinstance(gate, HardcoverLimiter):
        return {"limiter": "ok", "paused_until": None, "daily_remaining": None}
    state = gate.state()
    return {
        "limiter": state.limiter,
        "paused_until": iso(state.paused_until),
        "daily_remaining": state.daily_remaining,
    }


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def sync_status(profile: Literal["me", "household"] = "me") -> Status:
    """How the caller's syncs are doing: use it when the user asks whether their Goodreads
    sync is working, why a book hasn't reached Hardcover, or what's stuck.

    attention lists what needs a person (quarantined changes, a Hardcover bug, a feed not
    read for a day), each with a message and the action that fixes it.

    For the caller's own profile ("me", the default) it shows each source's last result, when
    it last ran and succeeded, when it next runs, any backoff after failed fetches, whether
    its first sync (the backfill) is done with how many books were linked and queued, and its
    recent runs with their errors. Then: how current the copy of the Hardcover library is;
    the Hardcover rate limiter (ok, paused, or holding back its daily reserve); quarantined
    changes, each with its book, field, old and new values, error and attempts (release them
    with retry_failed once the cause is fixed); the unmatched queue in counts by reason (see
    unmatched_books for the books themselves); and how many books Goodreads no longer lists.
    Times are ISO 8601 UTC. A review is shown only as its length and first 40 characters.

    With profile="household" it is the health view of every profile in the household: each
    one's attention codes and each source's last result and last success, with no books,
    values or errors.
    """
    caller = current_profile()
    bound = services()
    now = int(bound.clock())
    if profile == "household":
        # Operational data, shown for everyone whatever household_visible says (§3.4a.10).
        rows = await bound.db.read(health, bound.profiles, now)
        return Status(now=iso(now) or "", version=__version__, household=rows)
    enabled = bound.profiles[caller].goodreads is not None
    detail = await bound.db.read(status, caller, now, enabled)
    gate = bound.hardcover[caller].gate if caller in bound.hardcover else None
    answer = Status(
        profile=caller,
        now=iso(now) or "",
        version=__version__,
        hardcover=_limiter(gate),
        **detail,
    )
    if items := await bound.db.read(attention, caller, now, bound.profiles[caller]):
        answer["attention"] = items
    return answer


def health(
    conn: sqlite3.Connection, profiles: Mapping[str, ProfileConfig], now: int
) -> list[Health]:
    """Every profile's health: its attention codes and each source's last result and last
    success. Nothing that names a book, a value or an error (§3.4a.10)."""
    rows: list[Health] = []
    for name in sorted(profiles):
        codes = [item["code"] for item in attention(conn, name, now, profiles[name])]
        sources: dict[str, dict[str, str | None]] = {}
        for run_source in _enabled(profiles[name]):
            found = conn.execute(
                "SELECT last_result, last_success_at FROM schedules WHERE profile = ? AND "
                "source = ?",
                (name, run_source),
            ).fetchone() or (None, None)
            sources[run_source] = {"last_result": found[0], "last_success_at": iso(found[1])}
        rows.append(Health(profile=name, attention=codes, sources=sources))
    return rows


def _enabled(profile: ProfileConfig) -> list[str]:
    """The run sources a profile has, of those shipped so far."""
    return [s for s, identity in SOURCES.items() if identity == "goodreads" and profile.goodreads]


class Started(TypedDict):
    """A run ``sync_now`` started, or the one it found going."""

    source: str
    run_id: int
    started: bool
    """True when this call started it; false when it was already running."""
    since: str
    """When it started."""


class SyncNow(TypedDict):
    """``sync_now``'s answer: each source's run, and a line for Claude to relay."""

    runs: list[Started]
    note: str


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False))
async def sync_now(
    source: Literal["goodreads", "abs", "all"] = "all", full: bool = False
) -> SyncNow:
    """Start a sync of the caller's sources now, rather than waiting for the schedule: use it
    when the user has just changed something on Goodreads and wants it on Hardcover.

    - source: "goodreads" (the shelf RSS feed), "abs", or "all" (the default, every source the
      profile has).
    - full: also run the full Goodreads library export. Not available yet.

    It returns at once, with each run's id; a run already going is reported, not started
    twice. It doesn't wait for the run: read sync_status a little later for the outcome.
    """
    if full:
        raise ToolError(
            "A full sync with the Goodreads library export arrives in phase 4b; "
            "sync_now without full runs the shelf feed."
        )
    if source == "abs":
        raise ToolError("Audiobookshelf syncing arrives in phase 5.")
    caller = current_profile()
    scheduler = services().scheduler
    if not scheduler.enabled(caller):
        raise ToolError("This profile has no Goodreads sync: its config has no goodreads section.")
    run_id, since, new = await scheduler.run_now(caller, "sync_now")
    started = Started(source="gr_rss", run_id=run_id, started=new, since=iso(since) or "")
    if not new:
        note = f"A Goodreads run was already going (since {started['since']}, run {run_id})."
    else:
        note = "Started; read sync_status for how it went."
    return SyncNow(runs=[started], note=note)


class Released(TypedDict):
    """``retry_failed``'s answer: what was released, and whether a run was queued."""

    released: list[int]
    """The change ids released, pending again."""
    run: str


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=True))
async def retry_failed(change_id: int | Literal["all"]) -> Released:
    """Try quarantined changes again: use it once the cause in sync_status's quarantine list
    has been dealt with (Hardcover back up, a book fixed by hand).

    - change_id: one change's id from sync_status (quarantine.items[].change_id), or "all"
      for every one of the caller's quarantined changes.

    The changes become pending again with their attempts started over, and a sync of the
    caller's sources is queued straight away; read sync_status for how it went. Only the
    caller's own changes can be released.
    """
    caller = current_profile()
    bound = services()
    wanted = None if change_id == "all" else [change_id]
    released = await bound.db.write(release_changes, caller, wanted)
    if change_id != "all" and not released:
        raise ToolError(f"Change {change_id} isn't one of your quarantined changes.")
    if released and bound.scheduler.enabled(caller):
        bound.scheduler.nudge(caller, "sync_now")
        run = "queued"
    else:
        run = "none"
    return Released(released=released, run=run)
