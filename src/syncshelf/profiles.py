"""Keep the ``profiles`` table in step with the config file (SPEC §2.3).

The config file is the only place a profile is created or removed. At every start each
configured name is upserted. A name that has gone from the file is marked ``removed_at`` and
its open grants are revoked with ``profile_deleted``, so no connection outlives its profile;
its data is kept. Adding the name back clears ``removed_at``, but its old grants stay revoked:
the person connects again. A rename is a manual SQL step (phase 7 runbook), because to this
code it looks like one removal and one addition.
"""

import sqlite3
from collections.abc import Collection
from dataclasses import dataclass

__all__ = ["ProfileSync", "sync_profiles"]


@dataclass(frozen=True)
class ProfileSync:
    """What one :func:`sync_profiles` call changed, for the startup log. Names are sorted."""

    added: list[str]
    removed: list[str]
    restored: list[str]
    """Removed profiles whose names came back to the file."""
    revoked: int
    """Open grants revoked because their profile was removed."""


def sync_profiles(conn: sqlite3.Connection, names: Collection[str], now: int) -> ProfileSync:
    """Make ``profiles`` match the configured ``names``, at epoch second ``now``.

    A unit of work for :meth:`syncshelf.db.Database.write`: it runs inside the caller's
    transaction, so the removals and their revocations land together or not at all. Running it
    again with the same names changes nothing, and a removed profile keeps its first
    ``removed_at``.
    """
    configured = set(names)
    known = dict(conn.execute("SELECT name, removed_at IS NOT NULL FROM profiles"))

    added = sorted(configured - known.keys())
    restored = sorted(name for name in configured & known.keys() if known[name])
    removed = sorted(name for name, gone in known.items() if name not in configured and not gone)

    conn.executemany(
        "INSERT INTO profiles (name, created_at) VALUES (?, ?)", [(n, now) for n in added]
    )
    conn.executemany(
        "UPDATE profiles SET removed_at = NULL WHERE name = ?", [(n,) for n in restored]
    )
    conn.executemany(
        "UPDATE profiles SET removed_at = ? WHERE name = ?", [(now, n) for n in removed]
    )
    # Every removed profile, not just the ones removed now, so that no path (a grant row
    # written by hand, say) can leave a connection open on a removed profile.
    revoked = conn.execute(
        "UPDATE oauth_grant SET revoked_at = ?, revoked_reason = 'profile_deleted' "
        "WHERE revoked_at IS NULL "
        "AND profile IN (SELECT name FROM profiles WHERE removed_at IS NOT NULL)",
        (now,),
    ).rowcount
    return ProfileSync(added, removed, restored, revoked)
