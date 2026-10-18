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

``GET /authorize`` (§3.2.3) runs in two halves:

1. Whether the client and redirect can be trusted, through
   :func:`syncshelf.auth.cimd.check_client`. Until they are, ``redirect_uri`` is untrusted,
   so a refusal is an HTML :func:`error_page` and never a redirect.
2. The rest of the request checks (:func:`check_request`), then the Cloudflare Access login
   (:class:`syncshelf.auth.access.AccessVerifier`), then the email's profile. A refusal here
   is a 302 to the now-trusted ``redirect_uri`` with ``error``, ``state`` and ``iss``
   (:func:`redirect_error`), except an email with no profile, which gets a page saying so.
   A request that passes gets the consent page, which carries the checked request as a
   signed blob (:func:`sign_consent`) for ``POST /authorize`` to verify
   (:func:`verify_consent`).

``POST /authorize`` (§3.2.3) is the **Allow** button. A blob that doesn't verify gets an
error page, since nothing in it can be trusted. Past that, the request must come from the
consent page (``Origin`` is the issuer) and from the person it was shown to (a fresh Access
JWT for the signed email), or it is refused with a redirect. It then stores a code bound to
the signed request and redirects with ``code``, ``state`` and ``iss``.

``POST /token`` (§3.2.4) redeems a code for a grant and its tokens
(:func:`syncshelf.auth.tokens.exchange_code`), and rotates a refresh token
(:func:`syncshelf.auth.tokens.refresh_tokens`). It takes form-encoded bodies only, never
fetches a client document (the client is bound on the code or grant), and marks every
response uncacheable. Its errors are :func:`token_error` responses.

:func:`oauth_routes` are the authorization server's own routes, served only when
``server.auth_mode`` is ``oauth``. :func:`protected_resource_route` belongs to the resource
server, which runs in every mode, as the SDK's did. Both documents are plain JSON built from
the configured ``public_url`` string, never through pydantic's ``AnyHttpUrl``, which would
lowercase the host, drop a default port or add a trailing slash: clients compare the issuer
and ``resource`` exactly (RFC 8414, RFC 9207, §3.2.1). Both get the CORS and
``Cache-Control`` the SDK gives its own well-known routes.
"""

import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from importlib.resources import files
from string import Template
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.auth.routes import cors_middleware
from starlette.datastructures import QueryParams
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from syncshelf.auth.access import ACCESS_HEADER, AccessRefused, AccessVerifier
from syncshelf.auth.cimd import (
    CLIENT_LABELS,
    ClientDocuments,
    ClientRefused,
    check_client,
    client_allowed,
)
from syncshelf.auth.tokens import (
    ACCESS_TOKEN_TTL,
    OFFLINE_ACCESS,
    SCOPE,
    CodeExchange,
    ExchangeRefused,
    IssuedTokens,
    RefreshExchange,
    RefreshGrace,
    exchange_code,
    insert_code,
    refresh_tokens,
)
from syncshelf.config import Config, ServerConfig
from syncshelf.db import Database

__all__ = [
    "AUTHORIZE_PATH",
    "CONSENT_FIELD",
    "CONSENT_TTL",
    "METADATA_PATH",
    "OFFLINE_ACCESS",
    "PRM_PATH",
    "SCOPES_SUPPORTED",
    "TOKEN_PATH",
    "AuthorizeError",
    "CheckedRequest",
    "ConsentInvalid",
    "ConsentRequest",
    "access_email",
    "form_params",
    "authorization_server_metadata",
    "canonical_resource",
    "check_request",
    "consent_page",
    "error_page",
    "granted_scopes",
    "redirect_error",
    "token_error",
    "sign_consent",
    "verify_consent",
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

SCOPES_SUPPORTED = (SCOPE, OFFLINE_ACCESS)
"""The scopes both metadata documents advertise, in §3.2.2's order."""

CONSENT_FIELD = "consent"
"""The consent form's hidden field, which holds the signed blob."""

CONSENT_TTL = 600
"""How long a consent page's blob stays valid, in seconds (§3.2.3)."""

_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}")  # a base64url SHA-256, unpadded (RFC 7636)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})  # the redirect policy's (§3.2.3)
_B64URL = re.compile(r"[A-Za-z0-9_-]+")  # unpadded base64url, the consent blob's two parts
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}")  # RFC 7636 §4.1's code_verifier
_FORM = "application/x-www-form-urlencoded"
_GRANT_TYPES = frozenset({"authorization_code", "refresh_token"})  # as the metadata lists
_TOKEN_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}  # every /token answer

_WELL_KNOWN_METHODS = ["GET", "OPTIONS"]
_WELL_KNOWN_HEADERS = {"Cache-Control": "public, max-age=3600"}  # as the SDK's handlers send

_PAGE_HEADERS = {
    # The consent page's hardening (§3.2.3), on every page /authorize renders.
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'; default-src 'self'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
# The consent page alone relaxes to same-origin (#114): under no-referrer the Fetch standard
# sends `Origin: null` on its Allow form POST, which the Origin check would refuse. Same-origin
# still sends no referrer to any other site.
_CONSENT_HEADERS = _PAGE_HEADERS | {"Referrer-Policy": "same-origin"}

_ERROR_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>SyncShelf sign-in</title></head>
<body>
<h1>SyncShelf can't sign you in</h1>
<p>{message}</p>
</body>
</html>
"""

_CONSENT_PAGE = Template(
    files("syncshelf.auth").joinpath("templates", "consent.html").read_text(encoding="utf-8")
)

_START_AGAIN = "This sign-in page has expired or isn't valid. Start connecting again from Claude."

_LOOPBACK_WARNING = (
    "<p><strong>Code will be sent to a program on this computer ({where}).</strong> "
    "Continue only if you just started this from Claude Code.</p>\n"
)

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


def oauth_routes(
    config: Config,
    clients: ClientDocuments,
    access: AccessVerifier,
    grace: RefreshGrace,
    *,
    clock: Callable[[], float] = time.time,
) -> list[Route]:
    """The authorization server's routes, in §2.2 order, for the outer app before the MCP
    mount. Serve them only in ``oauth`` mode, whose keys (``config.server.oauth``) they need:
    dev mode disables them (§3.2.5).

    ``clients`` fetches and caches the client documents ``/authorize`` checks against, and
    ``access`` verifies the Cloudflare Access login. ``grace`` holds each refresh's new pair
    for a retry, and is the one the tools drop revoked grants from. ``clock`` gives epoch
    seconds.
    """
    server = config.server
    if server.oauth is None:
        raise ValueError("the authorization server's routes need oauth mode's config")
    consent_key = server.oauth.consent_hmac_key
    # Emails are lowercased and unique once loaded, so each names at most one profile.
    profiles = {p.email: p.name for p in config.profiles.values() if p.email is not None}

    async def authorize(request: Request) -> Response:
        params = request.query_params
        try:
            trusted = await check_client(
                clients, _single(params, "client_id"), _single(params, "redirect_uri")
            )
        except ClientRefused as refused:
            _log_refused(refused.reason)
            return error_page(refused.message, status_code=refused.status_code)
        # From here redirect_uri is trusted, so a refusal goes back to it.
        state = _one_or_none(params, "state")
        try:
            checked = check_request(params, server)
            email = await access_email(request, access)
        except AuthorizeError as refused:
            _log_refused(refused.reason, trusted.client_label)
            return redirect_error(trusted.redirect_uri, refused.error, state, server.public_url)
        if email not in profiles:
            _log_refused("no_profile", trusted.client_label)
            # Shown only to the person Access just signed in, so it may name their email.
            return error_page(f"There is no SyncShelf profile for {email}.", status_code=403)
        consent = ConsentRequest(
            client_id=trusted.client_id,
            redirect_uri=trusted.redirect_uri,
            code_challenge=checked.code_challenge,
            resource=checked.resource,
            state=state,
            scopes=checked.scopes,
            email=email,
            refresh_allowed=trusted.refresh_allowed,
        )
        return consent_page(consent, sign_consent(consent, consent_key, int(clock())))

    async def approve(request: Request) -> Response:
        params = form_params(request.headers.get("content-type"), await request.body())
        blobs = (params or {}).get(CONSENT_FIELD, [])
        if len(blobs) != 1:
            _log_refused("consent_missing")
            return error_page(_START_AGAIN)
        try:
            consent = verify_consent(blobs[0], consent_key, int(clock()))
        except ConsentInvalid as invalid:
            _log_refused(f"consent_{invalid.reason}")
            return error_page(_START_AGAIN)
        # Signed by this server, so the client and redirect are the ones GET trusted, unless a
        # new release dropped the client from the allowlist since.
        label = CLIENT_LABELS.get(consent.client_id)
        if label is None:
            _log_refused("client_not_allowed")
            return error_page(_START_AGAIN)
        try:
            # CSRF: only the consent page, served from the issuer, may post here (§3.2.3). Its
            # same-origin referrer policy is what lets the browser send a real Origin (#114).
            if request.headers.get("origin") != server.public_url:
                raise AuthorizeError("access_denied", "origin_mismatch")
            email = await access_email(request, access)
            if email != consent.email:
                raise AuthorizeError("access_denied", "email_mismatch")
        except AuthorizeError as refused:
            _log_refused(refused.reason, label)
            return redirect_error(
                consent.redirect_uri, refused.error, consent.state, server.public_url
            )
        profile = profiles.get(email)
        if profile is None:  # GET found it, so only a restart with a new config gets here
            _log_refused("no_profile", label)
            return error_page(f"There is no SyncShelf profile for {email}.", status_code=403)
        # The code row has no column for refresh_allowed, so a client whose document doesn't
        # allow refresh tokens is bound to scopes without offline_access (#115).
        scopes = tuple(
            scope for scope in consent.scopes if consent.refresh_allowed or scope != OFFLINE_ACCESS
        )
        now = int(clock())
        db: Database = request.app.state.db
        code = await db.write(
            lambda conn: insert_code(
                conn,
                client_id=consent.client_id,
                redirect_uri=consent.redirect_uri,
                code_challenge=consent.code_challenge,
                resource=consent.resource,
                scopes=scopes,
                profile=profile,
                email=email,
                now=now,
            )
        )
        log.info(
            "issued an authorization code to %s",
            label,
            extra={"event": "code_issued", "client_label": label, "profile": profile},
        )
        query = {"code": code, **({"state": consent.state} if consent.state is not None else {})}
        return _redirect(consent.redirect_uri, {**query, "iss": server.public_url})

    async def token(request: Request) -> Response:
        params = form_params(request.headers.get("content-type"), await request.body())
        if params is None:
            return _token_refused("invalid_request", "not_form_encoded")
        if any(len(values) > 1 for values in params.values()):
            return _token_refused("invalid_request", "repeated_parameter")
        args = {name: values[0] for name, values in params.items()}
        grant_type = args.get("grant_type")
        if grant_type is None:
            return _token_refused("invalid_request", "grant_type_missing")
        if grant_type not in _GRANT_TYPES:
            return _token_refused("unsupported_grant_type", "grant_type_unsupported")
        # The gate only: /token never fetches a client document (§3.2.4).
        client_id = args.get("client_id")
        if client_id is None or not client_allowed(client_id):
            return _token_refused("invalid_client", "client_not_allowed", status_code=401)
        resource = args.get("resource")
        if resource is not None:
            resource = canonical_resource(resource) or resource
        db: Database = request.app.state.db
        if grant_type == "refresh_token":
            return await refresh(db, args, client_id, resource)
        return await exchange(db, args, client_id, resource)

    async def exchange(
        db: Database, args: dict[str, str], client_id: str, resource: str | None
    ) -> Response:
        code, redirect_uri, verifier = (
            args.get("code"),
            args.get("redirect_uri"),
            args.get("code_verifier"),
        )
        if code is None or redirect_uri is None or verifier is None:
            return _token_refused("invalid_request", "parameter_missing")
        if not _VERIFIER.fullmatch(verifier):
            return _token_refused("invalid_request", "bad_code_verifier")
        exchange = CodeExchange(
            code=code,
            client_id=client_id,
            client_label=CLIENT_LABELS[client_id],
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            resource=resource,
        )
        outcome = await db.write(exchange_code, exchange, int(clock()))
        if isinstance(outcome, ExchangeRefused):
            if outcome.revoked_grant is not None:
                _revoked(outcome.revoked_grant, "code_replay", exchange.client_label, grace)
            return _token_refused(outcome.error, outcome.reason, exchange.client_label)
        log.info(
            "created grant %d for %s",
            outcome.grant_id,
            outcome.client_label,
            extra={
                "event": "grant_created",
                "grant_id": outcome.grant_id,
                "client_label": outcome.client_label,
            },
        )
        return _issued(outcome)

    async def refresh(
        db: Database, args: dict[str, str], client_id: str, resource: str | None
    ) -> Response:
        presented = args.get("refresh_token")
        if presented is None:
            return _token_refused("invalid_request", "parameter_missing")
        label = CLIENT_LABELS[client_id]
        rotation = RefreshExchange(presented, client_id, resource)
        outcome = await db.write(refresh_tokens, rotation, grace, int(clock()))
        if isinstance(outcome, ExchangeRefused):
            if outcome.revoked_grant is not None:
                revoked_label = outcome.revoked_label or label
                _revoked(outcome.revoked_grant, "refresh_reuse", revoked_label, grace)
            return _token_refused(outcome.error, outcome.reason, label)
        event = "refresh_retried" if outcome.retried else "refresh_rotated"
        log.info(
            "%s grant %d's refresh token",
            "re-sent the rotation of" if outcome.retried else "rotated",
            outcome.grant_id,
            extra={
                "event": event,
                "grant_id": outcome.grant_id,
                "client_label": outcome.client_label,
            },
        )
        return _issued(outcome)

    return [
        _well_known(METADATA_PATH, authorization_server_metadata(server)),
        # No CORS: neither Claude's backend nor Claude Code needs it (§3.2.4).
        Route(AUTHORIZE_PATH, endpoint=authorize, methods=["GET"]),
        Route(AUTHORIZE_PATH, endpoint=approve, methods=["POST"]),
        Route(TOKEN_PATH, endpoint=token, methods=["POST"]),
    ]


class AuthorizeError(Exception):
    """A trusted ``/authorize`` request is refused. ``error`` is the OAuth error code sent
    back to the client (RFC 6749 §4.1.2.1, RFC 8707 §2; the spec names only
    ``invalid_target``, and #110 asks it to confirm the rest); ``reason`` is a snake_case code
    for the log. Neither holds anything from the request."""

    def __init__(self, error: str, reason: str) -> None:
        super().__init__(reason)
        self.error = error
        self.reason = reason


@dataclass(frozen=True)
class CheckedRequest:
    """What :func:`check_request` takes from an authorization request."""

    code_challenge: str
    resource: str
    """The canonical form, which equals ``MCP_URL``'s."""
    scopes: tuple[str, ...]
    """The scopes to grant: :func:`granted_scopes`."""


def check_request(params: QueryParams, server: ServerConfig) -> CheckedRequest:
    """The §3.2.3 request hygiene, past the client and redirect, or :class:`AuthorizeError`.

    No parameter may be repeated; ``response_type`` must be ``code``;
    ``code_challenge_method`` must be exactly ``S256`` (never defaulted to ``plain``) with a
    43-character base64url ``code_challenge``; and ``resource`` is required and, canonicalised,
    must equal ``MCP_URL``'s canonical form (else ``invalid_target``). ``state`` isn't
    checked: it is echoed as sent.
    """
    if any(len(params.getlist(name)) > 1 for name in params):
        raise AuthorizeError("invalid_request", "repeated_parameter")
    response_type = params.get("response_type")
    if response_type is None:
        raise AuthorizeError("invalid_request", "response_type_missing")
    if response_type != "code":
        raise AuthorizeError("unsupported_response_type", "response_type_not_code")
    if params.get("code_challenge_method") != "S256":
        raise AuthorizeError("invalid_request", "challenge_method_not_s256")
    code_challenge = params.get("code_challenge")
    if code_challenge is None or not _CHALLENGE.fullmatch(code_challenge):
        raise AuthorizeError("invalid_request", "bad_code_challenge")
    resource = params.get("resource")
    expected = canonical_resource(server.mcp_url)
    if resource is None or expected is None or canonical_resource(resource) != expected:
        raise AuthorizeError("invalid_target", "resource_mismatch")
    return CheckedRequest(code_challenge, expected, granted_scopes(params.get("scope")))


def canonical_resource(resource: str) -> str | None:
    """``resource`` with its scheme and host lowercased and one trailing slash stripped
    (§3.2.3), or ``None`` when it isn't an absolute URL. Compare it with ``MCP_URL``'s
    canonical form."""
    try:
        parts = urlsplit(resource)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    path = parts.path.removesuffix("/")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, parts.query, parts.fragment)
    )


def granted_scopes(scope: str | None) -> tuple[str, ...]:
    """The scopes a grant carries for a requested ``scope``: always ``syncshelf``, plus
    ``offline_access`` when asked for. Unknown scopes are ignored, not failed (§3.2.2)."""
    requested = (scope or "").split()
    return (SCOPE, OFFLINE_ACCESS) if OFFLINE_ACCESS in requested else (SCOPE,)


def redirect_error(redirect_uri: str, error: str, state: str | None, issuer: str) -> Response:
    """A 302 to the trusted ``redirect_uri`` carrying ``error``, ``state`` (when the request
    sent exactly one) and ``iss`` (RFC 9207), for a refusal once the redirect is trusted."""
    query = {"error": error, **({"state": state} if state is not None else {}), "iss": issuer}
    return _redirect(redirect_uri, query)


def _redirect(redirect_uri: str, query: dict[str, str]) -> Response:
    separator = "&" if urlsplit(redirect_uri).query else "?"
    return RedirectResponse(
        redirect_uri + separator + urlencode(query),
        status_code=302,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def form_params(content_type: str | None, body: bytes) -> dict[str, list[str]] | None:
    """A form-encoded body's parameters, each name with every value it was sent with, or
    ``None`` when the body isn't one: another content type (JSON or multipart, say) or text
    that isn't UTF-8."""
    if (content_type or "").partition(";")[0].strip().lower() != _FORM:
        return None
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, errors="strict")
    except UnicodeDecodeError:
        return None
    params: dict[str, list[str]] = {}
    for name, value in pairs:
        params.setdefault(name, []).append(value)
    return params


def token_error(error: str, *, status_code: int = 400) -> JSONResponse:
    """A ``/token`` error response (RFC 6749 §5.2): ``{"error": error}``, uncacheable like
    every ``/token`` response. Only the code is sent, so nothing from the request is echoed."""
    return JSONResponse({"error": error}, status_code=status_code, headers=_TOKEN_HEADERS)


def _issued(issued: IssuedTokens) -> JSONResponse:
    """The success response for tokens a code exchange or refresh issued (§3.2.4)."""
    body: dict[str, Any] = {
        "access_token": issued.access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL,
    }
    if issued.refresh_token is not None:
        body["refresh_token"] = issued.refresh_token
    body["scope"] = " ".join(issued.scopes)
    return JSONResponse(body, headers=_TOKEN_HEADERS)


def _revoked(grant_id: int, reason: str, client_label: str, grace: RefreshGrace) -> None:
    """Log a grant the token endpoint revoked, and forget its held refresh pairs."""
    grace.drop_grants({grant_id})
    log.warning(
        "revoked grant %d: %s",
        grant_id,
        reason,
        extra={
            "event": "grant_revoked",
            "grant_id": grant_id,
            "client_label": client_label,
            "reason": reason,
        },
    )


def _token_refused(
    error: str, reason: str, client_label: str | None = None, *, status_code: int = 400
) -> JSONResponse:
    log.info(
        "refused a token request: %s",
        reason,
        extra={"event": "token_request_refused", "reason": reason, "client_label": client_label},
    )
    return token_error(error, status_code=status_code)


async def access_email(request: Request, access: AccessVerifier) -> str:
    """The signed-in email from ``request``'s Access JWT, the only header trusted for it, or
    :class:`AuthorizeError`: ``access_denied`` for a missing or bad JWT, and
    ``temporarily_unavailable`` when the signing keys can't be fetched."""
    try:
        return await access.verify(request.headers.get(ACCESS_HEADER))
    except AccessRefused as refused:
        error = "temporarily_unavailable" if refused.unavailable else "access_denied"
        raise AuthorizeError(error, refused.reason) from None


def _log_refused(reason: str, client_label: str | None = None) -> None:
    log.info(
        "refused an authorization request: %s",
        reason,
        extra={"event": "authorize_refused", "reason": reason, "client_label": client_label},
    )


def consent_page(consent: ConsentRequest, blob: str) -> HTMLResponse:
    """The consent page (§3.2.3): "Connect <client_id host> to SyncShelf as <email>?" with one
    **Allow** button that posts ``blob`` back in :data:`CONSENT_FIELD`. It names the
    ``client_id`` host, never the document's ``client_name``, which the client chose. A
    loopback redirect adds a warning naming the address the code goes to. Every echoed value
    is escaped, and the page can't be framed or cached. Its referrer policy is
    ``same-origin``, not ``no-referrer``, so its Allow POST carries the real ``Origin``
    (#114), and no referrer reaches any other site."""
    warning = ""
    redirect = urlsplit(consent.redirect_uri)
    if redirect.hostname in _LOOPBACK_HOSTS:
        where = redirect.netloc
        warning = _LOOPBACK_WARNING.format(where=html.escape(where))
    body = _CONSENT_PAGE.substitute(
        client_host=html.escape(urlsplit(consent.client_id).hostname or ""),
        email=html.escape(consent.email),
        warning=warning,
        action=html.escape(AUTHORIZE_PATH),
        field=html.escape(CONSENT_FIELD),
        consent=html.escape(blob),
    )
    return HTMLResponse(body, headers=_CONSENT_HEADERS)


# --- the consent blob -------------------------------------------------------------------


@dataclass(frozen=True)
class ConsentRequest:
    """The checked authorization request a consent page carries, signed, to the POST
    (§3.2.3). What the code is bound to comes from here, never from the POST's own fields."""

    client_id: str
    redirect_uri: str
    """Exactly as the request gave it: the code is bound to this string (§3.2.4)."""
    code_challenge: str
    resource: str
    """Canonical: equal to ``MCP_URL``'s canonical form."""
    state: str | None
    """Echoed verbatim on the redirect, or ``None`` when the request sent none."""
    scopes: tuple[str, ...]
    """The scopes to grant: ``syncshelf``, plus ``offline_access`` when requested."""
    email: str
    """The lowercased Access email; the POST's own Access email must equal it."""
    refresh_allowed: bool
    """Whether the client's document allows refresh tokens. Issue one only when this holds
    *and* ``offline_access`` is in :attr:`scopes`."""


class ConsentInvalid(Exception):
    """A consent blob is forged, altered, malformed or expired. ``reason`` is a snake_case
    code for the log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sign_consent(consent: ConsentRequest, key: str, now: int) -> str:
    """``consent`` as a blob for the form's hidden field: base64url JSON, a ``.``, and its
    base64url HMAC-SHA256 under ``key`` (``server.consent_hmac_key``). It expires
    :data:`CONSENT_TTL` seconds after ``now``. It is signed, not encrypted: everything in it
    is already the person's own request."""
    payload = {**asdict(consent), "exp": now + CONSENT_TTL}
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return body + "." + _b64encode(_mac(key, body))


def verify_consent(blob: str, key: str, now: int) -> ConsentRequest:
    """The :class:`ConsentRequest` in a blob from :func:`sign_consent`, or
    :class:`ConsentInvalid` when its signature doesn't verify under ``key``, or it has
    expired at ``now``. The signature is checked before anything in the blob is read."""
    body, _, signature = blob.partition(".")
    if not _B64URL.fullmatch(body) or not _B64URL.fullmatch(signature):
        raise ConsentInvalid("malformed")
    try:
        given = _b64decode(signature)
    except ValueError:
        raise ConsentInvalid("malformed") from None
    if not hmac.compare_digest(given, _mac(key, body)):
        raise ConsentInvalid("bad_signature")
    try:
        payload = json.loads(_b64decode(body))
        consent, expires_at = _consent_from(payload)
    except ValueError, TypeError, KeyError:
        # Signed with this key, so only a change to this format gets here.
        raise ConsentInvalid("malformed") from None
    if now >= expires_at:
        raise ConsentInvalid("expired")
    return consent


def _consent_from(payload: Any) -> tuple[ConsentRequest, int]:
    strings = ("client_id", "redirect_uri", "code_challenge", "resource", "email")
    if not isinstance(payload, dict) or not all(isinstance(payload[f], str) for f in strings):
        raise ValueError
    state, scopes, refresh, exp = (
        payload["state"],
        payload["scopes"],
        payload["refresh_allowed"],
        payload["exp"],
    )
    if (
        not (state is None or isinstance(state, str))
        or not isinstance(scopes, list)
        or not all(isinstance(scope, str) for scope in scopes)
        or not isinstance(refresh, bool)
        or not isinstance(exp, int)
        or isinstance(exp, bool)
    ):
        raise ValueError
    fields: Mapping[str, str] = {f: payload[f] for f in strings}
    consent = ConsentRequest(
        client_id=fields["client_id"],
        redirect_uri=fields["redirect_uri"],
        code_challenge=fields["code_challenge"],
        resource=fields["resource"],
        state=state,
        scopes=tuple(scopes),
        email=fields["email"],
        refresh_allowed=refresh,
    )
    return consent, exp


def _mac(key: str, body: str) -> bytes:
    return hmac.new(key.encode(), body.encode("ascii"), hashlib.sha256).digest()


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    """Decode unpadded base64url that :data:`_B64URL` already matched."""
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error as exc:  # e.g. a length no encoding produces
        raise ValueError("not base64url") from exc


def error_page(message: str, *, status_code: int = 400) -> HTMLResponse:
    """An HTML page showing ``message``, escaped, for an ``/authorize`` error that must not
    be redirected: one before the client and redirect are trusted (§3.2.3), or one with
    nowhere safe to send it. It can't be framed or cached, and, unlike the consent page, it
    sends no referrer at all, since it posts nothing.
    """
    return HTMLResponse(
        _ERROR_PAGE.format(message=html.escape(message)),
        status_code=status_code,
        headers=_PAGE_HEADERS,
    )


def _one_or_none(params: QueryParams, name: str) -> str | None:
    """Parameter ``name``'s value when the request sent it exactly once, else ``None``."""
    values = params.getlist(name)
    return values[0] if len(values) == 1 else None


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
