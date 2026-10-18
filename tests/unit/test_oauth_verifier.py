"""The ``TokenVerifier`` OAuth branch (SPEC §3.2.4): a stored access token reaches a tool as
its profile, and a revoked, expired, orphaned or foreign token gets 401."""

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from mcp.server.auth.middleware.auth_context import get_access_token
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.auth.tokens import SCOPE, TokenVerifier, insert_refresh_token, revoke_grant
from syncshelf.db import Database
from syncshelf.profiles import sync_profiles
from syncshelf.tools import ToolRegistry

from clock import FakeClock
from serving import (
    CLAUDE_CODE,
    MCP_HEADERS,
    MCP_URL,
    AuthedClient,
    grant_token,
    make_config,
    running,
)

pytestmark = pytest.mark.anyio

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def whoami_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool()
    async def whoami() -> dict[str, Any]:
        """Return the caller's identity."""
        token = get_access_token()
        assert token is not None
        return {"subject": token.subject, "claims": token.claims}

    return registry


def oauth_app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> Starlette:
    config = make_config(tmp_path, profiles=["alice", "bob"], auth_mode="oauth")
    return create_app(config, data_dir=data_dir, clock=clock.time, registry=ToolRegistry())


async def test_a_stored_token_reaches_a_tool_as_its_profile(
    authed_client: AuthedClient, clock: FakeClock
) -> None:
    async with authed_client("bob", profiles=["alice", "bob"], registry=whoami_registry()) as s:
        result = await s.mcp.call_tool("whoami", {})
    assert not result.is_error
    assert result.structured_content == {
        "subject": "bob",
        "claims": {
            "iss": "http://localhost:8000",
            "email": "bob@example.com",
            "grant_id": s.grant_id,
        },
    }


def revoke_token(conn: sqlite3.Connection, grant_id: int, now: int) -> None:
    conn.execute("UPDATE oauth_access_token SET revoked_at = ? WHERE grant_id = ?", (now, grant_id))


def remove_profile(conn: sqlite3.Connection, grant_id: int, now: int) -> None:
    # Only the profile: the grant stays open, so the verifier's own profile check is what
    # refuses (the profile sync would also revoke the grant).
    conn.execute("UPDATE profiles SET removed_at = ? WHERE name = 'alice'", (now,))


def revoke(conn: sqlite3.Connection, grant_id: int, now: int) -> None:
    revoke_grant(conn, grant_id, "user", now)


@pytest.mark.parametrize(
    "spoil", [revoke_token, revoke, remove_profile], ids=["token", "grant", "profile"]
)
async def test_a_revoked_token_grant_or_removed_profile_gets_401(
    tmp_path: Path, data_dir: Path, clock: FakeClock, spoil
) -> None:
    app = oauth_app(tmp_path, data_dir, clock)
    async with running(app, token=None) as http:
        db: Database = app.state.db
        grant_id, token = await grant_token(db, "alice", int(clock.time()))
        http.headers["authorization"] = f"Bearer {token}"
        assert (await http.post("/mcp", json=LIST, headers=MCP_HEADERS)).status_code == 200
        await db.write(spoil, grant_id, int(clock.time()))
        response = await http.post("/mcp", json=LIST, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]


async def test_a_token_expires_after_an_hour(
    tmp_path: Path, data_dir: Path, clock: FakeClock
) -> None:
    app = oauth_app(tmp_path, data_dir, clock)
    async with running(app, token=None) as http:
        _, token = await grant_token(app.state.db, "alice", int(clock.time()))
        http.headers["authorization"] = f"Bearer {token}"
        clock.advance(3599)
        assert (await http.post("/mcp", json=LIST, headers=MCP_HEADERS)).status_code == 200
        clock.advance(1)
        assert (await http.post("/mcp", json=LIST, headers=MCP_HEADERS)).status_code == 401


@pytest.mark.parametrize("kind", ["unknown", "refresh", "foreign_resource"])
async def test_an_unknown_refresh_or_foreign_token_gets_401(
    tmp_path: Path, data_dir: Path, clock: FakeClock, kind: str
) -> None:
    app = oauth_app(tmp_path, data_dir, clock)
    now = int(clock.time())
    async with running(app, token=None) as http:
        db: Database = app.state.db
        if kind == "unknown":
            token = "ssat_" + "A" * 43
        elif kind == "refresh":
            grant_id, _ = await grant_token(db, "alice", now)
            token = await db.write(insert_refresh_token, grant_id, now)
        else:
            _, token = await grant_token(db, "alice", now, resource="http://localhost:8000/other")
        http.headers["authorization"] = f"Bearer {token}"
        response = await http.post("/mcp", json=LIST, headers=MCP_HEADERS)
    assert response.status_code == 401


class TestVerifier:
    @pytest.fixture
    async def store(self, db: Database, clock: FakeClock) -> Database:
        await db.write(sync_profiles, ["alice"], int(clock.time()))
        return db

    def verifier(self, tmp_path: Path, db: Database, clock: FakeClock) -> TokenVerifier:
        verifier = TokenVerifier(make_config(tmp_path, auth_mode="oauth"), clock=clock.time)
        verifier.bind(db)
        return verifier

    async def test_it_returns_the_grants_identity(
        self, tmp_path: Path, store: Database, clock: FakeClock
    ) -> None:
        grant_id, token = await grant_token(store, "alice", int(clock.time()))
        found = await self.verifier(tmp_path, store, clock).verify_token(token)
        assert found is not None
        assert found.token == token
        assert found.client_id == CLAUDE_CODE
        assert found.scopes == [SCOPE, "offline_access"]
        assert found.expires_at == int(clock.time()) + 3600
        assert found.resource == MCP_URL
        assert found.subject == "alice"
        assert found.claims == {
            "iss": "http://localhost:8000",
            "email": "alice@example.com",
            "grant_id": grant_id,
        }

    async def test_unbound_it_refuses_everything(
        self, tmp_path: Path, store: Database, clock: FakeClock
    ) -> None:
        _, token = await grant_token(store, "alice", int(clock.time()))
        verifier = self.verifier(tmp_path, store, clock)
        verifier.bind(None)
        assert await verifier.verify_token(token) is None

    async def test_a_profile_missing_from_the_config_is_refused(
        self, tmp_path: Path, store: Database, clock: FakeClock
    ) -> None:
        await store.write(sync_profiles, ["alice", "carol"], int(clock.time()))
        _, token = await grant_token(store, "carol", int(clock.time()))
        assert await self.verifier(tmp_path, store, clock).verify_token(token) is None

    async def test_a_failed_last_used_write_still_accepts_the_token(
        self,
        tmp_path: Path,
        store: Database,
        clock: FakeClock,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _, token = await grant_token(store, "alice", int(clock.time()))
        verifier = self.verifier(tmp_path, store, clock)
        clock.advance(60)

        async def busy(*args: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(store, "write", busy)
        assert await verifier.verify_token(token) is not None
        assert [r for r in caplog.records if r.__dict__.get("event") == "grant_touch_failed"]

    async def test_last_used_at_moves_at_most_once_a_minute(
        self, tmp_path: Path, store: Database, clock: FakeClock
    ) -> None:
        start = int(clock.time())
        grant_id, token = await grant_token(store, "alice", start)
        verifier = self.verifier(tmp_path, store, clock)

        def last_used(conn: sqlite3.Connection) -> int:
            query = "SELECT last_used_at FROM oauth_grant WHERE id = ?"
            return conn.execute(query, (grant_id,)).fetchone()[0]

        for step, expected in [(30, start), (29, start), (1, start + 60), (59, start + 60)]:
            clock.advance(step)
            assert await verifier.verify_token(token) is not None
            assert await store.read(last_used) == expected
        clock.advance(1)
        assert await verifier.verify_token(token) is not None
        assert await store.read(last_used) == start + 120


async def test_raw_tokens_never_reach_the_logs(
    tmp_path: Path, data_dir: Path, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    app = oauth_app(tmp_path, data_dir, clock)
    async with running(app, token=None) as http:
        db: Database = app.state.db
        grant_id, token = await grant_token(db, "alice", int(clock.time()))
        http.headers["authorization"] = f"Bearer {token}"
        await http.post("/mcp", json=LIST, headers=MCP_HEADERS)
        await db.write(revoke, grant_id, int(clock.time()))
        await http.post("/mcp", json=LIST, headers=MCP_HEADERS)
    [refused] = [r for r in caplog.records if getattr(r, "event", None) == "token_refused"]
    assert refused.__dict__["grant_id"] == grant_id
    assert refused.__dict__["reason"] == "grant_revoked"
    everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert token not in everything
    assert token.removeprefix("ssat_") not in everything
