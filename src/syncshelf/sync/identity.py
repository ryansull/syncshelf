"""Links, source rows and their baselines in SQLite (SPEC §3.4a.4, §3.4a.6, §3.4a.12).

**Links** are shared facts about the world, ``(source, source_id) → book``, stored once for
both profiles. **Source rows** are per profile: their state (``linked | queued | ignored``),
the book that profile's writes target, and one **baseline** per field, the last source value
SyncShelf accepted with its ``observed_at``. A baseline is recorded whatever the row's state,
so a queued row's pending diff is always computable.

Every function here is a plain unit of work on the writer (or a reader) connection, inside
the caller's transaction (§2.4).
"""

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from syncshelf.sync.matcher import IdHit
from syncshelf.sync.observations import Batch, Observation

__all__ = [
    "Baseline",
    "IdentitySource",
    "Observed",
    "Pending",
    "Placement",
    "SourceRow",
    "backfilled",
    "commit",
    "load_rows",
    "match_input",
    "observe",
    "record_backfill",
    "shared_links",
]

IdentitySource = Literal["goodreads", "abs"]
"""``source`` on the identity tables. Goodreads' observation sources all map to ``goodreads``."""


@dataclass(frozen=True)
class Baseline:
    """One field's baseline, with its pending change if it has one (§3.4a.6)."""

    id: int
    """The ``change_id`` that ``retry_failed`` and the logs name."""
    field: str
    value: str | None
    observed_at: int | None
    """``None`` marks an empty baseline: a row first seen after the backfill."""
    prev_value: str | None = None
    pending_value: str | None = None
    pending_since: int | None = None
    """Set exactly when the field has a pending change."""
    quarantined_at: int | None = None

    @property
    def pending(self) -> bool:
        return self.pending_since is not None


@dataclass(frozen=True)
class SourceRow:
    """One profile's row in one source, with its baselines by field."""

    source_id: str
    state: Literal["linked", "queued", "ignored"]
    hc_book_id: int | None
    hc_user_book_id: int | None
    baselines: Mapping[str, Baseline] = field(default_factory=dict[str, Baseline])
    gone_at: int | None = None


@dataclass(frozen=True)
class Placement:
    """Where the backfill put one row: linked to a book in the profile's library, or queued."""

    observation: Observation
    hit: IdHit | None
    """The id hit the row was placed by, if any (a queued ``not_in_library`` row has one)."""
    linked: bool
    hc_user_book_id: int | None = None


@dataclass(frozen=True)
class Observed:
    """What diffing one batch found."""

    pending: list[Pending]
    """The pending changes ready to apply: on linked rows, not quarantined."""
    gone: int
    """Rows this batch concluded are gone from the source."""


@dataclass(frozen=True)
class Pending:
    """A pending change on a linked row, ready to apply."""

    baseline_id: int
    source_id: str
    field: str
    old: str | None
    new: str | None
    hc_book_id: int


def backfilled(conn: sqlite3.Connection, profile: str, source: IdentitySource) -> bool:
    """Whether the profile's backfill of ``source`` is done (a ``backfills`` row exists)."""
    found = conn.execute(
        "SELECT 1 FROM backfills WHERE profile = ? AND source = ?", (profile, source)
    ).fetchone()
    return found is not None


def shared_links(
    conn: sqlite3.Connection, source: IdentitySource, ids: Iterable[str]
) -> dict[str, IdHit]:
    """The active shared links for ``ids``, in the matcher's hit shape, so the backfill can
    reuse a link the other profile's rows already made (§3.4a.4, rule 6)."""
    wanted = list(ids)
    links: dict[str, IdHit] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT source_id, hc_book_id, hc_edition_id, method FROM links "  # noqa: S608
            f"WHERE source = ? AND state = 'active' AND source_id IN ({marks})",
            (source, *chunk),
        )
        for source_id, book_id, edition_id, method in rows:
            links[source_id] = IdHit(book_id, edition_id, "", method)
    return links


def load_rows(
    conn: sqlite3.Connection, profile: str, source: IdentitySource
) -> dict[str, SourceRow]:
    """Every source row of the profile's ``source``, with its baselines."""
    baselines: dict[str, dict[str, Baseline]] = {}
    for row in conn.execute(
        "SELECT source_id, id, field, value, observed_at, prev_value, pending_value, "
        "pending_since, quarantined_at FROM baseline_fields WHERE profile = ? AND source = ?",
        (profile, source),
    ):
        source_id, *rest = row
        baseline = Baseline(*rest)
        baselines.setdefault(source_id, {})[baseline.field] = baseline
    return {
        source_id: SourceRow(source_id, state, book, user_book, baselines.get(source_id, {}), gone)
        for source_id, state, book, user_book, gone in conn.execute(
            "SELECT source_id, state, hc_book_id, hc_user_book_id, gone_at FROM source_rows "
            "WHERE profile = ? AND source = ?",
            (profile, source),
        )
    }


def match_input(observation: Observation) -> str:
    """The row as the matcher saw it, as stored on ``source_rows.match_input`` (§3.4a.5)."""
    isbns = [i for i in (observation.isbn13, observation.isbn10) if i]
    return json.dumps(
        {
            "title": observation.title,
            "authors": [observation.author] if observation.author else [],
            "isbns": isbns,
            "asin": None,
            "gr_book_id": observation.source_id,
            "format": observation.format,
            "year": None,
        },
        ensure_ascii=False,
    )


def record_backfill(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    batch: Batch,
    placements: Iterable[Placement],
    run_id: int,
    now: int,
) -> tuple[int, int]:
    """Write the map-only backfill (§3.4a.12): every row linked or queued, its baselines set
    to what was observed, and the ``backfills`` stamp. Pushes nothing. Returns (rows linked,
    rows queued)."""
    linked = queued = 0
    for place in placements:
        obs, hit = place.observation, place.hit
        if place.linked:
            assert hit is not None
            linked += 1
            conn.execute(
                "INSERT INTO links (source, source_id, hc_book_id, hc_edition_id, format, method,"
                " linked_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT "
                "(source, source_id) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (source, obs.source_id, hit.book_id, hit.edition_id, obs.format, hit.method, now,
                 now),
            )  # fmt: skip
            conn.execute(
                "INSERT INTO source_rows (profile, source, source_id, state, hc_book_id, "
                "hc_user_book_id, match_input, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, 'linked', ?, ?, ?, ?, ?)",
                (profile, source, obs.source_id, hit.book_id, place.hc_user_book_id,
                 match_input(obs), now, now),
            )  # fmt: skip
        else:
            queued += 1
            reason = "not_in_library" if hit is not None else "no_hit"
            conn.execute(
                "INSERT INTO source_rows (profile, source, source_id, state, queue_reason, "
                "queued_at, match_input, first_seen_at, last_seen_at, backfill_queued) "
                "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, 1)",
                (profile, source, obs.source_id, reason, now, match_input(obs), now, now),
            )
            if hit is not None:
                conn.execute(
                    "INSERT INTO match_candidates (profile, source, source_id, rank, hc_book_id,"
                    " hc_edition_id, method, fetched_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                    (profile, source, obs.source_id, hit.book_id, hit.edition_id, hit.method,
                     now),
                )  # fmt: skip
        conn.executemany(
            "INSERT INTO baseline_fields (profile, source, source_id, field, value, observed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (profile, source, obs.source_id, name, value, batch.observed_at)
                for name, value in obs.fields.items()
            ],
        )
    conn.execute(
        "INSERT INTO backfills (profile, source, done_at, run_id, rows_linked, rows_queued) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (profile, source, now, run_id, linked, queued),
    )
    return linked, queued


def observe(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    batch: Batch,
    now: int,
) -> Observed:
    """Diff a batch against the rows' baselines (§3.4a.6), record what it saw, and return the
    pending changes ready to apply and how many rows it found gone.

    A field applies only if the batch carries it and is newer than its baseline (rule 8):
    - equal to the baseline: its ``observed_at`` advances, and any pending change it had is
      cleared, since the source went back;
    - different: it becomes the pending change. A value that differs from the one already
      pending supersedes it, starting over (the attempts and quarantine are cleared);
    - never observed on this row before: it only sets the baseline (rule 3).

    **Removals are recorded, never pushed** (rule 4). Only a complete enumeration may conclude
    a row is gone: it gets ``gone_at``, its ``last_seen_at`` freezes, and a ``gone`` row goes
    in ``change_log``. A row seen again isn't gone any more, so its ``gone_at`` clears.

    Only rows SyncShelf already has are diffed; rows first seen now are left to the caller.
    """
    assert batch.observed_at is not None, "a stale batch only fills"
    rows = load_rows(conn, profile, source)
    seen: list[tuple[int, str, str, str]] = []
    for obs in batch.rows:
        row = rows.get(obs.source_id)
        if row is None:
            continue
        seen.append((now, profile, source, obs.source_id))
        for name, value in obs.fields.items():
            baseline = row.baselines.get(name)
            if baseline is None:
                conn.execute(
                    "INSERT INTO baseline_fields (profile, source, source_id, field, value, "
                    "observed_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (profile, source, obs.source_id, name, value, batch.observed_at),
                )
            elif baseline.observed_at is None or batch.observed_at > baseline.observed_at:
                _diff_field(conn, baseline, value, batch.observed_at, now)
    conn.executemany(
        "UPDATE source_rows SET last_seen_at = ?, gone_at = NULL WHERE profile = ? "
        "AND source = ? AND source_id = ?",
        seen,
    )
    gone = 0
    if batch.complete:
        listed = {obs.source_id for obs in batch.rows}
        for row in rows.values():
            if row.source_id not in listed and row.gone_at is None:
                gone += 1
                _mark_gone(conn, profile, source, row, now)
    return Observed(_pending(conn, profile, source), gone)


def _mark_gone(
    conn: sqlite3.Connection, profile: str, source: IdentitySource, row: SourceRow, now: int
) -> None:
    conn.execute(
        "UPDATE source_rows SET gone_at = ? WHERE profile = ? AND source = ? AND source_id = ?",
        (now, profile, source, row.source_id),
    )
    conn.execute(
        "INSERT INTO change_log (profile, source, source_id, hc_book_id, hc_user_book_id, kind,"
        " at) VALUES (?, ?, ?, ?, ?, 'gone', ?)",
        (profile, source, row.source_id, row.hc_book_id, row.hc_user_book_id, now),
    )


def _diff_field(
    conn: sqlite3.Connection, baseline: Baseline, value: str | None, observed_at: int, now: int
) -> None:
    if value == baseline.value:
        # The source still says what was accepted (an empty field on an empty baseline
        # included); any pending change was undone at source.
        conn.execute(
            "UPDATE baseline_fields SET observed_at = ?, pending_value = NULL, "
            "pending_since = NULL, attempts = 0, first_failed_at = NULL, last_error = NULL, "
            "last_error_class = NULL, quarantined_at = NULL WHERE id = ?",
            (observed_at, baseline.id),
        )
    elif not baseline.pending or value != baseline.pending_value:
        # A new pending change, or a newer value superseding the one pending.
        conn.execute(
            "UPDATE baseline_fields SET pending_value = ?, pending_since = ?, attempts = 0, "
            "first_failed_at = NULL, last_error = NULL, last_error_class = NULL, "
            "quarantined_at = NULL WHERE id = ?",
            (value, now, baseline.id),
        )


def _pending(conn: sqlite3.Connection, profile: str, source: IdentitySource) -> list[Pending]:
    return [
        Pending(*row)
        for row in conn.execute(
            "SELECT b.id, b.source_id, b.field, b.value, b.pending_value, r.hc_book_id "
            "FROM baseline_fields b JOIN source_rows r USING (profile, source, source_id) "
            "WHERE b.profile = ? AND b.source = ? AND b.pending_since IS NOT NULL "
            "AND b.quarantined_at IS NULL AND r.state = 'linked' ORDER BY b.id",
            (profile, source),
        )
    ]


def commit(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    change: Pending,
    observed_at: int,
    hc_user_book_id: int,
    run_id: int | None,
    now: int,
) -> None:
    """After Hardcover took a change: advance its baseline and write the ``change_log`` row,
    in one transaction (§3.4a.6, rule 9)."""
    conn.execute(
        "UPDATE baseline_fields SET prev_value = value, value = pending_value, observed_at = ?, "
        "pending_value = NULL, pending_since = NULL, attempts = 0, first_failed_at = NULL, "
        "last_error = NULL, last_error_class = NULL, quarantined_at = NULL WHERE id = ?",
        (observed_at, change.baseline_id),
    )
    conn.execute(
        "UPDATE source_rows SET hc_user_book_id = ? WHERE profile = ? AND source = ? "
        "AND source_id = ?",
        (hc_user_book_id, profile, source, change.source_id),
    )
    conn.execute(
        "INSERT INTO change_log (run_id, profile, source, source_id, hc_book_id, "
        "hc_user_book_id, kind, field, old_value, new_value, at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'push', ?, ?, ?, ?)",
        (run_id, profile, source, change.source_id, change.hc_book_id, hc_user_book_id,
         change.field, change.old, change.new, now),
    )  # fmt: skip
