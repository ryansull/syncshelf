"""SyncShelf as the OAuth authorization server (SPEC §3.2): its routes on the outer app.

Discovery (§3.2.1, §3.2.2):

- ``GET /.well-known/oauth-authorization-server`` serves the authorization-server metadata,
  built from ``server.public_url``. It offers CIMD, not dynamic client registration, so there
  is no ``registration_endpoint`` and no ``/register`` route.
- ``GET /.well-known/oauth-protected-resource/mcp`` serves the protected-resource metadata
  (PRM). The SDK serves a PRM on the MCP mount too, but advertises as ``scopes_supported``
  only the scopes it requires of every token, and requiring ``offline_access`` would refuse
  every connection that didn't ask for a refresh token. So this route, on the outer app before
  the mount, shadows the SDK's with one that advertises both scopes: Claude asks for
  ``offline_access``, and so gets a refresh token, only when it is advertised.

``GET /authorize`` (§3.2.3) so far decides only whether the client and redirect can be
trusted, through :func:`syncshelf.auth.cimd.check_client`. Until they are, ``redirect_uri``
is untrusted, so a refusal is an HTML :func:`error_page` and never a redirect. A trusted
request goes on to :func:`continue_authorize`, which is still a placeholder: the Access JWT,
the rest of the request checks and consent follow (#66).

:func:`oauth_routes` are the authorization server's own routes, served only when
``server.auth_mode`` is ``oauth``. :func:`protected_resource_route` belongs to the resource
server, which runs in every mode, as the SDK's did. Both documents are plain JSON built from
the configured ``public_url`` string, never through pydantic's ``AnyHttpUrl``, which would
lowercase the host, drop a default port or add a trailing slash: clients compare the issuer
and ``resource`` exactly (RFC 8414, RFC 9207, §3.2.1). Both get the CORS and
``Cache-Control`` the SDK gives its own well-known routes.
"""

import html
import logging
from typing import Any

from mcp.server.auth.routes import cors_middleware
from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from syncshelf.auth.cimd import ClientDocuments, ClientRefused, TrustedClient, check_client
from syncshelf.auth.tokens import SCOPE
from syncshelf.config import ServerConfig

__all__ = [
    "AUTHORIZE_PATH",
    "METADATA_PATH",
    "OFFLINE_ACCESS",
    "PRM_PATH",
    "SCOPES_SUPPORTED",
    "TOKEN_PATH",
    "authorization_server_metadata",
    "continue_authorize",
    "error_page",
    "oauth_routes",
    "protected_resource_metadata",
    "protected_resource_route",
]

METADATA_PATH = "/.well-known/oauth-authorization-server"
"""Where the authorization-server metadata is served (RFC 8414, §2.2)."""

PRM_PATH = "/.well-known/oauth-protected-resource/mcp"
"""Where the PRM for ``/mcp`` is served (RFC 9728 §3.1, §2.2). It must be the path the SDK
names in its 401 challenge, or the challenge leads to the SDK's PRM, which has no scopes; a
test holds the two together."""

AUTHORIZE_PATH = "/authorize"
"""The authorization endpoint (§3.2.3)."""

TOKEN_PATH = "/token"
"""The token endpoint (§3.2.4)."""

OFFLINE_ACCESS = "offline_access"
"""The scope a client asks for to get a refresh token (§3.2.2)."""

SCOPES_SUPPORTED = (SCOPE, OFFLINE_ACCESS)
"""The scopes both metadata documents advertise, in §3.2.2's order."""

_WELL_KNOWN_METHODS = ["GET", "OPTIONS"]
_WELL_KNOWN_HEADERS = {"Cache-Control": "public, max-age=3600"}  # as the SDK's handlers send

_PAGE_HEADERS = {
    # The consent page's hardening (§3.2.3), on every page /authorize renders.
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'; default-src 'self'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}

_ERROR_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>SyncShelf sign-in</title></head>
<body>
<h1>SyncShelf can't sign you in</h1>
<p>{message}</p>
</body>
</html>
"""

log = logging.getLogger(__name__)


def authorization_server_metadata(server: ServerConfig) -> dict[str, Any]:
    """The §3.2.2 document for ``server.public_url``, which is the issuer."""
    issuer = server.public_url
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + AUTHORIZE_PATH,
        "token_endpoint": issuer + TOKEN_PATH,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # A public client: PKCE stands in for a secret (§3.2.1).
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "scopes_supported": list(SCOPES_SUPPORTED),
    }


def protected_resource_metadata(server: ServerConfig) -> dict[str, Any]:
    """The PRM for ``/mcp``: its ``resource`` is exactly the MCP URL, its one authorization
    server is the issuer, and it advertises :data:`SCOPES_SUPPORTED` (§3.2.1, §3.2.2).

    ``bearer_methods_supported`` is kept from the SDK's document, since MCP takes a bearer
    token in the header only.
    """
    return {
        "resource": server.mcp_url,
        "authorization_servers": [server.public_url],
        "scopes_supported": list(SCOPES_SUPPORTED),
        "bearer_methods_supported": ["header"],
    }


def oauth_routes(server: ServerConfig, clients: ClientDocuments) -> list[Route]:
    """The authorization server's routes, in §2.2 order, for the outer app before the MCP
    mount. Serve them only in ``oauth`` mode: dev mode disables them (§3.2.5).

    ``clients`` fetches and caches the client documents ``/authorize`` checks against.
    """

    async def authorize(request: Request) -> Response:
        params = request.query_params
        try:
            trusted = await check_client(
                clients, _single(params, "client_id"), _single(params, "redirect_uri")
            )
        except ClientRefused as refused:
            log.info(
                "refused an authorization request: %s",
                refused.reason,
                extra={"event": "authorize_refused", "reason": refused.reason},
            )
            return error_page(refused.message, status_code=refused.status_code)
        return await continue_authorize(request, trusted)

    return [
        _well_known(METADATA_PATH, authorization_server_metadata(server)),
        # No CORS: neither Claude's backend nor Claude Code needs it (§3.2.4).
        Route(AUTHORIZE_PATH, endpoint=authorize, methods=["GET"]),
    ]


async def continue_authorize(request: Request, client: TrustedClient) -> Response:
    """The rest of ``GET /authorize``, once ``client`` and its redirect are trusted.

    A placeholder until the Access JWT, the request checks and the consent page are built
    (#66): it answers 501 and issues nothing.
    """
    return error_page("Signing in to SyncShelf isn't available yet.", status_code=501)


def error_page(message: str, *, status_code: int = 400) -> HTMLResponse:
    """An HTML page showing ``message``, escaped, for an ``/authorize`` error that must not
    be redirected: one before the client and redirect are trusted (§3.2.3), or one with
    nowhere safe to send it. It carries the consent page's headers, so it can't be framed or
    cached.
    """
    return HTMLResponse(
        _ERROR_PAGE.format(message=html.escape(message)),
        status_code=status_code,
        headers=_PAGE_HEADERS,
    )


def _single(params: QueryParams, name: str) -> str | None:
    """Parameter ``name``'s one value, or ``None`` when it is missing. A repeated parameter
    is refused (RFC 6749 §3.1), here as an error page since nothing is trusted yet."""
    values = params.getlist(name)
    if len(values) > 1:
        raise ClientRefused("repeated_parameter", f"The sign-in request repeats {name}.")
    return values[0] if values else None


def protected_resource_route(server: ServerConfig) -> Route:
    """The PRM route. Placed before the MCP mount, it shadows the SDK's own PRM route."""
    return _well_known(PRM_PATH, protected_resource_metadata(server))


def _well_known(path: str, document: dict[str, Any]) -> Route:
    async def serve(request: Request) -> JSONResponse:
        return JSONResponse(document, headers=_WELL_KNOWN_HEADERS)

    return Route(
        path, endpoint=cors_middleware(serve, _WELL_KNOWN_METHODS), methods=_WELL_KNOWN_METHODS
    )
