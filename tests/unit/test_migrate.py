"""The migration runner (SPEC §2.4, §3.1): user_version, one transaction per file, the
pre-migration snapshot, a failed migration leaving the DB at the last good version, and a
database newer than the shipped migrations refusing to start."""

import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from syncshelf.db import connect
from syncshelf.db.migrate import (
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    discover,
    migrate,
    snapshot_path,
)

SPEC = Path(__file__).parents[2] / "docs" / "SPEC.md"

STEPS = {
    "0001_first.sql": "CREATE TABLE one (id INTEGER PRIMARY KEY);",
    "0002_second.sql": "CREATE TABLE two (id INTEGER PRIMARY KEY);\nINSERT INTO one VALUES (1);",
    "0003_third.sql": "CREATE TABLE three (id INTEGER PRIMARY KEY);",
}


def write_migrations(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8")
    return directory


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()
    return {name for (name,) in rows}


def user_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(tmp_path / "syncshelf.db")) as c:
        yield c


@pytest.fixture
def snapshots(tmp_path: Path) -> Path:
    path = tmp_path / "snapshots"
    path.mkdir()
    return path


class TestDiscover:
    def test_orders_files_by_number(self, tmp_path: Path) -> None:
        found = discover(write_migrations(tmp_path / "m", STEPS))
        assert [m.version for m in found] == [1, 2, 3]
        assert found[0] == Migration(1, "first", tmp_path / "m" / "0001_first.sql")

    def test_ignores_files_that_are_not_migrations(self, tmp_path: Path) -> None:
        directory = write_migrations(tmp_path / "m", {**STEPS, "README.txt": "", "x.sql": ""})
        assert [m.version for m in discover(directory)] == [1, 2, 3]

    @pytest.mark.parametrize(
        "files",
        [
            {"0001_a.sql": "", "0003_c.sql": ""},
            {"0002_b.sql": ""},
            {"0001_a.sql": "", "0001_b.sql": ""},
        ],
        ids=["gap", "not-from-one", "duplicate"],
    )
    def test_rejects_numbering_that_is_not_one_to_n(
        self, tmp_path: Path, files: dict[str, str]
    ) -> None:
        with pytest.raises(MigrationError, match="numbered"):
            discover(write_migrations(tmp_path / "m", files))

    def test_shipped_migrations_start_with_meta(self) -> None:
        assert discover(MIGRATIONS_DIR)[0].name == "meta"


class TestMigrate:
    def test_empty_to_each_step(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        directory = tmp_path / "m"
        expected: set[str] = set()
        for version, ((name, sql), table) in enumerate(
            zip(STEPS.items(), ["one", "two", "three"], strict=True), start=1
        ):
            write_migrations(directory, {name: sql})
            result = migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
            expected.add(table)
            assert result.applied == [version]
            assert (result.from_version, result.to_version) == (version - 1, version)
            assert user_version(conn) == version
            assert tables(conn) == expected

    def test_empty_to_latest_in_one_call(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        result = migrate(
            conn, snapshot_dir=snapshots, migrations_dir=write_migrations(tmp_path / "m", STEPS)
        )
        assert result.applied == [1, 2, 3]
        assert user_version(conn) == 3
        assert conn.execute("SELECT id FROM one").fetchall() == [(1,)]

    def test_nothing_pending_is_a_no_op_without_a_snapshot(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        directory = write_migrations(tmp_path / "m", STEPS)
        migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        for snapshot in snapshots.iterdir():
            snapshot.unlink()
        result = migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert result.applied == []
        assert result.snapshot is None
        assert list(snapshots.iterdir()) == []

    def test_each_file_commits_with_its_version_bump(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        migrate(
            conn, snapshot_dir=snapshots, migrations_dir=write_migrations(tmp_path / "m", STEPS)
        )
        assert not conn.in_transaction
        # A second connection sees the committed version, not just the migrating one.
        with closing(connect(tmp_path / "syncshelf.db")) as other:
            assert user_version(other) == 3


FIRST = {"0001_first.sql": STEPS["0001_first.sql"]}


def at_version_one(directory: Path, conn: sqlite3.Connection, snapshots: Path) -> None:
    """Migrate ``conn`` to version 1, which takes no snapshot (a fresh install)."""
    write_migrations(directory, FIRST)
    migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
    assert list(snapshots.iterdir()) == []


class TestSnapshot:
    def test_a_fresh_install_takes_no_snapshot(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        # At version 0 there is nothing to roll back to (#48).
        result = migrate(
            conn, snapshot_dir=snapshots, migrations_dir=write_migrations(tmp_path / "m", STEPS)
        )
        assert result.applied == [1, 2, 3]
        assert result.snapshot is None
        assert list(snapshots.iterdir()) == []

    def test_a_failed_fresh_install_takes_no_snapshot(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        directory = write_migrations(tmp_path / "m", {"0001_bad.sql": "NOT SQL;"})
        with pytest.raises(MigrationError):
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert list(snapshots.iterdir()) == []

    def test_snapshot_holds_the_db_as_it_was_before_migrating(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        directory = tmp_path / "m"
        at_version_one(directory, conn, snapshots)
        conn.execute("INSERT INTO one VALUES (7)")

        write_migrations(directory, STEPS)
        result = migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)

        assert result.snapshot == snapshot_path(snapshots, 1)
        assert result.snapshot.name == "pre-migration-0001.db"
        with closing(sqlite3.connect(result.snapshot)) as snap:
            assert user_version(snap) == 1
            assert tables(snap) == {"one"}
            assert snap.execute("SELECT id FROM one").fetchall() == [(7,)]

    def test_snapshot_is_taken_before_the_first_pending_file(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        # Even when the very first pending file fails, the snapshot already exists.
        directory = tmp_path / "m"
        at_version_one(directory, conn, snapshots)
        write_migrations(directory, {"0002_bad.sql": "NOT SQL;"})
        with pytest.raises(MigrationError):
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert snapshot_path(snapshots, 1).exists()

    def test_an_existing_snapshot_is_reused_and_never_overwritten(
        self,
        tmp_path: Path,
        conn: sqlite3.Connection,
        snapshots: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # The DB is still at the version the snapshot was taken at, so the existing file is
        # the true pre-migration state: keep it, say so, and migrate.
        directory = tmp_path / "m"
        at_version_one(directory, conn, snapshots)
        existing = snapshot_path(snapshots, 1)
        existing.write_bytes(b"keep me")
        write_migrations(directory, STEPS)
        with caplog.at_level(logging.INFO, logger="syncshelf.db.migrate"):
            result = migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert existing.read_bytes() == b"keep me"
        assert result.snapshot == existing
        assert user_version(conn) == 3
        assert any(getattr(r, "event", None) == "snapshot_reused" for r in caplog.records)

    def test_a_partial_snapshot_left_by_a_crash_is_replaced(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        # The snapshot is written under a temporary name and renamed once complete, so only a
        # finished snapshot ever carries the name that gets reused.
        directory = tmp_path / "m"
        at_version_one(directory, conn, snapshots)
        partial = snapshots / "pre-migration-0001.db.tmp"
        partial.write_bytes(b"half a snapshot")
        write_migrations(directory, STEPS)
        result = migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert result.snapshot == snapshot_path(snapshots, 1)
        assert not partial.exists()
        with closing(sqlite3.connect(result.snapshot)) as snap:
            assert user_version(snap) == 1


class TestFailedMigration:
    BAD = {
        **STEPS,
        "0002_second.sql": "CREATE TABLE two (id INTEGER);\nINSERT INTO nope VALUES (1);",
    }

    def test_aborts_and_leaves_the_last_good_version(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        directory = write_migrations(tmp_path / "m", self.BAD)
        with pytest.raises(MigrationError, match="0002_second") as caught:
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert caught.value.version == 2
        assert isinstance(caught.value.__cause__, sqlite3.Error)
        assert user_version(conn) == 1
        # The failed file's earlier statements rolled back with it, and 0003 never ran.
        assert tables(conn) == {"one"}
        assert not conn.in_transaction

    def test_a_rerun_after_fixing_the_file_resumes_from_the_last_good_version(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        directory = write_migrations(tmp_path / "m", self.BAD)
        with pytest.raises(MigrationError):
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        write_migrations(directory, STEPS)
        result = migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert result.applied == [2, 3]
        assert user_version(conn) == 3

    def test_a_failed_first_pending_migration_does_not_block_the_next_start(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        # At v1, 0002 fails after pre-migration-0001.db is written. Once 0002 is fixed, the
        # next start must reuse that snapshot rather than refuse to run.
        directory = tmp_path / "m"
        at_version_one(directory, conn, snapshots)
        conn.execute("INSERT INTO one VALUES (7)")

        write_migrations(directory, self.BAD)
        with pytest.raises(MigrationError):
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        snapshot = snapshot_path(snapshots, 1)
        taken = snapshot.read_bytes()

        write_migrations(directory, STEPS)
        result = migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert result.applied == [2, 3]
        assert result.snapshot == snapshot
        assert snapshot.read_bytes() == taken
        with closing(sqlite3.connect(snapshot)) as snap:
            assert user_version(snap) == 1
            assert snap.execute("SELECT id FROM one").fetchall() == [(7,)]


class TestNewerDatabase:
    def test_a_database_newer_than_the_migrations_refuses_naming_both_versions(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        # An older image started on a newer database (#48): refuse rather than run on a schema
        # this code doesn't know.
        conn.execute("PRAGMA user_version = 5")
        directory = write_migrations(tmp_path / "m", STEPS)
        with pytest.raises(MigrationError, match=r"version 5\b.*ships is 3$"):
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert user_version(conn) == 5
        assert list(snapshots.iterdir()) == []

    def test_a_database_at_the_newest_migration_starts(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        conn.execute("PRAGMA user_version = 3")
        result = migrate(
            conn, snapshot_dir=snapshots, migrations_dir=write_migrations(tmp_path / "m", STEPS)
        )
        assert (result.from_version, result.to_version, result.applied) == (3, 3, [])


class TestMeta:
    def test_shipped_migrations_bring_an_empty_db_to_meta(
        self, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        result = migrate(conn, snapshot_dir=snapshots)
        assert result.applied[0] == 1
        assert "meta" in tables(conn)

    def test_0001_meta_matches_appendix_a(self) -> None:
        spec = SPEC.read_text(encoding="utf-8")
        block = re.search(r"-- 0001_meta .*?\n(.*?)\n-- 0002_", spec, re.DOTALL)
        assert block is not None
        shipped = (MIGRATIONS_DIR / "0001_meta.sql").read_text(encoding="utf-8")
        assert normalise(shipped) == normalise(block.group(1))


def normalise(sql: str) -> str:
    """SQL without comments and with whitespace collapsed, for comparing DDL texts."""
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return " ".join(without_comments.split())
