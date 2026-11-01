"""A stateful in-memory Goodreads shelf RSS feed (SPEC §2.9, §3.4a.1).

``FakeGoodreads`` answers ``GET https://www.goodreads.com/review/list_rss/<user_id>`` from each
user's items, newest first, in the recorded feed's exact shape: the channel and every item are
built from ``tests/fixtures/goodreads/rss_page.xml``. It reproduces the feed's known traps:

- ``per_page`` is honoured up to 200 and silently falls back to 100 above that;
- a page past the end is HTTP 200 with a valid channel and no items;
- an unknown or misspelt shelf name returns the whole library, titled ``all``;
- an unknown user is a 404 ``text/html`` "invalid user_id" page;
- ``read`` never appears in ``user_shelves``.

Faults are injected with :meth:`FakeGoodreads.fail_next`.
"""

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import httpx2

FIXTURE = Path(__file__).parents[1] / "fixtures" / "goodreads" / "rss_page.xml"
HOST = "www.goodreads.com"
PATH_PREFIX = "/review/list_rss/"
BUILT_IN_SHELVES = ("read", "currently-reading", "to-read")

Fault = Literal["challenge", "captcha", "server_error", "html", "wrong_shelf", "timeout"]


@dataclass
class Request:
    """One request the fake answered: its user id, query and when it arrived."""

    user_id: str
    params: dict[str, str]
    at: float


@dataclass
class ExportRequest:
    """One signed-in export request the fake answered (phase 4b)."""

    method: str
    path: str
    at: float


@dataclass(frozen=True)
class _ExportPlan:
    """How the fake answers the next export request. See :meth:`FakeGoodreads.expect_export`."""

    rotate: int = 0
    expired: bool = False
    offsite: str | None = None
    body: str = "<html><body>import</body></html>"
    status: int = 200


@dataclass
class _User:
    name: str
    items: list[dict[str, str]]


class FakeGoodreads:
    """Goodreads' shelf RSS feed for any number of users.

    ``clock`` (anything with ``monotonic()``) timestamps each request, so a test can check the
    pacing between them.
    """

    def __init__(self, clock: Any = None) -> None:
        self.users: dict[str, _User] = {}
        self.requests: list[Request] = []
        self.export_requests: list[ExportRequest] = []
        self.rotations = 0
        self._export_plan = _ExportPlan()
        self._clock = clock
        self._faults: list[tuple[int | None, Fault]] = []

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport to hand a ``GoodreadsRss``."""
        return httpx2.MockTransport(self.handle)

    # --- state ----------------------------------------------------------------------------

    def add_user(self, user_id: str, name: str = "Alice") -> None:
        self.users[user_id] = _User(name, [])

    def add_item(
        self,
        user_id: str,
        book_id: int,
        title: str,
        *,
        shelf: str = "read",
        shelves: tuple[str, ...] = (),
        **fields: str,
    ) -> dict[str, str]:
        """Add a book to the top of a user's feed (the newest ``user_date_added``).

        ``shelf`` is its exclusive shelf and ``shelves`` its other shelves; ``user_shelves``
        is built from them as Goodreads does, leaving ``read`` out. ``fields`` set any other
        item element by name (``user_rating="4"``, ``user_read_at="Fri, 2 Oct 2026 …"``).
        """
        listed = [s for s in (shelf, *shelves) if s != "read"]
        item = {
            "book_id": str(book_id),
            "title": title,
            "author_name": "Ann Author",
            "isbn": "",
            "user_rating": "0",
            "user_read_at": "",
            "user_date_added": "Thu, 15 Oct 2026 08:09:10 -0700",
            "user_date_created": "Thu, 15 Oct 2026 08:09:10 -0700",
            "user_shelves": ", ".join(listed),
            "user_review": "",
            **fields,
        }
        self.users[user_id].items.insert(0, item)
        return item

    def fail_next(self, fault: Fault, *, page: int | None = None) -> None:
        """Answer the next request (or the next request for ``page``) with ``fault``."""
        self._faults.append((page, fault))

    # --- serving --------------------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = urlsplit(str(request.url))
        if url.hostname != HOST or url.scheme != "https":
            raise AssertionError(f"FakeGoodreads got a request for {url.scheme}://{url.hostname}")
        if not url.path.startswith(PATH_PREFIX):
            return self._export(request, url.path)
        if request.method != "GET":
            raise AssertionError(f"FakeGoodreads can't answer {request.method} {url.path}")
        params = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        user_id = url.path.removeprefix(PATH_PREFIX)
        at = self._clock.monotonic() if self._clock is not None else 0.0
        self.requests.append(Request(user_id, params, at))
        page = int(params.get("page") or 1)

        if fault := self._take_fault(page):
            if fault == "timeout":
                raise httpx2.ReadTimeout("timed out", request=request)
            return _fault(fault, self.users.get(user_id))
        user = self.users.get(user_id)
        if user is None:
            return httpx2.Response(
                404,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><body>invalid user_id</body></html>",
            )
        per_page = int(params.get("per_page") or 100)
        if per_page > 200:
            per_page = 100
        shelf = params.get("shelf") or "#ALL#"
        items = user.items
        title_shelf = "all"
        if shelf in self._known_shelves(user):
            items = [i for i in items if _on_shelf(i, shelf)]
            title_shelf = shelf
        shown = items[(page - 1) * per_page : page * per_page]
        return _feed(user.name, title_shelf, shown)

    # --- the signed-in export surface (phase 4b, §3.4b.4) ---------------------------------

    def expect_export(
        self,
        *,
        rotate: int = 0,
        expired: bool = False,
        offsite: str | None = None,
        body: str = "<html><body>import</body></html>",
        status: int = 200,
    ) -> None:
        """Set up how the next export request is answered.

        ``rotate`` answers that many hops with a same-URL redirect carrying a new
        ``_session_id2``, which is how §3.4b.1 says Goodreads regenerates it. ``expired``
        redirects to the sign-in page. ``offsite`` redirects to that absolute URL, for the leak
        guard. A WAF stop is injected with :meth:`fail_next` as for the feed.
        """
        self._export_plan = _ExportPlan(rotate, expired, offsite, body, status)

    def _export(self, request: httpx2.Request, path: str) -> httpx2.Response:
        at = self._clock.monotonic() if self._clock is not None else 0.0
        self.export_requests.append(ExportRequest(request.method, path, at))
        if fault := self._take_fault(0):
            if fault == "timeout":
                raise httpx2.ReadTimeout("timed out", request=request)
            return _fault(fault, None)
        plan = self._export_plan
        if plan.offsite is not None:
            # One-shot, so a same-host redirect can be followed to an ordinary 200 next hop.
            self._export_plan = _ExportPlan(plan.rotate, plan.expired, None, plan.body, plan.status)
            return httpx2.Response(302, headers={"location": plan.offsite})
        if plan.expired:
            return httpx2.Response(302, headers={"location": "/user/sign_in"})
        if plan.rotate > 0:
            self._export_plan = _ExportPlan(
                plan.rotate - 1, plan.expired, plan.offsite, plan.body, plan.status
            )
            self.rotations += 1
            # A same-URL redirect that carries the regenerated Rails cookie (§3.4b.1).
            return httpx2.Response(
                302,
                headers=[
                    ("location", path),
                    (
                        "set-cookie",
                        f"_session_id2=rotated-{self.rotations}; Domain=.goodreads.com; Path=/",
                    ),
                ],
            )
        return httpx2.Response(
            plan.status, headers={"content-type": "text/html; charset=utf-8"}, text=plan.body
        )

    def _take_fault(self, page: int) -> Fault | None:
        for i, (wanted, fault) in enumerate(self._faults):
            if wanted is None or wanted == page:
                del self._faults[i]
                return fault
        return None

    @staticmethod
    def _known_shelves(user: _User) -> set[str]:
        shelves = set(BUILT_IN_SHELVES)
        for item in user.items:
            shelves.update(s.strip() for s in item["user_shelves"].split(",") if s.strip())
        return shelves


def _on_shelf(item: dict[str, str], shelf: str) -> bool:
    listed = [s.strip() for s in item["user_shelves"].split(",") if s.strip()]
    if shelf == "read":
        return not set(listed) & {"currently-reading", "to-read"}
    return shelf in listed


def _template() -> tuple[ET.Element, ET.Element]:
    root = ET.fromstring(FIXTURE.read_text(encoding="utf-8"))
    channel = root.find("channel")
    assert channel is not None
    item = channel.find("item")
    assert item is not None, "the recorded page must hold at least one item"
    for old in channel.findall("item"):
        channel.remove(old)
    return root, item


def _feed(name: str, shelf: str, items: list[dict[str, str]]) -> httpx2.Response:
    root, item_template = _template()
    channel = root.find("channel")
    assert channel is not None
    for element in (channel.find("title"), channel.find("image/title")):
        if element is not None:
            element.text = f"{name}'s bookshelf: {shelf}"
    for values in items:
        item = copy.deepcopy(item_template)
        for tag, value in values.items():
            element = item.find(tag)
            assert element is not None, f"the recorded item has no <{tag}>"
            element.text = value
        book = item.find("book")
        if book is not None:
            book.set("id", values["book_id"])
        channel.append(item)
    body = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    return httpx2.Response(
        200,
        headers={"content-type": "application/xml; charset=utf-8", "cache-control": "max-age=0"},
        text=body,
    )


def _fault(fault: Fault, user: _User | None) -> httpx2.Response:
    if fault == "challenge":
        return httpx2.Response(202, headers={"x-amzn-waf-action": "challenge"}, text="")
    if fault == "captcha":
        headers = {"x-amzn-waf-action": "captcha", "content-type": "text/html"}
        return httpx2.Response(405, headers=headers, text="<html/>")
    if fault == "server_error":
        return httpx2.Response(503, text="Service Unavailable")
    if fault == "html":
        return httpx2.Response(
            200, headers={"content-type": "text/html"}, text="<html><body>Goodreads</body></html>"
        )
    # wrong_shelf: a valid feed, but for a shelf SyncShelf didn't ask for.
    return _feed(user.name if user else "Alice", "to-read", [])
