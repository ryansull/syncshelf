"""The grant and token store (SPEC §3.2.4): opaque prefixed tokens kept only as sha256 hex,
grants with their sliding refresh window, and revocation that keeps the first reason."""

import hashlib
import re
import sqlite3

import pytest

from syncshelf.auth.tokens import (
    ACCESS_TOKEN_TTL,
    CODE_TTL,
    REFRESH_WINDOW,
    SCOPE,
    CodeExchange,
    ExchangeRefused,
    IssuedTokens,
    create_grant,
    exchange_code,
    hash_token,
    insert_access_token,
    insert_code,
    insert_refresh_token,
    open_grants,
    revoke_grant,
    s256,
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


# --- authorization codes -----------------------------------------------------------------

CLIENT = "https://claude.ai/oauth/claude-code-client-metadata"
REDIRECT = "http://127.0.0.1:53111/callback"
RESOURCE = "https://shelf.example.com/mcp"
VERIFIER = "v" * 43


def new_code(conn: sqlite3.Connection, scopes: tuple[str, ...] = (SCOPE, "offline_access")) -> str:
    return insert_code(
        conn,
        client_id=CLIENT,
        redirect_uri=REDIRECT,
        code_challenge=s256(VERIFIER),
        resource=RESOURCE,
        scopes=scopes,
        profile="alice",
        email="alice@example.com",
        now=NOW,
    )


def presented(code: str, **overrides: str | None) -> CodeExchange:
    values: dict = {
        "code": code,
        "client_id": CLIENT,
        "client_label": "Claude Code",
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
        "resource": RESOURCE,
        **overrides,
    }
    return CodeExchange(**values)


def test_s256_is_unpadded_base64url_sha256() -> None:
    verifier = "dBjftJeZ4CVP-mB92K1uhhHpKtAy0QJE8VrtPv3EPA8"
    assert s256(verifier) == "8duhFHYBjoxPGby156xgbc7vNhaE_UtIWS5zMG_JMGw"  # 43 chars, no "="


def test_a_code_is_stored_hashed_with_a_120_second_life(conn: sqlite3.Connection) -> None:
    code = new_code(conn)
    assert re.fullmatch(TOKEN_BODY, code)
    row = conn.execute(
        "SELECT created_at, expires_at, used_at, grant_id FROM oauth_code WHERE code_hash = ?",
        (hashlib.sha256(code.encode()).hexdigest(),),
    ).fetchone()
    assert row == (NOW, NOW + 120, None, None)
    assert CODE_TTL == 120


def test_exchanging_a_code_makes_its_grant_and_tokens(conn: sqlite3.Connection) -> None:
    code = new_code(conn)
    issued = exchange_code(conn, presented(code), NOW + 5)
    assert isinstance(issued, IssuedTokens)
    assert issued.access_token.startswith("ssat_")
    assert issued.refresh_token is not None and issued.refresh_token.startswith("ssrt_")
    [grant] = open_grants(conn, "alice")
    assert (grant.id, grant.client_label, grant.resource) == (
        issued.grant_id,
        "Claude Code",
        RESOURCE,
    )
    assert grant.scopes == issued.scopes == (SCOPE, "offline_access")
    assert grant.created_at == NOW + 5
    row = conn.execute("SELECT used_at, grant_id FROM oauth_code").fetchone()
    assert row == (NOW + 5, issued.grant_id)


def test_no_offline_access_means_no_refresh_token(conn: sqlite3.Connection) -> None:
    issued = exchange_code(conn, presented(new_code(conn, (SCOPE,))), NOW)
    assert isinstance(issued, IssuedTokens)
    assert issued.refresh_token is None
    assert conn.execute("SELECT count(*) FROM oauth_refresh_token").fetchone() == (0,)


def test_a_code_expires_at_120_seconds(conn: sqlite3.Connection) -> None:
    code = new_code(conn)
    refused = exchange_code(conn, presented(code), NOW + CODE_TTL)
    assert refused == ExchangeRefused("invalid_grant", "code_expired")


def test_an_unknown_code_is_refused(conn: sqlite3.Connection) -> None:
    assert exchange_code(conn, presented("nope"), NOW) == ExchangeRefused(
        "invalid_grant", "code_unknown"
    )


def test_a_replay_revokes_the_grant_the_code_produced(conn: sqlite3.Connection) -> None:
    code = new_code(conn)
    issued = exchange_code(conn, presented(code), NOW)
    assert isinstance(issued, IssuedTokens)
    refused = exchange_code(conn, presented(code), NOW + 1)
    assert refused == ExchangeRefused("invalid_grant", "code_replay", issued.grant_id)
    assert open_grants(conn, "alice") == []
    # A second replay finds the grant already revoked and keeps the first revocation.
    again = exchange_code(conn, presented(code), NOW + 2)
    assert again == ExchangeRefused("invalid_grant", "code_replay")


def test_a_replay_after_a_failed_exchange_has_no_grant_to_revoke(
    conn: sqlite3.Connection,
) -> None:
    code = new_code(conn)
    first = exchange_code(conn, presented(code, code_verifier="w" * 43), NOW)
    assert first == ExchangeRefused("invalid_grant", "pkce_mismatch")
    assert exchange_code(conn, presented(code), NOW + 1) == ExchangeRefused(
        "invalid_grant", "code_replay"
    )
    assert conn.execute("SELECT count(*) FROM oauth_grant").fetchone() == (0,)


def test_a_removed_profiles_code_is_refused(conn: sqlite3.Connection) -> None:
    code = new_code(conn)
    sync_profiles(conn, ["bob"], NOW + 1)
    assert exchange_code(conn, presented(code), NOW + 2) == ExchangeRefused(
        "invalid_grant", "profile_removed"
    )
