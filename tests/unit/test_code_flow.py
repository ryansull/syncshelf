"""The code flow through the real app (SPEC §3.2.3 consent POST, §3.2.4, §3.2.8 level 2):
**Allow** issues a code, ``/token`` exchanges it, and the access token works on ``/mcp``.

The Access JWTs are forged with a test RSA key against a mocked JWKS, and the client
documents come from :class:`cimd_docs.FakeClaude`."""

import logging
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.auth.cimd import CLAUDE_APPS, HOSTED_CALLBACK
from syncshelf.auth.tokens import hash_token
from syncshelf.db import Database

from access_jwt import FakeAccess, routed
from cimd_docs import CLAUDE_CODE_DOC, FakeClaude, document_response
from clock import FakeClock
from oauth_flow import (
    CHALLENGE,
    LOOPBACK,
    STATE,
    allow,
    authorization_code,
    authorization_params,
    consent_blob,
    redirected_to,
    sign_in,
    token_form,
    token_json,
    token_request,
)
from serving import BASE_URL, CLAUDE_CODE, MCP_URL, TEAM_DOMAIN, make_config, mcp_client, running

pytestmark = pytest.mark.anyio

ERROR_PAGE = "SyncShelf can't sign you in"


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
        profiles=["alice", "bob"],
        profile_yaml={"alice": "{email: alice@example.com}", "bob": "{email: bob@example.com}"},
        auth_mode="oauth",
    )
    return create_app(
        config,
        data_dir=data_dir,
        clock=clock.time,
        http_transport=routed({"claude.ai": fake, str(urlsplit(TEAM_DOMAIN).hostname): access}),
    )


@pytest.fixture
async def client(app: Starlette) -> AsyncIterator[httpx2.AsyncClient]:
    async with running(app, token=None) as http:
        yield http


def store(app: Starlette) -> Database:
    return app.state.db


def _codes(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT code_hash, client_id, redirect_uri, code_challenge, resource, scopes, profile, "
        "email, created_at, expires_at, used_at, grant_id FROM oauth_code"
    ).fetchall()


def _grants(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return conn.execute(
        "SELECT id, profile, email, client_id, client_label, resource, scopes, revoked_reason "
        "FROM oauth_grant ORDER BY id"
    ).fetchall()


# --- the whole flow ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client_id", "redirect_uri", "client_name"),
    [(CLAUDE_CODE, LOOPBACK, "Claude Code"), (CLAUDE_APPS, HOSTED_CALLBACK, "Claude app")],
)
async def test_the_full_code_flow_ends_in_a_tool_call_on_mcp(
    client: httpx2.AsyncClient,
    access: FakeAccess,
    clock: FakeClock,
    client_id: str,
    redirect_uri: str,
    client_name: str,
) -> None:
    params = authorization_params(client_id, redirect_uri)
    jwt = access.token()
    response = await allow(client, await consent_blob(client, jwt, params), jwt)
    query = redirected_to(response, redirect_uri)
    assert response.headers["cache-control"] == "no-store"
    assert set(query) == {"code", "state", "iss"}
    assert (query["state"], query["iss"]) == (STATE, BASE_URL)

    clock.advance(30)
    tokens = token_json(
        await token_request(
            client,
            data=token_form(query["code"], client_id=client_id, redirect_uri=redirect_uri),
        )
    )
    assert set(tokens) == {"access_token", "token_type", "expires_in", "refresh_token", "scope"}
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["scope"] == "syncshelf offline_access"
    assert tokens["access_token"].startswith("ssat_")
    assert tokens["refresh_token"].startswith("ssrt_")

    client.headers["authorization"] = f"Bearer {tokens['access_token']}"
    async with mcp_client(client) as mcp:
        result = await mcp.call_tool("my_connections", {})
    assert not result.is_error, result.content
    assert result.structured_content is not None
    [connection] = result.structured_content["result"]
    assert connection["client"] == client_name
    assert connection["current"] is True


async def test_the_code_is_bound_to_the_signed_request_and_stored_hashed(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock
) -> None:
    now = int(clock.time())
    code = await authorization_code(client, access)
    assert len(code) == 43  # token_urlsafe(32)
    [row] = await store(app).read(_codes)
    assert row == (
        hash_token(code),
        CLAUDE_CODE,
        LOOPBACK,
        CHALLENGE,
        MCP_URL,
        "syncshelf offline_access",
        "alice",
        "alice@example.com",
        now,
        now + 120,
        None,
        None,
    )
    assert await store(app).read(_grants) == [], "the grant is made on redemption"

    tokens = token_json(await token_request(client, data=token_form(code)))
    [grant] = await store(app).read(_grants)
    assert grant == (
        grant[0],
        "alice",
        "alice@example.com",
        CLAUDE_CODE,
        "Claude Code",
        MCP_URL,
        "syncshelf offline_access",
        None,
    )
    [row] = await store(app).read(_codes)
    assert row[-2:] == (now, grant[0])
    assert tokens["scope"] == "syncshelf offline_access"


@pytest.mark.parametrize(
    ("scope", "granted"),
    [
        (None, "syncshelf"),
        ("syncshelf", "syncshelf"),
        ("syncshelf read:all", "syncshelf"),
        ("offline_access unknown", "syncshelf offline_access"),
    ],
)
async def test_grants_carry_syncshelf_plus_offline_access_when_asked(
    client: httpx2.AsyncClient, access: FakeAccess, scope: str | None, granted: str
) -> None:
    tokens = await sign_in(client, access, authorization_params(scope=scope))
    assert tokens["scope"] == granted
    assert ("refresh_token" in tokens) is ("offline_access" in granted)


async def test_a_client_whose_document_disallows_refresh_gets_no_refresh_token(
    client: httpx2.AsyncClient, fake: FakeClaude, access: FakeAccess
) -> None:
    fake.respond = lambda request: document_response(
        {**CLAUDE_CODE_DOC, "grant_types": ["authorization_code"]}
    )
    tokens = await sign_in(client, access)
    assert "refresh_token" not in tokens
    assert tokens["scope"] == "syncshelf"


async def test_raw_codes_and_tokens_are_never_logged(
    client: httpx2.AsyncClient, access: FakeAccess, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        code = await authorization_code(client, access)
        tokens = token_json(await token_request(client, data=token_form(code)))
        await token_request(client, data=token_form(code))  # a replay, logged as such
    logged = caplog.text + "".join(repr(r.__dict__) for r in caplog.records)
    for secret in (code, tokens["access_token"], tokens["refresh_token"]):
        assert secret not in logged
    events = [getattr(r, "event", None) for r in caplog.records]
    assert {"code_issued", "grant_created", "grant_revoked", "token_request_refused"} <= set(events)


# --- the consent POST ----------------------------------------------------------------------


async def assert_nothing_issued(app: Starlette) -> None:
    assert await store(app).read(_codes) == []


@pytest.mark.parametrize(
    "origin", [None, "null", "https://evil.example.com", BASE_URL + "/", "http://127.0.0.1:8000"]
)
async def test_a_post_not_from_the_issuer_is_access_denied(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess, origin: str | None
) -> None:
    jwt = access.token()
    response = await allow(client, await consent_blob(client, jwt), jwt, origin=origin)
    assert redirected_to(response) == {"error": "access_denied", "state": STATE, "iss": BASE_URL}
    await assert_nothing_issued(app)


@pytest.mark.parametrize("variant", ["other-person", "missing", "expired"])
async def test_the_post_needs_a_fresh_access_jwt_for_the_signed_email(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock, variant: str
) -> None:
    blob = await consent_blob(client, access.token())
    jwt = {
        "other-person": access.token("bob@example.com"),
        "missing": None,
        "expired": access.token(exp=int(clock.time()) - 31),
    }[variant]
    response = await allow(client, blob, jwt)
    assert redirected_to(response)["error"] == "access_denied"
    await assert_nothing_issued(app)


async def test_the_signed_email_is_compared_lowercased(
    client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    blob = await consent_blob(client, access.token("Alice@Example.com"))
    response = await allow(client, blob, access.token("ALICE@example.com"))
    assert "code" in redirected_to(response)


async def test_a_post_without_state_redirects_without_one(
    client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    jwt = access.token()
    blob = await consent_blob(client, jwt, authorization_params(state=None))
    assert set(redirected_to(await allow(client, blob, jwt))) == {"code", "iss"}


@pytest.mark.parametrize("variant", ["tampered", "expired", "missing", "repeated", "json"])
async def test_a_consent_blob_that_doesnt_verify_gets_an_error_page(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock, variant: str
) -> None:
    jwt = access.token()
    blob = await consent_blob(client, jwt)
    headers = {"cf-access-jwt-assertion": jwt, "origin": BASE_URL}
    if variant == "expired":
        clock.advance(600)
        jwt = headers["cf-access-jwt-assertion"] = access.token()
    body: dict[str, Any] = {
        "tampered": {"data": {"consent": "A" + blob[1:]}},
        "expired": {"data": {"consent": blob}},
        "missing": {"data": {}},
        "repeated": {"content": f"consent={blob}&consent={blob}"},
        "json": {"json": {"consent": blob}},
    }[variant]
    if variant == "repeated":
        headers["content-type"] = "application/x-www-form-urlencoded"
    response = await client.post("/authorize", headers=headers, **body)
    assert response.status_code == 400
    assert "location" not in response.headers
    assert ERROR_PAGE in response.text
    await assert_nothing_issued(app)


# --- the code exchange ---------------------------------------------------------------------


async def test_a_replayed_code_is_invalid_grant_and_revokes_its_grant(
    app: Starlette, client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    code = await authorization_code(client, access)
    tokens = token_json(await token_request(client, data=token_form(code)))
    replay = await token_request(client, data=token_form(code))
    assert token_json(replay, 400) == {"error": "invalid_grant"}
    [grant] = await store(app).read(_grants)
    assert grant[-1] == "code_replay"
    client.headers["authorization"] = f"Bearer {tokens['access_token']}"
    response = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(("wait", "status"), [(119, 200), (121, 400)])
async def test_a_code_lives_120_seconds(
    client: httpx2.AsyncClient, access: FakeAccess, clock: FakeClock, wait: int, status: int
) -> None:
    code = await authorization_code(client, access)
    clock.advance(wait)
    body = token_json(await token_request(client, data=token_form(code)), status)
    if status == 400:
        assert body == {"error": "invalid_grant"}


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"code_verifier": "x" * 43}, "invalid_grant"),
        ({"redirect_uri": "http://127.0.0.1:53112/callback"}, "invalid_grant"),
        ({"client_id": CLAUDE_APPS}, "invalid_grant"),
        ({"resource": BASE_URL}, "invalid_target"),
        ({"resource": MCP_URL + "/other"}, "invalid_target"),
        ({"resource": "not a url"}, "invalid_target"),
    ],
    ids=["pkce", "redirect", "client", "resource-root", "resource-path", "resource-junk"],
)
async def test_a_mismatch_with_the_code_is_refused_and_spends_it(
    app: Starlette,
    client: httpx2.AsyncClient,
    access: FakeAccess,
    overrides: dict[str, str],
    error: str,
) -> None:
    code = await authorization_code(client, access)
    response = await token_request(client, data=token_form(code, **overrides))
    assert token_json(response, 400) == {"error": error}
    assert await store(app).read(_grants) == []
    # The code is spent, so the right values can't follow up a guess.
    retry = await token_request(client, data=token_form(code))
    assert token_json(retry, 400) == {"error": "invalid_grant"}
    assert await store(app).read(_grants) == []


@pytest.mark.parametrize(
    "resource", [None, "HTTP://LOCALHOST:8000/mcp", "http://localhost:8000/mcp/"]
)
async def test_resource_is_optional_and_canonicalised(
    client: httpx2.AsyncClient, access: FakeAccess, resource: str | None
) -> None:
    code = await authorization_code(client, access)
    token_json(await token_request(client, data=token_form(code, resource=resource)))


async def test_an_unknown_code_is_invalid_grant(client: httpx2.AsyncClient) -> None:
    response = await token_request(client, data=token_form("not-a-code"))
    assert token_json(response, 400) == {"error": "invalid_grant"}


@pytest.mark.parametrize(
    "body",
    [
        {"json": token_form("code")},
        {"files": {"x": ("x", b"x")}, "data": token_form("code")},
        {"content": b"grant_type=authorization_code", "headers": {"content-type": "text/plain"}},
        {"content": b"", "headers": {}},
        {
            "content": b"grant_type=authorization_code&code=%FF",
            "headers": {"content-type": "application/x-www-form-urlencoded"},
        },
    ],
    ids=["json", "multipart", "text", "no-type", "not-utf8"],
)
async def test_only_a_form_encoded_body_is_accepted(
    client: httpx2.AsyncClient, body: dict[str, Any]
) -> None:
    response = await token_request(client, **body)
    assert token_json(response, 400) == {"error": "invalid_request"}


async def test_a_form_with_a_charset_is_accepted(
    client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    code = await authorization_code(client, access)
    response = await token_request(
        client,
        content=httpx2.QueryParams(token_form(code)).__str__().encode(),
        headers={"content-type": "application/x-www-form-urlencoded; charset=UTF-8"},
    )
    token_json(response)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"grant_type": None}, "invalid_request"),
        ({"code": None}, "invalid_request"),
        ({"redirect_uri": None}, "invalid_request"),
        ({"code_verifier": None}, "invalid_request"),
        ({"code_verifier": "x" * 42}, "invalid_request"),
        ({"code_verifier": "x" * 129}, "invalid_request"),
        ({"code_verifier": "x" * 42 + "+"}, "invalid_request"),
        ({"grant_type": "client_credentials"}, "unsupported_grant_type"),
        ({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer"}, "unsupported_grant_type"),
        # A refresh with no refresh_token: the code's parameters don't make it an exchange.
        ({"grant_type": "refresh_token"}, "invalid_request"),
    ],
)
async def test_a_bad_request_is_refused_before_the_code_is_spent(
    client: httpx2.AsyncClient,
    access: FakeAccess,
    overrides: dict[str, str | None],
    error: str,
) -> None:
    code = await authorization_code(client, access)
    response = await token_request(client, data=token_form(code, **overrides))
    assert token_json(response, 400) == {"error": error}
    token_json(await token_request(client, data=token_form(code)))


async def test_a_repeated_parameter_is_invalid_request(
    client: httpx2.AsyncClient, access: FakeAccess
) -> None:
    code = await authorization_code(client, access)
    form = [*token_form(code).items(), ("code", code)]
    response = await token_request(
        client,
        content=str(httpx2.QueryParams(form)).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert token_json(response, 400) == {"error": "invalid_request"}


@pytest.mark.parametrize(
    "client_id", [None, "", "https://evil.com/claude.ai/x", "https://claude.ai/oauth/other"]
)
async def test_a_client_failing_the_gate_is_invalid_client_and_nothing_is_fetched(
    client: httpx2.AsyncClient, access: FakeAccess, fake: FakeClaude, client_id: str | None
) -> None:
    code = await authorization_code(client, access)
    fetched = len(fake.requests)
    response = await token_request(client, data=token_form(code, client_id=client_id))
    assert token_json(response, 401) == {"error": "invalid_client"}
    assert len(fake.requests) == fetched


async def test_the_token_endpoint_never_fetches_a_client_document(
    client: httpx2.AsyncClient, access: FakeAccess, fake: FakeClaude
) -> None:
    code = await authorization_code(client, access)
    fetched = len(fake.requests)
    token_json(await token_request(client, data=token_form(code)))
    assert len(fake.requests) == fetched


async def test_the_token_endpoint_has_no_cors(client: httpx2.AsyncClient) -> None:
    response = await client.options(
        "/token",
        headers={"origin": "https://claude.ai", "access-control-request-method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers
