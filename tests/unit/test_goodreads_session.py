"""The startup reconciliation of Goodreads session state (SPEC §3.4b, #232).

A profile's session state has to agree with its configuration: one without credentials can
never sign in, so it reads ``disabled``, and one that gains them is offered a sign-in. Only
the sign-in machine moves a session out of a state it genuinely reached, so no other state is
touched.
"""

import sqlite3

import pytest

from syncshelf.sync.goodreads_session import reconcile_configured

from invariants import assert_accepted

NOW = 1_767_225_600
LATER = NOW + 3600


@pytest.fixture
def db(schema: sqlite3.Connection) -> sqlite3.Connection:
    for name in ("alice", "bob"):
        assert_accepted(
            schema, "INSERT INTO profiles (name, created_at) VALUES (?, ?)", (name, NOW)
        )
    return schema


def state(conn: sqlite3.Connection, profile: str) -> tuple[str, int] | None:
    return conn.execute(
        "SELECT state, state_since FROM goodreads_sessions WHERE profile = ?", (profile,)
    ).fetchone()


def test_a_profile_without_credentials_is_disabled(db: sqlite3.Connection) -> None:
    moved = reconcile_configured(db, [], ["alice"], NOW)
    assert moved.disabled == ["alice"]
    assert state(db, "alice") == ("disabled", NOW)


def test_a_profile_with_credentials_starts_needing_a_sign_in(db: sqlite3.Connection) -> None:
    moved = reconcile_configured(db, ["alice"], [], NOW)
    assert moved.enabled == ["alice"]
    assert state(db, "alice") == ("needs_sign_in", NOW)


def test_credentials_arriving_offer_a_sign_in(db: sqlite3.Connection) -> None:
    reconcile_configured(db, [], ["alice"], NOW)
    moved = reconcile_configured(db, ["alice"], [], LATER)
    assert moved.enabled == ["alice"]
    assert state(db, "alice") == ("needs_sign_in", LATER)


def test_credentials_taken_away_disable_the_session(db: sqlite3.Connection) -> None:
    assert_accepted(
        db,
        "INSERT INTO goodreads_sessions (profile, state, state_since) VALUES ('alice','ok',?)",
        (NOW,),
    )
    moved = reconcile_configured(db, [], ["alice"], LATER)
    assert moved.disabled == ["alice"]
    assert state(db, "alice") == ("disabled", LATER)


@pytest.mark.parametrize("sticky", ["challenged", "bad_credentials"])
def test_a_sticky_state_still_disables_without_credentials(
    db: sqlite3.Connection, sticky: str
) -> None:
    # Stickiness governs unattended reuse (§3.4b.2), which cannot happen with no password.
    assert_accepted(
        db,
        "INSERT INTO goodreads_sessions (profile, state, state_since) VALUES ('alice',?,?)",
        (sticky, NOW),
    )
    reconcile_configured(db, [], ["alice"], LATER)
    assert state(db, "alice") == ("disabled", LATER)


@pytest.mark.parametrize("held", ["ok", "needs_sign_in", "awaiting_code", "challenged"])
def test_a_state_reached_by_signing_in_is_left_alone(db: sqlite3.Connection, held: str) -> None:
    assert_accepted(
        db,
        "INSERT INTO goodreads_sessions (profile, state, state_since) VALUES ('alice',?,?)",
        (held, NOW),
    )
    moved = reconcile_configured(db, ["alice"], [], LATER)
    assert moved.enabled == []
    assert state(db, "alice") == (held, NOW)


def test_running_again_changes_nothing(db: sqlite3.Connection) -> None:
    reconcile_configured(db, ["alice"], ["bob"], NOW)
    moved = reconcile_configured(db, ["alice"], ["bob"], LATER)
    assert (moved.disabled, moved.enabled) == ([], [])
    assert state(db, "alice") == ("needs_sign_in", NOW)
    assert state(db, "bob") == ("disabled", NOW)


def test_both_profiles_are_reconciled_together(db: sqlite3.Connection) -> None:
    moved = reconcile_configured(db, ["alice"], ["bob"], NOW)
    assert (moved.enabled, moved.disabled) == (["alice"], ["bob"])
    assert state(db, "alice") == ("needs_sign_in", NOW)
    assert state(db, "bob") == ("disabled", NOW)


def test_no_profiles_is_a_no_op(db: sqlite3.Connection) -> None:
    moved = reconcile_configured(db, [], [], NOW)
    assert (moved.disabled, moved.enabled) == ([], [])
    assert state(db, "alice") is None
