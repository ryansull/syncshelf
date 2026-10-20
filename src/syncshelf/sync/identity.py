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

from syncshelf.sync.matcher import METHOD_RANK, Candidate, IdHit
from syncshelf.sync.observations import Batch, Observation

__all__ = [
    "Baseline",
    "IdentitySource",
    "Observed",
    "Pending",
    "Placement",
    "SourceRow",
    "add_rows",
    "LogRow",
    "backfilled",
    "follow_merges",
    "link_editions",
    "load_rows",
    "mark_stale",
    "match_input",
    "observe",
    "pending_for",
    "record_failure",
    "release_changes",
    "release_on_new_version",
    "release_quarantine",
    "record_backfill",
    "rematch_rows",
    "settle",
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
    match_input: str | None = None
    queue_reason: str | None = None
    backfill_queued: bool = False


@dataclass(frozen=True)
class Placement:
    """Where the backfill put one row: linked to a book in the profile's library, or queued."""

    observation: Observation
    hit: IdHit | None
    """The id hit the row was placed by, if any (a queued ``not_in_library`` row has one)."""
    linked: bool
    hc_user_book_id: int | None = None
    reason: str | None = None
    """Why a queued row is queued, when its hit doesn't say (a ``stale`` link)."""
    candidates: tuple[Candidate, ...] = ()
    """A queued row's candidates, best first (at most 5)."""


@dataclass(frozen=True)
class Observed:
    """What diffing one batch found."""

    pending: list[Pending]
    """The pending changes ready to apply: on linked rows this batch lists, not
    quarantined."""
    gone: int
    """Rows this batch concluded are gone from the source."""
    new: list[Observation] = field(default_factory=list[Observation])
    """Rows SyncShelf has never seen: the caller matches them, then :func:`add_rows`."""
    rematch: list[Observation] = field(default_factory=list[Observation])
    """Queued rows whose match input changed: the caller re-matches them, then
    :func:`rematch_rows` (§3.4a.5: re-matching happens only on a change or on request)."""


@dataclass(frozen=True)
class Pending:
    """A pending change on a linked row, ready to apply."""

    baseline_id: int
    source_id: str
    field: str
    old: str | None
    """The baseline's value: what was last accepted."""
    new: str | None
    """The pending value."""
    hc_book_id: int
    prev_value: str | None = None
    """The value before ``old``: the Read → Currently Reading → Read re-read signal."""
    pending_since: int | None = None
    """When SyncShelf first saw the change: the date of an undated finish, failing all else."""


@dataclass(frozen=True)
class LogRow:
    """One ``change_log`` row written with a settled change."""

    kind: Literal["push", "read_insert", "read_adopt"]
    field: str | None
    old: str | None = None
    new: str | None = None
    trigger: str | None = None
    date_source: str | None = None


def backfilled(conn: sqlite3.Connection, profile: str, source: IdentitySource) -> bool:
    """Whether the profile's backfill of ``source`` is done (a ``backfills`` row exists)."""
    found = conn.execute(
        "SELECT 1 FROM backfills WHERE profile = ? AND source = ?", (profile, source)
    ).fetchone()
    return found is not None


def shared_links(
    conn: sqlite3.Connection, source: IdentitySource, ids: Iterable[str]
) -> tuple[dict[str, IdHit], set[str]]:
    """The shared links for ``ids``: the active ones in the matcher's hit shape, so the
    backfill reuses a link the other profile's rows already made (§3.4a.4, rule 6), and the
    ids whose link is ``stale``, whose rows are queued until a person resolves them."""
    wanted = list(ids)
    active: dict[str, IdHit] = {}
    stale: set[str] = set()
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT source_id, hc_book_id, hc_edition_id, method, state FROM links "  # noqa: S608
            f"WHERE source = ? AND source_id IN ({marks})",
            (source, *chunk),
        )
        for source_id, book_id, edition_id, method, state in rows:
            if state == "stale":
                stale.add(source_id)
            else:
                active[source_id] = IdHit(book_id, edition_id, "", method)
    return active, stale


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
        source_id: SourceRow(
            source_id,
            state,
            book,
            user_book,
            baselines.get(source_id, {}),
            gone,
            given,
            reason,
            bool(backfill),
        )
        for source_id, state, book, user_book, gone, given, reason, backfill in conn.execute(
            "SELECT source_id, state, hc_book_id, hc_user_book_id, gone_at, match_input, "
            "queue_reason, backfill_queued FROM source_rows WHERE profile = ? AND source = ?",
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
        obs = place.observation
        if place.linked:
            linked += 1
        else:
            queued += 1
        _insert_row(conn, profile, source, place, now, backfill=True)
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

    Rows SyncShelf has never seen are returned as ``new``, for the caller to match and add
    with :func:`add_rows`.
    """
    assert batch.observed_at is not None, "a stale batch only fills"
    rows = load_rows(conn, profile, source)
    seen: list[tuple[int, str, str, str]] = []
    new: list[Observation] = []
    rematch: list[Observation] = []
    for obs in batch.rows:
        row = rows.get(obs.source_id)
        if row is None:
            if all(n.source_id != obs.source_id for n in new):
                new.append(obs)
            continue
        # A queued row whose details changed is matched again, unless a person must
        # settle it (a conflict with the shared link) or it was already taken this batch.
        if (
            row.state == "queued"
            and row.queue_reason != "profile_conflict"
            and row.match_input != match_input(obs)
            and all(r.source_id != obs.source_id for r in rematch)
        ):
            rematch.append(obs)
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
    # A row this batch didn't list keeps its pending change for a batch that does, so the
    # baseline is never stamped with a time it wasn't observed at.
    listed_now = {source_id for _, _, _, source_id in seen}
    pending = [p for p in _pending(conn, profile, source) if p.source_id in listed_now]
    return Observed(pending, gone, new, rematch)


def add_rows(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    batch: Batch,
    placements: Iterable[Placement],
    now: int,
) -> list[Pending]:
    """Add rows first seen after the backfill, each linked or queued, from an **empty
    baseline** (§3.4a.6, rule 3): every non-empty field of this first observation is a
    pending change, and an empty one just sets its baseline. Returns the pending changes on
    the rows linked."""
    assert batch.observed_at is not None
    added: list[str] = []
    for place in placements:
        obs = place.observation
        added.append(obs.source_id)
        _insert_row(conn, profile, source, place, now, backfill=False)
        # A status this observation couldn't read (#182) starts empty too, so the first
        # status seen later is a change, not a baseline.
        fields = {"status": None, **obs.fields}
        conn.executemany(
            "INSERT INTO baseline_fields (profile, source, source_id, field, value, observed_at,"
            " pending_value, pending_since) VALUES (?, ?, ?, ?, NULL, ?, ?, ?)",
            [
                (profile, source, obs.source_id, name)
                + (
                    (None, None, None)
                    if name not in obs.fields
                    else (batch.observed_at, None, None)
                    if value is None
                    else (None, value, now)
                )
                for name, value in fields.items()
            ],
        )
    return pending_for(conn, profile, source, added)


def rematch_rows(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    placements: Iterable[Placement],
    now: int,
) -> None:
    """Record a re-match of queued rows: a row now matched becomes linked, keeping its
    baselines, so its pending diff applies; one still unmatched keeps its place in the queue
    (``queued_at``) with its new reason and candidates. Either way its match input is the one
    just matched."""
    for place in placements:
        obs, hit = place.observation, place.hit
        if place.linked:
            assert hit is not None
            book = _link(conn, source, obs, hit, now)
            conn.execute(
                "UPDATE source_rows SET state = 'linked', hc_book_id = ?, queue_reason = NULL, "
                "queued_at = NULL, backfill_queued = 0, match_input = ? "
                "WHERE profile = ? AND source = ? AND source_id = ?",
                (book, match_input(obs), profile, source, obs.source_id),
            )
            _write_candidates(conn, profile, source, obs.source_id, (), now)
        else:
            conn.execute(
                "UPDATE source_rows SET queue_reason = ?, match_input = ? "
                "WHERE profile = ? AND source = ? AND source_id = ?",
                (_reason(place), match_input(obs), profile, source, obs.source_id),
            )
            _write_candidates(conn, profile, source, obs.source_id, place.candidates, now)


def follow_merges(conn: sqlite3.Connection, merged: Mapping[int, int], now: int) -> int:
    """Follow Hardcover's merges (§3.4a.4 rule 7): each ``merged`` book (old id → the book it
    was merged into) is rewritten to the canonical book on its shared links, on every
    profile's source rows and on queued rows' candidates, each row's rewrite logged as a
    ``merge``, never queued. Returns how many
    source rows moved."""
    moved = 0
    for old, canonical in merged.items():
        if old == canonical:
            continue
        rows = conn.execute(
            "SELECT profile, source, source_id, hc_user_book_id FROM source_rows "
            "WHERE hc_book_id = ?",
            (old,),
        ).fetchall()
        conn.execute("UPDATE links SET hc_book_id = ? WHERE hc_book_id = ?", (canonical, old))
        conn.execute("UPDATE source_rows SET hc_book_id = ? WHERE hc_book_id = ?", (canonical, old))
        conn.execute(
            "UPDATE match_candidates SET hc_book_id = ? WHERE hc_book_id = ?", (canonical, old)
        )
        conn.executemany(
            "INSERT INTO change_log (profile, source, source_id, hc_book_id, hc_user_book_id, "
            "kind, old_value, new_value, at) VALUES (?, ?, ?, ?, ?, 'merge', ?, ?, ?)",
            [
                (profile, source, source_id, canonical, user_book, str(old), str(canonical), now)
                for profile, source, source_id, user_book in rows
            ],
        )
        moved += len(rows)
    return moved


def mark_stale(conn: sqlite3.Connection, book_ids: Iterable[int], now: int) -> int:
    """Books Hardcover no longer returns (§3.4a.4 rule 7): their links become ``stale``, and
    each profile's rows on them are queued ``stale`` for a person to resolve, keeping their
    baselines. Returns how many rows were queued."""
    queued = 0
    for book_id in set(book_ids):
        rows = conn.execute(
            "SELECT profile, source, source_id, hc_user_book_id FROM source_rows "
            "WHERE hc_book_id = ? AND state = 'linked'",
            (book_id,),
        ).fetchall()
        conn.execute("UPDATE links SET state = 'stale' WHERE hc_book_id = ?", (book_id,))
        conn.execute(
            "UPDATE source_rows SET state = 'queued', hc_book_id = NULL, queue_reason = 'stale',"
            " queued_at = ? WHERE hc_book_id = ? AND state = 'linked'",
            (now, book_id),
        )
        conn.executemany(
            "INSERT INTO change_log (profile, source, source_id, hc_book_id, hc_user_book_id, "
            "kind, at) VALUES (?, ?, ?, ?, ?, 'stale', ?)",
            [
                (profile, source, source_id, book_id, user_book, now)
                for profile, source, source_id, user_book in rows
            ],
        )
        queued += len(rows)
    return queued


QUARANTINE_AFTER = 24 * 3600
"""A change is quarantined once it has failed for this long (seconds)..."""
QUARANTINE_ATTEMPTS = 3
"""...and at least this many times (§3.4a.7)."""

ErrorClass = Literal["transient", "permanent", "bug"]


def record_failure(
    conn: sqlite3.Connection,
    baseline_ids: Iterable[int],
    error_class: ErrorClass,
    error: str,
    now: int,
) -> set[int]:
    """Count one failed attempt on a change's baselines, which stay pending (§3.4a.7). A
    ``bug`` is quarantined at once; any other class once it has failed for
    :data:`QUARANTINE_AFTER` and :data:`QUARANTINE_ATTEMPTS` times. ``error`` must already be
    scrubbed. Returns the baselines now quarantined (each keeps its own history)."""
    ids = list(baseline_ids)
    conn.executemany(
        "UPDATE baseline_fields SET attempts = attempts + 1, "
        "first_failed_at = COALESCE(first_failed_at, ?), last_error = ?, last_error_class = ?, "
        "quarantined_at = CASE WHEN ? = 'bug' OR (attempts + 1 >= ? AND "
        "? - COALESCE(first_failed_at, ?) >= ?) THEN ? ELSE quarantined_at END WHERE id = ?",
        [
            (now, error, error_class, error_class, QUARANTINE_ATTEMPTS, now, now,
             QUARANTINE_AFTER, now, baseline_id)
            for baseline_id in ids
        ],
    )  # fmt: skip
    marks = ",".join("?" * len(ids))
    found = conn.execute(
        f"SELECT id FROM baseline_fields WHERE quarantined_at IS NOT NULL "  # noqa: S608
        f"AND id IN ({marks})",
        ids,
    )
    return {row[0] for row in found}


def release_quarantine(conn: sqlite3.Connection, profile: str | None = None) -> int:
    """Release every quarantined change (of one profile, or all): it is pending again with its
    attempts started over, so the next run tries it afresh (§3.4a.7). Returns how many."""
    where = "quarantined_at IS NOT NULL" + (" AND profile = ?" if profile else "")
    return conn.execute(
        "UPDATE baseline_fields SET quarantined_at = NULL, attempts = 0, "  # noqa: S608
        f"first_failed_at = NULL WHERE {where}",
        (profile,) if profile else (),
    ).rowcount


def release_changes(
    conn: sqlite3.Connection, profile: str, change_ids: Iterable[int] | None
) -> list[int]:
    """Release the profile's own quarantined changes, these ones or all of them (``None``):
    pending again, attempts started over (§3.4a.11 ``retry_failed``). Returns the ids
    released; another profile's change, or one not quarantined, is never among them."""
    ids = None if change_ids is None else list(change_ids)
    only = "" if ids is None else f" AND id IN ({','.join('?' * len(ids))})"
    rows = conn.execute(
        "UPDATE baseline_fields SET quarantined_at = NULL, attempts = 0, "  # noqa: S608
        f"first_failed_at = NULL WHERE profile = ? AND quarantined_at IS NOT NULL{only} "
        "RETURNING id",
        (profile, *(ids or [])),
    ).fetchall()
    return sorted(row[0] for row in rows)


VERSION_KEY = "last_started_version"


def release_on_new_version(conn: sqlite3.Connection, version: str) -> int | None:
    """At startup (§2.2 step 4): when this is a different SyncShelf version from the last to
    start (a missing row counts as different), release every quarantined change once, then
    record the version, in the same transaction, so a crash part-way retries next start.
    Returns how many were released, or ``None`` when the version is unchanged."""
    found = conn.execute("SELECT value FROM meta WHERE key = ?", (VERSION_KEY,)).fetchone()
    if found is not None and found[0] == version:
        return None
    released = release_quarantine(conn)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (VERSION_KEY, version),
    )
    return released


def pending_for(
    conn: sqlite3.Connection, profile: str, source: IdentitySource, ids: Iterable[str]
) -> list[Pending]:
    """The pending changes ready to apply on these rows."""
    wanted = set(ids)
    return [p for p in _pending(conn, profile, source) if p.source_id in wanted]


def _insert_row(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    place: Placement,
    now: int,
    *,
    backfill: bool,
) -> None:
    """A new source row, linked (with its shared link) or queued (with its candidates)."""
    obs, hit = place.observation, place.hit
    if place.linked:
        assert hit is not None
        book = _link(conn, source, obs, hit, now)
        conn.execute(
            "INSERT INTO source_rows (profile, source, source_id, state, hc_book_id, "
            "hc_user_book_id, match_input, first_seen_at, last_seen_at) "
            "VALUES (?, ?, ?, 'linked', ?, ?, ?, ?, ?)",
            (profile, source, obs.source_id, book, place.hc_user_book_id,
             match_input(obs), now, now),
        )  # fmt: skip
        return
    conn.execute(
        "INSERT INTO source_rows (profile, source, source_id, state, queue_reason, queued_at, "
        "match_input, first_seen_at, last_seen_at, backfill_queued) "
        "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
        (profile, source, obs.source_id, _reason(place), now, match_input(obs), now, now,
         int(backfill)),
    )  # fmt: skip
    _write_candidates(conn, profile, source, obs.source_id, place.candidates, now)


def _reason(place: Placement) -> str:
    return place.reason or ("not_in_library" if place.hit is not None else "no_hit")


def _write_candidates(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    source_id: str,
    candidates: Iterable[Candidate],
    now: int,
) -> None:
    conn.execute(
        "DELETE FROM match_candidates WHERE profile = ? AND source = ? AND source_id = ?",
        (profile, source, source_id),
    )
    conn.executemany(
        "INSERT INTO match_candidates (profile, source, source_id, rank, hc_book_id, "
        "hc_edition_id, method, title_score, author_score, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (profile, source, source_id, rank, c.book_id, c.edition_id, c.method,
             c.title_score, c.author_score, now)
            for rank, c in enumerate(candidates, start=1)
        ],
    )  # fmt: skip


def _link(
    conn: sqlite3.Connection, source: IdentitySource, obs: Observation, hit: IdHit, now: int
) -> int:
    """The shared link for a row, returning the book it now names, which the row's writes
    target. An existing link is replaced only by a strictly stronger method, and never a
    ``human`` one (§3.4a.4 rule 5; the trigger holds the last in SQL)."""
    found = conn.execute(
        "SELECT method, hc_book_id FROM links WHERE source = ? AND source_id = ?",
        (source, obs.source_id),
    ).fetchone()
    if found is None:
        conn.execute(
            "INSERT INTO links (source, source_id, hc_book_id, hc_edition_id, format, method,"
            " linked_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, obs.source_id, hit.book_id, hit.edition_id, obs.format, hit.method, now,
             now),
        )  # fmt: skip
    elif METHOD_RANK[hit.method] > METHOD_RANK[found[0]]:
        conn.execute(
            "UPDATE links SET hc_book_id = ?, hc_edition_id = ?, method = ?, state = 'active', "
            "linked_at = ?, last_seen_at = ? WHERE source = ? AND source_id = ?",
            (hit.book_id, hit.edition_id, hit.method, now, now, source, obs.source_id),
        )
    else:
        conn.execute(
            "UPDATE links SET last_seen_at = ? WHERE source = ? AND source_id = ?",
            (now, source, obs.source_id),
        )
        return found[1]
    return hit.book_id


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
            "SELECT b.id, b.source_id, b.field, b.value, b.pending_value, r.hc_book_id, "
            "b.prev_value, b.pending_since "
            "FROM baseline_fields b JOIN source_rows r USING (profile, source, source_id) "
            "WHERE b.profile = ? AND b.source = ? AND b.pending_since IS NOT NULL "
            "AND b.quarantined_at IS NULL AND r.state = 'linked' ORDER BY b.id",
            (profile, source),
        )
    ]


def settle(
    conn: sqlite3.Connection,
    profile: str,
    source: IdentitySource,
    source_id: str,
    hc_book_id: int,
    changes: list[Pending],
    observed_at: int,
    hc_user_book_id: int | None,
    logs: list[LogRow],
    run_id: int | None,
    now: int,
) -> None:
    """After Hardcover took a change (or when it needs no write): advance each baseline it
    settles and write its ``change_log`` rows, in one transaction (§3.4a.6, rule 9)."""
    conn.executemany(
        "UPDATE baseline_fields SET prev_value = value, value = pending_value, observed_at = ?, "
        "pending_value = NULL, pending_since = NULL, attempts = 0, first_failed_at = NULL, "
        "last_error = NULL, last_error_class = NULL, quarantined_at = NULL WHERE id = ?",
        [(observed_at, change.baseline_id) for change in changes],
    )
    if hc_user_book_id is not None:
        conn.execute(
            "UPDATE source_rows SET hc_user_book_id = ? WHERE profile = ? AND source = ? "
            "AND source_id = ?",
            (hc_user_book_id, profile, source, source_id),
        )
    conn.executemany(
        "INSERT INTO change_log (run_id, profile, source, source_id, hc_book_id, "
        "hc_user_book_id, kind, field, old_value, new_value, trigger, date_source, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (run_id, profile, source, source_id, hc_book_id, hc_user_book_id, row.kind,
             row.field, row.old, row.new, row.trigger, row.date_source, now)
            for row in logs
        ],
    )  # fmt: skip


def link_editions(
    conn: sqlite3.Connection, source: IdentitySource, ids: Iterable[str]
) -> dict[str, int]:
    """The edition each row's shared link names, for rows whose link has one (§3.3.5)."""
    wanted = list(ids)
    found: dict[str, int] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        marks = ",".join("?" * len(chunk))
        found.update(
            conn.execute(
                "SELECT source_id, hc_edition_id FROM links WHERE source = ? "  # noqa: S608
                f"AND hc_edition_id IS NOT NULL AND source_id IN ({marks})",
                (source, *chunk),
            ).fetchall()
        )
    return found
