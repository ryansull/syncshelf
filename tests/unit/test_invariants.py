"""The SQL-invariant harness, exercised on ``meta`` (the only phase 1 table).

Phases 4a and 6 add the real invariant tests (sticky-human trigger, state CHECKs, the one-pending
index) beside the migrations that create those tables (SPEC §3.1, Appendix D6).
"""

import sqlite3

import pytest

from invariants import assert_accepted, assert_rejected

INSERT = "INSERT INTO meta (key, value) VALUES (?, ?)"


def test_schema_fixture_is_migrated_to_the_latest_version(schema: sqlite3.Connection) -> None:
    assert schema.execute("PRAGMA user_version").fetchone()[0] >= 1


def test_the_db_rejects_a_bad_write(schema: sqlite3.Connection) -> None:
    error = assert_rejected(schema, INSERT, ("last_started_version", None), match="NOT NULL")
    assert "meta.value" in str(error)
    assert_accepted(schema, INSERT, ("last_started_version", "0.1.0"))
    assert_rejected(schema, INSERT, ("last_started_version", "0.2.0"), match="UNIQUE")
    assert schema.execute("SELECT value FROM meta").fetchall() == [("0.1.0",)]


def test_a_rejected_write_leaves_no_transaction_open(schema: sqlite3.Connection) -> None:
    assert_rejected(schema, INSERT, ("k", None))
    assert not schema.in_transaction


def test_an_accepted_bad_write_fails_the_test_and_is_rolled_back(
    schema: sqlite3.Connection,
) -> None:
    with pytest.raises(AssertionError, match="accepted"):
        assert_rejected(schema, INSERT, ("k", "v"))
    assert schema.execute("SELECT count(*) FROM meta").fetchone()[0] == 0


def test_a_rejection_for_the_wrong_reason_fails_the_test(schema: sqlite3.Connection) -> None:
    with pytest.raises(AssertionError, match="not for 'CHECK'"):
        assert_rejected(schema, INSERT, ("k", None), match="CHECK")


def test_a_rollback_trigger_is_handled(schema: sqlite3.Connection) -> None:
    schema.execute(
        "CREATE TEMP TRIGGER no_k BEFORE INSERT ON meta WHEN NEW.key = 'k' "
        "BEGIN SELECT RAISE(ROLLBACK, 'k is reserved'); END"
    )
    assert_rejected(schema, INSERT, ("k", "v"), match="k is reserved")
    assert not schema.in_transaction


def test_accepted_rolls_back_and_reraises_a_refusal(schema: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        assert_accepted(schema, INSERT, ("k", None))
    assert not schema.in_transaction
