"""The scripted PKCE client (SPEC §3.2.8 level 3): the SDK's own OAuth client signs in to the
real app as Claude Code would, and its first tool call works.

``mcp.client.auth.OAuthClientProvider`` does everything a real client does: the 401 from
``/mcp``, protected-resource and authorization-server discovery, CIMD (its ``client_id`` is
the Claude Code document's URL), S256, ``resource`` on both requests, and the RFC 9207 check
of ``iss`` on the redirect. Only the person is scripted: the redirect handler plays the
browser, opening ``/authorize`` with a forged Cloudflare Access JWT and pressing **Allow**,
and the callback handler hands back what the redirect carried.

The app is configured for a public name, which only ``oauth`` mode accepts (#69). Every
request, whichever host it names, goes to the app through ``ASGITransport``.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl
from starlette.applications import Starlette

from syncshelf.app import create_app

from access_jwt import FakeAccess, routed
from cimd_docs import FakeClaude
from clock import FakeClock
from oauth_flow import LOOPBACK, allow, consent_blob, redirected_to
from serving import CLAUDE_CODE, TEAM_DOMAIN, make_config

pytestmark = pytest.mark.anyio

PUBLIC_URL = "https://shelf.example.com"
MCP_URL = f"{PUBLIC_URL}/mcp"


class MemoryStorage:
    """The provider's token storage, in memory: a fresh client with nothing stored."""

    def __init__(self) -> None:
        self.tokens: OAuthToken | None = None
        self.client_info: OAuthClientInformationFull | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


class Person:
    """The person at the browser: signed in to Access as ``email``, pressing **Allow**.

    :meth:`open` is the provider's redirect handler and :meth:`callback` its callback handler.
    Each authorization URL it was sent to is kept in ``opened``.
    """

    def __init__(self, browser: httpx2.AsyncClient, access: FakeAccess, email: str) -> None:
        self.browser = browser
        self.access = access
        self.email = email
        self.opened: list[str] = []
        self._redirect: dict[str, str] | None = None

    async def open(self, authorization_url: str) -> None:
        self.opened.append(authorization_url)
        jwt = self.access.token(self.email)
        params = parse_qsl(urlsplit(authorization_url).query)
        blob = await consent_blob(self.browser, jwt, params)
        # The browser sends the consent page's origin; see #114 for what a real one sends.
        response = await allow(self.browser, blob, jwt, origin=PUBLIC_URL)
        self._redirect = redirected_to(response, dict(params)["redirect_uri"])

    async def callback(self) -> AuthorizationCodeResult:
        assert self._redirect is not None, "the redirect handler runs first"
        redirect, self._redirect = self._redirect, None
        return AuthorizationCodeResult(
            code=redirect["code"], state=redirect.get("state"), iss=redirect.get("iss")
        )


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def access(clock: FakeClock) -> FakeAccess:
    return FakeAccess(clock)


@pytest.fixture
def app(
    tmp_path: Path, data_dir: Path, clock: FakeClock, fake: FakeClaude, access: FakeAccess
) -> Starlette:
    config = make_config(
        tmp_path,
        PUBLIC_URL,
        profile_yaml={"alice": "{email: alice@example.com}"},
        auth_mode="oauth",
    )
    return create_app(
        config,
        data_dir=data_dir,
        clock=clock.time,
        http_transport=routed({"claude.ai": fake, str(urlsplit(TEAM_DOMAIN).hostname): access}),
    )


@pytest.fixture
async def transport(app: Starlette) -> AsyncIterator[httpx2.ASGITransport]:
    """The app, running its lifespan, as a transport for any number of clients."""
    async with app.router.lifespan_context(app):
        yield httpx2.ASGITransport(app=app)


async def test_the_sdk_oauth_client_signs_in_as_claude_code_and_calls_a_tool(
    transport: httpx2.ASGITransport, fake: FakeClaude, access: FakeAccess
) -> None:
    async with httpx2.AsyncClient(transport=transport, base_url=PUBLIC_URL) as browser:
        person = Person(browser, access, "alice@example.com")
        provider = OAuthClientProvider(
            MCP_URL,
            OAuthClientMetadata(
                redirect_uris=[AnyUrl(LOOPBACK)],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                client_name="Claude Code",
            ),
            MemoryStorage(),
            redirect_handler=person.open,
            callback_handler=person.callback,
            client_metadata_url=CLAUDE_CODE,
        )
        async with (
            httpx2.AsyncClient(transport=transport, auth=provider) as http,
            Client(streamable_http_client(MCP_URL, http_client=http)) as client,
        ):
            tools = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool("my_connections", {})

    assert {"my_connections", "revoke_connection"} <= tools
    assert not result.is_error
    assert result.structured_content is not None
    [connection] = result.structured_content["result"]
    assert (connection["client"], connection["current"]) == ("Claude Code", True)

    # One sign-in, as Claude Code: CIMD rather than registration, S256, and the resource
    # named. The provider checked the redirect's iss against the discovered issuer itself.
    [authorization_url] = person.opened
    request = dict(parse_qsl(urlsplit(authorization_url).query))
    assert authorization_url.startswith(f"{PUBLIC_URL}/authorize?")
    assert (request["client_id"], request["redirect_uri"]) == (CLAUDE_CODE, LOOPBACK)
    assert (request["code_challenge_method"], request["resource"]) == ("S256", MCP_URL)
    assert set(request["scope"].split()) == {"syncshelf", "offline_access"}
    assert [str(r.url) for r in fake.requests] == [CLAUDE_CODE]
