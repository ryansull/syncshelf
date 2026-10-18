"""A thin client for Hardcover's GraphQL API (SPEC §3.3.1).

One :class:`HardcoverClient` per profile, holding that profile's PAT. It sends each query,
reports what it cost (one token per top-level field) and the rate-limit headers that came back,
and turns every kind of failure into a typed :class:`HardcoverError`. It has no retry, pacing or
sync logic: the per-profile limiter (§3.3.3), passed in as its ``gate``, decides when a call may
go out and what a 429 means, and every call goes through it.

Error strings never carry the token or a response body. Hardcover echoes nothing secret today,
but a body can hold catalog text, which the logging allowlist keeps out of logs (§2.7).
"""

import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2

from syncshelf import __version__

__all__ = [
    "GOALS",
    "LIBRARY_PAGE",
    "Gate",
    "LISTS_MORE",
    "LIST_BOOKS_MORE",
    "HardcoverCapacityError",
    "HardcoverClient",
    "HardcoverError",
    "HardcoverHTTPError",
    "HardcoverMutationError",
    "HardcoverQueryError",
    "HardcoverRateLimited",
    "HardcoverUnavailable",
    "Library",
    "RateLimit",
    "Result",
    "check_mutation",
    "query_cost",
]

log = logging.getLogger(__name__)

USER_AGENT = f"syncshelf/{__version__} (+https://github.com/ryansull/syncshelf)"
"""Identifies SyncShelf to Hardcover, which asks API users to say who they are."""

LIBRARY_PAGE = 1000
"""Hardcover silently caps every list field at 1000 rows, so the library is paged by this."""

TIMEOUT = 35.0
"""A little over Hardcover's 30 s query timeout, so its own 408 arrives before ours fires."""

ME = "query Me { me { id } }"

GOALS = "query Goals { me { goals { metric goal progress start_date end_date state } } }"
"""The token owner's reading goals with their progress (§3.3.7, ``reading_stats``)."""

_USER_BOOK_FIELDS = """
    id book_id edition_id status_id rating has_review review_raw review_has_spoilers
    date_added first_started_reading_date privacy_setting_id updated_at
    user_book_reads(order_by: {id: asc}) {
      id started_at finished_at paused_at edition_id progress_pages progress_seconds
      edition { reading_format_id }
    }
    edition { id reading_format_id pages audio_seconds isbn_13 isbn_10 asin }
    book {
      id title subtitle release_year pages canonical_id
      cached_contributors cached_featured_series
      genres: cached_tags(path: "Genre")
      moods: cached_tags(path: "Mood")
      tags: cached_tags(path: "Tag")
      default_audio_edition { id audio_seconds }
      default_ebook_edition { id }
      default_physical_edition { id pages }
    }
"""

_USER_BOOKS_PAGE = (
    "user_books(where: {user_id: {_eq: $user_id}, id: {_gt: $after}}, "
    "order_by: {id: asc}, limit: $limit)"
)
_LISTS_PAGE = (
    "lists(where: {user_id: {_eq: $user_id}, id: {_gt: $lists_after}}, "
    "order_by: {id: asc}, limit: $limit) "
    "{ id name list_books(order_by: {id: asc}, limit: $limit) { id book_id } }"
)

LIBRARY = f"""
query Library($user_id: Int!, $limit: Int!, $after: Int!, $lists_after: Int!) {{
  {_USER_BOOKS_PAGE} {{{_USER_BOOK_FIELDS}  }}
  {_LISTS_PAGE}
}}
"""
"""The library query of §3.3.1: the first page of user_books, plus the first of the lists."""

LIBRARY_MORE = f"""
query LibraryMore($user_id: Int!, $limit: Int!, $after: Int!) {{
  {_USER_BOOKS_PAGE} {{{_USER_BOOK_FIELDS}  }}
}}
"""
"""Later pages of user_books alone, so a page past the first costs 1 rather than 2."""

LISTS_MORE = f"""
query ListsMore($user_id: Int!, $limit: Int!, $lists_after: Int!) {{
  {_LISTS_PAGE}
}}
"""
"""Later pages of lists, for a user with more than 1000 (cost 1)."""

LIST_BOOKS_MORE = """
query ListBooksMore($list_id: Int!, $limit: Int!, $after: Int!) {
  list_books(where: {list_id: {_eq: $list_id}, id: {_gt: $after}},
             order_by: {id: asc}, limit: $limit) { id book_id }
}
"""
"""Later pages of one list's books, for a list of more than 1000 (cost 1)."""


class HardcoverError(Exception):
    """Any failed Hardcover call. The message is safe to log and store (§2.7)."""


class HardcoverUnavailable(HardcoverError):
    """Transient: a network failure, a timeout, an undecodable response, a 408 or a 5xx.
    Safe to retry later."""


class HardcoverHTTPError(HardcoverError):
    """A non-200 transport error (``{"error": …}``): bad auth, a refused operator, and so on."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Hardcover returned HTTP {status}: {message}")
        self.status = status


class HardcoverRateLimited(HardcoverHTTPError):
    """A 429. ``rate`` carries ``Retry-After`` and ``Ratelimit-Reset`` for the limiter."""

    def __init__(self, rate: RateLimit) -> None:
        super().__init__(429, "rate limited")
        self.rate = rate


class HardcoverCapacityError(HardcoverHTTPError):
    """``403 request_exceeds_capacity``: a query asked for too many top-level fields. A bug in
    SyncShelf, never retried (§3.3.3)."""


class HardcoverQueryError(HardcoverError):
    """A 200 whose body carries GraphQL ``errors``: the query itself is wrong. A bug."""


class HardcoverMutationError(HardcoverError):
    """A mutation answered 200 with its own ``error`` field set: Hardcover refused the write
    (for example a rating off the half-star grid) and left the row unchanged."""

    def __init__(self, mutation: str, message: str) -> None:
        super().__init__(f"{mutation} was refused: {message}")
        self.mutation = mutation


@dataclass(frozen=True)
class RateLimit:
    """The rate-limit headers of one response, each ``None`` when absent or unreadable.

    ``reset`` and ``daily_reset`` are epoch seconds; ``retry_after`` and ``ratelimit_reset``
    are seconds from now, as Hardcover sends them on a 429.
    """

    remaining: int | None = None
    limit: int | None = None
    reset: float | None = None
    daily_remaining: int | None = None
    daily_limit: int | None = None
    daily_reset: float | None = None
    retry_after: float | None = None
    ratelimit_reset: float | None = None

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimit:
        """Read the ``X-Ratelimit-*``, ``X-Ratelimit-Daily-*``, ``Retry-After`` and
        ``Ratelimit-Reset`` headers. ``headers`` must look names up case-insensitively."""
        return cls(
            remaining=_int(headers.get("x-ratelimit-remaining")),
            limit=_int(headers.get("x-ratelimit-limit")),
            reset=_float(headers.get("x-ratelimit-reset")),
            daily_remaining=_int(headers.get("x-ratelimit-daily-remaining")),
            daily_limit=_int(headers.get("x-ratelimit-daily-limit")),
            daily_reset=_float(headers.get("x-ratelimit-daily-reset")),
            retry_after=_float(headers.get("retry-after")),
            ratelimit_reset=_float(headers.get("ratelimit-reset")),
        )


@dataclass(frozen=True)
class Result:
    """A successful call: the ``data`` object, what the call cost and the rate headers."""

    data: dict[str, Any]
    cost: int
    rate: RateLimit


@dataclass
class Library:
    """A profile's whole library, as the library query returns it, every page joined."""

    user_books: list[dict[str, Any]] = field(default_factory=list)
    lists: list[dict[str, Any]] = field(default_factory=list)
    cost: int = 0
    """The tokens the fetch spent, summed over its pages."""
    rate: RateLimit = field(default_factory=RateLimit)
    """The headers of the last page."""


class Gate(Protocol):
    """What paces a client's calls: the profile's limiter (``syncshelf.sync.limiter``)."""

    async def run(self, cost: int, send: Callable[[], Awaitable[Result]]) -> Result: ...


class HardcoverClient:
    """One profile's connection to Hardcover.

    ``token`` is the PAT without ``Bearer ``; the client adds the prefix. ``gate`` is the
    profile's limiter, which every call goes through. It must be passed, so an unthrottled
    client is always a visible choice: ``gate=None`` is for tests of the client alone.
    ``transport`` is for tests, which route requests to an in-memory fake (§2.9).
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        gate: Gate | None,
        transport: httpx2.AsyncBaseTransport | None = None,
        timeout: float = TIMEOUT,
    ) -> None:
        self._url = url
        self._gate = gate
        self._http = httpx2.AsyncClient(
            transport=transport,
            timeout=timeout,
            headers={"authorization": f"Bearer {token}", "user-agent": USER_AGENT},
        )
        self._me_id: int | None = None

    async def aclose(self) -> None:
        """Close the underlying connection pool."""
        await self._http.aclose()

    async def execute(self, query: str, variables: Mapping[str, Any] | None = None) -> Result:
        """Send one GraphQL operation through the gate and return its ``data``.

        Raises :class:`HardcoverRateLimited` on a 429, :class:`HardcoverCapacityError` on
        ``403 request_exceeds_capacity``, :class:`HardcoverHTTPError` on any other non-200,
        :class:`HardcoverUnavailable` on a transient failure, and :class:`HardcoverQueryError`
        when the body carries GraphQL ``errors``, plus whatever the gate raises when the call
        can't go out. Mutation ``error`` fields are left for :func:`check_mutation`, since only
        the caller knows which fields are mutations.
        """
        cost = query_cost(query)
        if self._gate is None:
            return await self._send(query, variables, cost)
        return await self._gate.run(cost, lambda: self._send(query, variables, cost))

    async def _send(self, query: str, variables: Mapping[str, Any] | None, cost: int) -> Result:
        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = dict(variables)
        started = time.monotonic()
        try:
            response = await self._http.post(self._url, json=payload)
        except httpx2.RequestError as exc:
            raise HardcoverUnavailable(f"Hardcover is unreachable ({type(exc).__name__})") from None
        ms = round((time.monotonic() - started) * 1000)
        log.debug(
            "POST %s %d",
            "/v1/graphql",
            response.status_code,
            extra={
                "event": "http_request",
                "service": "hardcover",
                "method": "POST",
                "status": response.status_code,
                "ms": ms,
            },
        )
        rate = RateLimit.from_headers(response.headers)
        status = response.status_code
        if status == 429:
            raise HardcoverRateLimited(rate)
        if status == 408 or status >= 500:
            raise HardcoverUnavailable(f"Hardcover returned HTTP {status}")
        if status != 200:
            message = _error_message(response)
            if status == 403 and "request_exceeds_capacity" in message:
                raise HardcoverCapacityError(status, message)
            raise HardcoverHTTPError(status, message)
        try:
            body = response.json()
        except ValueError:
            raise HardcoverUnavailable("Hardcover returned a body that is not JSON") from None
        if not isinstance(body, dict):
            raise HardcoverUnavailable("Hardcover returned an unexpected body")
        if body.get("errors"):
            raise HardcoverQueryError(_graphql_errors(body["errors"]))
        data = body.get("data")
        if not isinstance(data, dict):
            raise HardcoverQueryError("Hardcover returned no data")
        return Result(data=data, cost=cost, rate=rate)

    async def me_id(self) -> int:
        """The token owner's Hardcover user id, fetched once and then cached.

        Every query on user data filters by it, because Hardcover still lets a token read
        other users' rows (§3.3.1, "Own data only").
        """
        if self._me_id is not None:
            return self._me_id
        result = await self.execute(ME)
        me = result.data.get("me")
        # `me` is a list of one.
        if (
            not isinstance(me, list)
            or len(me) != 1
            or not isinstance(me[0], dict)
            or not isinstance(me[0].get("id"), int)
        ):
            raise HardcoverQueryError("Hardcover returned an unexpected me")
        user_id: int = me[0]["id"]
        self._me_id = user_id
        return user_id

    async def library(self, page_size: int = LIBRARY_PAGE) -> Library:
        """Fetch the whole library: every user_book with its reads, editions and book, plus
        the user's lists and their books.

        Every list field is paged past Hardcover's silent 1000-row cap by keyset (``id``
        after the last one seen, #103), so a row added or removed between pages can't make
        another be skipped or repeated. A short page means the end, so ``page_size``
        (smaller only in tests) is clamped to the cap: a larger request would come back cut
        to 1000 rows and look like the last page.
        """
        page_size = min(page_size, LIBRARY_PAGE)
        user_id = await self.me_id()
        library = Library()

        async def run(query: str, variables: dict[str, Any]) -> dict[str, Any]:
            result = await self.execute(query, {"user_id": user_id, "limit": page_size} | variables)
            library.cost += result.cost
            library.rate = result.rate
            return result.data

        data = await run(LIBRARY, {"after": 0, "lists_after": 0})
        page = data.get("user_books") or []
        lists = data.get("lists") or []
        library.user_books.extend(page)
        while len(page) == page_size:
            page = (await run(LIBRARY_MORE, {"after": page[-1]["id"]})).get("user_books") or []
            library.user_books.extend(page)
        more_lists = lists
        while len(more_lists) == page_size:
            more_lists = (await run(LISTS_MORE, {"lists_after": more_lists[-1]["id"]})).get(
                "lists"
            ) or []
            lists.extend(more_lists)
        for lst in lists:
            books = lst.get("list_books") or []
            more = books
            while len(more) == page_size:
                variables = {"list_id": lst["id"], "after": more[-1]["id"]}
                more = (await run(LIST_BOOKS_MORE, variables)).get("list_books") or []
                books.extend(more)
            lst["list_books"] = books
        library.lists = lists
        return library


def check_mutation(data: Mapping[str, Any], mutation: str) -> dict[str, Any]:
    """Return ``data[mutation]``, raising :class:`HardcoverMutationError` when Hardcover set
    its ``error`` field. Hardcover answers a refused write with 200, so every mutation result
    goes through this (§3.3.1, "Errors")."""
    result = data.get(mutation)
    if not isinstance(result, dict):
        raise HardcoverQueryError(f"Hardcover returned no {mutation} result")
    error = result.get("error")
    if error:
        raise HardcoverMutationError(mutation, str(error))
    return result


# --- cost -------------------------------------------------------------------------------

_TOKEN = re.compile(
    r'"""(?:[^"]|"(?!""))*"""'  # block string
    r'|"(?:[^"\\]|\\.)*"'  # string
    r"|#[^\n]*"  # comment
    r"|[A-Za-z_][A-Za-z0-9_]*"  # name
    r"|\.\.\."  # spread
    r"|[{}()\[\]:]"
    r"|\S",
)


def query_cost(query: str) -> int:
    """Count the top-level fields of a GraphQL operation: what Hardcover charges for it.

    Each top-level field costs one token, and more than 10 is refused outright (§3.3.1). An
    alias counts as its own field. Arguments and nested selections are skipped.
    """
    tokens = [t for t in _TOKEN.findall(query) if not t.startswith("#")]
    # Skip the operation header (`query Name($v: T!)`) up to the first top-level brace.
    i = 0
    parens = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "(":
            parens += 1
        elif t == ")":
            parens -= 1
        elif t == "{" and parens == 0:
            break
        i += 1
    else:
        raise ValueError("not a GraphQL operation: no selection set")

    count = 0
    depth = 0
    parens = 0
    expect_field = True
    skip = False  # the next name isn't a field: a spread's fragment or type, a directive
    for t in tokens[i:]:
        if skip and (t[0].isalpha() or t[0] == "_"):
            # `... on Type`: pass over `on`, then still skip the type.
            skip = t == "on"
            continue
        if t in ("...", "@"):
            # `...Fragment`, `... on Type { }` and `@directive` name no field. A named
            # fragment's own fields go uncounted, so SyncShelf's queries don't use them.
            skip = True
            continue
        if t == "(":
            parens += 1
        elif t == ")":
            parens -= 1
        elif parens:
            continue
        elif t == "{":
            depth += 1
            expect_field = True
        elif t == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and t == ":":
            # `alias: field`: the alias was already counted as the field.
            expect_field = False
        elif depth == 1 and (t[0].isalpha() or t[0] == "_"):
            if expect_field:
                count += 1
            expect_field = True
    return count


# --- helpers ----------------------------------------------------------------------------


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _error_message(response: httpx2.Response) -> str:
    """The ``error`` of a transport-error body, else a generic line. Never the raw body.

    The error may be a string or an object; an object gives its code and message both, so a
    code such as ``request_exceeds_capacity`` survives whatever its message says.
    """
    try:
        body = response.json()
    except ValueError:
        return "no detail"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        parts = [str(error[k]) for k in ("code", "message") if error.get(k)]
        error = ": ".join(parts)
    return str(error)[:200] if error else "no detail"


def _graphql_errors(errors: Any) -> str:
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        first = errors[0]
        code = (first.get("extensions") or {}).get("code")
        message = str(first.get("message", ""))[:200]
        return f"Hardcover refused the query ({code}): {message}" if code else message
    return "Hardcover refused the query"
