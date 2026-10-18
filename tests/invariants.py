"""The harness for SQL-invariant tests (SPEC §2.4, §3.1).

An invariant test proves that the **database itself** refuses a bad write: a trigger, a
``CHECK``, a ``NOT NULL`` or a unique index, not the Python code in front of it. Phases 4a and
6 write theirs with the migration that creates each table, using :func:`assert_rejected`
against the ``schema`` fixture (a connection migrated by the shipped migrations).
"""

import sqlite3
from collections.abc import Sequence

type Params = Sequence[object] | dict[str, object]


def assert_rejected(
    conn: sqlite3.Connection, sql: str, params: Params = (), *, match: str = ""
) -> sqlite3.IntegrityError:
    """Run one write in its own transaction and assert SQLite refuses it.

    The write must fail with ``sqlite3.IntegrityError`` (constraint, ``CHECK``, unique index or a
    trigger's ``RAISE(ABORT, …)``) whose message contains ``match``. The transaction is rolled
    back either way, so a write that wrongly succeeds leaves no trace for the next assertion.
    Returns the error so a test can inspect it further.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError as error:
        if match not in str(error):
            raise AssertionError(f"rejected, but not for {match!r}: {error}") from error
        return error
    finally:
        # A trigger's RAISE(ROLLBACK, …) has already ended the transaction.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    raise AssertionError(f"the database accepted a write it should refuse: {sql}")


def assert_accepted(conn: sqlite3.Connection, sql: str, params: Params = ()) -> None:
    """Run one write and commit it, failing the test if SQLite refuses it.

    The control for :func:`assert_rejected`: the same statement with the bad value fixed should
    pass, so the rejection is known to come from the invariant under test.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(sql, params)
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
