"""The grant and token store (SPEC §3.2.4): opaque prefixed tokens kept only as sha256 hex,
grants with their sliding refresh window, and revocation that keeps the first reason."""

import hashlib
import re
import sqlite3

import pytest

from syncshelf.auth.tokens import (
    ACCESS_TOKEN_TTL,
    REFRESH_WINDOW,
    SCOPE,
    create_grant,
    hash_token,
    insert_access_token,
    insert_refresh_token,
    open_grants,
    revoke_grant,
    touch_grant,
)
from syncshelf.profiles import sync_profiles

NOW = 4_102_444_800
TOKEN_BODY = r"[A-Za-z0-9_-]{43}"  # token_urlsafe(32)


@pytest.fixture
def conn(schema: sqlite3.Connection) -> sqlite3.Connection:
    sync_profiles(schema, ["alice", "bob"], NOW)
    return schema


def new_grant(conn: sqlite3.Connection, profile: str = "alice", now: int = NOW) -> int:
    return create_grant(
        conn,
        profile=profile,
        email=f"{profile}@example.com",
        client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
        client_label="claude.ai",
        resource="https://shelf.example.com/mcp",
        scopes=[SCOPE, "offline_access"],
        now=now,
    )


def test_a_grant_starts_used_now_with_a_full_refresh_window(conn: sqlite3.Connection) -> None:
    grant_id = new_grant(conn)
    [grant] = open_grants(conn, "alice")
    assert grant.id == grant_id
    assert grant.scopes == (SCOPE, "offline_access")
    assert (grant.created_at, grant.last_used_at) == (NOW, NOW)
    assert grant.refresh_expires_at == NOW + REFRESH_WINDOW == NOW + 90 * 86400
    assert grant.revoked_at is None
    stored = conn.execute("SELECT scopes FROM oauth_grant WHERE id = ?", (grant_id,)).fetchone()
    assert stored == ("syncshelf offline_access",)


@pytest.mark.parametrize(
    ("insert", "table", "prefix"),
    [
        (insert_access_token, "oauth_access_token", "ssat_"),
        (insert_refresh_token, "oauth_refresh_token", "ssrt_"),
    ],
)
def test_tokens_are_prefixed_and_only_their_hash_is_stored(
    conn: sqlite3.Connection, insert, table: str, prefix: str
) -> None:
    grant_id = new_grant(conn)
    raw = insert(conn, grant_id, NOW)
    assert re.fullmatch(prefix + TOKEN_BODY, raw)
    assert insert(conn, grant_id, NOW) != raw
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    assert hashlib.sha256(raw.encode()).hexdigest() == hash_token(raw)
    assert hash_token(raw) in {row[0] for row in rows}
    assert raw not in repr(rows)


def test_an_access_token_lives_an_hour(conn: sqlite3.Connection) -> None:
    raw = insert_access_token(conn, new_grant(conn), NOW)
    row = conn.execute(
        "SELECT created_at, expires_at, revoked_at FROM oauth_access_token WHERE token_hash = ?",
        (hash_token(raw),),
    ).fetchone()
    assert row == (NOW, NOW + ACCESS_TOKEN_TTL, None)
    assert ACCESS_TOKEN_TTL == 3600


def test_open_grants_are_the_profiles_own_unrevoked_ones(conn: sqlite3.Connection) -> None:
    first, second = new_grant(conn), new_grant(conn, now=NOW + 1)
    new_grant(conn, "bob")
    assert revoke_grant(conn, first, "user", NOW + 2)
    assert [g.id for g in open_grants(conn, "alice")] == [second]


def test_a_second_revocation_keeps_the_first(conn: sqlite3.Connection) -> None:
    grant_id = new_grant(conn)
    assert revoke_grant(conn, grant_id, "code_replay", NOW + 1)
    assert not revoke_grant(conn, grant_id, "user", NOW + 2)
    row = conn.execute(
        "SELECT revoked_at, revoked_reason FROM oauth_grant WHERE id = ?", (grant_id,)
    ).fetchone()
    assert row == (NOW + 1, "code_replay")


def test_touch_moves_last_used_at_most_once_a_minute(conn: sqlite3.Connection) -> None:
    grant_id = new_grant(conn)
    assert not touch_grant(conn, grant_id, NOW + 59)
    assert touch_grant(conn, grant_id, NOW + 60)
    assert not touch_grant(conn, grant_id, NOW + 61)
    [grant] = open_grants(conn, "alice")
    assert grant.last_used_at == NOW + 60
