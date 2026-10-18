"""Refresh rotation and the 60-second grace window through the real app (SPEC §3.2.4, §3.2.7,
§3.2.8 level 2): each refresh rotates the token and slides the 90-day window, a retry inside
the grace period gets the same pair, and reuse after it revokes the connection.

"Restart" is a second app on the same data directory, whose grace cache starts empty."""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.auth.cimd import CLAUDE_APPS
from syncshelf.auth.tokens import GRACE_PERIOD, REFRESH_WINDOW, RefreshGrace, hash_token
from syncshelf.db import Database
from syncshelf.logging import scrub

from access_jwt import FakeAccess, routed
from cimd_docs import FakeClaude
from clock import FakeClock
from oauth_flow import authorization_code, sign_in, token_form, token_json, token_request
from serving import BASE_URL, CLAUDE_CODE, MCP_URL, TEAM_DOMAIN, make_config, mcp_client, running

pytestmark = pytest.mark.anyio

DAY = 86400
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

AppFactory = Callable[..., Starlette]


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def access(clock: FakeClock) -> FakeAccess:
    return FakeAccess(clock)


@pytest.fixture
def make_app(
    tmp_path: Path, data_dir: Path, clock: FakeClock, fake: FakeClaude, access: FakeAccess
) -> AppFactory:
    """A new app on the one data directory, so a second one is a restart. ``profiles``
    are the profiles its config has."""

    def make(profiles: tuple[str, ...] = ("alice", "bob")) -> Starlette:
        config_dir = tmp_path / "-".join(profiles)
        config_dir.mkdir(exist_ok=True)
        config = make_config(
            config_dir,
            profiles=profiles,
            profile_yaml={name: f"{{email: {name}@example.com}}" for name in profiles},
            auth_mode="oauth",
        )
        return create_app(
            config,
            data_dir=data_dir,
            clock=clock.time,
            http_transport=routed({"claude.ai": fake, str(urlsplit(TEAM_DOMAIN).hostname): access}),
        )

    return make


@pytest.fixture
def app(make_app: AppFactory) -> Starlette:
    return make_app()


@pytest.fixture
async def client(app: Starlette) -> AsyncIterator[httpx2.AsyncClient]:
    async with running(app, token=None) as http:
        yield http


@asynccontextmanager
async def restarted(make_app: AppFactory, **kwargs: Any) -> AsyncIterator[httpx2.AsyncClient]:
    async with running(make_app(**kwargs), token=None) as http:
        yield http


def store(app: Starlette) -> Database:
    return app.state.db


def grace(app: Starlette) -> RefreshGrace:
    return app.state.grace


def refresh_form(refresh_token: str, /, **overrides: str | None) -> dict[str, str]:
    """A valid refresh body for a Claude Code grant. An override of ``None`` drops it."""
    form: dict[str, str | None] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLAUDE_CODE,
        "resource": MCP_URL,
        **overrides,
    }
    return {name: value for name, value in form.items() if value is not None}


async def refresh(
    client: httpx2.AsyncClient, refresh_token: str, status: int = 200, /, **overrides: str | None
) -> dict[str, Any]:
    response = await token_request(client, data=refresh_form(refresh_token, **overrides))
    return token_json(response, status)


async def mcp_status(client: httpx2.AsyncClient, access_token: str) -> int:
    response = await client.post(
        "/mcp",
        json=LIST,
        headers={
            "authorization": f"Bearer {access_token}",
            "accept": "application/json, text/event-stream",
        },
    )
    return response.status_code


def _refresh_rows(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT token_hash, grant_id, created_at, rotated_at, successor_hash, revoked_at "
        "FROM oauth_refresh_token ORDER BY created_at, rowid"
    ).fetchall()


def _grants(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id, last_used_at, refresh_expires_at, revoked_reason FROM oauth_grant ORDER BY id"
    ).fetchall()


# --- rotation ------------------------------------------------------------------------------


async def test_a_refresh_rotates_the_token_and_slides_the_window(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock
) -> None:
    first = await sign_in(client, access)
    clock.advance(1800)
    second = await refresh(client, first["refresh_token"])
    assert set(second) == {"access_token", "token_type", "expires_in", "refresh_token", "scope"}
    assert (second["token_type"], second["expires_in"]) == ("Bearer", 3600)
    assert second["scope"] == "syncshelf offline_access"
    assert second["access_token"].startswith("ssat_")
    assert second["refresh_token"].startswith("ssrt_")
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]

    now = int(clock.time())
    old, new = await store(app).read(_refresh_rows)
    assert old[0] == hash_token(first["refresh_token"])
    assert (old[3], old[4]) == (now, hash_token(second["refresh_token"]))
    assert new[0] == hash_token(second["refresh_token"])
    assert (new[2], new[3], new[4]) == (now, None, None)
    [(_, last_used_at, refresh_expires_at, revoked)] = await store(app).read(_grants)
    assert (last_used_at, refresh_expires_at, revoked) == (now, now + REFRESH_WINDOW, None)

    # Both access tokens work: rotation doesn't cut short the one issued before.
    assert await mcp_status(client, second["access_token"]) == 200
    assert await mcp_status(client, first["access_token"]) == 200
    client.headers["authorization"] = f"Bearer {second['access_token']}"
    async with mcp_client(client) as mcp:
        result = await mcp.call_tool("my_connections", {})
    assert not result.is_error, result.content
    del client.headers["authorization"]

    # The successor rotates in turn.
    clock.advance(GRACE_PERIOD + 1)
    third = await refresh(client, second["refresh_token"])
    assert third["refresh_token"] not in (first["refresh_token"], second["refresh_token"])


@pytest.mark.parametrize("resource", [None, "HTTP://LOCALHOST:8000/mcp", MCP_URL + "/"])
async def test_resource_is_optional_and_canonicalised(
    client: httpx2.AsyncClient, access: FakeAccess, resource: str | None
) -> None:
    tokens = await sign_in(client, access)
    await refresh(client, tokens["refresh_token"], resource=resource)


@pytest.mark.parametrize(
    ("overrides", "status", "error"),
    [
        ({"client_id": CLAUDE_APPS}, 400, "invalid_grant"),
        ({"resource": BASE_URL}, 400, "invalid_target"),
        ({"resource": "not a url"}, 400, "invalid_target"),
        ({"client_id": None}, 401, "invalid_client"),
        ({"client_id": "https://claude.ai/oauth/other"}, 401, "invalid_client"),
        ({"refresh_token": None}, 400, "invalid_request"),
        ({"refresh_token": "ssrt_not-a-token"}, 400, "invalid_grant"),
    ],
    ids=["client", "resource", "resource-junk", "no-client", "bad-client", "missing", "unknown"],
)
async def test_a_bad_refresh_is_refused_and_leaves_the_token_working(
    app: Starlette,
    client: httpx2.AsyncClient,
    access: FakeAccess,
    fake: FakeClaude,
    overrides: dict[str, str | None],
    status: int,
    error: str,
) -> None:
    tokens = await sign_in(client, access)
    fetched = len(fake.requests)
    assert await refresh(client, tokens["refresh_token"], status, **overrides) == {"error": error}
    assert [row[3] for row in await store(app).read(_refresh_rows)] == [None]
    await refresh(client, tokens["refresh_token"])
    assert len(fake.requests) == fetched  # /token never fetches a client document


async def test_a_refresh_never_fetches_a_client_document(
    client: httpx2.AsyncClient, access: FakeAccess, fake: FakeClaude
) -> None:
    tokens = await sign_in(client, access)
    fetched = len(fake.requests)
    await refresh(client, tokens["refresh_token"])
    assert len(fake.requests) == fetched


# --- the grace window ----------------------------------------------------------------------


async def test_a_retry_inside_the_grace_window_gets_the_same_pair(
    app: Starlette,
    client: httpx2.AsyncClient,
    access: FakeAccess,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = await sign_in(client, access)
    with caplog.at_level(logging.INFO):
        rotated = await refresh(client, first["refresh_token"])
        clock.advance(GRACE_PERIOD - 1)
        retried = await refresh(client, first["refresh_token"])
    assert retried == rotated
    assert len(await store(app).read(_refresh_rows)) == 2  # one rotation, not two
    events = [getattr(r, "event", None) for r in caplog.records]
    assert events.count("refresh_rotated") == 1
    assert events.count("refresh_retried") == 1
    # The retried pair is the live one.
    assert await mcp_status(client, retried["access_token"]) == 200
    await refresh(client, retried["refresh_token"])


async def test_a_retry_inside_the_window_from_another_client_is_refused_without_revoking(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    first = await sign_in(client, access)
    rotated = await refresh(client, first["refresh_token"])
    wrong = await refresh(client, first["refresh_token"], 400, client_id=CLAUDE_APPS)
    assert wrong == {"error": "invalid_grant"}
    assert await refresh(client, first["refresh_token"]) == rotated


@pytest.mark.parametrize("after", [GRACE_PERIOD, GRACE_PERIOD + 1, 30 * DAY])
async def test_reuse_after_the_window_revokes_the_connection(
    app: Starlette,
    client: httpx2.AsyncClient,
    access: FakeAccess,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    after: int,
) -> None:
    first = await sign_in(client, access)
    rotated = await refresh(client, first["refresh_token"])
    clock.advance(after)
    with caplog.at_level(logging.INFO):
        reused = await refresh(client, first["refresh_token"], 400)
    assert reused == {"error": "invalid_grant"}
    [(_, _, _, reason)] = await store(app).read(_grants)
    assert reason == "refresh_reuse"
    assert [
        getattr(r, "reason", None)
        for r in caplog.records
        if getattr(r, "event", None) == "grant_revoked"
    ] == ["refresh_reuse"]
    # The whole connection is dead: the access tokens get 401, the successor invalid_grant.
    if after < 3600:
        assert await mcp_status(client, rotated["access_token"]) == 401
    assert await refresh(client, rotated["refresh_token"], 400) == {"error": "invalid_grant"}
    assert len(grace(app)) == 0


async def test_a_retry_after_a_restart_inside_the_window_is_refused_without_revoking(
    make_app: AppFactory, access: FakeAccess, clock: FakeClock
) -> None:
    async with restarted(make_app) as before:
        first = await sign_in(before, access)
        rotated = await refresh(before, first["refresh_token"])
    clock.advance(GRACE_PERIOD - 1)
    after = make_app()
    async with running(after, token=None) as again:
        assert await refresh(again, first["refresh_token"], 400) == {"error": "invalid_grant"}
        assert [row[3] for row in await store(after).read(_grants)] == [None]
        # The client signs in again, or carries on with the pair it did receive.
        assert await mcp_status(again, rotated["access_token"]) == 200
        await refresh(again, rotated["refresh_token"])


async def test_concurrent_refreshes_with_one_token_rotate_it_once(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    first = await sign_in(client, access)
    results = await asyncio.gather(*(refresh(client, first["refresh_token"]) for _ in range(8)))
    assert all(result == results[0] for result in results)
    rows = await store(app).read(_refresh_rows)
    assert len(rows) == 2
    assert [row[3] is not None for row in rows] == [True, False]


async def test_the_grace_cache_holds_the_raw_pair_as_a_secret_for_the_window(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock
) -> None:
    first = await sign_in(client, access)
    rotated = await refresh(client, first["refresh_token"])
    assert len(grace(app)) == 1
    for raw in (rotated["access_token"], rotated["refresh_token"]):
        assert scrub(f"x {raw} y") == "x *** y"
    clock.advance(GRACE_PERIOD)
    assert grace(app).get(hash_token(first["refresh_token"]), int(clock.time())) is None
    assert len(grace(app)) == 0
    for raw in (rotated["access_token"], rotated["refresh_token"]):
        assert raw in scrub(raw)


async def test_raw_refresh_tokens_are_never_logged(
    client: httpx2.AsyncClient,
    access: FakeAccess,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        first = await sign_in(client, access)
        rotated = await refresh(client, first["refresh_token"])
        await refresh(client, first["refresh_token"])
        clock.advance(GRACE_PERIOD)
        await refresh(client, first["refresh_token"], 400)
    logged = caplog.text + "".join(repr(r.__dict__) for r in caplog.records)
    for secret in (*first.values(), *rotated.values()):
        if isinstance(secret, str) and secret.startswith(("ssat_", "ssrt_")):
            assert secret not in logged


# --- revocation and the cache --------------------------------------------------------------


async def test_revoke_connection_drops_the_grants_held_pairs(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    here = await sign_in(client, access)
    other = await sign_in(client, access)
    bobs = await sign_in(client, access, email="bob@example.com")
    current = await refresh(client, here["refresh_token"])
    for tokens in (other, bobs):
        await refresh(client, tokens["refresh_token"])
    assert len(grace(app)) == 3
    client.headers["authorization"] = f"Bearer {current['access_token']}"
    async with mcp_client(client) as mcp:
        result = await mcp.call_tool("revoke_connection", {"target": "all_others"})
    assert not result.is_error, result.content
    del client.headers["authorization"]
    assert len(grace(app)) == 2  # the current connection's and bob's are kept
    assert await refresh(client, other["refresh_token"], 400) == {"error": "invalid_grant"}
    assert await refresh(client, here["refresh_token"]) == current


async def test_a_replayed_code_drops_its_grants_held_pairs(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    code = await authorization_code(client, access)
    tokens = token_json(await token_request(client, data=token_form(code)))
    await refresh(client, tokens["refresh_token"])
    assert len(grace(app)) == 1
    token_json(await token_request(client, data=token_form(code)), 400)
    assert len(grace(app)) == 0
    assert await refresh(client, tokens["refresh_token"], 400) == {"error": "invalid_grant"}


# --- the sliding window and the profile ----------------------------------------------------


async def test_the_window_slides_with_each_refresh(
    client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock
) -> None:
    tokens = await sign_in(client, access)
    clock.advance(89 * DAY)
    tokens = await refresh(client, tokens["refresh_token"])  # day 89 still works
    clock.advance(89 * DAY)
    tokens = await refresh(client, tokens["refresh_token"])  # and slid 90 days on
    clock.advance(90 * DAY)
    assert await refresh(client, tokens["refresh_token"], 400) == {"error": "invalid_grant"}


async def test_a_refresh_is_refused_one_second_before_and_after_the_window(
    client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock
) -> None:
    tokens = await sign_in(client, access)
    clock.advance(REFRESH_WINDOW - 1)
    tokens = await refresh(client, tokens["refresh_token"])
    clock.advance(REFRESH_WINDOW)
    assert await refresh(client, tokens["refresh_token"], 400) == {"error": "invalid_grant"}


async def test_a_removed_profile_cannot_refresh(make_app: AppFactory, access: FakeAccess) -> None:
    async with restarted(make_app) as before:
        tokens = await sign_in(before, access)
    async with restarted(make_app, profiles=("bob",)) as again:
        assert await refresh(again, tokens["refresh_token"], 400) == {"error": "invalid_grant"}


async def test_a_removed_profile_is_refused_even_on_an_open_grant(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    tokens = await sign_in(client, access)

    def remove(conn: sqlite3.Connection) -> None:
        # Only the profile is marked: the startup sync would also revoke its grants.
        conn.execute("UPDATE profiles SET removed_at = 1 WHERE name = 'alice'")

    await store(app).write(remove)
    assert await refresh(client, tokens["refresh_token"], 400) == {"error": "invalid_grant"}
    assert [row[3] for row in await store(app).read(_grants)] == [None]
