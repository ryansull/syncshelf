"""``FakeHardcover``: an in-memory Hardcover behind an ``httpx2.MockTransport`` (SPEC §2.9).

It answers only the operations SyncShelf sends, keyed by GraphQL operation name, from state a
test sets up with :meth:`FakeHardcover.add_user` and :meth:`FakeHardcover.add_user_book`. Rows
have exactly the shape the library query returns, which the contract test checks against the
recorded fixture.

Traps reproduced so far (§3.3.10): the silent 1000-row cap on every list field, 429 with
``Retry-After``, and a 200 whose mutation result carries ``error``. Later tickets add the write
traps (auto "today" reads, upsert resetting omitted fields, journals left by a delete, lagging
aggregates).
"""

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from itertools import count
from pathlib import Path
from typing import Any

import httpx2

URL = "https://api.hardcover.app/v1/graphql"
ROW_CAP = 1000
"""Hardcover returns at most this many rows for any list field, without saying so."""

_OPERATION = re.compile(r"\b(?:query|mutation)\s+([A-Za-z_][A-Za-z0-9_]*)")

FAULTS = ("rate_limited", "capacity", "unavailable", "timeout", "validation")
"""What :meth:`FakeHardcover.fail_next` can inject."""


@dataclass
class _Fault:
    kind: str
    retry_after: int
    message: str
    user_id: int | None = None
    after: int = 0
    """Matching requests still to let through before this one fails."""


class FakeHardcover:
    """A stateful Hardcover for one or more users, each reached by their own token."""

    def __init__(self) -> None:
        self.tokens: dict[str, int] = {}
        self.user_books: dict[int, list[dict[str, Any]]] = {}
        self.lists: dict[int, list[dict[str, Any]]] = {}
        self.goals: dict[int, list[dict[str, Any]]] = {}
        self.series: dict[int, dict[str, Any]] = {}
        """Series by id, each ``{id, name, book_series: [{position, book}]}``; see
        :meth:`add_series`."""
        self.search_index: list[dict[str, Any]] = []
        """Typesense documents ``Search`` answers from, in relevance order; see
        :meth:`add_search_doc`."""
        self.catalog: dict[int, dict[str, Any]] = {}
        """Every book the fake knows, by id, with the fields ``Book`` answers beyond the
        library query's. Shelving a book adds it."""
        self.requests: list[tuple[str, dict[str, Any]]] = []
        """Each request's operation name and variables, in order."""
        self.callers: list[int] = []
        """The user id whose token sent each request, in the same order."""
        self.daily_remaining = 5000
        self._faults: dict[str, list[_Fault]] = {}
        self._ids = count(1)

    @property
    def transport(self) -> httpx2.MockTransport:
        """The transport to hand a ``HardcoverClient``."""
        return httpx2.MockTransport(self.handle)

    # --- state ----------------------------------------------------------------------------

    def add_user(self, token: str, user_id: int | None = None) -> int:
        """Register a user reachable with ``token``; returns its user id."""
        uid = user_id if user_id is not None else 1000 + len(self.tokens)
        self.tokens[token] = uid
        self.user_books.setdefault(uid, [])
        self.lists.setdefault(uid, [])
        return uid

    def add_user_book(self, user_id: int, book_id: int, **fields: Any) -> dict[str, Any]:
        """Shelve ``book_id`` for ``user_id`` and return the row, in library-query shape.

        ``fields`` override top-level user_book fields; ``book=``, ``edition=`` and
        ``user_book_reads=`` are merged into the defaults.
        """
        row = user_book_row(next(self._ids), book_id)
        if (known := self.catalog.get(book_id)) is not None:
            # A book already in the catalog keeps its details on every shelf it's added to.
            row["book"] = {k: known.get(k, v) for k, v in row["book"].items()}
        for key in ("book", "edition"):
            if key in fields:
                override = fields.pop(key)
                row[key] = None if override is None else {**row[key], **override}
        if "user_book_reads" in fields:
            row["user_book_reads"] = [read_row(**r) for r in fields.pop("user_book_reads")]
        row.update(fields)
        self.user_books[user_id].append(row)
        if row["book"] is not None:
            self.catalog[book_id] = {
                **book_extras(),
                **self.catalog.get(book_id, {}),
                **row["book"],
            }
        return row

    def add_series(
        self, series_id: int, name: str, entries: list[tuple[float | None, int, str]], **more: Any
    ) -> None:
        """A series and its ``book_series`` rows: ``(position, book_id, title)`` each, with
        ``more`` giving ``users_count`` / ``compilation`` per book id (``{book_id: value}``)."""
        users = more.get("users_count", {})
        compilations = more.get("compilation", {})
        self.series[series_id] = {
            "id": series_id,
            "name": name,
            "book_series": [
                {
                    "position": position,
                    "book": {
                        "id": book_id,
                        "title": title,
                        "users_count": users.get(book_id, 100),
                        "compilation": compilations.get(book_id, False),
                    },
                }
                for position, book_id, title in entries
            ],
        }

    def add_search_doc(self, book_id: int, title: str, **fields: Any) -> dict[str, Any]:
        """Index a book for ``Search``, in the recorded document's exact shape: every key
        Hardcover sends, with ``fields`` over the template's scrubbed values."""
        doc = copy.deepcopy(_search_template()["document"])
        defaults = {
            "author_names": [],
            "genres": [],
            "series_names": [],
            "users_count": 0,
            "rating": 0.0,
            "featured_series": None,
            "subtitle": None,
            "description": None,
        }
        doc.update({**defaults, **fields, "id": str(book_id), "title": title})
        self.search_index.append(doc)
        return doc

    def add_book(self, book_id: int, **fields: Any) -> dict[str, Any]:
        """Put an unshelved book in the catalog, with ``fields`` over the defaults."""
        book = {**user_book_row(0, book_id)["book"], **book_extras(), **fields}
        self.catalog[book_id] = book
        return book

    def add_list(self, user_id: int, name: str, book_ids: list[int]) -> None:
        list_books = [{"id": next(self._ids), "book_id": b} for b in book_ids]
        self.lists[user_id].append({"id": next(self._ids), "name": name, "list_books": list_books})

    def fail_next(
        self,
        operation: str,
        kind: str,
        *,
        retry_after: int = 7,
        message: str = "refused",
        user_id: int | None = None,
        after: int = 0,
    ) -> None:
        """Make the next request for ``operation`` fail with ``kind`` (one of :data:`FAULTS`),
        from any user, or only from ``user_id``; with ``after``, that many succeed first.

        ``validation`` answers 200 with ``message`` in the mutation result's ``error``.
        """
        if kind not in FAULTS:
            raise ValueError(f"unknown fault {kind!r}")
        fault = _Fault(kind, retry_after, message, user_id, after)
        self._faults.setdefault(operation, []).append(fault)

    # --- transport ------------------------------------------------------------------------

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        auth = request.headers.get("authorization")
        if auth is None:
            return _error(400, "Unable to verify token")
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return _error(400, "Use 'Bearer <token>'")
        user_id = self.tokens.get(token)
        if user_id is None:
            return _error(401, "invalid_token")

        payload = json.loads(request.content)
        query: str = payload["query"]
        variables: dict[str, Any] = payload.get("variables") or {}
        match = _OPERATION.search(query)
        operation = match.group(1) if match else ""
        self.requests.append((operation, variables))
        self.callers.append(user_id)

        faults = self._faults.get(operation, [])
        fault = next((f for f in faults if f.user_id in (None, user_id)), None)
        if fault is not None and fault.after:
            fault.after -= 1
            fault = None
        if fault is not None:
            faults.remove(fault)
            if fault.kind == "timeout":
                raise httpx2.ReadTimeout("timed out", request=request)
            if fault.kind == "rate_limited":
                return httpx2.Response(
                    429,
                    json={"error": "Throttled"},
                    headers={
                        "retry-after": str(fault.retry_after),
                        "ratelimit-reset": str(fault.retry_after),
                    },
                )
            if fault.kind == "capacity":
                return _error(403, "request_exceeds_capacity")
            if fault.kind == "unavailable":
                return _error(503, "Service Unavailable")
            # validation: a 200 whose (only) top-level field carries the error.
            field = _first_field(query)
            return self._ok({field: {"id": None, "error": fault.message}})

        handler = self._handlers().get(operation)
        if handler is None:
            raise AssertionError(f"FakeHardcover has no handler for operation {operation!r}")
        return self._ok(handler(user_id, variables))

    def _handlers(self) -> dict[str, Callable[[int, dict[str, Any]], dict[str, Any]]]:
        return {
            "Me": lambda uid, _v: {"me": [{"id": uid}]},
            "Goals": lambda uid, _v: {"me": [{"goals": self.goals.get(uid, [])}]},
            "Library": self._library,
            "LibraryMore": self._library_more,
            "ListsMore": lambda uid, v: {"lists": self._lists_page(uid, v)},
            "ListBooksMore": self._list_books_more,
            "Book": self._book,
            "Shelf": lambda uid, v: {"user_books": self._shelf(uid, v)},
            "Similar": self._similar,
            "Search": self._search,
            "Series": lambda _u, v: {
                "series": [self.series[i] for i in sorted(v["ids"]) if i in self.series]
            },
            "SeriesByName": lambda _u, v: {
                "series": [s for s in self.series.values() if s["name"] == v["name"]][:5]
            },
        }

    def _ok(self, data: dict[str, Any]) -> httpx2.Response:
        self.daily_remaining -= 1
        return httpx2.Response(
            200,
            json={"data": data},
            headers={
                "x-ratelimit-limit": "10",
                "x-ratelimit-remaining": "9",
                "x-ratelimit-daily-limit": "5000",
                "x-ratelimit-daily-remaining": str(self.daily_remaining),
                "x-ratelimit-daily-reset": "4102531200",
            },
        )

    @staticmethod
    def _page(rows: list[dict[str, Any]], after: int, limit: int) -> list[dict[str, Any]]:
        """Rows with ``id`` after ``after``, by id, at most ``limit`` and never past the
        silent cap: a keyset page."""
        later = sorted((r for r in rows if r["id"] > after), key=lambda r: r["id"])
        return later[: min(limit, ROW_CAP)]

    def _user_books_page(self, user_id: int, variables: dict[str, Any]) -> list[dict[str, Any]]:
        assert variables["user_id"] == user_id, "a query must filter by the caller's own id"
        return self._page(self.user_books[user_id], variables["after"], variables["limit"])

    def _lists_page(self, user_id: int, variables: dict[str, Any]) -> list[dict[str, Any]]:
        assert variables["user_id"] == user_id, "a query must filter by the caller's own id"
        lists = self._page(self.lists[user_id], variables["lists_after"], variables["limit"])
        # Each list's books are a list field too, capped the same way.
        return [
            {**lst, "list_books": self._page(lst["list_books"], 0, variables["limit"])}
            for lst in lists
        ]

    def _list_books_more(self, user_id: int, variables: dict[str, Any]) -> dict[str, Any]:
        lst = next(x for x in self.lists[user_id] if x["id"] == variables["list_id"])
        return {"list_books": self._page(lst["list_books"], variables["after"], variables["limit"])}

    def _library(self, user_id: int, variables: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_books": self._user_books_page(user_id, variables),
            "lists": self._lists_page(user_id, variables),
        }

    def _library_more(self, user_id: int, variables: dict[str, Any]) -> dict[str, Any]:
        return {"user_books": self._user_books_page(user_id, variables)}

    def _shelf(self, user_id: int, variables: dict[str, Any]) -> list[dict[str, Any]]:
        assert variables["user_id"] == user_id, "a query must filter by the caller's own id"
        rows = [r for r in self.user_books[user_id] if r["book_id"] == variables["book_id"]]
        return [{k: v for k, v in r.items() if k != "book"} for r in rows]

    def _book(self, user_id: int, variables: dict[str, Any]) -> dict[str, Any]:
        book = self.catalog.get(variables["book_id"])
        return {"books": [book] if book else [], "user_books": self._shelf(user_id, variables)}

    def _search(self, _user_id: int, variables: dict[str, Any]) -> dict[str, Any]:
        """Typesense, roughly: a word match on title or author unless the query is ``*``,
        ``author_names`` / ``genres`` filters joined by ``&&``, an optional sort, pages."""
        docs = list(self.search_index)
        q = variables["q"].strip().casefold()
        if q != "*":
            words = q.split()
            text = {id(d): " ".join([d["title"], *d["author_names"]]).casefold() for d in docs}
            docs = [d for d in docs if all(w in text[id(d)] for w in words)]
        for clause in (variables.get("filter") or "").split("&&"):
            if m := re.fullmatch(r"\s*(author_names|genres):=\[`([^`]*)`\]\s*", clause):
                docs = [d for d in docs if m.group(2) in d[m.group(1)]]
            elif clause.strip():
                raise AssertionError(f"FakeHardcover can't filter by {clause!r}")
        if sort := variables.get("sort"):
            field, _, direction = sort.partition(":")
            docs.sort(key=lambda d: d.get(field) or 0, reverse=direction == "desc")
        per, page = min(variables["per"], 25), variables["page"]
        shown = docs[(page - 1) * per : page * per]
        hits = []
        for d in shown:
            hit = copy.deepcopy(_search_template())
            hit["document"] = d
            hits.append(hit)
        results = {**_search_results(), "found": len(docs), "page": page, "hits": hits}
        return {"search": {"ids": [int(d["id"]) for d in shown], "results": results}}

    def _similar(self, _user_id: int, variables: dict[str, Any]) -> dict[str, Any]:
        # Hardcover returns an _in match in its own order, not the order asked for.
        fields = ("id", "title", "release_year", "rating", "cached_contributors")
        found = sorted(i for i in variables["ids"] if i in self.catalog)
        return {"books": [{f: self.catalog[i][f] for f in fields} for i in found]}


def user_book_row(user_book_id: int, book_id: int) -> dict[str, Any]:
    """A shelved book with no reads, in exactly the library query's shape."""
    return {
        "id": user_book_id,
        "book_id": book_id,
        "edition_id": book_id * 10,
        "status_id": 1,
        "rating": None,
        "has_review": False,
        "review_raw": None,
        "review_has_spoilers": False,
        "date_added": "2026-01-01",
        "first_started_reading_date": None,
        "privacy_setting_id": 3,
        "updated_at": "2026-01-01T00:00:00+00:00",
        "user_book_reads": [],
        "edition": {
            "id": book_id * 10,
            "reading_format_id": 1,
            "pages": 300,
            "audio_seconds": None,
            "isbn_13": None,
            "isbn_10": None,
            "asin": None,
        },
        "book": {
            "id": book_id,
            "title": f"Book {book_id}",
            "subtitle": None,
            "release_year": 2020,
            "pages": 300,
            "canonical_id": None,
            "cached_contributors": [
                {"author": {"id": 1, "name": "An Author"}, "contribution": "Author"}
            ],
            "cached_featured_series": {},
            "genres": [],
            "moods": [],
            "tags": [],
            "default_audio_edition": None,
            "default_ebook_edition": None,
            "default_physical_edition": {"id": book_id * 10, "pages": 300},
        },
    }


_FIXTURES = Path(__file__).parents[1] / "fixtures" / "hardcover"


@cache
def _recorded_search() -> dict[str, Any]:
    return json.loads((_FIXTURES / "search.json").read_text(encoding="utf-8"))["body"]


def _search_template() -> dict[str, Any]:
    """A recorded hit: every key Hardcover sends with one, scrubbed."""
    return _recorded_search()["data"]["search"]["results"]["hits"][0]


def _search_results() -> dict[str, Any]:
    """The recorded ``results`` envelope, without its hits."""
    results = _recorded_search()["data"]["search"]["results"]
    return {k: v for k, v in results.items() if k != "hits"}


def book_extras() -> dict[str, Any]:
    """The fields ``Book`` answers beyond the library query's book, empty by default."""
    return {
        "rating": None,
        "cached_similar_book_ids": [],
        "book_series": [],
        "warnings": [],
        "narrated": None,
    }


def read_row(
    id: int,
    started_at: str | None = None,
    finished_at: str | None = None,
    edition_id: int | None = None,
    reading_format_id: int | None = None,
) -> dict[str, Any]:
    """One user_book_read, in the library query's shape."""
    return {
        "id": id,
        "started_at": started_at,
        "finished_at": finished_at,
        "paused_at": None,
        "edition_id": edition_id,
        "progress_pages": None,
        "progress_seconds": None,
        "edition": None if edition_id is None else {"reading_format_id": reading_format_id},
    }


def _error(status: int, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={"error": message})


def _first_field(query: str) -> str:
    body = query[query.index("{") + 1 :]
    return re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", body).group(1)  # type: ignore[union-attr]
