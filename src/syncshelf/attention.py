"""Attention items: the things a person must act on (SPEC §3.4a.10).

An item is ``{code, message, action, id?}``, computed from state on every read for one
profile; nothing about it is stored. Four are raised so far:

- ``quarantined``: at least one change is quarantined (§3.4a.7). Cleared when none is.
- ``hardcover_bug``: a quarantined change failed as a bug (``request_exceeds_capacity``).
  Cleared when that change is released.
- ``source_stale``: an **enabled** source has had no success in its own window — a day for
  the RSS feed, **seven days for the CSV export**, which only runs every five (§3.4b.5).
  Cleared by the next success. One item however many sources are stale, because the code is
  what the household view and ``/healthz`` list.
- ``goodreads_session``: the Goodreads session needs a person — ``needs_sign_in``,
  ``challenged`` or ``bad_credentials`` (§3.4b.2). Cleared when the state leaves those.

**A stack with no browser sidecar raises nothing for the CSV source** (#232): the source is
switched off through :func:`syncshelf.sources.enabled`, not left permanently stale.

Kept out on purpose: mirror refresh errors, single failed runs, recovered transients, and the
unmatched queue. A book needing a match is normal ambiguity; attention means sync is broken.

Messages name counts and sources, never titles or values (§2.7): the household view and
``/healthz`` show these codes to everyone.
"""

import sqlite3
from typing import NotRequired, TypedDict

from syncshelf import sources
from syncshelf.config import ProfileConfig
from syncshelf.sync.goodreads_session import ADVICE, NEEDS_A_PERSON, SessionState

__all__ = ["CSV_STALE_AFTER", "STALE_AFTER", "AttentionItem", "attention", "session"]

STALE_AFTER = 24 * 3600
"""An RSS source with no success for longer than this (seconds) raises ``source_stale``."""
CSV_STALE_AFTER = 7 * 86400
"""The same for the CSV export, which only runs every five days (§3.4a.10, §3.4b.5)."""
_WINDOWS = {"gr_rss": STALE_AFTER, "gr_csv": CSV_STALE_AFTER}
_STALE_MESSAGES = {
    "gr_rss": "The Goodreads feed hasn't been read successfully for over a day.",
    "gr_csv": "The Goodreads library export hasn't run successfully for over a week.",
}
"""What each stale source says, naming its own window rather than a vague “too long”."""


class AttentionItem(TypedDict):
    """One thing a person must act on."""

    code: str
    message: str
    action: str
    id: NotRequired[int]
    """For event items, the id ``dismiss_attention`` takes (phase 6)."""


def attention(
    conn: sqlite3.Connection,
    profile: str,
    now: int,
    config: ProfileConfig,
    sidecar: bool = False,
) -> list[AttentionItem]:
    """The profile's attention items, from state, in a fixed order.

    ``config`` and ``sidecar`` together say which of its sources are enabled
    (:func:`syncshelf.sources.enabled`): ``sidecar`` is whether the stack has a browser
    sidecar at all, which only the CSV source needs. It defaults to ``False``, the reading
    that switches the CSV source **off** — so a caller that cannot know says nothing about
    the export rather than reporting one that could never run (#232).
    """
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
    switched_on = sources.enabled(config, sidecar=sidecar)
    stale = [
        source
        for source, on in switched_on.items()
        if on and _stale(conn, profile, now, source, _WINDOWS[source])
    ]
    if stale:
        # One item, so the household view and /healthz don't list the code twice.
        items.append(
            AttentionItem(
                code="source_stale",
                message=" ".join(_STALE_MESSAGES[source] for source in stale),
                action="See sync_status for the last runs and their errors.",
            )
        )
    if switched_on["gr_csv"] and (state := session(conn, profile)) in NEEDS_A_PERSON:
        items.append(
            AttentionItem(
                code="goodreads_session",
                message=_SESSION_MESSAGES[state],
                action=ADVICE[state],
            )
        )
    return items


_SESSION_MESSAGES: dict[SessionState, str] = {
    "needs_sign_in": "Goodreads needs you to sign in again before it can export your library.",
    "challenged": "Goodreads showed a bot check, so SyncShelf stopped using the session.",
    "bad_credentials": "Goodreads rejected the configured password.",
}
"""Why the session needs a person, in words the household view may show anyone.

Never the ``detail``: that is one profile's, while these messages travel with the code.
"""


def session(conn: sqlite3.Connection, profile: str) -> SessionState:
    """``profile``'s stored Goodreads session state, or ``needs_sign_in`` with no row.

    No row means nothing has signed in yet, which is exactly what ``needs_sign_in`` says, so
    a profile that has never signed in raises attention like one whose session has lapsed.
    """
    found = conn.execute(
        "SELECT state FROM goodreads_sessions WHERE profile = ?", (profile,)
    ).fetchone()
    return "needs_sign_in" if found is None else found[0]


def _stale(conn: sqlite3.Connection, profile: str, now: int, source: str, after: int) -> bool:
    """No success for over ``after`` seconds: since the last success; never having had one,
    since the first run, or, never having run at all, since it was first due (a loop that
    never runs is broken too)."""
    since = conn.execute(
        "SELECT coalesce(s.last_success_at, (SELECT min(started_at) FROM runs WHERE "
        "profile = ? AND source = ?), s.next_due_at) FROM schedules s "
        "WHERE s.profile = ? AND s.source = ?",
        (profile, source, profile, source),
    ).fetchone()
    if since is None:  # never scheduled: a source just configured
        since = conn.execute(
            "SELECT min(started_at) FROM runs WHERE profile = ? AND source = ?",
            (profile, source),
        ).fetchone()
    return since[0] is not None and now - since[0] > after
