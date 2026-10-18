"""Build and drive the real ASGI app in tests: a dev-mode config, a running lifespan, and an
MCP client that presents a bearer token.

Everything goes through the production app and verifier (SPEC §2.9). The OAuth
``authed_client(profile)`` fixture arrives with the token store (#62).
"""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from textwrap import dedent

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette

from syncshelf.config import Config, load_config

SECRET = "fake-consent-key"
DEV_TOKEN = "fake-dev-token-for-tests"
BASE_URL = "http://localhost:8000"  # "localhost:*" only matches a Host with a port
ENVIRON = {"CONSENT": SECRET, "DEV_TOKEN": DEV_TOKEN}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
"""The headers a raw Streamable HTTP POST to ``/mcp`` needs."""


def make_config(
    tmp_path: Path,
    public_url: str = "http://localhost:8000",
    *,
    profiles: Iterable[str] = ("alice",),
    dev_profile: str | None = None,
    profile_yaml: dict[str, str] | None = None,
) -> Config:
    """A dev-mode config acting as ``dev_profile`` (default: the first profile).

    ``profile_yaml`` gives a profile's body as one line of flow YAML, ``{}`` by default.
    """
    profiles = list(profiles)
    bodies = profile_yaml or {}
    dev = dev_profile or (profiles[0] if profiles else "alice")
    path = tmp_path / "profiles.yaml"
    path.write_text(
        dedent(f"""\
            server:
              public_url: {public_url}
              auth_mode: dev
              dev:
                token: ${{DEV_TOKEN}}
                profile: {dev}
              forwarded_allow_ips: 127.0.0.1
              consent_hmac_key: ${{CONSENT}}
            """)
        + "profiles:\n"
        + "".join(f"  {name}: {bodies.get(name, '{}')}\n" for name in profiles),
        encoding="utf-8",
    )
    return load_config(path, environ=ENVIRON)


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
    async with Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http)) as client:
        yield client
