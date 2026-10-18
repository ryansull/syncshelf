"""``my_connections`` and ``revoke_connection`` (SPEC §3.2.7), through the real app and the
production verifier: a profile sees and revokes only its own grants, ``all_others`` keeps the
current one, and ``all`` ends the current session with a 401."""

import logging
import sqlite3
from typing import Any

import pytest
from mcp.types import ToolAnnotations

from syncshelf.app import create_app
from syncshelf.auth.tokens import SCOPE, create_grant, insert_access_token
from syncshelf.db import Database

from clock import FakeClock
from serving import MCP_HEADERS, AuthedClient, grant_token, make_config, mcp_client, running

pytestmark = pytest.mark.anyio

LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
START_ISO = "2100-01-01T00:00:00+00:00"
"""``FakeClock.START`` as the tools write it."""

HOSTED = "https://claude.ai/oauth/mcp-oauth-client-metadata"


async def hosted_grant(db: Database, profile: str, now: int) -> int:
    """A Claude app (``claude.ai``) grant, as a sign-in from the hosted client would leave."""

    def issue(conn: sqlite3.Connection) -> int:
        grant_id = create_grant(
            conn,
            profile=profile,
            email=f"{profile}@example.com",
            client_id=HOSTED,
            client_label="claude.ai",
            resource="http://localhost:8000/mcp",
            scopes=[SCOPE],
            now=now,
        )
        insert_access_token(conn, grant_id, now)
        return grant_id

    return await db.write(issue)


async def revocations(db: Database) -> dict[int, tuple[int | None, str | None]]:
    def read(conn: sqlite3.Connection) -> dict[int, tuple[int | None, str | None]]:
        rows = conn.execute("SELECT id, revoked_at, revoked_reason FROM oauth_grant")
        return {grant_id: (at, reason) for grant_id, at, reason in rows}

    return await db.read(read)


def rows(result: Any) -> list[dict[str, Any]]:
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content["result"]


def error_text(result: Any) -> str:
    assert result.is_error
    return result.content[0].text


class TestMyConnections:
    async def test_lists_own_open_grants_oldest_first_with_the_current_one_marked(
        self, authed_client: AuthedClient, clock: FakeClock
    ) -> None:
        async with authed_client("alice", profiles=["alice", "bob"]) as s:
            clock.advance(60)
            app_grant = await hosted_grant(s.db, "alice", int(clock.time()))
            await grant_token(s.db, "bob", int(clock.time()))
            listed = rows(await s.mcp.call_tool("my_connections", {}))
        assert listed == [
            {
                "grant_id": s.grant_id,
                "client": "Claude Code",
                "created_at": START_ISO,
                # This call moved it: verification touches a grant at most once a minute.
                "last_used_at": "2100-01-01T00:01:00+00:00",
                "current": True,
            },
            {
                "grant_id": app_grant,
                "client": "Claude app",
                "created_at": "2100-01-01T00:01:00+00:00",
                "last_used_at": "2100-01-01T00:01:00+00:00",
                "current": False,
            },
        ]

    async def test_revoked_grants_are_omitted(
        self, authed_client: AuthedClient, clock: FakeClock
    ) -> None:
        async with authed_client("alice") as s:
            other, _ = await grant_token(s.db, "alice", int(clock.time()))
            await s.mcp.call_tool("revoke_connection", {"target": other})
            listed = rows(await s.mcp.call_tool("my_connections", {}))
        assert [row["grant_id"] for row in listed] == [s.grant_id]


class TestRevokeOne:
    async def test_revokes_an_own_grant_with_reason_user_and_returns_it(
        self, authed_client: AuthedClient, clock: FakeClock
    ) -> None:
        async with authed_client("alice") as s:
            clock.advance(120)
            other, _ = await grant_token(s.db, "alice", int(clock.time()))
            revoked = rows(await s.mcp.call_tool("revoke_connection", {"target": other}))
            state = await revocations(s.db)
        assert [(r["grant_id"], r["client"], r["current"]) for r in revoked] == [
            (other, "Claude Code", False)
        ]
        assert state[other] == (int(clock.time()), "user")
        assert state[s.grant_id] == (None, None)

    async def test_another_profiles_grant_is_refused_and_left_open(
        self, authed_client: AuthedClient, clock: FakeClock
    ) -> None:
        async with authed_client("alice", profiles=["alice", "bob"]) as s:
            bobs, _ = await grant_token(s.db, "bob", int(clock.time()))
            result = await s.mcp.call_tool("revoke_connection", {"target": bobs})
            state = await revocations(s.db)
        assert str(bobs) in error_text(result)
        assert "my_connections" in error_text(result)
        assert state[bobs] == (None, None)

    async def test_an_unknown_or_already_revoked_id_is_refused(
        self, authed_client: AuthedClient, clock: FakeClock
    ) -> None:
        async with authed_client("alice") as s:
            other, _ = await grant_token(s.db, "alice", int(clock.time()))
            await s.mcp.call_tool("revoke_connection", {"target": other})
            clock.advance(60)
            again = await s.mcp.call_tool("revoke_connection", {"target": other})
            unknown = await s.mcp.call_tool("revoke_connection", {"target": 9999})
            state = await revocations(s.db)
        assert again.is_error and unknown.is_error
        # The first revocation stands.
        assert state[other] == (int(clock.time()) - 60, "user")

    async def test_revoking_the_current_grant_by_id_ends_the_session(
        self, authed_client: AuthedClient
    ) -> None:
        async with authed_client("alice") as s:
            # The SDK client lists tools after a call to validate its result, unless it
            # already has the schemas; once the grant is revoked, that listing would get 401.
            await s.mcp.list_tools()
            revoked = rows(await s.mcp.call_tool("revoke_connection", {"target": s.grant_id}))
            response = await s.http.post("/mcp", json=LIST, headers=MCP_HEADERS)
        assert [r["current"] for r in revoked] == [True]
        assert response.status_code == 401

    @pytest.mark.parametrize("target", ["everything", True, "1", 1.0])
    async def test_anything_but_an_id_or_a_keyword_is_refused(
        self, authed_client: AuthedClient, target: object
    ) -> None:
        # Not coerced to grant 1, which is the current grant here.
        async with authed_client("alice") as s:
            result = await s.mcp.call_tool("revoke_connection", {"target": target})
            state = await revocations(s.db)
        assert s.grant_id == 1
        assert result.is_error
        assert state[s.grant_id] == (None, None)


class TestRevokeMany:
    async def test_all_others_keeps_the_current_grant(
        self, authed_client: AuthedClient, clock: FakeClock
    ) -> None:
        async with authed_client("alice", profiles=["alice", "bob"]) as s:
            first, _ = await grant_token(s.db, "alice", int(clock.time()))
            second = await hosted_grant(s.db, "alice", int(clock.time()))
            bobs, _ = await grant_token(s.db, "bob", int(clock.time()))
            revoked = rows(await s.mcp.call_tool("revoke_connection", {"target": "all_others"}))
            still = rows(await s.mcp.call_tool("my_connections", {}))
            state = await revocations(s.db)
        assert sorted(r["grant_id"] for r in revoked) == sorted([first, second])
        assert [r["grant_id"] for r in still] == [s.grant_id]
        assert state[bobs] == (None, None)

    async def test_all_revokes_the_current_grant_so_the_next_call_gets_401(
        self, authed_client: AuthedClient, clock: FakeClock
    ) -> None:
        async with authed_client("alice", profiles=["alice", "bob"]) as s:
            other, _ = await grant_token(s.db, "alice", int(clock.time()))
            bobs, _ = await grant_token(s.db, "bob", int(clock.time()))
            # The SDK client lists tools after a call to validate its result, unless it
            # already has the schemas; once the grant is revoked, that listing would get 401.
            await s.mcp.list_tools()
            revoked = rows(await s.mcp.call_tool("revoke_connection", {"target": "all"}))
            response = await s.http.post("/mcp", json=LIST, headers=MCP_HEADERS)
            state = await revocations(s.db)
        assert {r["grant_id"] for r in revoked} == {s.grant_id, other}
        assert response.status_code == 401
        assert state[s.grant_id][1] == "user"
        assert state[bobs] == (None, None)

    async def test_nothing_to_revoke_is_an_empty_list(self, authed_client: AuthedClient) -> None:
        async with authed_client("alice") as s:
            revoked = rows(await s.mcp.call_tool("revoke_connection", {"target": "all_others"}))
        assert revoked == []

    async def test_each_revocation_logs_its_grant_id(
        self, authed_client: AuthedClient, clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        async with authed_client("alice") as s:
            other, _ = await grant_token(s.db, "alice", int(clock.time()))
            with caplog.at_level(logging.INFO, logger="syncshelf"):
                await s.mcp.call_tool("revoke_connection", {"target": other})
        [line] = [r for r in caplog.records if getattr(r, "event", None) == "grant_revoked"]
        assert line.__dict__["grant_id"] == other
        assert line.__dict__["client_label"] == "Claude Code"
        assert line.__dict__["reason"] == "user"


async def test_every_profile_sees_both_tools(authed_client: AuthedClient) -> None:
    # bob has no permissions at all.
    async with authed_client("bob", profiles=["alice", "bob"]) as s:
        tools = {t.name: t for t in (await s.mcp.list_tools()).tools}
    assert {"my_connections", "revoke_connection"} <= set(tools)
    assert tools["my_connections"].annotations == ToolAnnotations(read_only_hint=True)


async def test_in_dev_mode_no_grant_is_current(tmp_path: Any, data_dir: Any) -> None:
    # The dev token has no grant behind it (§3.2.5), so every grant is one of the "others".
    app = create_app(make_config(tmp_path), data_dir=data_dir)
    async with running(app) as http:
        grant_id, _ = await grant_token(app.state.db, "alice", FakeClock.START)
        async with mcp_client(http) as mcp:
            listed = rows(await mcp.call_tool("my_connections", {}))
            revoked = rows(await mcp.call_tool("revoke_connection", {"target": "all_others"}))
            after = await mcp.list_tools()
    assert [(r["grant_id"], r["current"]) for r in listed] == [(grant_id, False)]
    assert [r["grant_id"] for r in revoked] == [grant_id]
    assert after.tools  # the dev token is no grant, so revoking can't end the session
