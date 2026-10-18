"""The migration runner (SPEC §2.4, §3.1): user_version, one transaction per file, the
pre-migration snapshot, and a failed migration leaving the DB at the last good version."""

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


class TestSnapshot:
    def test_snapshot_holds_the_db_as_it_was_before_migrating(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        directory = tmp_path / "m"
        write_migrations(directory, {"0001_first.sql": STEPS["0001_first.sql"]})
        migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
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
        directory = write_migrations(tmp_path / "m", {"0001_bad.sql": "NOT SQL;"})
        with pytest.raises(MigrationError):
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert snapshot_path(snapshots, 0).exists()

    def test_an_existing_snapshot_is_never_overwritten(
        self, tmp_path: Path, conn: sqlite3.Connection, snapshots: Path
    ) -> None:
        existing = snapshot_path(snapshots, 0)
        existing.write_bytes(b"keep me")
        directory = write_migrations(tmp_path / "m", STEPS)
        with pytest.raises(MigrationError, match="snapshot"):
            migrate(conn, snapshot_dir=snapshots, migrations_dir=directory)
        assert existing.read_bytes() == b"keep me"
        assert user_version(conn) == 0


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
