"""Stored library CSVs (``ingest_batches``, SPEC §3.4a.8): each upload kept as received, so a
run can apply it and later runs can learn the profile's shelves from it (#182).

Only the last :data:`KEEP` per profile are kept. A batch without ``observed_at`` (an upload
with no ``generated_at``) is **stale**: it only fills, never pushes (§3.4a.8).

**Batches, then RSS** (#204): every Goodreads run first applies the profile's unprocessed
batches, the dated ones oldest generation time first and then the stale ones, then reads RSS.
An upload that lands during a run waits for the next one; "the newer observation wins"
(§3.4a.6 rule 8) makes the order safe.
"""

import sqlite3
from dataclasses import dataclass

from syncshelf.clients.goodreads_csv import BadExport, parse_export
from syncshelf.sync.observations import ShelfVocabulary, Source

__all__ = [
    "KEEP",
    "Stored",
    "payloads",
    "processed",
    "store",
    "stored_ids",
    "unprocessed",
    "vocabulary",
]

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
        "ingest_batches WHERE profile = ? ORDER BY id DESC LIMIT ?)",  # never the clock
        (profile, profile, KEEP),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


@dataclass(frozen=True)
class Stored:
    """A stored batch a run is yet to apply."""

    id: int
    source: Source
    observed_at: int | None
    """``None`` for a stale upload, which only fills."""
    payload: bytes


def unprocessed(conn: sqlite3.Connection, profile: str) -> list[Stored]:
    """The profile's batches no run has applied: the dated ones, oldest generation time
    first, then the stale ones, oldest upload first, so a fill never comes before an
    observation it could have yielded to."""
    return [
        Stored(*row)
        for row in conn.execute(
            "SELECT id, source, observed_at, payload FROM ingest_batches WHERE profile = ? "
            "AND processed_at IS NULL ORDER BY stale, observed_at, id",
            (profile,),
        )
    ]


def processed(conn: sqlite3.Connection, batch_id: int, run_id: int, now: int) -> None:
    """Record that a run applied the batch."""
    conn.execute(
        "UPDATE ingest_batches SET processed_at = ?, run_id = ? WHERE id = ?",
        (now, run_id, batch_id),
    )


def stored_ids(conn: sqlite3.Connection, profile: str) -> list[int]:
    """The ids of the profile's stored exports, newest first: what :func:`payloads` would
    return, cheaply."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT id FROM ingest_batches WHERE profile = ? ORDER BY id DESC", (profile,)
        )
    ]


def payloads(conn: sqlite3.Connection, profile: str) -> list[bytes]:
    """Every stored export of the profile, stale ones too, newest first."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT payload FROM ingest_batches WHERE profile = ? ORDER BY id DESC", (profile,)
        )
    ]


def vocabulary(exports: list[bytes]) -> ShelfVocabulary:
    """What stored exports say about the profile's shelves (#182): any shelf shown as an
    ``Exclusive Shelf`` is exclusive, and one shown only in ``Bookshelves`` is a tag. RSS
    lists both together, so it can tell them apart only through this."""
    exclusive: set[str] = set()
    listed: set[str] = set()
    for export in exports:
        try:
            rows = parse_export(export)
        except BadExport:
            continue  # checked when it was uploaded; one the parser now refuses teaches nothing
        for row in rows:
            exclusive.add(row.exclusive_shelf)
            listed.update(row.bookshelves)
    return ShelfVocabulary(frozenset(exclusive), frozenset(listed - exclusive))
