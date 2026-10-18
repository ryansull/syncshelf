"""The database layer every phase writes through (SPEC §2.4).

There is exactly one **writer** connection, owned by a single-thread executor. A write is one
plain sync function run there inside one ``BEGIN IMMEDIATE … COMMIT``, so single-writer is a
structural fact rather than a convention. That matters for safety, not just tidiness: the base
image's SQLite is inside the WAL-reset bug range, which needs two connections writing or
checkpointing at once. Reads run through ``asyncio.to_thread`` on per-thread connections that
are opened ``query_only``, so a read can never become a second writer.
"""

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from syncshelf.db.migrate import MIGRATIONS_DIR, MigrationResult, migrate

__all__ = ["Database", "MigrationResult", "connect"]

_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)


def connect(
    path: Path, *, read_only: bool = False, check_same_thread: bool = True
) -> sqlite3.Connection:
    """Open ``path`` the one way SyncShelf opens SQLite.

    The connection is in autocommit mode (callers issue ``BEGIN`` themselves) with WAL,
    ``synchronous=NORMAL``, ``busy_timeout=5000`` and foreign keys on. ``read_only`` sets
    ``query_only``, so any write through it fails.
    """
    conn = sqlite3.connect(path, autocommit=True, check_same_thread=check_same_thread)
    try:
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        if read_only:
            conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


class Database:
    """The open database: one writer thread and per-thread read connections.

    Make one with :meth:`open` at startup and :meth:`close` it on shutdown. Every unit of work
    is a plain sync function taking the connection as its first argument::

        await db.write(record_run, profile, source)   # one BEGIN IMMEDIATE … COMMIT
        rows = await db.read(list_runs, profile)       # one read snapshot
    """

    def __init__(
        self,
        path: Path,
        migration: MigrationResult,
        writer: sqlite3.Connection,
        writer_executor: ThreadPoolExecutor,
    ) -> None:
        """Use :meth:`open`; this only records what ``open`` already set up."""
        self.path = path
        """The database file."""
        self.migration = migration
        """What :meth:`open`'s migration run did, for the startup log."""
        self._writer_executor: ThreadPoolExecutor | None = writer_executor
        self._writer: sqlite3.Connection | None = writer
        self._local = threading.local()
        self._readers: list[sqlite3.Connection] = []
        self._readers_lock = threading.Lock()
        self._closed = False

    @classmethod
    async def open(
        cls,
        path: Path,
        *,
        snapshot_dir: Path,
        migrations_dir: Path = MIGRATIONS_DIR,
    ) -> Database:
        """Open the writer, migrate on it, and return the ready database.

        Pending migrations are applied as :func:`syncshelf.db.migrate.migrate` describes, with
        the pre-migration snapshot written to ``snapshot_dir``. On a failed migration the
        writer is closed and :class:`~syncshelf.db.migrate.MigrationError` propagates, so
        startup aborts with nothing left open.
        """
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="syncshelf-db-writer")
        loop = asyncio.get_running_loop()
        try:
            writer = await loop.run_in_executor(executor, connect, path)
        except BaseException:
            executor.shutdown(wait=True)
            raise
        try:
            result = await loop.run_in_executor(
                executor,
                partial(migrate, writer, snapshot_dir=snapshot_dir, migrations_dir=migrations_dir),
            )
        except BaseException:
            await loop.run_in_executor(executor, writer.close)
            executor.shutdown(wait=True)
            raise
        return cls(path, result, writer, executor)

    async def write[*Ts, R](self, fn: Callable[[sqlite3.Connection, *Ts], R], *args: *Ts) -> R:
        """Run ``fn(conn, *args)`` on the writer inside one ``BEGIN IMMEDIATE … COMMIT``.

        If ``fn`` raises, the transaction is rolled back and the exception propagates. ``fn``
        must not manage the transaction itself.
        """
        if self._closed or self._writer_executor is None:
            raise RuntimeError("the database is closed")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._writer_executor, self._transact, fn, args)

    def _transact[*Ts, R](self, fn: Callable[[sqlite3.Connection, *Ts], R], args: tuple[*Ts]) -> R:
        conn = self._writer
        assert conn is not None
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn, *args)
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result

    async def read[*Ts, R](self, fn: Callable[[sqlite3.Connection, *Ts], R], *args: *Ts) -> R:
        """Run ``fn(conn, *args)`` on this thread's read connection, in one read snapshot.

        The connection is ``query_only``: a write through it fails. The snapshot is released
        when ``fn`` returns, so a long-lived reader never holds back a checkpoint.
        """
        if self._closed:
            raise RuntimeError("the database is closed")
        return await asyncio.to_thread(self._snapshot, fn, args)

    def _snapshot[*Ts, R](self, fn: Callable[[sqlite3.Connection, *Ts], R], args: tuple[*Ts]) -> R:
        conn = self._reader()
        conn.execute("BEGIN")
        try:
            return fn(conn, *args)
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")

    def _reader(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            # check_same_thread=False only so close() can close it from the event loop's
            # thread; in use, each read connection belongs to the one thread that opened it.
            conn = connect(self.path, read_only=True, check_same_thread=False)
            with self._readers_lock:
                if self._closed:
                    conn.close()
                    raise RuntimeError("the database is closed")
                self._readers.append(conn)
            self._local.conn = conn
        return conn

    async def close(self) -> None:
        """Close every connection and stop the writer thread. Safe to call twice.

        Call it once no reads or writes are in flight (the lifespan does, after cancelling the
        sync loops).
        """
        if self._closed:
            return
        with self._readers_lock:
            self._closed = True
            readers, self._readers = self._readers, []
        for conn in readers:
            conn.close()
        executor, self._writer_executor = self._writer_executor, None
        writer, self._writer = self._writer, None
        if executor is not None:
            if writer is not None:
                await asyncio.get_running_loop().run_in_executor(executor, writer.close)
            executor.shutdown(wait=True)
