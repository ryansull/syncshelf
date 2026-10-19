"""A thin client for a Goodreads shelf RSS feed (SPEC §3.4a.1).

SyncShelf reads each profile's whole library from ``/review/list_rss/<user_id>`` with
``shelf=#ALL#``, 200 items a page, until a page comes back empty. An unknown or misspelt shelf
name returns the whole library, so no other shelf is ever requested.

**Usable or not.** A page counts only if it is HTTP 200, parses as RSS with a ``<channel>``,
and the channel is titled ``…'s bookshelf: all``, the ``#ALL#`` form seen on the first live
fetch (2026-10-18). Anything else is a failed fetch, never "no books": a 404 user, an AWS WAF
challenge (202, ``x-amzn-waf-action``) or CAPTCHA (405), an HTML page, a network error. Paging
stops at the first unusable page and never retries past it, and the pass is **complete** only
if every page was usable and paging ended on an empty page (§3.4a.3).

**Politeness.** ``robots.txt`` disallows the feed for crawlers; it's a subscription link, so
SyncShelf keeps to a reader's pace: one pass per run, at least :data:`MIN_INTERVAL` seconds
between requests through the profile's :class:`Pacer`, and an honest ``User-Agent``.

The RSS ``key`` (for a private profile) and ``user_name`` are never logged (§2.7). Outbound
calls are logged at DEBUG without the query string or the user id.
"""

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol

import httpx2

from syncshelf import __version__

__all__ = [
    "ALL_SHELF",
    "MIN_INTERVAL",
    "PER_PAGE",
    "USER_AGENT",
    "GoodreadsRss",
    "Pacer",
    "RssItem",
    "RssPass",
    "rss_url",
]

log = logging.getLogger(__name__)

RSS_URL = "https://www.goodreads.com/review/list_rss/{user_id}"
ALL_SHELF = "#ALL#"
PER_PAGE = 200
"""Goodreads honours ``per_page`` up to 200; above that it silently falls back to 100."""
MIN_INTERVAL = 2.0
"""Seconds between one Goodreads request ending and the next starting, per profile."""
TIMEOUT = 30.0
USER_AGENT = f"syncshelf/{__version__} (+https://github.com/ryansull/syncshelf)"
"""Says what SyncShelf is: a self-hosted reader of its own users' shelves."""
MAX_PAGES = 100
"""A guard against paging that never ends: 20,000 books, far past any household library."""

_ALL_TITLE_SUFFIX = "'s bookshelf: all"
_ITEM_FIELDS = (
    "book_id",
    "title",
    "author_name",
    "isbn",
    "user_rating",
    "user_read_at",
    "user_date_added",
    "user_date_created",
    "user_shelves",
    "user_review",
)


class Clock(Protocol):
    """What the pacer needs from a clock: the injectable test clock satisfies it (§2.9)."""

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class Pacer:
    """One Goodreads request at a time per profile, :data:`MIN_INTERVAL` apart (§2.2).

    Every Goodreads call for a profile takes a :meth:`turn`: the RSS feed now, and the
    library export and sign-in in phase 4b, so the profile's traffic never overlaps itself.
    """

    def __init__(self, clock: Clock | None = None, interval: float = MIN_INTERVAL) -> None:
        self._clock = clock or _SystemClock()
        self._interval = interval
        self._lock = asyncio.Lock()
        self._last: float | None = None

    @asynccontextmanager
    async def turn(self) -> AsyncIterator[None]:
        """Wait for the profile's previous request to be :data:`MIN_INTERVAL` behind us."""
        async with self._lock:
            if self._last is not None:
                wait = self._last + self._interval - self._clock.monotonic()
                if wait > 0:
                    await self._clock.sleep(wait)
            try:
                yield
            finally:
                self._last = self._clock.monotonic()


@dataclass(frozen=True)
class RssItem:
    """One feed item's fields, as the text Goodreads sent (empty when the element is)."""

    book_id: str
    """The Goodreads **edition** id: the source row key."""
    title: str
    author_name: str
    isbn: str
    user_rating: str
    user_read_at: str
    user_date_added: str
    user_date_created: str
    user_shelves: str
    user_review: str


@dataclass(frozen=True)
class RssPass:
    """One paged pass over a profile's ``#ALL#`` feed."""

    items: list[RssItem] = field(default_factory=list[RssItem])
    pages: int = 0
    """Usable pages fetched, the closing empty page included."""
    complete: bool = False
    """Every page was usable and paging ended on an empty page."""
    problem: str | None = None
    """Why paging stopped before the empty page, naming the page; ``None`` when complete."""
    challenged: bool = False
    """The stop was an AWS WAF challenge or CAPTCHA: stop, never retry past it (§1)."""

    @property
    def usable(self) -> bool:
        """Whether anything usable was fetched: at least the first page."""
        return self.pages > 0


class _Unusable(Exception):
    def __init__(self, reason: str, *, challenged: bool = False) -> None:
        super().__init__(reason)
        self.challenged = challenged


def rss_url(user_id: str) -> str:
    """A profile's feed URL, without the query."""
    return RSS_URL.format(user_id=user_id)


class GoodreadsRss:
    """One profile's shelf RSS feed.

    ``pacer`` is the profile's Goodreads pacer, shared with every other Goodreads call for
    that profile. ``transport`` is for tests, which route requests to an in-memory fake.
    """

    def __init__(
        self,
        user_id: str,
        *,
        key: str | None = None,
        pacer: Pacer,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self._url = rss_url(user_id)
        self._key = key
        self._pacer = pacer
        self._http = httpx2.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"user-agent": USER_AGENT},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._http.aclose()

    async def fetch_all(self) -> RssPass:
        """Page through the ``#ALL#`` feed until an empty page or the first unusable one."""
        items: list[RssItem] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            try:
                found = await self._page(page)
            except _Unusable as exc:
                return RssPass(
                    items, page - 1, problem=f"page {page}: {exc}", challenged=exc.challenged
                )
            if not found:
                return RssPass(items, page, complete=True)
            # Pages are disjoint; one that repeats earlier items means paging went wrong.
            if any(item.book_id in seen for item in found):
                return RssPass(items, page - 1, problem=f"page {page}: repeats earlier items")
            seen.update(item.book_id for item in found)
            items.extend(found)
        return RssPass(items, MAX_PAGES, problem=f"more than {MAX_PAGES} pages")

    async def _page(self, page: int) -> list[RssItem]:
        params = {"shelf": ALL_SHELF, "per_page": str(PER_PAGE), "page": str(page)}
        if self._key:
            params["key"] = self._key
        async with self._pacer.turn():
            started = time.monotonic()
            try:
                response = await self._http.get(self._url, params=params)
            except httpx2.RequestError as exc:
                raise _Unusable(f"Goodreads is unreachable ({type(exc).__name__})") from None
        log.debug(
            "GET %s %d",
            "/review/list_rss",
            response.status_code,
            extra={
                "event": "http_request",
                "service": "goodreads",
                "method": "GET",
                "status": response.status_code,
                "ms": round((time.monotonic() - started) * 1000),
            },
        )
        return _items(response)


def _items(response: httpx2.Response) -> list[RssItem]:
    """A usable page's items, or :class:`_Unusable` saying why the page doesn't count."""
    status = response.status_code
    waf = response.headers.get("x-amzn-waf-action")
    if waf or status in (202, 405):
        kind = "CAPTCHA" if status == 405 or waf == "captcha" else "challenge"
        raise _Unusable(f"Goodreads answered with a WAF {kind} (HTTP {status})", challenged=True)
    if status == 404:
        raise _Unusable("Goodreads has no such user (HTTP 404)")
    if status != 200:
        raise _Unusable(f"Goodreads returned HTTP {status}")
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        raise _Unusable("the feed is not XML") from None
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise _Unusable("the feed is not RSS with a channel")
    if not (channel.findtext("title") or "").endswith(_ALL_TITLE_SUFFIX):
        raise _Unusable("the feed doesn't name the #ALL# shelf")
    return [_item(element) for element in channel.findall("item")]


def _item(element: ET.Element) -> RssItem:
    values = {name: (element.findtext(name) or "").strip() for name in _ITEM_FIELDS}
    return RssItem(**values)
