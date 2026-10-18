"""The two Claude client documents (SPEC §3.2.1), and a ``MockTransport`` that serves them.

These are built from the spec, not recorded: the capture from 2026-10-17 isn't in the repo,
and the fixture scrubber would turn every URL in it into a placeholder (#101). Each
holds the fields §3.2.1 and §3.2.3 name: ``client_id``, ``redirect_uris``,
``token_endpoint_auth_method`` and ``grant_types``. The hosted document's ``jwt-bearer`` grant
is the one the research capture noted (#22).
"""

import json
from collections.abc import Callable

import httpx2

from syncshelf.auth.cimd import CLAUDE_APPS, CLAUDE_CODE, HOSTED_CALLBACK

CLAUDE_APPS_DOC: dict[str, object] = {
    "client_id": CLAUDE_APPS,
    "redirect_uris": [HOSTED_CALLBACK],
    "token_endpoint_auth_method": "none",
    "grant_types": [
        "authorization_code",
        "refresh_token",
        "urn:ietf:params:oauth:grant-type:jwt-bearer",
    ],
    "response_types": ["code"],
}

CLAUDE_CODE_DOC: dict[str, object] = {
    "client_id": CLAUDE_CODE,
    "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"],
    "token_endpoint_auth_method": "none",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
}

DOCUMENTS = {CLAUDE_APPS: CLAUDE_APPS_DOC, CLAUDE_CODE: CLAUDE_CODE_DOC}

CACHE_CONTROL = "public, max-age=300"
"""What claude.ai serves both documents with (§3.2.1)."""


def document_response(doc: object, **headers: str) -> httpx2.Response:
    """A 200 serving ``doc`` as claude.ai does, with ``headers`` added or overriding."""
    return httpx2.Response(
        200,
        content=json.dumps(doc).encode(),
        headers={"content-type": "application/json", "cache-control": CACHE_CONTROL, **headers},
    )


class FakeClaude:
    """A ``MockTransport`` handler for claude.ai's two documents that records each request.

    ``respond`` replaces the default answer (the document for the requested URL, or 404)."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.respond: Callable[[httpx2.Request], httpx2.Response] | None = None

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.respond is not None:
            return self.respond(request)
        doc = DOCUMENTS.get(str(request.url))
        return document_response(doc) if doc is not None else httpx2.Response(404)

    @property
    def transport(self) -> httpx2.MockTransport:
        """A transport answering through this fake."""
        return httpx2.MockTransport(self)
