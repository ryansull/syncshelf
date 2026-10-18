"""SyncShelf as the OAuth authorization server (SPEC §3.2): its routes on the outer app.

Only discovery is built so far (§3.2.1, §3.2.2):

- ``GET /.well-known/oauth-authorization-server`` serves the authorization-server metadata,
  built from ``server.public_url``. It offers CIMD, not dynamic client registration, so there
  is no ``registration_endpoint`` and no ``/register`` route.
- ``GET /.well-known/oauth-protected-resource/mcp`` serves the protected-resource metadata
  (PRM). The SDK serves a PRM on the MCP mount too, but advertises as ``scopes_supported``
  only the scopes it requires of every token, and requiring ``offline_access`` would refuse
  every connection that didn't ask for a refresh token. So this route, on the outer app before
  the mount, shadows the SDK's with one that advertises both scopes: Claude asks for
  ``offline_access``, and so gets a refresh token, only when it is advertised.

:func:`oauth_routes` are the authorization server's own routes, served only when
``server.auth_mode`` is ``oauth``. :func:`protected_resource_route` belongs to the resource
server, which runs in every mode, as the SDK's did. Both documents are plain JSON built from
the configured ``public_url`` string, never through pydantic's ``AnyHttpUrl``, which would
lowercase the host, drop a default port or add a trailing slash: clients compare the issuer
and ``resource`` exactly (RFC 8414, RFC 9207, §3.2.1). Both get the CORS and
``Cache-Control`` the SDK gives its own well-known routes.
"""

from typing import Any

from mcp.server.auth.routes import cors_middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

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


def oauth_routes(server: ServerConfig) -> list[Route]:
    """The authorization server's routes, in §2.2 order, for the outer app before the MCP
    mount. Serve them only in ``oauth`` mode: dev mode disables them (§3.2.5)."""
    return [_well_known(METADATA_PATH, authorization_server_metadata(server))]


def protected_resource_route(server: ServerConfig) -> Route:
    """The PRM route. Placed before the MCP mount, it shadows the SDK's own PRM route."""
    return _well_known(PRM_PATH, protected_resource_metadata(server))


def _well_known(path: str, document: dict[str, Any]) -> Route:
    async def serve(request: Request) -> JSONResponse:
        return JSONResponse(document, headers=_WELL_KNOWN_HEADERS)

    return Route(
        path, endpoint=cors_middleware(serve, _WELL_KNOWN_METHODS), methods=_WELL_KNOWN_METHODS
    )
