"""Drive the OAuth code flow through the real app, as Claude and the person signing in would
(SPEC §3.2.3, §3.2.4, §3.2.8 level 2): ``GET /authorize`` for the consent page, ``POST
/authorize`` to allow it, then ``POST /token`` to exchange the code.

Each step is its own helper, so a test can stop at or tamper with any one of them;
:func:`sign_in` runs them all and returns the token response.
"""

import base64
import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx2

from access_jwt import FakeAccess
from serving import BASE_URL, CLAUDE_CODE, MCP_URL

LOOPBACK = "http://127.0.0.1:53111/callback"
"""A Claude Code redirect, which its client document registers on any port."""
VERIFIER = "fake-code-verifier.0123456789_abcdefghijklmnop~"
"""A PKCE ``code_verifier`` using every kind of character RFC 7636 §4.1 allows."""
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode()[:-1]
""":data:`VERIFIER`'s S256 challenge, worked out here rather than by the code under test."""
STATE = 'st&te=<x> "y"'
TOKEN_HEADERS = {"cache-control": "no-store", "pragma": "no-cache"}
"""The headers on every ``/token`` response (§3.2.4)."""


def authorization_params(
    client_id: str = CLAUDE_CODE, redirect_uri: str = LOOPBACK, **overrides: str | None
) -> list[tuple[str, str]]:
    """A valid authorization request, as Claude sends it. An override of ``None`` drops the
    parameter."""
    params: dict[str, str | None] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": MCP_URL,
        "state": STATE,
        "scope": "syncshelf offline_access",
        **overrides,
    }
    return [(name, value) for name, value in params.items() if value is not None]


async def consent_blob(
    client: httpx2.AsyncClient, jwt: str, params: list[tuple[str, str]] | None = None
) -> str:
    """The signed blob from the consent page ``GET /authorize`` serves for ``params``."""
    response = await client.get(
        "/authorize",
        params=params or authorization_params(),
        headers={"cf-access-jwt-assertion": jwt},
    )
    assert response.status_code == 200, response.text
    match = re.search(r'name="consent" value="([^"]+)"', response.text)
    assert match is not None
    return match[1]


async def allow(
    client: httpx2.AsyncClient, blob: str, jwt: str | None, *, origin: str | None = BASE_URL
) -> httpx2.Response:
    """Press **Allow**: ``POST /authorize`` with ``blob``, as the browser would send it from
    the consent page, with ``jwt`` as the Access JWT and ``origin`` as ``Origin``."""
    headers = {}
    if jwt is not None:
        headers["cf-access-jwt-assertion"] = jwt
    if origin is not None:
        headers["origin"] = origin
    return await client.post("/authorize", data={"consent": blob}, headers=headers)


def redirected_to(response: httpx2.Response, redirect_uri: str = LOOPBACK) -> dict[str, str]:
    """The query of a 302 to ``redirect_uri``."""
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(redirect_uri + "?")
    return dict(parse_qsl(urlsplit(location).query))


async def authorization_code(
    client: httpx2.AsyncClient,
    access: FakeAccess,
    params: list[tuple[str, str]] | None = None,
    *,
    email: str = "alice@example.com",
) -> str:
    """Sign in as ``email``, allow the request, and return the code it redirects with."""
    params = params or authorization_params()
    jwt = access.token(email)
    response = await allow(client, await consent_blob(client, jwt, params), jwt)
    redirect_uri = dict(params)["redirect_uri"]
    return redirected_to(response, redirect_uri)["code"]


def token_form(code: str, /, **overrides: str | None) -> dict[str, str]:
    """A valid code-exchange body for ``code`` from :func:`authorization_params`'s request.
    An override of ``None`` drops the parameter."""
    form: dict[str, str | None] = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLAUDE_CODE,
        "redirect_uri": LOOPBACK,
        "code_verifier": VERIFIER,
        "resource": MCP_URL,
        **overrides,
    }
    return {name: value for name, value in form.items() if value is not None}


async def token_request(client: httpx2.AsyncClient, **kwargs: Any) -> httpx2.Response:
    """``POST /token`` with ``kwargs`` as httpx sends them (``data=`` for a form)."""
    return await client.post("/token", **kwargs)


def token_json(response: httpx2.Response, status: int = 200) -> dict[str, Any]:
    """A ``/token`` response's JSON, after checking its status and the headers every
    ``/token`` response carries, and that it allows no cross-origin reads."""
    assert response.status_code == status, response.text
    for name, value in TOKEN_HEADERS.items():
        assert response.headers[name] == value
    assert "access-control-allow-origin" not in response.headers
    return response.json()


async def sign_in(
    client: httpx2.AsyncClient,
    access: FakeAccess,
    params: list[tuple[str, str]] | None = None,
    *,
    email: str = "alice@example.com",
) -> dict[str, Any]:
    """The whole code flow for ``params`` (default :func:`authorization_params`) as
    ``email``: the token response's JSON, with ``access_token`` and, when the request asked
    for ``offline_access``, ``refresh_token``."""
    params = params or authorization_params()
    code = await authorization_code(client, access, params, email=email)
    request = dict(params)
    form = token_form(code, client_id=request["client_id"], redirect_uri=request["redirect_uri"])
    return token_json(await token_request(client, data=form))
