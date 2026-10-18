"""SQL invariants of ``0002_auth`` (SPEC §2.4, Appendix A): the database itself refuses a
grant whose revocation is half-recorded or has an unknown reason, and deleting a grant takes
its tokens with it."""

import sqlite3

import pytest

from invariants import assert_accepted, assert_rejected

NOW = 1_767_225_600

GRANT = (
    "INSERT INTO oauth_grant (id, profile, email, client_id, client_label, resource, scopes, "
    "created_at, last_used_at, refresh_expires_at, revoked_at, revoked_reason) "
    "VALUES (?, 'alice', 'alice@example.com', 'https://claude.ai/oauth/mcp-oauth-client-metadata', "
    "'claude.ai', 'https://shelf.example.com/mcp', 'syncshelf', ?, ?, ?, ?, ?)"
)
REVOKE = "UPDATE oauth_grant SET revoked_at = ?, revoked_reason = ? WHERE id = 1"
REASONS = ("user", "admin", "code_replay", "refresh_reuse", "profile_deleted")


def grant(grant_id: int, revoked_at: int | None = None, reason: str | None = None) -> tuple:
    return (grant_id, NOW, NOW, NOW + 90 * 86400, revoked_at, reason)


@pytest.fixture
def auth(schema: sqlite3.Connection) -> sqlite3.Connection:
    assert_accepted(schema, "INSERT INTO profiles (name, created_at) VALUES ('alice', ?)", (NOW,))
    return schema


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_the_migration_creates_every_auth_table(schema: sqlite3.Connection) -> None:
    tables = {row[0] for row in schema.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert {
        "profiles",
        "oauth_grant",
        "oauth_code",
        "oauth_refresh_token",
        "oauth_access_token",
        "oauth_client_cache",
    } <= tables


class TestProfiles:
    @pytest.mark.parametrize("name", ["Alice", "1alice", "", "a" * 33])
    def test_a_bad_name_is_refused(self, schema: sqlite3.Connection, name: str) -> None:
        sql = "INSERT INTO profiles (name, created_at) VALUES (?, ?)"
        assert_rejected(schema, sql, (name, NOW), match="CHECK")
        assert_accepted(schema, sql, ("a" * 32, NOW))

    def test_a_grant_needs_an_existing_profile(self, schema: sqlite3.Connection) -> None:
        assert_rejected(schema, GRANT, grant(1), match="FOREIGN KEY")


class TestGrantRevocation:
    def test_an_open_grant_is_accepted(self, auth: sqlite3.Connection) -> None:
        assert_accepted(auth, GRANT, grant(1))

    @pytest.mark.parametrize("reason", REASONS)
    def test_every_spec_reason_is_accepted(self, auth: sqlite3.Connection, reason: str) -> None:
        assert_accepted(auth, GRANT, grant(1, NOW, reason))

    def test_an_unknown_reason_is_refused(self, auth: sqlite3.Connection) -> None:
        assert_rejected(auth, GRANT, grant(1, NOW, "expired"), match="CHECK")

    def test_revoked_at_without_a_reason_is_refused(self, auth: sqlite3.Connection) -> None:
        assert_rejected(auth, GRANT, grant(1, NOW, None), match="CHECK")

    def test_a_reason_without_revoked_at_is_refused(self, auth: sqlite3.Connection) -> None:
        assert_rejected(auth, GRANT, grant(1, None, "user"), match="CHECK")

    def test_revoking_must_set_both_columns(self, auth: sqlite3.Connection) -> None:
        assert_accepted(auth, GRANT, grant(1))
        assert_rejected(auth, REVOKE, (NOW, None), match="CHECK")
        assert_rejected(auth, REVOKE, (None, "admin"), match="CHECK")
        assert_accepted(auth, REVOKE, (NOW, "admin"))


class TestCascades:
    def test_deleting_a_grant_deletes_its_tokens(self, auth: sqlite3.Connection) -> None:
        assert_accepted(auth, GRANT, grant(1))
        assert_accepted(auth, GRANT, grant(2))
        for grant_id in (1, 2):
            assert_accepted(
                auth,
                "INSERT INTO oauth_access_token (token_hash, grant_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (f"access-{grant_id}", grant_id, NOW, NOW + 3600),
            )
        assert_accepted(
            auth,
            "INSERT INTO oauth_refresh_token (token_hash, grant_id, created_at) VALUES (?, 1, ?)",
            ("refresh-new", NOW),
        )
        assert_accepted(
            auth,
            "INSERT INTO oauth_refresh_token (token_hash, grant_id, created_at, rotated_at, "
            "successor_hash) VALUES ('refresh-old', 1, ?, ?, 'refresh-new')",
            (NOW, NOW),
        )

        assert_accepted(auth, "DELETE FROM oauth_grant WHERE id = 1")

        assert count(auth, "oauth_refresh_token") == 0
        assert auth.execute("SELECT grant_id FROM oauth_access_token").fetchall() == [(2,)]

    def test_a_token_needs_an_existing_grant(self, auth: sqlite3.Connection) -> None:
        assert_rejected(
            auth,
            "INSERT INTO oauth_access_token (token_hash, grant_id, created_at, expires_at) "
            "VALUES ('access', 9, ?, ?)",
            (NOW, NOW + 3600),
            match="FOREIGN KEY",
        )

    def test_a_redeemed_code_keeps_its_grant_from_being_deleted(
        self, auth: sqlite3.Connection
    ) -> None:
        # oauth_code.grant_id has no cascade: a code row is evidence for replay detection.
        assert_accepted(auth, GRANT, grant(1))
        assert_accepted(
            auth,
            "INSERT INTO oauth_code (code_hash, client_id, redirect_uri, code_challenge, "
            "resource, scopes, profile, email, created_at, expires_at, used_at, grant_id) "
            "VALUES ('code', 'client', 'https://claude.ai/api/mcp/auth_callback', 'challenge', "
            "'https://shelf.example.com/mcp', 'syncshelf', 'alice', 'alice@example.com', "
            "?, ?, ?, 1)",
            (NOW, NOW + 120, NOW),
        )
        assert_rejected(auth, "DELETE FROM oauth_grant WHERE id = 1", match="FOREIGN KEY")


def test_a_client_document_over_5_kib_is_refused(schema: sqlite3.Connection) -> None:
    sql = (
        "INSERT INTO oauth_client_cache (client_id, document_json, fetched_at, fresh_until) "
        "VALUES ('https://claude.ai/oauth/mcp-oauth-client-metadata', ?, ?, ?)"
    )
    assert_rejected(schema, sql, ("x" * 5121, NOW, NOW), match="CHECK")
    assert_accepted(schema, sql, ("x" * 5120, NOW, NOW))
