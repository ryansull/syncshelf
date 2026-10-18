"""The purge of expired OAuth rows (SPEC §2.4 Retention, as decided in #77): what it deletes,
what it keeps at each boundary, and that it never touches a grant."""

import sqlite3

import pytest

from syncshelf.auth.tokens import (
    ACCESS_TOKEN_TTL,
    CODE_RETENTION,
    REVOKED_RETENTION,
    SCOPE,
    PurgeCounts,
    create_grant,
    hash_token,
    insert_access_token,
    insert_refresh_token,
    purge_expired,
    revoke_grant,
)
from syncshelf.profiles import sync_profiles

NOW = 4_102_444_800
DAY = 86400


@pytest.fixture
def conn(schema: sqlite3.Connection) -> sqlite3.Connection:
    sync_profiles(schema, ["alice"], NOW)
    return schema


def new_grant(conn: sqlite3.Connection, now: int = NOW) -> int:
    return create_grant(
        conn,
        profile="alice",
        email="alice@example.com",
        client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
        client_label="claude.ai",
        resource="https://shelf.example.com/mcp",
        scopes=[SCOPE, "offline_access"],
        now=now,
    )


def add_code(conn: sqlite3.Connection, expires_at: int, grant_id: int | None = None) -> str:
    code_hash = hash_token(f"code-{expires_at}-{grant_id}")
    conn.execute(
        "INSERT INTO oauth_code (code_hash, client_id, redirect_uri, code_challenge, resource, "
        "scopes, profile, email, created_at, expires_at, used_at, grant_id) "
        "VALUES (?, 'c', 'r', 'x', 'res', ?, 'alice', 'alice@example.com', ?, ?, ?, ?)",
        (code_hash, SCOPE, expires_at - 120, expires_at, None, grant_id),
    )
    return code_hash


def count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def test_nothing_to_purge_counts_zero_and_is_falsy(conn: sqlite3.Connection) -> None:
    new_grant(conn)
    counts = purge_expired(conn, NOW)
    assert counts == PurgeCounts(codes=0, access_tokens=0, refresh_tokens=0)
    assert not counts


@pytest.mark.parametrize(
    ("age", "purged"),
    [(CODE_RETENTION - 1, False), (CODE_RETENTION, False), (CODE_RETENTION + 1, True)],
)
def test_a_code_goes_more_than_a_day_after_it_expired(
    conn: sqlite3.Connection, age: int, purged: bool
) -> None:
    assert CODE_RETENTION == DAY
    add_code(conn, NOW - age)
    counts = purge_expired(conn, NOW)
    assert counts.codes == int(purged)
    assert count(conn, "oauth_code") == int(not purged)


def test_a_redeemed_code_goes_and_leaves_its_grant(conn: sqlite3.Connection) -> None:
    grant_id = new_grant(conn, NOW - 2 * DAY)
    add_code(conn, NOW - 2 * DAY, grant_id)
    assert purge_expired(conn, NOW).codes == 1
    assert count(conn, "oauth_grant") == 1


@pytest.mark.parametrize(("offset", "purged"), [(-1, True), (0, True), (1, False)])
def test_an_access_token_goes_once_it_has_expired(
    conn: sqlite3.Connection, offset: int, purged: bool
) -> None:
    # The verifier refuses a token at its expires_at, so that is when the row is dead.
    grant_id = new_grant(conn)
    insert_access_token(conn, grant_id, NOW - ACCESS_TOKEN_TTL + offset)
    counts = purge_expired(conn, NOW)
    assert counts.access_tokens == int(purged)
    assert count(conn, "oauth_access_token") == int(not purged)


@pytest.mark.parametrize(("offset", "purged"), [(-1, True), (0, True), (1, False)])
def test_refresh_tokens_go_once_their_grants_window_has_closed(
    conn: sqlite3.Connection, offset: int, purged: bool
) -> None:
    grant_id = new_grant(conn)
    insert_refresh_token(conn, grant_id, NOW)
    conn.execute(
        "UPDATE oauth_grant SET refresh_expires_at = ? WHERE id = ?", (NOW + offset, grant_id)
    )
    counts = purge_expired(conn, NOW)
    assert counts.refresh_tokens == int(purged)
    assert count(conn, "oauth_refresh_token") == int(not purged)
    assert count(conn, "oauth_grant") == 1


@pytest.mark.parametrize(
    ("age", "purged"),
    [(REVOKED_RETENTION - 1, False), (REVOKED_RETENTION, False), (REVOKED_RETENTION + 1, True)],
)
def test_refresh_tokens_go_more_than_90_days_after_their_grant_was_revoked(
    conn: sqlite3.Connection, age: int, purged: bool
) -> None:
    assert REVOKED_RETENTION == 90 * DAY
    grant_id = new_grant(conn)
    insert_refresh_token(conn, grant_id, NOW)
    # The window is still open, so only the revocation can make these rows go.
    conn.execute(
        "UPDATE oauth_grant SET refresh_expires_at = ? WHERE id = ?", (NOW + DAY, grant_id)
    )
    assert revoke_grant(conn, grant_id, "user", NOW - age)
    assert purge_expired(conn, NOW).refresh_tokens == int(purged)
    assert count(conn, "oauth_grant") == 1


def test_a_rotated_chain_goes_whole_despite_its_successor_links(
    conn: sqlite3.Connection,
) -> None:
    grant_id = new_grant(conn)
    hashes = [hash_token(insert_refresh_token(conn, grant_id, NOW)) for _ in range(3)]
    for old, new in zip(hashes, hashes[1:], strict=False):
        conn.execute(
            "UPDATE oauth_refresh_token SET rotated_at = ?, successor_hash = ? "
            "WHERE token_hash = ?",
            (NOW, new, old),
        )
    conn.execute("UPDATE oauth_grant SET refresh_expires_at = ? WHERE id = ?", (NOW, grant_id))
    assert purge_expired(conn, NOW).refresh_tokens == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_only_the_dead_rows_go_and_grants_never_do(conn: sqlite3.Connection) -> None:
    live = new_grant(conn)
    expired = new_grant(conn, NOW - 200 * DAY)  # window closed 110 days ago
    revoked = new_grant(conn)
    assert revoke_grant(conn, revoked, "admin", NOW - 100 * DAY)
    for grant_id in (live, expired, revoked):
        insert_refresh_token(conn, grant_id, NOW - 1)
        insert_access_token(conn, grant_id, NOW - ACCESS_TOKEN_TTL)  # just expired
        insert_access_token(conn, grant_id, NOW)  # live
    add_code(conn, NOW - 2 * DAY, live)
    add_code(conn, NOW)

    counts = purge_expired(conn, NOW)

    assert counts == PurgeCounts(codes=1, access_tokens=3, refresh_tokens=2)
    assert counts
    assert count(conn, "oauth_grant") == 3
    assert count(conn, "oauth_code") == 1
    assert count(conn, "oauth_access_token") == 3
    remaining = conn.execute("SELECT grant_id FROM oauth_refresh_token").fetchall()
    assert remaining == [(live,)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert purge_expired(conn, NOW) == PurgeCounts(0, 0, 0)
