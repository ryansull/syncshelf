"""The read tools over a profile's Hardcover library (SPEC §3.3.7, §3.3.8).

``get_my_library`` reads the mirror (§3.3.4), refreshed first when it's older than the TTL,
and answers in compact **library rows** of a fixed shape, so a page of 25 stays small in
Claude's context. Every answer carries ``as_of``, and ``stale`` with a reason when the refresh
failed and the copy was served anyway.

Reads without a format are reported in an explicit ``unknown`` bucket, never guessed (§3.3.2).
"""

import base64
import hashlib
import json
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from syncshelf.sync.mirror import Freshness, NoMirror
from syncshelf.tools import HOUSEHOLD, ME, REGISTRY, current_profile, resolve_profiles, services

__all__ = ["FORMATS", "SORTS", "STATUSES", "LibraryPage", "LibraryRow", "get_my_library"]

STATUSES: dict[int, str] = {
    1: "want-to-read",
    2: "currently-reading",
    3: "read",
    4: "paused",
    5: "did-not-finish",
    6: "ignored",
}
"""Hardcover's status ids and their slugs (§3.3.1)."""

FORMATS: dict[int, tuple[str, ...]] = {
    1: ("print",),
    2: ("audio",),
    3: ("audio", "print"),
    4: ("ebook",),
}
"""``reading_format_id`` → the format buckets a read counts in. 3 is Hardcover's "Both", which
counts as print and audio until #108 decides. Anything else, and NULL, is ``unknown``."""

UNKNOWN = "unknown"
TOP_GENRES = 3

Status = Literal["want-to-read", "currently-reading", "read", "paused", "did-not-finish", "ignored"]
Format = Literal["audio", "ebook", "print", "unknown"]
Sort = Literal["date_added", "last_finished", "rating", "title", "author"]


class Series(TypedDict):
    name: str
    position: float | None


class LibraryRow(TypedDict):
    """One shelved book, compactly (§3.3.8)."""

    book_id: int
    title: str
    author: str | None
    status: str
    rating: float | None
    formats: list[str]
    """The formats of its reads; ``unknown`` for a read Hardcover has no format for."""
    last_finished: str | None
    read_count: int
    series: Series | None
    genres: list[str]
    profile: NotRequired[str]
    """Whose shelf the row is from, on ``household`` answers."""


class LibraryPage(TypedDict):
    """A page of library rows, with what the whole filtered set holds."""

    books: list[LibraryRow]
    total: int
    next_cursor: str | None
    counts_by_status: NotRequired[dict[str, int]]
    as_of: str
    stale: bool
    stale_reason: NotRequired[str]


@dataclass(frozen=True)
class _Read:
    finished_on: str | None
    formats: tuple[str, ...]


@dataclass(frozen=True)
class _Book:
    """A library row plus what filtering and sorting need beyond it."""

    user_book_id: int
    date_added: str | None
    row: LibraryRow
    series_id: int | None = None
    authors: tuple[str, ...] = ()
    contributors: tuple[str, ...] = ()
    all_genres: tuple[str, ...] = ()
    reads: tuple[_Read, ...] = field(default=())


type _Key = tuple[int, str | float, int]
"""``(rank, value, user_book_id)``: ``rank`` puts a book missing the value after every book
that has one, whichever way the sort runs; ties go by Hardcover's user_book id."""


def _high_first(value: str | float | None, empty: str | float, book: _Book) -> _Key:
    return (0, empty, book.user_book_id) if value is None else (1, value, book.user_book_id)


def _low_first(value: str | float | None, empty: str | float, book: _Book) -> _Key:
    return (1, empty, book.user_book_id) if value is None else (0, value, book.user_book_id)


SORTS: dict[str, tuple[Callable[[_Book], _Key], bool]] = {
    # name: (key, descending)
    "date_added": (lambda b: _high_first(b.date_added, "", b), True),
    "last_finished": (lambda b: _high_first(b.row["last_finished"], "", b), True),
    "rating": (lambda b: _high_first(b.row["rating"], 0.0, b), True),
    "title": (lambda b: _low_first(b.row["title"].casefold() or None, "", b), False),
    "author": (lambda b: _low_first((b.row["author"] or "").casefold() or None, "", b), False),
}
"""Each ``sort`` value: its key, and whether it runs high to low [assembly choice]."""


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_my_library(
    status: Status | None = None,
    query: str | None = None,
    author: str | None = None,
    genre: str | None = None,
    series: str | None = None,
    format: Format | None = None,  # noqa: A002 - the spec's argument name
    min_rating: Annotated[float, Field(ge=0.5, le=5)] | None = None,
    finished_from: date | None = None,
    finished_to: date | None = None,
    sort: Sort = "date_added",
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
    cursor: str | None = None,
    profile: str = ME,
) -> LibraryPage:
    """List books on Hardcover shelves, the caller's by default, filtered and sorted.

    Use this for questions about the user's own library: what they're currently reading,
    what's on their want-to-read (by author, in a genre), what they read in a period, what
    they rated highest last year, and how many of each. For one book's full record (reads with
    dates, review, series, tags) use get_book instead. Every filter is optional, and filters
    combine (all must match).

    Arguments:
    - status: one shelf, as a slug: want-to-read, currently-reading, read, paused,
      did-not-finish or ignored.
    - query: text found in the title or any contributor's name (case-insensitive).
    - author: text found in an author's name (not a narrator's).
    - genre: one of the book's Hardcover genres, exactly but case-insensitive (e.g. Fantasy).
    - series: text found in the featured series' name, or its Hardcover series id.
    - format: audio, ebook, print, or unknown: books with at least one read in that format.
    - min_rating: books rated at least this (0.5–5). Unrated books are left out.
    - finished_from, finished_to: ISO dates (inclusive): books with a read finished in that
      range. Books with no dated read never match.
    - sort: date_added (newest first, the default), last_finished (most recent first),
      rating (highest first), title or author (A–Z). Books missing the value come last.
      "Highest rated last year" is finished_from/finished_to for that year with sort=rating.
    - limit: rows per page, 1 to 100 (default 25).
    - cursor: the next_cursor from the previous page, with the same filters and sort.
    - profile: "me" (the default), another profile's name, or "household" for the caller's
      shelves and those of every profile that shares its books. Use it for "what have we both
      read?" or "what did my partner think of X?". On household answers each row has a
      profile field saying whose shelf it's from, and a book both have is two rows. A profile
      that hasn't opted in to sharing is refused. Status, rating, dates and formats are
      shared; reviews never are.

    Each row has book_id, title, author (the first author), status, rating (0.5–5 in half
    stars, or null), formats (of its reads: audio, ebook, print, or unknown when Hardcover
    has no format for a read), last_finished (ISO date, or null), read_count, series (name
    and position, or null) and genres (top 3). Many books shelved as read have no reads
    logged, so read_count 0 and last_finished null are common; say so rather than guess.

    The result has total (books matching, across all pages) and next_cursor (null on the
    last page). When total is more than one page, counts_by_status gives the count for each
    shelf over everything that matched, so counting questions need no paging. Answers come
    from SyncShelf's copy of the library, refreshed when older than a few minutes: as_of says
    when it was taken, and stale is true (with stale_reason) when Hardcover couldn't be reached
    and an older copy was used. An edit just made in Hardcover itself may not show yet.
    """
    caller = current_profile()
    if finished_from and finished_to and finished_from > finished_to:
        raise ToolError("finished_from is after finished_to; give the earlier date first.")
    wanted = _Filters(
        status=status,
        query=_fold(query),
        author=_fold(author),
        genre=_fold(genre),
        series=_fold(series),
        format=format,
        min_rating=min_rating,
        finished_from=finished_from.isoformat() if finished_from else None,
        finished_to=finished_to.isoformat() if finished_to else None,
        profile=profile,
    )
    after = _after(cursor, sort, wanted.digest())
    bound = services()
    # Refused here, before any refresh, when a named profile hasn't opted in (§3.3.4).
    names = resolve_profiles(profile, caller, bound.profiles)
    freshness: dict[str, Freshness] = {}
    books: list[_Book] = []
    for name in names:
        try:
            # Each profile's copy refreshes with that profile's own token.
            freshness[name] = await bound.mirror.fresh(name)
        except NoMirror as exc:
            whose = "" if name == caller else f"For {name}: "
            raise ToolError(whose + str(exc)) from None
        shelf = await bound.db.read(_library, name)
        if profile == HOUSEHOLD:
            shelf = [replace(b, row=_tagged(b.row, name)) for b in shelf]
        books += shelf
    books = [b for b in books if wanted.match(b)]
    key, descending = SORTS[sort]
    books.sort(key=key, reverse=descending)
    # The cursor is the last row's sort key, not an offset, so a refresh between pages
    # (books added or removed) never repeats or skips a row.
    if after is None:
        rest = books
    elif descending:
        rest = [b for b in books if key(b) < after]
    else:
        rest = [b for b in books if key(b) > after]
    page = rest[:limit]
    result = LibraryPage(
        books=[b.row for b in page],
        total=len(books),
        next_cursor=_cursor(sort, wanted.digest(), key(page[-1])) if len(rest) > limit else None,
        **_freshness(freshness),
    )
    if len(books) > len(page):
        counts = Counter(b.row["status"] for b in books)
        result["counts_by_status"] = {s: counts[s] for s in STATUSES.values() if counts[s]}
    return result


# --- filtering ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Filters:
    status: str | None
    query: str | None
    author: str | None
    profile: str
    """Not a filter on rows: carried so a cursor can't be reused for another profile."""
    genre: str | None
    series: str | None
    format: str | None
    min_rating: float | None
    finished_from: str | None
    finished_to: str | None

    def match(self, b: _Book) -> bool:
        row = b.row
        if self.status is not None and row["status"] != self.status:
            return False
        if self.query is not None and not any(
            self.query in text.casefold() for text in (row["title"], *b.contributors)
        ):
            return False
        if self.author is not None and not any(self.author in a.casefold() for a in b.authors):
            return False
        if self.genre is not None and self.genre not in (g.casefold() for g in b.all_genres):
            return False
        if self.series is not None and not self._series(b):
            return False
        if self.min_rating is not None and (
            row["rating"] is None or row["rating"] < self.min_rating
        ):
            return False
        by_read = self.format is not None or self.finished_from or self.finished_to
        # One read must match both: "audiobooks finished last year" means a listen that year.
        return not by_read or any(self._read(r) for r in b.reads)

    def digest(self) -> str:
        """A short fingerprint of these filters, which a cursor carries so it can't be reused
        under different ones."""
        text = json.dumps(asdict(self), sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()[:12]

    def _series(self, b: _Book) -> bool:
        assert self.series is not None
        name = b.row["series"]["name"].casefold() if b.row["series"] else ""
        # isascii too: isdigit() alone accepts superscripts that int() refuses.
        wanted_id = self.series.isascii() and self.series.isdigit()
        by_id = wanted_id and b.series_id == int(self.series)
        return by_id or (bool(name) and self.series in name)

    def _read(self, read: _Read) -> bool:
        if self.format is not None and self.format not in read.formats:
            return False
        if self.finished_from is None and self.finished_to is None:
            return True
        if read.finished_on is None:
            return False
        finished = read.finished_on[:10]  # the date, whatever Hardcover sent after it
        if self.finished_from is not None and finished < self.finished_from:
            return False
        return self.finished_to is None or finished <= self.finished_to


def _fold(text: str | None) -> str | None:
    """A filter's text, casefolded, or ``None`` when it's absent or blank."""
    folded = text.strip().casefold() if text is not None else ""
    return folded or None


# --- reading the mirror -------------------------------------------------------------------


def _library(conn: sqlite3.Connection, profile: str) -> list[_Book]:
    reads: dict[int, list[_Read]] = {}
    for user_book_id, finished_on, format_id in conn.execute(
        "SELECT hc_user_book_id, finished_on, reading_format_id FROM mirror_reads "
        "WHERE profile = ?",
        (profile,),
    ):
        formats = FORMATS.get(format_id or 0, (UNKNOWN,))
        reads.setdefault(user_book_id, []).append(_Read(finished_on, formats))
    rows = conn.execute(
        "SELECT ub.hc_user_book_id, ub.hc_book_id, ub.status_id, ub.rating, ub.date_added, "
        "b.title, b.contributors_json, b.series_id, b.series_name, b.series_position, "
        "b.tags_json "
        "FROM mirror_user_books ub LEFT JOIN mirror_books b "
        "ON b.profile = ub.profile AND b.hc_book_id = ub.hc_book_id "
        "WHERE ub.profile = ?",
        (profile,),
    )
    return [_book(row, reads.get(row[0], [])) for row in rows]


def _book(row: tuple[Any, ...], reads: list[_Read]) -> _Book:
    (user_book_id, book_id, status_id, rating, date_added, title, contributors_json, series_id,
     series_name, series_position, tags) = row  # fmt: skip
    contributors: list[dict[str, Any]] = json.loads(contributors_json) if contributors_json else []
    genres: list[str] = json.loads(tags).get("genres", []) if tags else []
    finished = [r.finished_on for r in reads if r.finished_on]
    # cached_contributors gives authors no role, and narrators, editors or translators one.
    authors = tuple(c["name"] for c in contributors if not c.get("role"))
    return _Book(
        user_book_id,
        date_added,
        LibraryRow(
            book_id=book_id,
            title=title or "",
            # Only narrators and the like credited: naming one as the author would mislead.
            author=authors[0] if authors else None,
            status=STATUSES.get(status_id, str(status_id)),
            rating=rating,
            formats=sorted({fmt for r in reads for fmt in r.formats}),
            last_finished=max(finished) if finished else None,
            read_count=len(reads),
            series=Series(name=series_name, position=series_position) if series_name else None,
            genres=genres[:TOP_GENRES],
        ),
        series_id=series_id,
        authors=authors,
        contributors=tuple(c["name"] for c in contributors),
        all_genres=tuple(genres),
        reads=tuple(reads),
    )


# --- paging and freshness -----------------------------------------------------------------


def _cursor(sort: str, digest: str, key: _Key) -> str:
    text = json.dumps([sort, digest, *key])
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _after(cursor: str | None, sort: str, digest: str) -> _Key | None:
    if cursor is None:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        given_sort, given_digest, rank, value, user_book_id = decoded
        if given_sort != sort or given_digest != digest:
            raise ToolError(
                "That cursor is from a page with a different sort or filters. Pass the same "
                "ones as the page it came from, or leave cursor out to start again."
            )
        # The value must compare with this sort's keys: a number for rating, else text.
        numeric = sort == "rating"
        value_ok = (
            isinstance(value, int | float) and not isinstance(value, bool)
            if numeric
            else isinstance(value, str)
        )
        if rank not in (0, 1) or not value_ok or type(user_book_id) is not int:
            raise ValueError
        return (rank, value, user_book_id)
    except ValueError, TypeError:  # binascii.Error and JSON/Unicode errors are ValueErrors
        raise ToolError(
            "That cursor isn't one this tool gave out. Pass next_cursor from the previous "
            "page, or leave cursor out to start from the beginning."
        ) from None


def _tagged(row: LibraryRow, profile: str) -> LibraryRow:
    tagged = LibraryRow(**row)
    tagged["profile"] = profile
    return tagged


def _freshness(by_profile: dict[str, Freshness]) -> dict[str, Any]:
    """``as_of`` and ``stale`` for an answer drawn from one or more copies: the oldest copy's
    ``as_of``, and stale when any copy is, with each stale profile's reason."""
    oldest = min(f.as_of for f in by_profile.values())
    out: dict[str, Any] = {
        "as_of": datetime.fromtimestamp(oldest, UTC).isoformat(),
        "stale": any(f.stale for f in by_profile.values()),
    }
    reasons = {name: f.stale_reason for name, f in by_profile.items() if f.stale_reason}
    if len(by_profile) == 1 and reasons:
        out["stale_reason"] = next(iter(reasons.values()))
    elif reasons:
        out["stale_reason"] = "; ".join(f"{name}: {why}" for name, why in reasons.items())
    return out
