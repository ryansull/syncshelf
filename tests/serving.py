"""Build and drive the real ASGI app in tests: a config, a running lifespan, and an MCP
client that presents a bearer token, either the dev token or an OAuth one from the store.

Everything goes through the production app and verifier (SPEC §2.9): :func:`authed` puts a
grant and a token hash straight into the store, and the request is checked like any other.
"""

import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from syncshelf.auth.tokens import SCOPE, create_grant, insert_access_token
from syncshelf.config import Config, load_config
from syncshelf.db import Database

SECRET = "fake-consent-key"
DEV_TOKEN = "fake-dev-token-for-tests-32-chars-or-more"  # config refuses under 32 (#75)
BASE_URL = "http://localhost:8000"  # "localhost:*" only matches a Host with a port
MCP_URL = f"{BASE_URL}/mcp"
ACCESS_AUD = "fake-access-aud-tag"
TEAM_DOMAIN = "https://example.cloudflareaccess.com"
ENVIRON = {"CONSENT": SECRET, "DEV_TOKEN": DEV_TOKEN, "ACCESS_AUD": ACCESS_AUD}
HARDCOVER_URL = "https://api.hardcover.app/v1/graphql"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
"""The headers a raw Streamable HTTP POST to ``/mcp`` needs."""
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
"""The Claude Code ``client_id`` (§3.2.1), which test grants are issued to."""


def make_config(
    tmp_path: Path,
    public_url: str = "http://localhost:8000",
    *,
    profiles: Iterable[str] = ("alice",),
    dev_profile: str | None = None,
    profile_yaml: dict[str, str] | None = None,
    auth_mode: str = "dev",
) -> Config:
    """A config in ``auth_mode``: ``dev`` acting as ``dev_profile`` (default: the first
    profile), or ``oauth`` with the test Access app (:data:`TEAM_DOMAIN`, :data:`ACCESS_AUD`)
    and consent key (:data:`SECRET`).

    ``profile_yaml`` gives a profile's body as one line of flow YAML, ``{}`` by default. Each
    profile also gets the Hardcover token :func:`hardcover_token` names, unless its body sets one.
    """
    profiles = list(profiles)
    bodies = profile_yaml or {}
    dev = dev_profile or (profiles[0] if profiles else "alice")
    dev_block = f"  dev:\n    token: ${{DEV_TOKEN}}\n    profile: {dev}\n"
    access_block = f"  access:\n    team_domain: {TEAM_DOMAIN}\n    aud: ${{ACCESS_AUD}}\n"
    path = tmp_path / "profiles.yaml"
    path.write_text(
        dedent(f"""\
            server:
              public_url: {public_url}
              auth_mode: {auth_mode}
              forwarded_allow_ips: 127.0.0.1
              consent_hmac_key: ${{CONSENT}}
            """)
        + (dev_block if auth_mode == "dev" else access_block)
        + f"services:\n  hardcover_url: {HARDCOVER_URL}\n"
        + "profiles:\n"
        + "".join(f"  {name}: {_with_token(name, bodies.get(name, '{}'))}\n" for name in profiles),
        encoding="utf-8",
    )
    tokens = {f"HC_{name.upper()}": hardcover_token(name) for name in profiles}
    return load_config(path, environ=ENVIRON | tokens)


def hardcover_token(profile: str) -> str:
    """The Hardcover token :func:`make_config` gives ``profile``, for a ``FakeHardcover`` user."""
    return f"fake-hardcover-token-{profile}"


def _with_token(name: str, body: str) -> str:
    if "hardcover_token" in body:
        return body
    # Quoted: in flow YAML a bare ${...} reads as a nested mapping.
    entry = f'hardcover_token: "${{HC_{name.upper()}}}"'
    inner = body.strip()[1:-1].strip()
    return f"{{{entry}, {inner}}}" if inner else f"{{{entry}}}"


@asynccontextmanager
async def running(
    app: Starlette, token: str | None = DEV_TOKEN
) -> AsyncIterator[httpx2.AsyncClient]:
    """Run the app's lifespan (ASGITransport doesn't) and yield a client for it that sends
    ``token`` as its bearer token, or no ``Authorization`` header when ``token`` is None."""
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=BASE_URL, headers=headers
        ) as client,
    ):
        yield client


@asynccontextmanager
async def mcp_client(http: httpx2.AsyncClient) -> AsyncIterator[Client]:
    """The SDK's MCP client over ``http``, connected to ``/mcp``."""
    async with Client(streamable_http_client(MCP_URL, http_client=http)) as client:
        yield client


async def grant_token(
    db: Database, profile: str, now: int, *, resource: str = MCP_URL
) -> tuple[int, str]:
    """Put a Claude Code grant for ``profile``, and one access token on it, into the store as
    the code exchange would, at ``now``. Returns the grant id and the raw token."""

    def issue(conn: sqlite3.Connection) -> tuple[int, str]:
        grant_id = create_grant(
            conn,
            profile=profile,
            email=f"{profile}@example.com",
            client_id=CLAUDE_CODE,
            client_label="Claude Code",
            resource=resource,
            scopes=[SCOPE, "offline_access"],
            now=now,
        )
        return grant_id, insert_access_token(conn, grant_id, now)

    return await db.write(issue)


@dataclass(frozen=True)
class Authed:
    """A signed-in MCP session from :func:`authed`, and what is behind it."""

    mcp: Client
    http: httpx2.AsyncClient
    """The raw client, already sending the token, for requests the MCP client can't make."""
    db: Database
    grant_id: int
    token: str
    """The raw access token the session presents."""


@asynccontextmanager
async def authed(app: Starlette, profile: str, now: int) -> AsyncIterator[Authed]:
    """Run ``app`` (an ``oauth``-mode app) and sign in to its ``/mcp`` as ``profile`` with a
    grant and access token issued at ``now``. The ``authed_client`` fixture drives this."""
    async with running(app, token=None) as http:
        db: Database = app.state.db
        grant_id, token = await grant_token(db, profile, now)
        http.headers["authorization"] = f"Bearer {token}"
        async with mcp_client(http) as client:
            yield Authed(client, http, db, grant_id, token)


type AuthedClient = Callable[..., AbstractAsyncContextManager[Authed]]
"""The ``authed_client`` fixture's type: ``authed_client(profile, **options)``."""
