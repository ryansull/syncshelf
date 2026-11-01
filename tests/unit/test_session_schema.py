"""SQL invariants of ``0005_goodreads_session`` (SPEC §2.4, §3.4b, Appendix A): the database
itself holds one session row per profile, refuses a state the sign-in machine never writes, and
insists every state is stamped. The jar lives on disk, so nothing here stores a session."""

import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from syncshelf.db import connect
from syncshelf.db.migrate import MIGRATIONS_DIR, migrate, snapshot_path

from invariants import assert_accepted, assert_rejected

SPEC = Path(__file__).parents[2] / "docs" / "SPEC.md"
NOW = 1_767_225_600
SESSION = (
    "INSERT INTO goodreads_sessions (profile, state, state_since, detail, last_auto_sign_in_at)"
    " VALUES (?, ?, ?, ?, ?)"
)
STATES = (
    "ok",
    "needs_sign_in",
    "awaiting_approval",
    "awaiting_code",
    "challenged",
    "bad_credentials",
    "disabled",
)
"""Every state Appendix A lists, which the migration must accept from the start."""


@pytest.fixture
def session(schema: sqlite3.Connection) -> sqlite3.Connection:
    assert_accepted(schema, "INSERT INTO profiles (name, created_at) VALUES ('alice', ?)", (NOW,))
    return schema


def test_the_migration_creates_the_session_table(schema: sqlite3.Connection) -> None:
    tables = {row[0] for row in schema.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert "goodreads_sessions" in tables


def test_0005_goodreads_session_matches_appendix_a() -> None:
    spec = SPEC.read_text(encoding="utf-8")
    block = re.search(r"-- 0005_goodreads_session .*?\n(.*?)\n-- 0006_", spec, re.DOTALL)
    assert block is not None
    shipped = (MIGRATIONS_DIR / "0005_goodreads_session.sql").read_text(encoding="utf-8")
    assert _normalise(shipped) == _normalise(block.group(1))


def test_a_phase_4a_database_is_snapshotted_then_migrated(tmp_path: Path) -> None:
    phase4a = tmp_path / "phase4a"
    phase4a.mkdir()
    for name in ("0001_meta.sql", "0002_auth.sql", "0003_mirror.sql", "0004_sync.sql"):
        shutil.copy(MIGRATIONS_DIR / name, phase4a / name)
    with closing(connect(tmp_path / "db.sqlite")) as conn:
        migrate(conn, snapshot_dir=tmp_path, migrations_dir=phase4a)
        result = migrate(conn, snapshot_dir=tmp_path)
        assert (result.from_version, result.applied) == (4, [5])
        assert result.snapshot == snapshot_path(tmp_path, 4)
        assert result.snapshot.exists()


def _normalise(sql: str) -> str:
    return " ".join(re.sub(r"--[^\n]*", "", sql).split())


class TestGoodreadsSessions:
    @pytest.mark.parametrize("state", STATES)
    def test_every_state_in_the_spec_is_accepted(
        self, session: sqlite3.Connection, state: str
    ) -> None:
        assert_accepted(session, SESSION, ("alice", state, NOW, None, None))

    @pytest.mark.parametrize("state", ["signed_in", "pending", "expired", "locked", ""])
    def test_a_state_outside_the_spec_is_refused(
        self, session: sqlite3.Connection, state: str
    ) -> None:
        assert_rejected(session, SESSION, ("alice", state, NOW, None, None), match="CHECK")

    def test_one_session_per_profile(self, session: sqlite3.Connection) -> None:
        assert_accepted(session, SESSION, ("alice", "ok", NOW, None, None))
        assert_rejected(
            session, SESSION, ("alice", "needs_sign_in", NOW, None, None), match="UNIQUE"
        )

    def test_a_session_needs_a_known_profile(self, session: sqlite3.Connection) -> None:
        assert_rejected(session, SESSION, ("nobody", "ok", NOW, None, None), match="FOREIGN KEY")

    def test_every_state_is_stamped(self, session: sqlite3.Connection) -> None:
        assert_rejected(session, SESSION, ("alice", "ok", None, None, None), match="NOT NULL")

    def test_a_state_needs_no_detail_and_no_auto_sign_in(self, session: sqlite3.Connection) -> None:
        # `disabled` is set at startup for a profile with no credentials (§3.4b, #232), before
        # any sign-in has been attempted, so both optional columns stay NULL.
        assert_accepted(session, SESSION, ("alice", "disabled", NOW, None, None))
        row = session.execute(
            "SELECT detail, last_auto_sign_in_at FROM goodreads_sessions WHERE profile = 'alice'"
        ).fetchone()
        assert row == (None, None)

    def test_a_detail_and_an_auto_sign_in_stamp_are_kept(self, session: sqlite3.Connection) -> None:
        assert_accepted(session, SESSION, ("alice", "challenged", NOW, "a bot check appeared", NOW))
        row = session.execute(
            "SELECT state, detail, last_auto_sign_in_at FROM goodreads_sessions"
        ).fetchone()
        assert row == ("challenged", "a bot check appeared", NOW)

    def test_a_session_cannot_be_orphaned(self, session: sqlite3.Connection) -> None:
        # A profile taken out of the config is stamped `removed_at`, never deleted
        # (`sync_profiles`), so this is the database refusing to orphan a session rather than a
        # path the service takes.
        assert_accepted(session, SESSION, ("alice", "ok", NOW, None, None))
        assert_rejected(session, "DELETE FROM profiles WHERE name = 'alice'", match="FOREIGN KEY")
