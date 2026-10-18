"""``GET /authorize`` through the real app, so far: the client gate, the fetch and redirect
matching (SPEC §3.2.3). Every failure here is an HTML error page, never a redirect."""

import logging
from pathlib import Path

import httpx2
import pytest
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.auth.cimd import CLAUDE_APPS, CLAUDE_CODE, HOSTED_CALLBACK
from syncshelf.tools import ToolRegistry

from cimd_docs import FakeClaude
from clock import FakeClock
from serving import make_config, running

pytestmark = pytest.mark.anyio

LOOPBACK = "http://127.0.0.1:53111/callback"
PAGE_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'; default-src 'self'",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
}


@pytest.fixture
def fake() -> FakeClaude:
    return FakeClaude()


@pytest.fixture
def app(tmp_path: Path, data_dir: Path, clock: FakeClock, fake: FakeClaude) -> Starlette:
    return create_app(
        make_config(tmp_path, auth_mode="oauth"),
        data_dir=data_dir,
        clock=clock.time,
        registry=ToolRegistry(),
        http_transport=fake.transport,
    )


async def authorize(app: Starlette, params: list[tuple[str, str]]) -> httpx2.Response:
    async with running(app, token=None) as client:
        return await client.get("/authorize", params=params)


def assert_error_page(response: httpx2.Response, status: int) -> None:
    assert response.status_code == status
    assert "location" not in response.headers
    assert response.headers["content-type"].startswith("text/html")
    for name, value in PAGE_HEADERS.items():
        assert response.headers[name] == value
    assert "access-control-allow-origin" not in response.headers


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


@pytest.mark.parametrize(
    ("client_id", "redirect_uri"), [(CLAUDE_APPS, HOSTED_CALLBACK), (CLAUDE_CODE, LOOPBACK)]
)
async def test_a_trusted_request_reaches_the_next_step(
    app: Starlette, fake: FakeClaude, client_id: str, redirect_uri: str
) -> None:
    # The next step (Access, request checks, consent) is #66; until then it answers 501.
    response = await authorize(app, [("client_id", client_id), ("redirect_uri", redirect_uri)])
    assert_error_page(response, 501)
    assert [str(r.url) for r in fake.requests] == [client_id]
