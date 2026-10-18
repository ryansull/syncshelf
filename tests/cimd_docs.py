"""The two Claude client documents (SPEC §3.2.1), and a ``MockTransport`` that serves them.

These are claude.ai's real documents, fetched once each on 2026-10-18 with an anonymous GET
(#101). Both came back ``200`` with ``Content-Type: application/json`` and
``Cache-Control: public, max-age=300``, which :class:`FakeClaude` serves them with. They live
here rather than under ``tests/fixtures/`` because the fixture scrubber would blank every URL
in them, and a CIMD document is made of URLs.

Beyond the fields §3.2.1 and §3.2.3 name, each also carries ``client_name`` and
``client_uri``, which :func:`~syncshelf.auth.cimd.parse_document` ignores.
"""

import json
from collections.abc import Callable

import httpx2

from syncshelf.auth.cimd import CLAUDE_APPS, CLAUDE_CODE

CLAUDE_APPS_JSON = (
    '{"client_id":"https://claude.ai/oauth/mcp-oauth-client-metadata",'
    '"client_name":"Claude",'
    '"client_uri":"https://claude.ai",'
    '"redirect_uris":["https://claude.ai/api/mcp/auth_callback"],'
    '"grant_types":["authorization_code","refresh_token","urn:ietf:params:oauth:grant-type:jwt-bearer"],'
    '"response_types":["code"],'
    '"token_endpoint_auth_method":"none"}'
)
"""The Claude apps' document, byte for byte as claude.ai served it on 2026-10-18."""

CLAUDE_CODE_JSON = (
    '{"client_id":"https://claude.ai/oauth/claude-code-client-metadata",'
    '"client_name":"Claude Code",'
    '"client_uri":"https://claude.ai",'
    '"redirect_uris":["http://localhost/callback","http://127.0.0.1/callback"],'
    '"grant_types":["authorization_code","refresh_token"],'
    '"response_types":["code"],'
    '"token_endpoint_auth_method":"none"}'
)
"""Claude Code's document, byte for byte as claude.ai served it on 2026-10-18."""

CLAUDE_APPS_DOC: dict[str, object] = json.loads(CLAUDE_APPS_JSON)
CLAUDE_CODE_DOC: dict[str, object] = json.loads(CLAUDE_CODE_JSON)

_BODIES = {CLAUDE_APPS: CLAUDE_APPS_JSON, CLAUDE_CODE: CLAUDE_CODE_JSON}

CONTENT_TYPE = "application/json"
CACHE_CONTROL = "public, max-age=300"
"""The headers claude.ai served both documents with on 2026-10-18 (as §3.2.1 says)."""


def document_response(doc: object, **headers: str) -> httpx2.Response:
    """A 200 serving ``doc`` as claude.ai does, with ``headers`` added or overriding.

    ``doc`` is JSON-encoded unless it is already a ``str``, which is served as is."""
    body = doc if isinstance(doc, str) else json.dumps(doc)
    return httpx2.Response(
        200,
        content=body.encode(),
        headers={"content-type": CONTENT_TYPE, "cache-control": CACHE_CONTROL, **headers},
    )


class FakeClaude:
    """A ``MockTransport`` handler for claude.ai's two documents that records each request.

    ``respond`` replaces the default answer (the exact document for the requested URL, or
    404)."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.respond: Callable[[httpx2.Request], httpx2.Response] | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.respond is not None:
            return self.respond(request)
        body = _BODIES.get(str(request.url))
        return document_response(body) if body is not None else httpx2.Response(404)

    @property
    def transport(self) -> httpx2.MockTransport:
        """A transport answering through this fake."""
        return httpx2.MockTransport(self)
