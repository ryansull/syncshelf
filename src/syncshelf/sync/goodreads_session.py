"""The Goodreads session state (SPEC §3.4b.1, §3.4b.2).

``goodreads_sessions`` holds one row per profile saying where that person's sign-in stands.
The session itself is the cookie jar on disk (§3.4b.3), never here, so a copy of the database
carries no live session.

This module currently holds the startup reconciliation only; the sign-in state machine, the
jar and the sidecar connection arrive with
`#224 <https://github.com/ryansull/syncshelf/issues/224>`_.

Each unit here is a plain function on the writer connection, run inside its transaction.
"""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

__all__ = ["SessionReconcile", "SessionState", "reconcile_configured"]

SessionState = Literal[
    "ok",
    "needs_sign_in",
    "awaiting_approval",
    "awaiting_code",
    "challenged",
    "bad_credentials",
    "disabled",
]
"""Every state ``goodreads_sessions.state`` accepts (Appendix A, §3.4b.2).

``disabled`` means this profile has no Goodreads credentials, so there is nothing to sign in
to. An absent ``services.browser_ws`` is **not** this state: it switches 4b off stack-wide as
configuration, and never per profile (`#232
<https://github.com/ryansull/syncshelf/issues/232>`_).
"""


@dataclass(frozen=True)
class SessionReconcile:
    """What one startup reconciliation did, for the log."""

    disabled: list[str]
    """Profiles newly moved to ``disabled``, having no credentials configured."""
    enabled: list[str]
    """Profiles moved off ``disabled`` to ``needs_sign_in``, now that they have credentials."""


def reconcile_configured(
    conn: sqlite3.Connection,
    with_credentials: Collection[str],
    without_credentials: Collection[str],
    now: int,
) -> SessionReconcile:
    """Make each profile's session state agree with whether it has credentials, at ``now``.

    A unit of work for :meth:`syncshelf.db.Database.write`. Startup passes the configured
    profiles split by whether ``goodreads.password`` is set, so the state never claims a
    sign-in that configuration has made impossible:

    - a profile **without** credentials becomes ``disabled``, whatever state it held. Nothing
      about the old state is actionable once there is no password to sign in with, and the
      stickiness of ``challenged`` and ``bad_credentials`` (§3.4b.2) only governs *unattended*
      reuse, which cannot happen either;
    - a profile **with** credentials that sits at ``disabled`` becomes ``needs_sign_in``, so it
      is offered a sign-in rather than staying switched off. Any other state is left alone,
      because only the sign-in machine moves a session out of a state it genuinely reached.

    Running it again changes nothing. Returns which profiles moved, each list sorted.
    """
    disabled = _move(conn, without_credentials, "disabled", now, "state <> 'disabled'")
    enabled = _move(conn, with_credentials, "needs_sign_in", now, "state = 'disabled'")
    return SessionReconcile(disabled, enabled)


def _move(
    conn: sqlite3.Connection,
    profiles: Collection[str],
    state: SessionState,
    now: int,
    when: str,
) -> list[str]:
    """Set ``state`` on each of ``profiles`` whose row matches ``when``, inserting a missing
    row. Returns the profiles that changed, sorted."""
    moved: list[str] = []
    for name in sorted(profiles):
        changed = conn.execute(
            "INSERT INTO goodreads_sessions (profile, state, state_since) VALUES (?, ?, ?) "
            f"ON CONFLICT (profile) DO UPDATE SET state = ?, state_since = ? WHERE {when}",
            (name, state, now, state, now),
        ).rowcount
        if changed:
            moved.append(name)
    return moved
