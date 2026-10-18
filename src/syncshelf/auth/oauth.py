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
server, which runs in every mode. Both well-known documents are served the SDK's way, with
its models, CORS and ``Cache-Control``, so a browser-based client can read them.
"""

from urllib.parse import urlsplit

from mcp.server.auth.handlers.metadata import MetadataHandler, ProtectedResourceMetadataHandler
from mcp.server.auth.routes import build_resource_metadata_url, cors_middleware
from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata
from pydantic import AnyHttpUrl
from starlette.routing import Route

from syncshelf.auth.tokens import SCOPE
from syncshelf.config import ServerConfig

__all__ = [
    "AUTHORIZE_PATH",
    "METADATA_PATH",
    "OFFLINE_ACCESS",
    "SCOPES_SUPPORTED",
    "TOKEN_PATH",
    "authorization_server_metadata",
    "oauth_routes",
    "protected_resource_metadata",
    "protected_resource_route",
]

METADATA_PATH = "/.well-known/oauth-authorization-server"
"""Where the authorization-server metadata is served (RFC 8414, §3.2.1)."""

AUTHORIZE_PATH = "/authorize"
"""The authorization endpoint (§3.2.3)."""

TOKEN_PATH = "/token"
"""The token endpoint (§3.2.4)."""

OFFLINE_ACCESS = "offline_access"
"""The scope a client asks for to get a refresh token (§3.2.2)."""

SCOPES_SUPPORTED = (SCOPE, OFFLINE_ACCESS)
"""The scopes both metadata documents advertise, in §3.2.2's order."""

_WELL_KNOWN_METHODS = ["GET", "OPTIONS"]


def authorization_server_metadata(server: ServerConfig) -> OAuthMetadata:
    """The §3.2.2 document for ``server.public_url``, which is the issuer.

    Validated from strings, as in :func:`syncshelf.app.create_app`, so the issuer keeps its
    exact form: ``AnyHttpUrl()`` alone would add a trailing slash, and clients compare the
    issuer exactly (RFC 8414, RFC 9207).
    """
    issuer = server.public_url
    return OAuthMetadata.model_validate(
        {
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
    )


def protected_resource_metadata(server: ServerConfig) -> ProtectedResourceMetadata:
    """The PRM for ``/mcp``: its ``resource`` is exactly the MCP URL, its one authorization
    server is the issuer, and it advertises :data:`SCOPES_SUPPORTED` (§3.2.1, §3.2.2)."""
    return ProtectedResourceMetadata.model_validate(
        {
            "resource": server.mcp_url,
            "authorization_servers": [server.public_url],
            "scopes_supported": list(SCOPES_SUPPORTED),
        }
    )


def oauth_routes(server: ServerConfig) -> list[Route]:
    """The authorization server's routes, in §2.2 order, for the outer app before the MCP
    mount. Serve them only in ``oauth`` mode: dev mode disables them (§3.2.5)."""
    metadata = MetadataHandler(authorization_server_metadata(server))
    return [
        Route(
            METADATA_PATH,
            endpoint=cors_middleware(metadata.handle, _WELL_KNOWN_METHODS),
            methods=_WELL_KNOWN_METHODS,
        ),
    ]


def protected_resource_route(server: ServerConfig) -> Route:
    """The PRM route, at the path the SDK names in its 401 challenge, so it shadows the SDK's
    own PRM route when placed before the MCP mount."""
    handler = ProtectedResourceMetadataHandler(protected_resource_metadata(server))
    path = urlsplit(str(build_resource_metadata_url(AnyHttpUrl(server.mcp_url)))).path
    return Route(
        path,
        endpoint=cors_middleware(handler.handle, _WELL_KNOWN_METHODS),
        methods=_WELL_KNOWN_METHODS,
    )
