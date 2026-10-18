"""The profiles table sync (SPEC §2.3): every configured name is upserted at startup, a name
gone from the file is marked removed and its open grants revoked, and a name added back
comes back to life with its old grants still revoked."""

import sqlite3

from syncshelf.profiles import ProfileSync, sync_profiles

from clock import FakeClock

GRANT = (
    "INSERT INTO oauth_grant (profile, email, client_id, client_label, resource, scopes, "
    "created_at, last_used_at, refresh_expires_at, revoked_at, revoked_reason) "
    "VALUES (?, ?, 'client', 'claude.ai', 'https://shelf.example.com/mcp', 'syncshelf', "
    "0, 0, 0, ?, ?)"
)


def run(conn: sqlite3.Connection, names: list[str], now: float) -> ProfileSync:
    conn.execute("BEGIN IMMEDIATE")
    result = sync_profiles(conn, names, int(now))
    conn.execute("COMMIT")
    return result


def profiles(conn: sqlite3.Connection) -> dict[str, tuple[int, int | None]]:
    rows = conn.execute("SELECT name, created_at, removed_at FROM profiles")
    return {name: (created, removed) for name, created, removed in rows}


def grants(conn: sqlite3.Connection) -> list[tuple[str, int | None, str | None]]:
    rows = conn.execute("SELECT profile, revoked_at, revoked_reason FROM oauth_grant ORDER BY id")
    return list(rows)


def add_grant(
    conn: sqlite3.Connection, profile: str, revoked_at: int | None = None, reason: str | None = None
) -> None:
    conn.execute(GRANT, (profile, f"{profile}@example.com", revoked_at, reason))


def test_every_configured_name_is_inserted(schema: sqlite3.Connection, clock: FakeClock) -> None:
    result = run(schema, ["alice", "bob"], clock.time())
    now = int(clock.time())
    assert profiles(schema) == {"alice": (now, None), "bob": (now, None)}
    assert result == ProfileSync(added=["alice", "bob"], removed=[], restored=[], revoked=0)


def test_a_second_start_changes_nothing(schema: sqlite3.Connection, clock: FakeClock) -> None:
    run(schema, ["alice", "bob"], clock.time())
    before = profiles(schema)
    clock.advance(3600)
    assert run(schema, ["bob", "alice"], clock.time()) == ProfileSync([], [], [], 0)
    assert profiles(schema) == before


def test_a_name_gone_from_the_file_is_removed_and_its_open_grants_revoked(
    schema: sqlite3.Connection, clock: FakeClock
) -> None:
    start = int(clock.time())
    run(schema, ["alice", "bob"], start)
    add_grant(schema, "alice")
    add_grant(schema, "alice")
    add_grant(schema, "alice", start, "user")
    add_grant(schema, "bob")
    clock.advance(60)
    now = int(clock.time())

    result = run(schema, ["bob"], now)

    assert result == ProfileSync(added=[], removed=["alice"], restored=[], revoked=2)
    assert profiles(schema) == {"alice": (start, now), "bob": (start, None)}
    assert grants(schema) == [
        ("alice", now, "profile_deleted"),
        ("alice", now, "profile_deleted"),
        ("alice", start, "user"),  # an already revoked grant keeps its own reason
        ("bob", None, None),
    ]


def test_a_removed_profile_keeps_its_first_removal_time(
    schema: sqlite3.Connection, clock: FakeClock
) -> None:
    run(schema, ["alice", "bob"], clock.time())
    clock.advance(60)
    removed = int(clock.time())
    run(schema, ["bob"], removed)
    clock.advance(60)
    assert run(schema, ["bob"], clock.time()) == ProfileSync([], [], [], 0)
    assert profiles(schema)["alice"][1] == removed


def test_re_adding_a_name_clears_removed_at_and_its_old_grants_stay_revoked(
    schema: sqlite3.Connection, clock: FakeClock
) -> None:
    start = int(clock.time())
    run(schema, ["alice"], start)
    add_grant(schema, "alice")
    clock.advance(60)
    removed = int(clock.time())
    run(schema, [], removed)
    clock.advance(60)

    result = run(schema, ["alice"], clock.time())

    assert result == ProfileSync(added=[], removed=[], restored=["alice"], revoked=0)
    assert profiles(schema) == {"alice": (start, None)}
    assert grants(schema) == [("alice", removed, "profile_deleted")]


def test_an_empty_profiles_section_removes_every_profile(
    schema: sqlite3.Connection, clock: FakeClock
) -> None:
    run(schema, ["alice", "bob"], clock.time())
    result = run(schema, [], clock.time())
    assert result.removed == ["alice", "bob"]
    assert all(removed is not None for _, removed in profiles(schema).values())
