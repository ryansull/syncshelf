"""The forward-only migration runner (SPEC §2.4).

Migrations are the numbered files ``migrations/NNNN_<name>.sql``, and ``PRAGMA user_version``
records the last one applied. Before applying anything, the runner snapshots the database with
``VACUUM INTO``, or reuses the snapshot an earlier failed start already took. Each pending
file then runs in its own ``BEGIN IMMEDIATE`` transaction together with its ``user_version``
bump, so a failure leaves the database at the last good version and the runner stops there.
There are no down-migrations.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
"""Where the shipped migration files live."""

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")

log = logging.getLogger(__name__)


class MigrationError(Exception):
    """A migration could not be applied, or the migration set itself is unusable.

    ``version`` is the migration that failed, when there is one. The database is left at the
    last good version, and startup must abort.
    """

    def __init__(self, message: str, version: int | None = None) -> None:
        super().__init__(message)
        self.version = version


@dataclass(frozen=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    path: Path


@dataclass(frozen=True)
class MigrationResult:
    """What one run of :func:`migrate` did, for the startup log."""

    from_version: int
    to_version: int
    applied: list[int] = field(default_factory=list[int])
    snapshot: Path | None = None
    """The pre-migration snapshot, or ``None`` when nothing was pending."""


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """List the migrations in ``directory`` in order.

    Files not named ``NNNN_<name>.sql`` are ignored. The numbers must run 1, 2, … with no gap
    or repeat, because ``user_version`` alone says which have been applied.
    """
    found: list[Migration] = []
    for path in directory.iterdir():
        if m := _FILENAME.match(path.name):
            found.append(Migration(int(m.group(1)), m.group(2), path))
    found.sort(key=lambda m: m.version)
    numbers = [m.version for m in found]
    if numbers != list(range(1, len(found) + 1)):
        raise MigrationError(f"migrations must be numbered 1..N with no gaps; found {numbers}")
    return found


def snapshot_path(snapshot_dir: Path, version: int) -> Path:
    """The snapshot of a database at ``version``, taken before migrating it further."""
    # NNNN as the snapshot's own version is provisional until #48 settles it.
    return snapshot_dir / f"pre-migration-{version:04d}.db"


def user_version(conn: sqlite3.Connection) -> int:
    """The number of the last migration applied to ``conn``'s database."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(
    conn: sqlite3.Connection,
    *,
    snapshot_dir: Path,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> MigrationResult:
    """Bring ``conn``'s database up to the newest migration in ``migrations_dir``.

    ``conn`` must be an autocommit connection from :func:`syncshelf.db.connect` with no
    transaction open; in the service it is the writer. If anything is pending, the database is
    first copied to ``pre-migration-NNNN.db`` in ``snapshot_dir`` (``NNNN`` being the version it
    is at). If that snapshot already exists, it is kept as it is and reused: the database is
    still at that version, so the file is its true pre-migration state (left by an earlier
    start whose first pending migration failed). It is never overwritten. Raises
    :class:`MigrationError` on any failure, after which the database is at the last version
    that applied cleanly.
    """
    current = user_version(conn)
    # A database newer than the shipped migrations passes through unchecked: see #48.
    pending = [m for m in discover(migrations_dir) if m.version > current]
    if not pending:
        return MigrationResult(current, current)

    snapshot = snapshot_path(snapshot_dir, current)
    if snapshot.exists():
        log.info(
            "reusing the existing pre-migration snapshot %s",
            snapshot,
            extra={"event": "snapshot_reused", "snapshot": str(snapshot)},
        )
    else:
        _take_snapshot(conn, snapshot)

    applied: list[int] = []
    for migration in pending:
        _apply(conn, migration)
        applied.append(migration.version)
    return MigrationResult(current, applied[-1], applied, snapshot)


def _take_snapshot(conn: sqlite3.Connection, snapshot: Path) -> None:
    # Written under a temporary name and renamed once complete, because an existing snapshot
    # is reused as is: a crash part-way through must never leave a partial file under the
    # final name. A leftover temporary file is from such a crash, and is always safe to drop.
    partial = snapshot.with_name(snapshot.name + ".tmp")
    try:
        partial.unlink(missing_ok=True)
        conn.execute("VACUUM INTO ?", (str(partial),))
        partial.rename(snapshot)
    except (OSError, sqlite3.Error) as error:
        raise MigrationError(f"could not write the pre-migration snapshot {snapshot}") from error


def _apply(conn: sqlite3.Connection, migration: Migration) -> None:
    sql = migration.path.read_text(encoding="utf-8")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # With autocommit=True, executescript adds no COMMIT of its own, so the whole file and
        # the version bump stay in this one transaction.
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {migration.version:d}")
        conn.execute("COMMIT")
    except sqlite3.Error as error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(
            f"migration {migration.path.name} failed: {error}", migration.version
        ) from error
