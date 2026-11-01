"""Attention items: the things a person must act on (SPEC §3.4a.10).

An item is ``{code, message, action, id?}``, computed from state on every read for one
profile; nothing about it is stored. Phase 4a raises three:

- ``quarantined``: at least one change is quarantined (§3.4a.7). Cleared when none is.
- ``hardcover_bug``: a quarantined change failed as a bug (``request_exceeds_capacity``).
  Cleared when that change is released.
- ``source_stale``: an enabled RSS source has had no success for more than a day. Cleared by
  the next success.

Kept out on purpose: mirror refresh errors, single failed runs, recovered transients, and the
unmatched queue. A book needing a match is normal ambiguity; attention means sync is broken.

Messages name counts and sources, never titles or values (§2.7): the household view and
``/healthz`` show these codes to everyone.
"""

import sqlite3
from typing import NotRequired, TypedDict

from syncshelf.config import ProfileConfig

__all__ = ["STALE_AFTER", "AttentionItem", "attention"]

STALE_AFTER = 24 * 3600
"""An RSS source with no success for longer than this (seconds) raises ``source_stale``."""


class AttentionItem(TypedDict):
    """One thing a person must act on."""

    code: str
    message: str
    action: str
    id: NotRequired[int]
    """For event items, the id ``dismiss_attention`` takes (phase 6)."""


def attention(
    conn: sqlite3.Connection, profile: str, now: int, config: ProfileConfig
) -> list[AttentionItem]:
    """The profile's attention items, from state, in a fixed order. ``config`` says which of
    its sources are enabled."""
    items: list[AttentionItem] = []
    quarantined, bugs = conn.execute(
        "SELECT count(*), coalesce(sum(last_error_class = 'bug'), 0) FROM baseline_fields "
        "WHERE profile = ? AND quarantined_at IS NOT NULL",
        (profile,),
    ).fetchone()
    if quarantined:
        changes = "change" if quarantined == 1 else "changes"
        items.append(
            AttentionItem(
                code="quarantined",
                message=f"{quarantined} Goodreads {changes} can't reach Hardcover.",
                action="See sync_status for what failed, then retry_failed once it's fixed.",
            )
        )
    if bugs:
        items.append(
            AttentionItem(
                code="hardcover_bug",
                message="Hardcover refused a change as too large: a SyncShelf bug.",
                action="See sync_status, and report it as a bug.",
            )
        )
    if config.goodreads is not None and _stale(conn, profile, now):
        items.append(
            AttentionItem(
                code="source_stale",
                message="The Goodreads feed hasn't been read successfully for over a day.",
                action="See sync_status for the last runs and their errors.",
            )
        )
    return items


def _stale(conn: sqlite3.Connection, profile: str, now: int) -> bool:
    """No success for over a day: since the last success; never having had one, since the
    first run, or, never having run at all, since it was first due (a loop that never runs
    is broken too)."""
    since = conn.execute(
        "SELECT coalesce(s.last_success_at, (SELECT min(started_at) FROM runs WHERE "
        "profile = ? AND source = 'gr_rss'), s.next_due_at) FROM schedules s "
        "WHERE s.profile = ? AND s.source = 'gr_rss'",
        (profile, profile),
    ).fetchone()
    if since is None:  # never scheduled: a source just configured
        found = conn.execute(
            "SELECT min(started_at) FROM runs WHERE profile = ? AND source = 'gr_rss'",
            (profile,),
        ).fetchone()
        since = found
    return since[0] is not None and now - since[0] > STALE_AFTER
