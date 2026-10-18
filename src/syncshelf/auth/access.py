"""Cloudflare Access JWT verification for ``/authorize`` (SPEC §3.2.3).

Cloudflare Access guards ``/authorize``, GET and POST: it signs the person in and forwards
the request with a signed JWT in ``Cf-Access-Jwt-Assertion``. That signed header is the only
thing SyncShelf trusts about who is signing in. ``Cf-Access-Authenticated-User-Email`` and
every other unsigned header are ignored, because anything that reaches the origin some other
way could set them.

:class:`AccessVerifier` checks the JWT and returns the lowercased email:

- the key comes from the team's JWKS, ``<team_domain>/cdn-cgi/access/certs``, fetched with
  ``httpx2`` so the event loop never blocks (``PyJWKClient`` fetches synchronously). The set
  is cached and fetched again only for a ``kid`` it doesn't hold, which is how a rotated key
  is picked up. Each fetch replaces the whole set; whether it should also expire is #110;
- ``RS256`` only, ``iss`` equal to the team domain, ``aud`` containing the app's AUD tag,
  ``exp`` and ``iat`` required with 30 s leeway, ``type == "app"``, and a non-empty
  ``email``.

The times are checked against the injected clock, not PyJWT's own ``time.time()``, so tests
drive expiry with the one test clock (§2.9).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, NoReturn

import httpx2
import jwt

from syncshelf.auth.cimd import FETCH_TIMEOUT

__all__ = [
    "ACCESS_HEADER",
    "ALGORITHM",
    "CERTS_PATH",
    "JWKS_TIMEOUT",
    "LEEWAY",
    "AccessRefused",
    "AccessVerifier",
]

ACCESS_HEADER = "cf-access-jwt-assertion"
"""The header carrying the Access JWT, as ASGI spells it (lowercase)."""

CERTS_PATH = "/cdn-cgi/access/certs"
"""Where, under the team domain, Access publishes its signing keys."""

ALGORITHM = "RS256"
"""The only signing algorithm accepted."""

LEEWAY = 30
"""Seconds of clock skew allowed on ``exp``, ``iat`` and ``nbf``."""

JWKS_TIMEOUT = FETCH_TIMEOUT
"""The key fetch's timeouts: the client-document fetch's (§3.2.6)."""

log = logging.getLogger(__name__)


class AccessRefused(Exception):
    """The Access JWT is missing or doesn't verify. ``reason`` is a short snake_case code for
    the log; it never holds anything from the token. ``unavailable`` is true when the keys
    couldn't be fetched, so the fault is not the request's."""

    def __init__(self, reason: str, *, unavailable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.unavailable = unavailable


class AccessVerifier:
    """Verifies Access JWTs for one Access app: ``team_domain`` and its ``aud`` tag.

    The app builds this before its lifespan, and the lifespan opens it with :meth:`running`,
    which binds the HTTP client that fetches the keys. ``clock`` gives epoch seconds, as
    ``create_app``'s does. ``transport`` replaces the network, for tests (§2.9).
    """

    def __init__(
        self,
        team_domain: str,
        aud: str,
        *,
        clock: Callable[[], float] = time.time,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._issuer = team_domain
        self._certs_url = team_domain + CERTS_PATH
        self._aud = aud
        self._clock = clock
        self._transport = transport
        self._http: httpx2.AsyncClient | None = None
        self._keys: Mapping[str, Any] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Fetch keys over the network until the block exits."""
        async with httpx2.AsyncClient(
            follow_redirects=False,
            timeout=JWKS_TIMEOUT,
            trust_env=False,  # so a stray HTTPS_PROXY can't reroute the fetch
            transport=self._transport,
        ) as http:
            self._http = http
            try:
                yield
            finally:
                self._http = None

    async def verify(self, token: str | None) -> str:
        """The lowercased ``email`` of a valid Access JWT, or :class:`AccessRefused`."""
        if not token:
            raise AccessRefused("access_jwt_missing")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise AccessRefused("access_jwt_malformed") from None
        # PyJWT refuses any other alg too; checking first means no key fetch for one.
        if header.get("alg") != ALGORITHM:
            raise AccessRefused("access_jwt_bad_alg")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AccessRefused("access_jwt_no_kid")
        key = await self._key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                audience=self._aud,  # aud is an array; PyJWT accepts one containing it
                issuer=self._issuer,
                options={
                    "require": ["exp", "iat", "iss", "aud"],
                    # Checked below against the injected clock instead.
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
        except jwt.InvalidAudienceError:
            raise AccessRefused("access_jwt_bad_aud") from None
        except jwt.InvalidIssuerError:
            raise AccessRefused("access_jwt_bad_iss") from None
        except jwt.InvalidSignatureError:
            raise AccessRefused("access_jwt_bad_signature") from None
        except jwt.InvalidTokenError:
            raise AccessRefused("access_jwt_invalid") from None
        self._check_times(claims)
        if claims.get("type") != "app":
            raise AccessRefused("access_jwt_bad_type")
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise AccessRefused("access_jwt_no_email")
        return email.strip().lower()

    def _check_times(self, claims: Mapping[str, Any]) -> None:
        now = self._clock()
        times = {}
        for name in ("exp", "iat", "nbf"):
            value = claims.get(name)
            if value is None and name == "nbf":
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise AccessRefused(f"access_jwt_bad_{name}")
            times[name] = value
        if times["exp"] <= now - LEEWAY:
            raise AccessRefused("access_jwt_expired")
        if times["iat"] > now + LEEWAY or times.get("nbf", now) > now + LEEWAY:
            raise AccessRefused("access_jwt_not_yet_valid")

    async def _key(self, kid: str) -> Any:
        key = self._keys.get(kid)
        if key is not None:
            return key
        async with self._lock:
            # Another request may have fetched the set while this one waited.
            key = self._keys.get(kid)
            if key is None:
                self._keys = await self._fetch()
                key = self._keys.get(kid)
        if key is None:
            raise AccessRefused("access_jwt_unknown_kid")
        return key

    async def _fetch(self) -> Mapping[str, Any]:
        """The team's RSA signing keys by ``kid``. A failure keeps the cached set."""
        http = self._http
        if http is None:
            raise AccessRefused("not_running", unavailable=True)
        started = time.perf_counter()
        try:
            response = await http.get(self._certs_url, headers={"accept": "application/json"})
        except httpx2.HTTPError as exc:
            self._fetch_failed(f"transport_{type(exc).__name__}")
        log.debug(
            "GET access %s %d",
            CERTS_PATH,
            response.status_code,
            extra={
                "event": "http_call",
                "service": "access",
                "method": "GET",
                "path": CERTS_PATH,
                "status": response.status_code,
                "ms": round((time.perf_counter() - started) * 1000),
            },
        )
        if response.status_code != 200:
            self._fetch_failed(f"status_{response.status_code}")
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
            jwks = jwt.PyJWKSet.from_dict(body)
        except ValueError, jwt.PyJWTError:  # bad JSON, or no usable key in it
            self._fetch_failed("bad_jwks")
        keys = {
            k.key_id: k.key
            for k in jwks.keys
            if k.key_id and k.key_type == "RSA" and k.algorithm_name == ALGORITHM
        }
        if not keys:  # a set this verifier can't use must not replace one it can
            self._fetch_failed("no_rs256_keys")
        return keys

    def _fetch_failed(self, reason: str) -> NoReturn:
        log.warning(
            "could not fetch the Access signing keys (%s)",
            reason,
            extra={"event": "fetch_failed", "service": "access", "reason": reason},
        )
        raise AccessRefused("jwks_unavailable", unavailable=True)
