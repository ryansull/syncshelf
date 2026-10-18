"""A fake Cloudflare Access: a test RSA key that signs Access JWTs, and a ``MockTransport``
handler serving its JWKS (SPEC §3.2.8 level 2).

:class:`FakeAccess` forges the JWT that Access would put in ``Cf-Access-Jwt-Assertion``
for the test team domain and AUD tag in :mod:`serving`. :func:`routed` joins it with
:class:`cimd_docs.FakeClaude` into the one transport ``create_app`` takes.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from clock import FakeClock
from serving import ACCESS_AUD, TEAM_DOMAIN

CERTS_URL = TEAM_DOMAIN + "/cdn-cgi/access/certs"

_KEYS: dict[str, rsa.RSAPrivateKey] = {}


def rsa_key(name: str) -> rsa.RSAPrivateKey:
    """A 2048-bit test key, made once per ``name`` per test run (generation is slow)."""
    if name not in _KEYS:
        _KEYS[name] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _KEYS[name]


def jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    """``key``'s public half as a JWK, as Access publishes it."""
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**data, "kid": kid, "alg": "RS256", "use": "sig"}


class FakeAccess:
    """Signs Access JWTs and serves the JWKS that verifies them, recording each JWKS request.

    ``published`` is the key set served, by ``kid``; ``respond`` replaces the default answer.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.kid = "key-1"
        self.published: dict[str, rsa.RSAPrivateKey] = {self.kid: rsa_key("access-1")}
        self.requests: list[httpx2.Request] = []
        self.respond: Callable[[httpx2.Request], httpx2.Response] | None = None

    def token(
        self,
        email: str = "alice@example.com",
        *,
        key: rsa.RSAPrivateKey | None = None,
        kid: str | None = None,
        algorithm: str = "RS256",
        drop: tuple[str, ...] = (),
        **claims: Any,
    ) -> str:
        """An Access JWT for ``email``, valid now. ``claims`` override or add claims, ``drop``
        removes them; ``key``, ``kid`` and ``algorithm`` change how it is signed."""
        now = int(self.clock.time())
        payload: dict[str, Any] = {
            "aud": [ACCESS_AUD],
            "email": email,
            "exp": now + 3600,
            "iat": now,
            "nbf": now,
            "iss": TEAM_DOMAIN,
            "type": "app",
            "sub": "00000000-0000-0000-0000-000000000000",
            **claims,
        }
        for name in drop:
            payload.pop(name, None)
        signing_key: Any = key or self.published[self.kid]
        if algorithm == "none":
            signing_key = None
        elif algorithm.startswith("HS"):
            signing_key = "a-shared-secret-of-at-least-32-bytes!"
        return jwt.encode(
            payload, signing_key, algorithm=algorithm, headers={"kid": kid or self.kid}
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.respond is not None:
            return self.respond(request)
        if str(request.url) != CERTS_URL:
            return httpx2.Response(404)
        keys = [jwk(key, kid) for kid, key in self.published.items()]
        return httpx2.Response(200, json={"keys": keys})


def routed(handlers: Mapping[str, Callable[[httpx2.Request], httpx2.Response]]) -> Any:
    """One ``MockTransport`` sending each request to the handler for its host."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        handler = handlers.get(request.url.host)
        return handler(request) if handler is not None else httpx2.Response(404)

    return httpx2.MockTransport(handle)
