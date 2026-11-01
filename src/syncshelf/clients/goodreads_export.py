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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx2

from syncshelf.clients.goodreads_rss import TIMEOUT, USER_AGENT, Clock, Pacer
from syncshelf.sync.goodreads_session import Cookie, CookieJar

__all__ = [
    "BASE",
    "DOWNLOAD_PATH",
    "EXPORT_PATH",
    "POLL_INTERVAL",
    "POLL_TIMEOUT",
    "IMPORT_PATH",
    "REUSE_WITHIN",
    "MAX_REDIRECTS",
    "SIGN_IN_PATHS",
    "Challenged",
    "ExistingExport",
    "FetchedExport",
    "ExportError",
    "GoodreadsExport",
    "ImportPage",
    "ImportPageError",
    "fetch_export",
    "parse_import_page",
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


# --- the import page (§3.4b.4 step 1) -------------------------------------------------------

IMPORT_PATH = "/review/import"
"""The page that carries the CSRF token, the user id and any existing export."""

REUSE_WITHIN = timedelta(hours=24)
"""§3.4b.4 step 2: an existing export younger than this is reused rather than re-triggered.

Triggering **deletes the previous file**, so reuse is not only cheaper, it is the only way to
avoid destroying a good export to make an equivalent one.
"""

_LINK_TEXT = re.compile(r"Your export from\s+(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}:\d{2})")
_LINK_FORMAT = "%m/%d/%Y - %H:%M"
_EXPORT_PATH = re.compile(r"^/review_porter/export/(\d+)")


class ImportPageError(ExportError):
    """The import page could not be read.

    §3.4b.4 notes the rest of Goodreads is migrating to Next.js, so this scraping is the part
    expected to break first. The message names what was missing, so a failure says why.
    """


@dataclass(frozen=True)
class ExistingExport:
    """The export Goodreads already has, from the ``div#exportFile`` link."""

    path: str
    """Where to download it, as a Goodreads path."""
    generated_at: datetime
    """When Goodreads says it was generated, read in the profile's timezone.

    §3.4b.4 says to confirm that timezone in the attended opening test (#219); until then the
    profile's configured ``timezone`` is what the link text is read in.
    """

    def fresh_at(self, now: datetime) -> bool:
        """Whether this export is young enough to reuse at ``now`` (§3.4b.4 step 2)."""
        return now - self.generated_at < REUSE_WITHIN


@dataclass(frozen=True)
class ImportPage:
    """What the import page offers before any export is triggered."""

    csrf_token: str = field(repr=False)
    """Never logged: it authorises the trigger."""
    user_id: str
    """The numeric id the trigger posts to."""
    export: ExistingExport | None = None
    """The existing export, when the page shows one."""

    def reuse(self, now: datetime) -> ExistingExport | None:
        """The export to reuse at ``now``, or ``None`` to trigger a new one.

        Nothing to reuse and an export older than 24 h both mean trigger (§3.4b.4 step 2).
        """
        if self.export is not None and self.export.fresh_at(now):
            return self.export
        return None


def parse_import_page(html: str, tz: tzinfo) -> ImportPage:
    """Read the import page. Raises :class:`ImportPageError` naming what was missing.

    ``tz`` is the profile's timezone, which the ``#exportFile`` link text is read in: the text
    carries no offset, so it can only be read against a timezone the caller supplies.
    """
    scraper = _ImportPageScraper()
    scraper.feed(html)
    token = scraper.csrf_token or scraper.authenticity_token
    if token is None:
        raise ImportPageError("the import page has no CSRF token")
    user_id = scraper.user_id or _user_id_from(scraper.export_href)
    if user_id is None:
        raise ImportPageError("the import page does not say which user id to export")
    return ImportPage(csrf_token=token, user_id=user_id, export=_existing(scraper, tz))


def _existing(scraper: _ImportPageScraper, tz: tzinfo) -> ExistingExport | None:
    """The existing export, or ``None`` when the page shows none.

    A link whose text cannot be read is an error, not "no export": treating an unreadable
    timestamp as absent would trigger a new export and delete the file it describes.
    """
    if scraper.export_href is None:
        return None
    found = _LINK_TEXT.search(scraper.export_text)
    if found is None:
        raise ImportPageError("the existing export's date could not be read")
    try:
        stamp = datetime.strptime(f"{found.group(1)} - {found.group(2)}", _LINK_FORMAT)
    except ValueError:
        raise ImportPageError("the existing export's date could not be read") from None
    return ExistingExport(scraper.export_href, stamp.replace(tzinfo=tz))


def _user_id_from(href: str | None) -> str | None:
    if href is None:
        return None
    found = _EXPORT_PATH.match(href)
    return found.group(1) if found else None


class _ImportPageScraper(HTMLParser):
    """Pulls the few things §3.4b.4 names out of the import page.

    An HTML parser rather than one big regular expression, so that attribute order, quoting and
    unrelated markup around them don't decide whether the page reads.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.csrf_token: str | None = None
        self.authenticity_token: str | None = None
        self.user_id: str | None = None
        self.export_href: str | None = None
        self.export_text = ""
        self._in_export = 0
        self._collecting = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        at = {name: value or "" for name, value in attrs}
        if tag == "meta" and at.get("name") == "csrf-token":
            self.csrf_token = at.get("content") or None
        elif tag == "input" and at.get("name") == "authenticity_token":
            self.authenticity_token = at.get("value") or None
        elif tag == "form" and (found := _EXPORT_PATH.match(at.get("action", ""))):
            self.user_id = found.group(1)
        elif tag == "div" and at.get("id") == "exportFile":
            self._in_export = 1
        elif self._in_export:
            if tag == "a" and self.export_href is None:
                self.export_href = at.get("href") or None
                self._collecting = True
            self._in_export += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._collecting = False
        if self._in_export:
            self._in_export -= 1

    def handle_data(self, data: str) -> None:
        if self._collecting:
            self.export_text += data


# --- the export run (§3.4b.4 steps 2 to 4) --------------------------------------------------

EXPORT_PATH = "/review_porter/export/{user_id}"
"""Where the trigger posts (§3.4b.4 step 2)."""

DOWNLOAD_PATH = "/review_porter/export/{user_id}/goodreads_export.csv"
"""Where the finished export is fetched from, and polled for (§3.4b.4 step 3)."""

POLL_INTERVAL = 10.0
POLL_TIMEOUT = 600.0
"""§3.4b.4 step 3: poll every 10 s for up to 10 minutes. A 404 means not ready yet."""

_TRIGGER_HEADERS = {
    "x-requested-with": "XMLHttpRequest",
    "referer": f"{BASE}{IMPORT_PATH}",
    "origin": BASE,
}
"""Everything §3.4b.4 step 2 sends besides the CSRF token and the form body."""


@dataclass(frozen=True)
class FetchedExport:
    """A library export fetched from Goodreads, ready to be stored as a batch."""

    payload: bytes
    """The CSV exactly as received, for ``ingest_batches.payload``."""
    observed_at: datetime
    """When the export's contents were true of the library (§3.4b.4).

    The **trigger time** when SyncShelf triggered it, and otherwise the ``#exportFile``
    timestamp read in the profile's timezone.
    """
    reused: bool
    """Whether an export Goodreads already had was used instead of triggering a new one."""


async def fetch_export(
    client: GoodreadsExport, page: ImportPage, *, now: datetime, clock: Clock
) -> FetchedExport:
    """Get a library export for ``page``'s user, triggering one only when needed.

    Reuse comes first, because triggering **deletes the previous file** (§3.4b.4 step 2): an
    export younger than 24 h is downloaded as it stands.

    When the trigger is **refused**, an export Goodreads already has is downloaded whatever its
    age rather than failing. A refused trigger deleted nothing, so the old file is still there,
    and older real data beats none — 4a's "newer observation wins" already handles its age.
    Only with no export at all is this a failed fetch. Decided on #231.

    Raises :class:`ExportError` when there is nothing to download or the poll times out, and
    :class:`SessionExpired` or :class:`Challenged` from the transport.
    """
    if fresh := page.reuse(now):
        return await _download(client, fresh.path, fresh.generated_at, reused=True)

    response = await client.post(
        EXPORT_PATH.format(user_id=page.user_id),
        data={"format": "json"},
        headers={"x-csrf-token": page.csrf_token, **_TRIGGER_HEADERS},
    )
    if response.is_success or response.status_code == 302:
        await _await_ready(client, page.user_id, clock)
        # Triggered, so the export is current as of when we asked for it, not of any link text.
        path = DOWNLOAD_PATH.format(user_id=page.user_id)
        return await _download(client, path, now, reused=False)

    # The response body is undocumented and the cooldown unverified (§3.4b.4), so the status is
    # all there is to log; the fallback is what keeps a cooldown from looking like an outage.
    log.warning(
        "Goodreads refused to start a library export",
        extra={"event": "goodreads_export_refused", "status": response.status_code},
    )
    if page.export is None:
        raise ExportError(f"Goodreads refused to start an export ({response.status_code})")
    return await _download(client, page.export.path, page.export.generated_at, reused=True)


async def _await_ready(client: GoodreadsExport, user_id: str, clock: Clock) -> None:
    """Poll until the export exists, or give up after :data:`POLL_TIMEOUT` (§3.4b.4 step 3)."""
    path = DOWNLOAD_PATH.format(user_id=user_id)
    started = clock.monotonic()
    while True:
        if (await client.get(path)).status_code != 404:
            return
        if clock.monotonic() - started >= POLL_TIMEOUT:
            raise ExportError(
                f"Goodreads had not finished the export after {POLL_TIMEOUT:.0f} seconds"
            )
        await clock.sleep(POLL_INTERVAL)


async def _download(
    client: GoodreadsExport, path: str, observed_at: datetime, *, reused: bool
) -> FetchedExport:
    response = await client.get(path)
    if not response.is_success:
        raise ExportError(f"the Goodreads export could not be downloaded ({response.status_code})")
    log.info(
        "fetched a Goodreads library export",
        extra={
            "event": "goodreads_export_fetched",
            "bytes": len(response.content),
            "reused": reused,
        },
    )
    return FetchedExport(response.content, observed_at, reused)
