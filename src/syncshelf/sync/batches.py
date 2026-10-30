"""Stored library CSVs (``ingest_batches``, SPEC §3.4a.8): each upload kept as received, so a
run can apply it and later runs can learn the profile's shelves from it (#182).

Only the last :data:`KEEP` per profile are kept. A batch without ``observed_at`` (an upload
with no ``generated_at``) is **stale**: it only fills, never pushes (#168).
"""

import sqlite3

from syncshelf.sync.observations import Source

__all__ = ["KEEP", "store"]

KEEP = 3
"""Stored batches kept per profile, newest first."""


def store(
    conn: sqlite3.Connection,
    profile: str,
    source: Source,
    received_at: int,
    observed_at: int | None,
    payload: bytes,
    row_count: int,
) -> int:
    """Keep one uploaded export, complete (its header validated), and return its id. Older
    batches past the last :data:`KEEP` are dropped."""
    cursor = conn.execute(
        "INSERT INTO ingest_batches (profile, source, received_at, observed_at, stale, complete,"
        " payload, row_count) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
        (profile, source, received_at, observed_at, int(observed_at is None), payload, row_count),
    )
    conn.execute(
        "DELETE FROM ingest_batches WHERE profile = ? AND id NOT IN (SELECT id FROM "
        "ingest_batches WHERE profile = ? ORDER BY received_at DESC, id DESC LIMIT ?)",
        (profile, profile, KEEP),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid
