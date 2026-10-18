"""Connections and the writer (SPEC §2.4): PRAGMAs on every connection, writes as one
``BEGIN IMMEDIATE … COMMIT`` on the single writer thread, reads on per-thread connections."""

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import anyio
import pytest

from syncshelf.db import Database, connect
from syncshelf.db.migrate import MigrationError


def pragmas(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        name: conn.execute(f"PRAGMA {name}").fetchone()[0]
        for name in ("journal_mode", "synchronous", "busy_timeout", "foreign_keys")
    }


EXPECTED = {"journal_mode": "wal", "synchronous": 1, "busy_timeout": 5000, "foreign_keys": 1}


class TestConnect:
    def test_sets_the_pragmas_and_autocommit(self, tmp_path: Path) -> None:
        with closing(connect(tmp_path / "x.db")) as conn:
            assert pragmas(conn) == EXPECTED
            assert conn.autocommit is True
            assert not conn.in_transaction

    def test_read_only_connections_refuse_writes(self, tmp_path: Path) -> None:
        path = tmp_path / "x.db"
        with closing(connect(path)) as writer:
            writer.execute("CREATE TABLE t (x)")
        with closing(connect(path, read_only=True)) as reader:
            assert pragmas(reader) == EXPECTED
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("INSERT INTO t VALUES (1)")


def create_t(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE t (x INTEGER NOT NULL)")


def insert(conn: sqlite3.Connection, *values: int) -> str:
    assert conn.in_transaction
    conn.executemany("INSERT INTO t VALUES (?)", [(v,) for v in values])
    return threading.current_thread().name


def select_all(conn: sqlite3.Connection) -> list[int]:
    return [x for (x,) in conn.execute("SELECT x FROM t ORDER BY x")]


@pytest.mark.anyio
class TestDatabase:
    async def test_open_migrates_and_reports_it(self, db: Database, tmp_path: Path) -> None:
        assert db.migration.applied[0] == 1
        assert db.migration.snapshot is not None
        assert db.path == tmp_path / "syncshelf.db"
        version = await db.read(lambda c: c.execute("PRAGMA user_version").fetchone()[0])
        assert version == db.migration.to_version

    async def test_every_connection_has_the_pragmas(self, db: Database) -> None:
        assert await db.write(pragmas) == EXPECTED
        assert await db.read(pragmas) == EXPECTED

    async def test_writes_run_in_one_transaction_on_the_writer_thread(self, db: Database) -> None:
        await db.write(create_t)
        names = {await db.write(insert, n) for n in range(3)}
        assert len(names) == 1
        assert names != {threading.current_thread().name}
        assert await db.read(select_all) == [0, 1, 2]

    async def test_a_failed_write_rolls_back_entirely(self, db: Database) -> None:
        await db.write(create_t)

        def half_then_fail(conn: sqlite3.Connection) -> None:
            insert(conn, 1)
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await db.write(half_then_fail)
        with pytest.raises(sqlite3.IntegrityError):
            await db.write(lambda c: c.execute("INSERT INTO t VALUES (NULL)"))
        assert await db.read(select_all) == []
        assert await db.write(insert, 5)  # the writer is still usable afterwards

    async def test_a_failed_commit_rolls_back_and_leaves_the_writer_usable(
        self, db: Database
    ) -> None:
        # A deferred foreign key is checked only at COMMIT, so the COMMIT itself fails. The
        # transaction must not stay open, or every later write would fail to BEGIN.
        def schema(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            conn.execute(
                "CREATE TABLE child (parent_id INTEGER"
                " REFERENCES parent (id) DEFERRABLE INITIALLY DEFERRED)"
            )

        await db.write(schema)
        with pytest.raises(sqlite3.IntegrityError):
            await db.write(lambda c: c.execute("INSERT INTO child VALUES (99)"))
        await db.write(lambda c: c.execute("INSERT INTO parent VALUES (1)"))
        assert await db.read(lambda c: c.execute("SELECT count(*) FROM child").fetchone()[0]) == 0

    async def test_reads_use_per_thread_read_only_connections(self, db: Database) -> None:
        await db.write(create_t)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await db.read(lambda c: c.execute("INSERT INTO t VALUES (1)"))

        seen: list[tuple[str, int]] = []

        def identify(conn: sqlite3.Connection) -> None:
            seen.append((threading.current_thread().name, id(conn)))

        async with anyio.create_task_group() as tg:
            for _ in range(8):
                tg.start_soon(db.read, identify)
        by_thread: dict[str, set[int]] = {}
        for thread, conn_id in seen:
            by_thread.setdefault(thread, set()).add(conn_id)
        assert all(len(ids) == 1 for ids in by_thread.values())
        assert len({i for ids in by_thread.values() for i in ids}) == len(by_thread)

    async def test_a_read_is_one_snapshot_released_when_it_returns(self, db: Database) -> None:
        await db.write(create_t)
        assert await db.read(lambda c: c.in_transaction) is True
        # Nothing holds the read snapshot between calls, so a later write is visible.
        await db.write(insert, 9)
        assert await db.read(select_all) == [9]

    async def test_close_releases_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "closing.db"
        db = await Database.open(path, snapshot_dir=tmp_path)
        await db.read(lambda c: c.execute("SELECT 1").fetchone())
        await db.close()
        await db.close()  # idempotent
        with pytest.raises(RuntimeError, match="closed"):
            await db.read(lambda c: None)
        with pytest.raises(RuntimeError, match="closed"):
            await db.write(lambda c: None)
        # On Windows an open handle would stop this.
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        assert not path.exists()

    async def test_a_failed_migration_closes_the_writer_and_raises(self, tmp_path: Path) -> None:
        migrations = tmp_path / "m"
        migrations.mkdir()
        (migrations / "0001_bad.sql").write_text("NOT SQL;", encoding="utf-8")
        path = tmp_path / "failed.db"
        with pytest.raises(MigrationError):
            await Database.open(path, snapshot_dir=tmp_path, migrations_dir=migrations)
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        assert not path.exists()
