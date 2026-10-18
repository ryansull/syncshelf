"""Which clients may sign in, and where their codes may go (SPEC §3.2.1, §3.2.3, §3.2.6).

SyncShelf accepts exactly two clients, both identified by a Client ID Metadata Document
(CIMD) URL on ``claude.ai``. ``GET /authorize`` trusts a request's client and redirect only
after three steps, in this order:

1. **The client gate** (:func:`client_allowed`) compares the ``client_id`` to the two
   allowlisted URLs, with ``urlsplit`` rules as defence in depth. It runs before any network
   I/O, so the only host ever fetched is ``claude.ai``.
2. **The fetch** (:class:`ClientDocuments`) gets the client's document under the §3.2.6
   limits, checks it, and caches only valid ones in ``oauth_client_cache``. A last-good copy is
   served stale when a fetch fails, so a claude.ai outage or a bot challenge doesn't block
   sign-in.
3. **Redirect matching** (:func:`redirect_allowed`) requires the ``redirect_uri`` to be in the
   document *and* in SyncShelf's own policy set.

:func:`check_client` runs all three and returns a :class:`TrustedClient`, or raises
:class:`ClientRefused`. Until it returns, ``redirect_uri`` is untrusted, so a refusal is shown
as an error page and never redirected to.
"""

import asyncio
import json
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx2

from syncshelf.db import Database

__all__ = [
    "CLAUDE_APPS",
    "CLAUDE_CODE",
    "CLIENT_LABELS",
    "FETCH_TIMEOUT",
    "FRESH_MAX",
    "FRESH_MIN",
    "HOSTED_CALLBACK",
    "MAX_DOCUMENT_BYTES",
    "STALE_WINDOW",
    "CachedDocument",
    "ClientDocument",
    "ClientDocuments",
    "ClientRefused",
    "DocumentError",
    "TrustedClient",
    "check_client",
    "client_allowed",
    "freshness",
    "load_cached",
    "parse_document",
    "redirect_allowed",
    "save_document",
]

CLAUDE_APPS = "https://claude.ai/oauth/mcp-oauth-client-metadata"
"""The ``client_id`` of the Claude apps: web, Desktop, mobile and Cowork (§3.2.1)."""

CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
"""The ``client_id`` of Claude Code (§3.2.1)."""

CLIENT_LABELS: Mapping[str, str] = {CLAUDE_APPS: "claude.ai", CLAUDE_CODE: "Claude Code"}
"""The allowlist: each accepted ``client_id`` and its ``client_label``, by URL (§3.2.1)."""

HOSTED_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
"""The Claude apps' one redirect, and the only non-loopback redirect the policy allows."""

MAX_DOCUMENT_BYTES = 5120
"""A document body past this many bytes is refused, and the download aborted (§3.2.6)."""

FRESH_MIN = 5 * 60
"""The shortest a fetched document is fresh for, whatever its ``max-age`` (§3.2.6)."""

FRESH_MAX = 24 * 3600
"""The longest a fetched document is fresh for, whatever its ``max-age`` (§3.2.6)."""

STALE_WINDOW = 7 * 86400
"""How long past ``fresh_until`` the last-good copy is still served when a fetch fails."""

FETCH_TIMEOUT = httpx2.Timeout(5.0, connect=3.0)
"""The fetch's timeouts (§3.2.6)."""

_MAX_ID_LENGTH = 255
_LOOPBACK_REGISTERED = frozenset({"127.0.0.1", "localhost", "[::1]"})
_PORT = r"([1-9][0-9]{0,4})"  # no leading zero, so each port has one spelling
_LOOPBACK_POLICY = re.compile(rf"http://(?:127\.0\.0\.1|localhost):{_PORT}/callback")

log = logging.getLogger(__name__)


def client_allowed(client_id: str) -> bool:
    """Whether ``client_id`` passes the client gate (§3.2.3). No I/O.

    It must be one of :data:`CLIENT_LABELS` by exact string. The ``urlsplit`` rules can't
    widen that; they are there so a mistake in the allowlist can't let in a URL that would
    send the fetch anywhere but ``claude.ai``.
    """
    if len(client_id) > _MAX_ID_LENGTH or client_id not in CLIENT_LABELS:
        return False
    parts = urlsplit(client_id)
    segments = parts.path.split("/")
    return (
        parts.scheme == "https"
        and parts.netloc == "claude.ai"  # so no userinfo and no explicit port
        and parts.hostname == "claude.ai"
        and not parts.query
        and not parts.fragment
        and "?" not in client_id
        and "#" not in client_id
        and parts.path not in ("", "/")
        and "." not in segments
        and ".." not in segments
    )


@dataclass(frozen=True)
class ClientDocument:
    """A client's metadata document, already checked by :func:`parse_document`."""

    client_id: str
    redirect_uris: tuple[str, ...]
    refresh_allowed: bool
    """Whether its ``grant_types`` include ``refresh_token``. Only then may the client be
    issued refresh tokens (§3.2.3)."""


class DocumentError(Exception):
    """A document couldn't be fetched or isn't valid. ``reason`` is a short snake_case code
    for the log; it never holds anything from the response."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_document(client_id: str, body: str | bytes) -> ClientDocument:
    """Check a document fetched from ``client_id`` per §3.2.3, or raise
    :class:`DocumentError`.

    It must be a JSON object whose ``client_id`` is the URL exactly, with a non-empty list of
    string ``redirect_uris``, ``token_endpoint_auth_method`` absent or ``"none"``, no
    ``client_secret``, and a list of ``grant_types`` that includes ``authorization_code``.
    Other fields (``client_name``, extra grant types such as ``jwt-bearer``) are ignored.
    """
    try:
        doc = json.loads(body)
    except ValueError, RecursionError:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise DocumentError("not_json") from None
    if not isinstance(doc, dict):
        raise DocumentError("not_an_object")
    if doc.get("client_id") != client_id:
        raise DocumentError("client_id_mismatch")
    redirect_uris = doc.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or not all(isinstance(uri, str) for uri in redirect_uris)
    ):
        raise DocumentError("bad_redirect_uris")
    if doc.get("token_endpoint_auth_method", "none") != "none" or "client_secret" in doc:
        raise DocumentError("not_a_public_client")
    grant_types = doc.get("grant_types")
    if not isinstance(grant_types, list) or "authorization_code" not in grant_types:
        raise DocumentError("no_authorization_code_grant")
    return ClientDocument(client_id, tuple(redirect_uris), "refresh_token" in grant_types)


def redirect_allowed(document: ClientDocument, redirect_uri: str) -> bool:
    """Whether a code for ``document``'s client may be sent to ``redirect_uri`` (§3.2.3).

    It must pass both checks:

    1. **membership** in the document's ``redirect_uris`` by exact string, except that a
       registered ``http`` URI on ``127.0.0.1``, ``localhost`` or ``[::1]`` with no port
       matches a request that differs from it only by a port from 1 to 65535;
    2. **SyncShelf's policy**: :data:`HOSTED_CALLBACK` exactly, or
       ``http://127.0.0.1:<port>/callback`` or ``http://localhost:<port>/callback``.

    The policy is a whole-string match, which is what refuses userinfo, ``https`` loopback,
    look-alike hosts, a trailing slash, extra path or query, and percent-encoded or
    dot-segment paths, whatever a document registers.
    """
    return _in_policy(redirect_uri) and any(
        _registered_match(registered, redirect_uri) for registered in document.redirect_uris
    )


def _in_policy(redirect_uri: str) -> bool:
    if redirect_uri == HOSTED_CALLBACK:
        return True
    match = _LOOPBACK_POLICY.fullmatch(redirect_uri)
    return match is not None and int(match[1]) <= 65535


def _registered_match(registered: str, requested: str) -> bool:
    if requested == registered:
        return True
    try:
        parts = urlsplit(registered)
    except ValueError:  # e.g. an unclosed IPv6 bracket in the document
        return False
    # netloc is the bare host only when there is no userinfo and no port.
    if parts.scheme != "http" or parts.netloc not in _LOOPBACK_REGISTERED:
        return False
    origin = f"http://{parts.netloc}"
    if not registered.startswith(origin):  # e.g. an uppercase scheme
        return False
    rest = registered[len(origin) :]  # the path and query, which must be identical
    match = re.fullmatch(re.escape(origin + ":") + _PORT + re.escape(rest), requested)
    return match is not None and int(match[1]) <= 65535


def freshness(cache_control: str | None) -> int:
    """How long a fetched document stays fresh, in seconds: its ``Cache-Control: max-age``,
    clamped to :data:`FRESH_MIN` – :data:`FRESH_MAX` (§3.2.6). With no usable ``max-age``,
    the minimum."""
    max_age = 0
    for directive in (cache_control or "").split(","):
        name, _, value = directive.strip().partition("=")
        if name.strip().lower() == "max-age":
            try:
                max_age = int(value.strip().strip('"'))
            except ValueError:
                max_age = 0
            break
    return min(max(max_age, FRESH_MIN), FRESH_MAX)


@dataclass(frozen=True)
class CachedDocument:
    """A last-good document from ``oauth_client_cache``."""

    document: ClientDocument
    fetched_at: int
    fresh_until: int


def load_cached(conn: sqlite3.Connection, client_id: str) -> CachedDocument | None:
    """The cached document for ``client_id``, or ``None``. A row that no longer passes
    :func:`parse_document` counts as no row."""
    row = conn.execute(
        "SELECT document_json, fetched_at, fresh_until FROM oauth_client_cache WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        document = parse_document(client_id, row[0])
    except DocumentError:
        return None
    return CachedDocument(document, row[1], row[2])


def save_document(
    conn: sqlite3.Connection,
    client_id: str,
    document_json: str,
    fetched_at: int,
    fresh_until: int,
    etag: str | None,
) -> None:
    """Store a valid document as ``client_id``'s last-good copy, replacing any older one.
    Only ever pass a body that :func:`parse_document` accepted: errors are never cached."""
    conn.execute(
        "INSERT INTO oauth_client_cache (client_id, document_json, fetched_at, fresh_until, "
        "etag) VALUES (?, ?, ?, ?, ?) ON CONFLICT (client_id) DO UPDATE SET "
        "document_json = excluded.document_json, fetched_at = excluded.fetched_at, "
        "fresh_until = excluded.fresh_until, etag = excluded.etag",
        (client_id, document_json, fetched_at, fresh_until, etag),
    )


@dataclass(frozen=True)
class _Fetched:
    text: str
    document: ClientDocument
    fresh_for: int
    etag: str | None


class ClientDocuments:
    """Fetches and caches the allowlisted clients' documents (§3.2.6).

    :meth:`get` serves a fresh cached copy without any I/O. Otherwise it fetches, at most one
    fetch at a time per URL, and stores the result only if it is valid. When the fetch fails
    it serves the last-good copy for up to :data:`STALE_WINDOW` past its freshness. Requests
    that queued behind a failed fetch share its outcome instead of each waiting out their own
    timeout; failures are never kept beyond that.

    The app builds this before its lifespan, and the lifespan opens it with :meth:`running`,
    which binds the database and the HTTP client. ``clock`` gives epoch seconds, as
    ``create_app``'s does. ``transport`` replaces the network, for tests (§2.9).
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._clock = clock
        self._transport = transport
        self._db: Database | None = None
        self._http: httpx2.AsyncClient | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._finished: dict[str, int] = {}  # fetches finished, per client_id
        self._failures: dict[str, DocumentError] = {}  # the last fetch's failure, if it failed

    @asynccontextmanager
    async def running(self, db: Database) -> AsyncIterator[None]:
        """Serve documents from ``db`` and the network until the block exits."""
        async with httpx2.AsyncClient(
            follow_redirects=False,  # the CIMD draft says MUST NOT follow redirects
            timeout=FETCH_TIMEOUT,
            trust_env=False,  # so a stray HTTPS_PROXY can't reroute the fetch
            transport=self._transport,
        ) as http:
            self._db, self._http = db, http
            try:
                yield
            finally:
                self._db, self._http = None, None

    async def get(self, client_id: str) -> ClientDocument:
        """The checked document for ``client_id``, or :class:`DocumentError` when there is
        none to serve. A ``client_id`` that fails the client gate is refused before any
        I/O, whoever calls this."""
        if not client_allowed(client_id):
            raise DocumentError("client_not_allowed")
        db = self._db
        if db is None:
            raise DocumentError("not_running")
        seen = self._finished.get(client_id, 0)
        cached = await self._load(db, client_id)
        if cached is not None and int(self._clock()) < cached.fresh_until:
            return cached.document
        async with self._locks.setdefault(client_id, asyncio.Lock()):
            # Another request may have fetched while this one waited.
            cached = await self._load(db, client_id)
            now = int(self._clock())
            if cached is not None and now < cached.fresh_until:
                return cached.document
            failure = self._failures.get(client_id)
            if failure is not None and self._finished.get(client_id, 0) != seen:
                # A fetch finished, and failed, while this request waited. Share its outcome
                # rather than queue another full timeout behind it. It is never kept past
                # that: a request arriving after it fetches again.
                return self._stale_or_raise(client_id, cached, now, failure)
            try:
                fetched = await self._fetch(client_id)
            except DocumentError as exc:
                self._failures[client_id] = exc
                return self._stale_or_raise(client_id, cached, now, exc)
            else:
                self._failures.pop(client_id, None)
            finally:
                self._finished[client_id] = self._finished.get(client_id, 0) + 1
            try:
                await db.write(
                    save_document,
                    client_id,
                    fetched.text,
                    now,
                    now + fetched.fresh_for,
                    fetched.etag,
                )
            except sqlite3.Error, RuntimeError:
                # The document is valid; failing to cache it must not fail the sign-in.
                label = CLIENT_LABELS[client_id]
                log.warning(
                    "could not cache the %s client document",
                    label,
                    exc_info=True,
                    extra={"event": "cimd_cache_failed", "client_label": label},
                )
            return fetched.document

    async def _load(self, db: Database, client_id: str) -> CachedDocument | None:
        try:
            return await db.read(load_cached, client_id)
        except sqlite3.Error, RuntimeError:
            # An unreadable cache must not fail the sign-in: the fetch may still work.
            label = CLIENT_LABELS[client_id]
            log.warning(
                "could not read the cached %s client document",
                label,
                exc_info=True,
                extra={"event": "cimd_cache_failed", "client_label": label},
            )
            return None

    def _stale_or_raise(
        self, client_id: str, cached: CachedDocument | None, now: int, exc: DocumentError
    ) -> ClientDocument:
        label = CLIENT_LABELS[client_id]
        stale = cached is not None and now < cached.fresh_until + STALE_WINDOW
        log.warning(
            "could not fetch the %s client document (%s); %s",
            label,
            exc.reason,
            "serving the last-good copy" if stale else "no usable copy is cached",
            extra={
                "event": "fetch_failed",
                "service": "cimd",
                "client_label": label,
                "reason": exc.reason,
                "stale_served": stale,
            },
        )
        if cached is None or not stale:
            raise exc
        return cached.document

    async def _fetch(self, client_id: str) -> _Fetched:
        http = self._http
        if http is None:
            raise DocumentError("not_running")
        started = time.perf_counter()
        try:
            async with http.stream(
                "GET",
                client_id,
                # identity, so the 5 KB cap counts what is held in memory, not what a
                # compressed body would inflate to.
                headers={"accept": "application/json", "accept-encoding": "identity"},
            ) as response:
                log.debug(
                    "GET cimd %s %d",
                    urlsplit(client_id).path,
                    response.status_code,
                    extra={
                        "event": "http_call",
                        "service": "cimd",
                        "method": "GET",
                        "path": urlsplit(client_id).path,
                        "status": response.status_code,
                        "ms": round((time.perf_counter() - started) * 1000),
                    },
                )
                if response.status_code != 200:
                    raise DocumentError(f"status_{response.status_code}")
                media_type = response.headers.get("content-type", "").partition(";")[0]
                if media_type.strip().lower() != "application/json":
                    raise DocumentError("not_application_json")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise DocumentError("encoded_body")
                body = bytearray()
                async for (
                    chunk
                ) in response.aiter_bytes():  # no encoding, so these are the raw bytes
                    body += chunk
                    if len(body) > MAX_DOCUMENT_BYTES:
                        raise DocumentError("too_large")
                cache_control = response.headers.get("cache-control")
                etag = response.headers.get("etag")
        except httpx2.HTTPError as exc:
            raise DocumentError(f"transport_{type(exc).__name__}") from None
        # Decoded once, strictly, so the text checked is exactly the text cached.
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            raise DocumentError("not_utf8") from None
        document = parse_document(client_id, text)
        return _Fetched(text, document, freshness(cache_control), etag)


@dataclass(frozen=True)
class TrustedClient:
    """A client and redirect ``/authorize`` may act on: the result of :func:`check_client`.

    From here on ``redirect_uri`` is trusted, so later errors go to it as a redirect.
    """

    client_id: str
    client_label: str
    """``claude.ai`` or ``Claude Code`` (§3.2.1), for the grant."""
    redirect_uri: str
    """Exactly as the request gave it: the code is bound to this string (§3.2.4)."""
    refresh_allowed: bool
    """Whether this client may be issued refresh tokens (§3.2.3)."""


class ClientRefused(Exception):
    """The client or redirect can't be trusted. ``message`` is for the error page and holds
    nothing from the request; ``reason`` is a snake_case code for the log; ``status_code``
    is the page's HTTP status."""

    def __init__(self, reason: str, message: str, status_code: int = 400) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.status_code = status_code


async def check_client(
    clients: ClientDocuments, client_id: str | None, redirect_uri: str | None
) -> TrustedClient:
    """Run the client gate, the fetch and redirect matching, in that order (§3.2.3), or
    raise :class:`ClientRefused`. Nothing is fetched unless the gate passes."""
    if client_id is None or not client_allowed(client_id):
        raise ClientRefused(
            "client_not_allowed",
            "This app can't sign in to SyncShelf. Only Claude (the Claude apps and "
            "Claude Code) can connect.",
        )
    if redirect_uri is None:
        raise ClientRefused(
            "redirect_uri_missing", "The sign-in request is missing its redirect_uri."
        )
    try:
        document = await clients.get(client_id)
    except DocumentError as exc:
        raise ClientRefused(
            f"document_{exc.reason}",
            "SyncShelf couldn't get Claude's sign-in details from claude.ai. "
            "Try connecting again in a few minutes.",
            status_code=502,
        ) from None
    if not redirect_allowed(document, redirect_uri):
        raise ClientRefused(
            "redirect_uri_not_allowed",
            "The sign-in request's redirect_uri isn't one this app may use.",
        )
    return TrustedClient(
        client_id, CLIENT_LABELS[client_id], redirect_uri, document.refresh_allowed
    )
