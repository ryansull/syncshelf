"""The jar-backed Goodreads HTTP transport (SPEC §3.4b.1, §3.4b.3, §3.4b.4).

Every signed-in Goodreads request goes through :class:`GoodreadsExport`. The export flow
itself — the import page, the 24-hour reuse decision, the trigger and the poll — is built on
top of this in `#222 <https://github.com/ryansull/syncshelf/issues/222>`_ and
`#223 <https://github.com/ryansull/syncshelf/issues/223>`_.

What this layer is responsible for:

- **Cookie rotation.** ``Set-Cookie`` is applied on **every** response, redirects included.
  §3.4b.1: ``_session_id2`` is regenerated from the Amazon set through a same-URL redirect, and
  a client that doesn't apply the new cookie follows that redirect forever. The chain is
  bounded so a client that still loops fails instead of hanging.
- **The leak guard.** The jar is only ever sent to ``https://www.goodreads.com``. A redirect to
  any other host, or to ``http``, is refused rather than followed with cookies attached
  (§3.4b.3).
- **Pacing.** Each request takes a turn on the profile's :class:`Pacer`, shared with the RSS
  feed, so one profile's Goodreads traffic never overlaps itself and stays ≥ 2 s apart.
- **The two stop conditions.** A redirect to the sign-in page means the session has expired; an
  AWS WAF challenge (202) or CAPTCHA (405) means stop and never retry (§3.4b.1).

Cookie values and CSRF tokens are never logged. Outbound calls are logged at DEBUG by method
and path only.
"""

import logging
from collections.abc import Mapping
from urllib.parse import urljoin, urlsplit

import httpx2

from syncshelf.clients.goodreads_rss import TIMEOUT, USER_AGENT, Pacer
from syncshelf.sync.goodreads_session import Cookie, CookieJar

__all__ = [
    "BASE",
    "MAX_REDIRECTS",
    "SIGN_IN_PATHS",
    "Challenged",
    "ExportError",
    "GoodreadsExport",
    "SessionExpired",
]

log = logging.getLogger(__name__)

BASE = "https://www.goodreads.com"
"""The only origin the jar is ever sent to (§3.4b.3)."""

HOST = urlsplit(BASE).hostname
MAX_REDIRECTS = 5
"""A bound on one request's redirect chain. §3.4b.1's rotation needs one hop; a chain that
keeps going means the rotation isn't settling, which is a bug worth failing on."""

SIGN_IN_PATHS = ("/user/sign_in", "/user/new")
"""A redirect to either means the session has expired (§3.4b.1, §3.4b.4 step 1)."""

_WAF_HEADER = "x-amzn-waf-action"
_WAF_STOPS = {202: "challenge", 405: "captcha"}
"""§3.4b.1: a challenge is 202 and a CAPTCHA is 405, each with :data:`_WAF_HEADER`. Both mean
stop. SyncShelf never solves, evades or works around either (§3.4b, Boundary)."""


class ExportError(Exception):
    """A Goodreads export request that cannot be completed.

    The message is safe to log and to put in a session ``detail``: it never carries a cookie,
    a CSRF token or a password.
    """


class SessionExpired(ExportError):
    """Goodreads redirected to the sign-in page, so the jar no longer carries a session."""


class Challenged(ExportError):
    """An AWS WAF challenge or CAPTCHA. Sticky: stop, and never retry past it (§3.4b.2)."""


class GoodreadsExport:
    """Signed-in Goodreads requests for one profile, backed by its cookie jar.

    ``jar`` seeds the session; read :attr:`jar` back after any request to persist the rotated
    cookies. ``pacer`` is the profile's Goodreads pacer, shared with its RSS feed.
    ``transport`` is for tests, which route requests to an in-memory fake.
    """

    def __init__(
        self,
        jar: CookieJar,
        *,
        pacer: Pacer,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self._pacer = pacer
        self._http = httpx2.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"user-agent": USER_AGENT},
            # Redirects are followed by hand so that every hop can be checked against the leak
            # guard before the jar is sent to it.
            follow_redirects=False,
            cookies=_to_httpx(jar),
        )

    @property
    def jar(self) -> CookieJar:
        """The current cookies, including anything Goodreads rotated in."""
        return _to_jar(self._http.cookies)

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._http.aclose()

    async def get(self, path: str) -> httpx2.Response:
        """``GET`` a Goodreads path with the jar, following rotation redirects."""
        return await self._send(self._http.build_request("GET", _url(path)))

    async def post(
        self, path: str, *, data: Mapping[str, str], headers: Mapping[str, str]
    ) -> httpx2.Response:
        """``POST`` a form to a Goodreads path with the jar.

        ``headers`` carries the CSRF token and the ``XMLHttpRequest`` marker the export trigger
        needs (§3.4b.4 step 2); its values are never logged.
        """
        return await self._send(
            self._http.build_request("POST", _url(path), data=dict(data), headers=dict(headers))
        )

    async def _send(self, request: httpx2.Request) -> httpx2.Response:
        for hop in range(MAX_REDIRECTS + 1):
            async with self._pacer.turn():
                log.debug(
                    "calling Goodreads",
                    extra={
                        "event": "goodreads_export_request",
                        "method": request.method,
                        "path": urlsplit(str(request.url)).path,
                        "hop": hop,
                    },
                )
                response = await self._http.send(request)
            # httpx applies Set-Cookie to the client jar on every response it sends, redirects
            # included, which is exactly what §3.4b.1's rotation needs.
            location = response.headers.get("location")
            if stop := _WAF_STOPS.get(response.status_code):
                # §3.4b.4 says "a WAF 202/405 at any step", so the status alone stops us: both
                # are abnormal for these endpoints, and stopping is the safe direction. The
                # header is logged when it confirms which it was, never required.
                log.warning(
                    "Goodreads returned an AWS WAF stop, so this profile needs a person",
                    extra={
                        "event": "goodreads_export_challenged",
                        "status": response.status_code,
                        "waf_action": response.headers.get(_WAF_HEADER),
                    },
                )
                raise Challenged(f"Goodreads returned an AWS WAF {stop} ({response.status_code})")
            if not response.is_redirect or location is None:
                return response
            request = self._redirect(request, location)
        raise ExportError(f"Goodreads redirected more than {MAX_REDIRECTS} times")

    def _redirect(self, request: httpx2.Request, location: str) -> httpx2.Request:
        """The next request in a redirect chain, or a refusal.

        The guard is here rather than at the call sites so that no path can follow a redirect
        off Goodreads with the jar attached.
        """
        target = urljoin(str(request.url), location)
        parts = urlsplit(target)
        if parts.scheme != "https" or parts.hostname != HOST:
            # Never send the jar anywhere else, and never downgrade it to http (§3.4b.3).
            raise ExportError(f"Goodreads redirected to {parts.scheme}://{parts.hostname}")
        if parts.path in SIGN_IN_PATHS:
            raise SessionExpired("Goodreads redirected to its sign-in page")
        return self._http.build_request("GET", target)


def _url(path: str) -> str:
    """``path`` as an absolute Goodreads URL. A path is all a caller ever passes."""
    return urljoin(BASE, path)


def _to_httpx(jar: CookieJar) -> httpx2.Cookies:
    cookies = httpx2.Cookies()
    for cookie in jar.cookies:
        cookies.set(
            cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/")
        )
    return cookies


def _to_jar(cookies: httpx2.Cookies) -> CookieJar:
    """The client's cookies as a :class:`CookieJar`, sorted so a saved jar is stable."""
    found: list[Cookie] = []
    for raw in cookies.jar:
        cookie = Cookie(
            name=raw.name, value=raw.value or "", domain=raw.domain, path=raw.path or "/"
        )
        if raw.expires is not None:
            cookie["expires"] = float(raw.expires)
        if raw.secure:
            cookie["secure"] = True
        found.append(cookie)
    return CookieJar(tuple(sorted(found, key=lambda c: (c["name"], c["domain"]))))
