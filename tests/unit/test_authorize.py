"""``GET /authorize`` through the real app (SPEC §3.2.3): the client gate, the fetch and
redirect matching, then the request checks, the Access JWT, the profile and the consent page.

Until the client and redirect are trusted every failure is an HTML error page, never a
redirect; after, it is a 302 to the redirect. The Access JWTs are forged with a test RSA key
and checked against a mocked JWKS (§3.2.8 level 2)."""

import logging
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.auth.cimd import CLAUDE_APPS, CLAUDE_CODE, HOSTED_CALLBACK
from syncshelf.auth.oauth import ConsentRequest, error_page, verify_consent
from syncshelf.db import Database
from syncshelf.tools import ToolRegistry

from access_jwt import FakeAccess, routed, rsa_key
from cimd_docs import CLAUDE_APPS_DOC, FakeClaude, document_response
from clock import FakeClock
from serving import BASE_URL, MCP_URL, SECRET, TEAM_DOMAIN, make_config, running

pytestmark = pytest.mark.anyio

LOOPBACK = "http://127.0.0.1:53111/callback"
PAGE_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'; default-src 'self'",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
}
# The consent page alone lets its own Allow POST carry a real Origin (#114).
CONSENT_HEADERS = PAGE_HEADERS | {"referrer-policy": "same-origin"}


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
        profile_yaml={"alice": "{email: alice@example.com}", "bob": "{email: Bob@Example.com}"},
        auth_mode="oauth",
    )
    return create_app(
        config,
        data_dir=data_dir,
        clock=clock.time,
        registry=ToolRegistry(),
        http_transport=routed({"claude.ai": fake, str(urlsplit(TEAM_DOMAIN).hostname): access}),
    )


async def authorize(
    app: Starlette, params: list[tuple[str, str]], jwt: str | None = None, **headers: str
) -> httpx2.Response:
    if jwt is not None:
        headers["cf-access-jwt-assertion"] = jwt
    async with running(app, token=None) as client:
        return await client.get("/authorize", params=params, headers=headers)


def assert_error_page(response: httpx2.Response, status: int) -> None:
    assert response.status_code == status
    assert "location" not in response.headers
    assert response.headers["content-type"].startswith("text/html")
    for name, value in PAGE_HEADERS.items():
        assert response.headers[name] == value
    assert "access-control-allow-origin" not in response.headers


async def test_the_error_page_sends_no_referrer() -> None:
    # Only the consent page is relaxed to same-origin (#114); nothing else needs its Origin.
    assert error_page("Start again.").headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "params",
    [
        [("redirect_uri", HOSTED_CALLBACK)],
        [("client_id", "https://evil.com/claude.ai/x"), ("redirect_uri", HOSTED_CALLBACK)],
        [("client_id", "https://claude.ai/oauth/other"), ("redirect_uri", HOSTED_CALLBACK)],
        [("client_id", CLAUDE_APPS), ("client_id", CLAUDE_APPS), ("redirect_uri", HOSTED_CALLBACK)],
        [("client_id", CLAUDE_APPS)],
        [("client_id", CLAUDE_APPS), ("redirect_uri", HOSTED_CALLBACK), ("redirect_uri", "x")],
    ],
    ids=["no-client", "foreign", "unlisted-path", "repeated-client", "no-redirect", "repeated"],
)
async def test_an_untrusted_request_gets_an_error_page_and_nothing_is_fetched(
    app: Starlette, fake: FakeClaude, params: list[tuple[str, str]]
) -> None:
    assert_error_page(await authorize(app, params), 400)
    assert fake.requests == []


@pytest.mark.parametrize("name", ["client_id", "redirect_uri"])
async def test_a_repeated_parameter_is_refused_as_such(
    app: Starlette, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    params = [("client_id", CLAUDE_APPS), ("redirect_uri", HOSTED_CALLBACK), (name, "x")]
    with caplog.at_level(logging.INFO, logger="syncshelf.auth.oauth"):
        assert_error_page(await authorize(app, params), 400)
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "authorize_refused"]
    assert getattr(record, "reason", None) == "repeated_parameter"


async def test_a_redirect_the_client_doesnt_register_gets_an_error_page(
    app: Starlette, fake: FakeClaude
) -> None:
    response = await authorize(app, [("client_id", CLAUDE_APPS), ("redirect_uri", LOOPBACK)])
    assert_error_page(response, 400)
    assert len(fake.requests) == 1


async def test_a_fetch_failure_with_nothing_cached_gets_an_error_page(
    app: Starlette, fake: FakeClaude
) -> None:
    fake.respond = lambda request: httpx2.Response(503)
    response = await authorize(app, [("client_id", CLAUDE_CODE), ("redirect_uri", LOOPBACK)])
    assert_error_page(response, 502)


async def test_echoed_text_is_escaped(app: Starlette) -> None:
    response = await authorize(
        app, [("client_id", "<script>x</script>"), ("redirect_uri", HOSTED_CALLBACK)]
    )
    assert "<script>" not in response.text


# --- past the client gate: request checks, Access, profile, consent ------------------------

CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"  # RFC 7636's example S256 challenge
STATE = 'st&te=<x> "y"'


def request_params(
    client_id: str = CLAUDE_APPS, redirect_uri: str = HOSTED_CALLBACK, **overrides: str | None
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


def redirect_query(response: httpx2.Response, redirect_uri: str = HOSTED_CALLBACK) -> dict:
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith(redirect_uri + "?")
    assert response.headers["cache-control"] == "no-store"
    return dict(parse_qsl(urlsplit(location).query))


def assert_redirected(response: httpx2.Response, error: str) -> None:
    assert redirect_query(response) == {"error": error, "state": STATE, "iss": BASE_URL}


def page_text(response: httpx2.Response) -> str:
    return " ".join(re.sub(r"<[^>]+>", "", response.text).split())


def consent_of(response: httpx2.Response, now: float) -> ConsentRequest:
    match = re.search(r'name="consent" value="([^"]+)"', response.text)
    assert match is not None
    return verify_consent(match[1], SECRET, int(now))


async def test_a_valid_request_gets_the_consent_page(
    app: Starlette, access: FakeAccess, clock: FakeClock
) -> None:
    response = await authorize(app, request_params(), access.token())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    for name, value in CONSENT_HEADERS.items():
        assert response.headers[name] == value
    assert "access-control-allow-origin" not in response.headers
    text = page_text(response)
    assert "Connect claude.ai to SyncShelf as alice@example.com?" in text
    assert '<form method="post" action="/authorize">' in response.text
    assert "Allow" in text
    assert "this computer" not in text
    assert consent_of(response, clock.time()) == ConsentRequest(
        client_id=CLAUDE_APPS,
        redirect_uri=HOSTED_CALLBACK,
        code_challenge=CHALLENGE,
        resource=MCP_URL,
        state=STATE,
        scopes=("syncshelf", "offline_access"),
        email="alice@example.com",
        refresh_allowed=True,
    )


async def test_a_claude_code_redirect_adds_the_loopback_warning(
    app: Starlette, access: FakeAccess
) -> None:
    response = await authorize(app, request_params(CLAUDE_CODE, LOOPBACK), access.token())
    assert response.status_code == 200
    text = page_text(response)
    assert "Connect claude.ai to SyncShelf as alice@example.com?" in text
    assert (
        "Code will be sent to a program on this computer (127.0.0.1:53111). "
        "Continue only if you just started this from Claude Code." in text
    )


async def test_the_page_names_the_client_id_host_never_client_name(
    app: Starlette, fake: FakeClaude, access: FakeAccess
) -> None:
    fake.respond = lambda request: document_response(
        {**CLAUDE_APPS_DOC, "client_name": "Totally Legit App"}
    )
    response = await authorize(app, request_params(), access.token())
    assert response.status_code == 200
    assert "Totally Legit App" not in response.text


async def test_the_email_is_lowercased_before_the_profile_lookup(
    app: Starlette, access: FakeAccess, clock: FakeClock
) -> None:
    response = await authorize(app, request_params(), access.token("BOB@example.com"))
    assert response.status_code == 200
    assert consent_of(response, clock.time()).email == "bob@example.com"


def _issued(conn: sqlite3.Connection) -> tuple[int, int]:
    grants = conn.execute("SELECT count(*) FROM oauth_grant").fetchone()[0]
    codes = conn.execute("SELECT count(*) FROM oauth_code").fetchone()[0]
    return grants, codes


async def test_an_email_with_no_profile_gets_a_page_and_nothing_is_issued(
    app: Starlette, access: FakeAccess
) -> None:
    async with running(app, token=None) as client:
        response = await client.get(
            "/authorize",
            params=request_params(),
            headers={"cf-access-jwt-assertion": access.token("<carol>@example.com")},
        )
        db: Database = app.state.db
        issued = await db.read(_issued)
    assert_error_page(response, 403)
    assert "There is no SyncShelf profile for &lt;carol&gt;@example.com." in response.text
    assert "<carol>" not in response.text
    assert 'name="consent"' not in response.text
    assert issued == (0, 0)


@pytest.mark.parametrize(
    "variant", ["missing", "bad-signature", "alg-hs256", "wrong-aud", "wrong-type", "expired"]
)
async def test_a_bad_access_jwt_is_access_denied(
    app: Starlette, access: FakeAccess, clock: FakeClock, variant: str
) -> None:
    token = {
        "missing": None,
        "bad-signature": access.token(key=rsa_key("someone-else")),
        "alg-hs256": access.token(algorithm="HS256"),
        "wrong-aud": access.token(aud=["another-app"]),
        "wrong-type": access.token(type="org"),
        "expired": access.token(exp=int(clock.time()) - 31),
    }[variant]
    assert_redirected(await authorize(app, request_params(), token), "access_denied")


async def test_the_unsigned_email_header_is_never_trusted(app: Starlette) -> None:
    response = await authorize(
        app, request_params(), **{"cf-access-authenticated-user-email": "alice@example.com"}
    )
    assert_redirected(response, "access_denied")


async def test_unavailable_access_keys_are_temporarily_unavailable(
    app: Starlette, access: FakeAccess
) -> None:
    access.respond = lambda request: httpx2.Response(503)
    response = await authorize(app, request_params(), access.token())
    assert_redirected(response, "temporarily_unavailable")


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"response_type": None}, "invalid_request"),
        ({"response_type": "token"}, "unsupported_response_type"),
        ({"code_challenge_method": None}, "invalid_request"),
        ({"code_challenge_method": "plain"}, "invalid_request"),
        ({"code_challenge_method": "s256"}, "invalid_request"),
        ({"code_challenge": None}, "invalid_request"),
        ({"code_challenge": CHALLENGE[:-1]}, "invalid_request"),
        ({"code_challenge": CHALLENGE + "A"}, "invalid_request"),
        ({"code_challenge": CHALLENGE[:-1] + "="}, "invalid_request"),
        ({"resource": None}, "invalid_target"),
        ({"resource": BASE_URL}, "invalid_target"),
        ({"resource": MCP_URL + "/other"}, "invalid_target"),
        ({"resource": MCP_URL + "//"}, "invalid_target"),
        ({"resource": "https://localhost:8000/mcp"}, "invalid_target"),
        ({"resource": MCP_URL + "?x=1"}, "invalid_target"),
        ({"resource": "not a url"}, "invalid_target"),
    ],
)
async def test_request_hygiene_redirects_the_error(
    app: Starlette, access: FakeAccess, overrides: dict[str, str | None], error: str
) -> None:
    response = await authorize(app, request_params(**overrides), access.token())
    assert_redirected(response, error)


async def test_a_bad_request_is_refused_before_any_access_key_fetch(
    app: Starlette, access: FakeAccess
) -> None:
    await authorize(app, request_params(response_type="token"), access.token())
    assert access.requests == []


@pytest.mark.parametrize("name", ["state", "scope", "resource", "code_challenge", "extra"])
async def test_any_repeated_parameter_is_invalid_request(
    app: Starlette, access: FakeAccess, name: str
) -> None:
    params = [*request_params(), (name, "x"), *([(name, "y")] if name == "extra" else [])]
    query = redirect_query(await authorize(app, params, access.token()))
    assert query["error"] == "invalid_request"
    assert ("state" in query) is (name != "state"), "a repeated state isn't echoed"


async def test_an_error_for_a_request_without_state_sends_none(
    app: Starlette, access: FakeAccess
) -> None:
    response = await authorize(app, request_params(state=None, response_type="x"), access.token())
    assert redirect_query(response) == {"error": "unsupported_response_type", "iss": BASE_URL}


@pytest.mark.parametrize(
    "resource", ["HTTP://LOCALHOST:8000/mcp", "http://Localhost:8000/mcp/", MCP_URL]
)
async def test_resource_is_canonicalised(
    app: Starlette, access: FakeAccess, clock: FakeClock, resource: str
) -> None:
    response = await authorize(app, request_params(resource=resource), access.token())
    assert response.status_code == 200
    assert consent_of(response, clock.time()).resource == MCP_URL


@pytest.mark.parametrize(
    ("scope", "granted"),
    [
        (None, ("syncshelf",)),
        ("syncshelf", ("syncshelf",)),
        ("offline_access", ("syncshelf", "offline_access")),
        ("syncshelf offline_access read:all", ("syncshelf", "offline_access")),
        ("unknown", ("syncshelf",)),
    ],
)
async def test_scopes_are_syncshelf_plus_offline_access_when_asked(
    app: Starlette,
    access: FakeAccess,
    clock: FakeClock,
    scope: str | None,
    granted: tuple[str, ...],
) -> None:
    response = await authorize(app, request_params(scope=scope), access.token())
    assert consent_of(response, clock.time()).scopes == granted


async def test_a_client_without_the_refresh_grant_is_marked_so(
    app: Starlette, fake: FakeClaude, access: FakeAccess, clock: FakeClock
) -> None:
    fake.respond = lambda request: document_response(
        {**CLAUDE_APPS_DOC, "grant_types": ["authorization_code"]}
    )
    response = await authorize(app, request_params(), access.token())
    assert consent_of(response, clock.time()).refresh_allowed is False


async def test_a_trusted_refusal_logs_its_reason_but_not_the_email(
    app: Starlette, access: FakeAccess, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger="syncshelf.auth.oauth"):
        await authorize(app, request_params(), access.token("carol@example.com"))
    (record,) = [r for r in caplog.records if getattr(r, "event", None) == "authorize_refused"]
    assert getattr(record, "reason", None) == "no_profile"
    assert getattr(record, "client_label", None) == "claude.ai"
    assert "carol" not in record.getMessage()
