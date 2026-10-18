"""The read tools over a profile's Hardcover library (SPEC §3.3.7, §3.3.8).

``get_my_library`` reads the mirror (§3.3.4), refreshed first when it's older than the TTL,
and answers in compact **library rows** of a fixed shape, so a page of 25 stays small in
Claude's context. Every answer carries ``as_of``, and ``stale`` with a reason when the refresh
failed and the copy was served anyway.

Reads without a format are reported in an explicit ``unknown`` bucket, never guessed (§3.3.2).
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
import sqlite3
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from syncshelf.clients.hardcover import (
    BOOK,
    BOOKS_BY_ID,
    GOALS,
    SEARCH,
    SEARCH_PAGE,
    SERIES,
    SERIES_BY_NAME,
    SHELF,
    SIMILAR,
    HardcoverClient,
    HardcoverError,
    Result,
)
from syncshelf.config import ProfileConfig
from syncshelf.sync.matcher import (
    TITLE_FLOOR,
    Scored,
    author_score,
    auto_accepts,
    is_likely,
    normalise,
    title_score,
)
from syncshelf.sync.mirror import AUTHOR_ROLES, Freshness, Mirror, NoMirror, tag_names
from syncshelf.tools import ME, REGISTRY, current_profile, resolve_profiles, services

__all__ = [
    "FORMATS",
    "SORTS",
    "STATUSES",
    "LibraryPage",
    "LibraryRow",
    "BookRecord",
    "Stats",
    "check_books",
    "get_book",
    "search_books",
    "series_progress",
    "get_my_library",
    "reading_stats",
]

log = logging.getLogger(__name__)

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

    Use this for questions about the user's library, or a household member's (see profile):
    what they're currently reading, what's on their want-to-read (by author, in a genre), what
    they read in a period, what they rated highest last year, what both have read, and how
    many of each. For one book's full record (reads with
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
      shared; reviews never are. If someone's books can't be read yet, the answer comes
      from the rest, with stale true and stale_reason naming them.

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
    )
    bound = services()
    # Refused here, before any refresh, when a named profile hasn't opted in (§3.3.4).
    names = resolve_profiles(profile, caller, bound.profiles)
    # Bound to the profiles resolved, so "me" and the caller's own name share cursors.
    digest = wanted.digest(names)
    after = _after(cursor, sort, digest)
    freshness = await _refresh(bound.mirror, names, caller)
    # Rows name their shelf whenever the answer isn't only the caller's own.
    tag = names != (caller,)
    books: list[_Book] = []
    for name in names:
        shelf = await bound.db.read(_library, name)
        books += [replace(b, row=_tagged(b.row, name)) for b in shelf] if tag else shelf
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
        next_cursor=_cursor(sort, digest, key(page[-1])) if len(rest) > limit else None,
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

    def digest(self, profiles: tuple[str, ...]) -> str:
        """A short fingerprint of these filters and the shelves read, which a cursor carries
        so it can't be reused under different ones."""
        text = json.dumps([asdict(self), profiles], sort_keys=True)
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
    genres = _genres(tags)
    finished = [r.finished_on for r in reads if r.finished_on]
    authors = _authors(contributors)
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


def _authors(contributors: list[dict[str, Any]]) -> tuple[str, ...]:
    """The authors among a book's stored contributors: the mirror stores an author's role as
    null, and a narrator's, editor's or translator's as that role."""
    return tuple(c["name"] for c in contributors if not c.get("role"))


def _genres(tags_json: str | None) -> list[str]:
    """A book's genres, most used first, from the mirror's ``tags_json``."""
    return json.loads(tags_json).get("genres", []) if tags_json else []


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


type _Copy = Freshness | NoMirror
"""A profile's copy as an answer can use it: how fresh it is, or that it has none yet."""


async def _refresh(mirror: Mirror, names: tuple[str, ...], caller: str) -> dict[str, _Copy]:
    """Make each profile's copy fresh, at the same time: each has its own token, limiter and
    lock. A profile with no copy yet is noted, so the answer comes from the others, flagged
    stale (#116). Only when no profile asked about has a copy is the answer refused."""
    copies: dict[str, _Copy] = {}

    async def one(name: str) -> None:
        try:
            copies[name] = await mirror.fresh(name)
        except NoMirror as exc:
            copies[name] = exc

    async with asyncio.TaskGroup() as tg:
        for name in names:
            tg.create_task(one(name))
    if all(isinstance(c, NoMirror) for c in copies.values()):
        first = names[0]
        whose = "" if first == caller or len(names) == 1 else f"For {first}: "
        raise ToolError(whose + str(copies[first]))
    return {name: copies[name] for name in names}


def _tagged(row: LibraryRow, profile: str) -> LibraryRow:
    tagged = LibraryRow(**row)
    tagged["profile"] = profile
    return tagged


def _freshness(by_profile: dict[str, _Copy]) -> dict[str, Any]:
    """``as_of`` and ``stale`` for an answer drawn from one or more copies: the oldest copy's
    ``as_of``, and stale when any copy is stale or missing, with each one's reason."""
    copies = {name: c for name, c in by_profile.items() if isinstance(c, Freshness)}
    oldest = min(f.as_of for f in copies.values())
    reasons = {
        name: str(c) if isinstance(c, NoMirror) else c.stale_reason
        for name, c in by_profile.items()
        if isinstance(c, NoMirror) or c.stale_reason
    }
    out: dict[str, Any] = {
        "as_of": datetime.fromtimestamp(oldest, UTC).isoformat(),
        "stale": bool(reasons) or any(f.stale for f in copies.values()),
    }
    if len(by_profile) == 1 and reasons:
        out["stale_reason"] = next(iter(reasons.values()))
    elif reasons:
        out["stale_reason"] = "; ".join(f"{name}: {why}" for name, why in reasons.items())
    return out


# --- reading_stats ------------------------------------------------------------------------

GroupBy = Literal["month", "format", "genre", "author", "rating"]
MAX_BUCKETS = 15
"""Buckets shown per grouping before the rest are merged into ``other`` (§3.3.8)."""

_PERIOD_RANGE = re.compile(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


class Totals(TypedDict):
    books: int
    """Distinct books with a read finished in the period."""
    reads: int
    pages: int
    """From print and ebook reads: the book's page count, per read."""
    hours: float
    """From audio reads: the audio edition's length, per read."""
    unknown_format_reads: int
    """Reads with no format in Hardcover: counted in books and reads, not in pages or hours."""


class Bucket(Totals):
    key: str


class Goal(TypedDict):
    metric: str
    goal: float
    progress: float
    start_date: str | None
    end_date: str | None
    state: str


class NotCounted(TypedDict):
    """What the stats can't see, so answers can say so rather than undercount quietly."""

    undated_reads: int
    """Reads of books shelved as read that have no finish date."""
    read_without_reads: int
    """Books shelved as read with no read logged at all (common for imported libraries)."""


class Stats(TypedDict):
    period: dict[str, str | None]
    totals: Totals
    groups: NotRequired[list[Bucket]]
    goals: NotRequired[list[Goal]]
    not_counted: NotCounted
    as_of: str
    stale: bool
    stale_reason: NotRequired[str]


@dataclass(frozen=True)
class _StatRead:
    book: tuple[str, int]  # (profile, user_book_id): a book on one shelf
    finished_on: str
    formats: tuple[str, ...]
    pages: int | None
    audio_seconds: int | None
    genres: tuple[str, ...]
    author: str | None
    rating: float | None


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def reading_stats(period: str, group_by: GroupBy | None = None, profile: str = ME) -> Stats:
    """Count books, pages and hours read in a period, optionally grouped, with goal progress.

    Use this for "how many books, pages or hours this year?", "audiobooks vs ebooks this
    year", "my favourite authors", "my top genres" and "how am I doing on my reading goal?".
    It returns aggregates only, never a list of books; for the books themselves use
    get_my_library with finished_from/finished_to.

    Arguments:
    - period: YYYY (a year), YYYY-MM (a month), last_30_days, last_12_months, all, or
      YYYY-MM-DD..YYYY-MM-DD (inclusive). Relative periods are read in the profile's time
      zone. A read counts in the period its finish date falls in.
    - group_by: month, format (audio, ebook, print), genre (each book's top 3), author (the
      first author) or rating (the book's rating). At most 15 groups, then "other", and always
      an "unknown" group for reads without that information (no format, no genre, no author,
      unrated).
    - profile: "me" (the default), another profile's name, or "household" for everyone who
      shares their books; their reads are counted together.

    totals has books (distinct books with a read finished in the period), reads, pages (from
    print and ebook reads), hours (from audio reads) and unknown_format_reads (reads Hardcover
    has no format for: counted as books and reads, but in neither pages nor hours).
    not_counted says what no period can include: undated_reads (finished but with no date)
    and read_without_reads (books shelved as read with no read logged, which is common for
    libraries imported into Hardcover). Mention these when they're large, so the user knows
    the numbers are a floor. goals lists the profile's Hardcover reading goals with progress
    (left out for household answers, or when Hardcover can't be reached). as_of and stale are
    as in get_my_library.
    """
    caller = current_profile()
    bound = services()
    names = resolve_profiles(profile, caller, bound.profiles)
    now = datetime.fromtimestamp(bound.clock(), UTC)
    # Each profile's dates are in its own zone, so "today" is worked out per profile.
    spans = {name: _period(period, now, bound.profiles[name]) for name in names}
    one = len(names) == 1
    async with asyncio.TaskGroup() as tg:
        refreshing = tg.create_task(_refresh(bound.mirror, names, caller))
        goals = tg.create_task(_goals(bound.hardcover[names[0]])) if one else None
    freshness = refreshing.result()
    in_period: list[_StatRead] = []
    not_counted = NotCounted(undated_reads=0, read_without_reads=0)
    for name in names:
        shelf, undated, without = await bound.db.read(_stat_reads, name)
        start, end = spans[name]
        in_period += [
            r
            for r in shelf
            if (start is None or r.finished_on >= start) and (end is None or r.finished_on <= end)
        ]
        not_counted["undated_reads"] += undated
        not_counted["read_without_reads"] += without
    start, end = spans[caller] if caller in spans else spans[names[0]]
    result = Stats(
        period={"from": start, "to": end},
        totals=_totals(in_period),
        not_counted=not_counted,
        **_freshness(freshness),
    )
    if group_by is not None:
        result["groups"] = _groups(in_period, group_by)
    if goals is not None and (fetched := goals.result()) is not None:
        result["goals"] = fetched
    return result


def _period(period: str, now: datetime, caller: ProfileConfig) -> tuple[str | None, str | None]:
    """The inclusive ISO date range a ``period`` names, ``None`` for an open end."""
    today = now.astimezone(caller.tzinfo()).date()
    try:
        if period == "all":
            return (None, None)
        if period == "last_30_days":
            return ((today - timedelta(days=29)).isoformat(), today.isoformat())
        if period == "last_12_months":
            try:
                year_ago = today.replace(year=today.year - 1)
            except ValueError:  # 29 February, a year after a leap day
                year_ago = today.replace(year=today.year - 1, day=28)
            return ((year_ago + timedelta(days=1)).isoformat(), today.isoformat())
        if re.fullmatch(r"\d{4}", period):
            return (f"{period}-01-01", f"{period}-12-31")
        if re.fullmatch(r"\d{4}-\d{2}", period):
            first = date.fromisoformat(f"{period}-01")
            last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            return (first.isoformat(), last.isoformat())
        if match := _PERIOD_RANGE.fullmatch(period):
            start, end = (date.fromisoformat(d) for d in match.groups())
            if start > end:
                raise ToolError(
                    "The period's first date is after its last; give the earlier one first."
                )
            return (start.isoformat(), end.isoformat())
    except ValueError:
        pass
    raise ToolError(
        "period must be YYYY, YYYY-MM, last_30_days, last_12_months, all, or "
        "YYYY-MM-DD..YYYY-MM-DD."
    )


def _stat_reads(conn: sqlite3.Connection, profile: str) -> tuple[list[_StatRead], int, int]:
    """The profile's dated reads with what grouping needs, plus the undated reads of read books
    and the read books with no reads."""
    rows = conn.execute(
        "SELECT r.hc_user_book_id, r.finished_on, r.reading_format_id, b.pages, "
        "b.audio_seconds, b.tags_json, b.contributors_json, ub.rating, ub.status_id "
        "FROM mirror_reads r JOIN mirror_user_books ub "
        "ON ub.profile = r.profile AND ub.hc_user_book_id = r.hc_user_book_id "
        "LEFT JOIN mirror_books b ON b.profile = ub.profile AND b.hc_book_id = ub.hc_book_id "
        "WHERE r.profile = ?",
        (profile,),
    )
    reads: list[_StatRead] = []
    undated = 0
    for user_book_id, finished_on, format_id, pages, seconds, tags, people, rating, status in rows:
        if finished_on is None:
            undated += status == 3
            continue
        authors = _authors(json.loads(people) if people else [])
        reads.append(
            _StatRead(
                book=(profile, user_book_id),
                finished_on=finished_on[:10],
                formats=FORMATS.get(format_id or 0, (UNKNOWN,)),
                pages=pages,
                audio_seconds=seconds,
                genres=tuple(_genres(tags)[:TOP_GENRES]),
                author=authors[0] if authors else None,
                rating=rating,
            )
        )
    [(without,)] = conn.execute(
        "SELECT count(*) FROM mirror_user_books ub WHERE ub.profile = ? AND ub.status_id = 3 "
        "AND NOT EXISTS (SELECT 1 FROM mirror_reads r WHERE r.profile = ub.profile "
        "AND r.hc_user_book_id = ub.hc_user_book_id)",
        (profile,),
    )
    return reads, undated, without


@dataclass
class _Tally:
    books: set[tuple[str, int]] = field(default_factory=set)
    reads: int = 0
    pages: int = 0
    seconds: int = 0
    unknown: int = 0

    def add(self, read: _StatRead) -> None:
        self.books.add(read.book)
        self.reads += 1
        if UNKNOWN in read.formats:
            self.unknown += 1
        if {"print", "ebook"} & set(read.formats):
            self.pages += read.pages or 0
        if "audio" in read.formats:
            self.seconds += read.audio_seconds or 0

    def merge(self, other: _Tally) -> None:
        self.books |= other.books
        self.reads += other.reads
        self.pages += other.pages
        self.seconds += other.seconds
        self.unknown += other.unknown

    def totals(self) -> Totals:
        return Totals(
            books=len(self.books),
            reads=self.reads,
            pages=self.pages,
            hours=round(self.seconds / 3600, 1),
            unknown_format_reads=self.unknown,
        )


def _totals(reads: list[_StatRead]) -> Totals:
    tally = _Tally()
    for read in reads:
        tally.add(read)
    return tally.totals()


def _keys(read: _StatRead, group_by: str) -> tuple[str, ...]:
    """The buckets one read counts in; ``unknown`` when it has nothing to group by."""
    if group_by == "month":
        return (read.finished_on[:7],)
    if group_by == "format":
        return read.formats
    if group_by == "genre":
        return read.genres or (UNKNOWN,)
    if group_by == "author":
        return (read.author,) if read.author else (UNKNOWN,)
    return (f"{read.rating:g}",) if read.rating is not None else (UNKNOWN,)


def _groups(reads: list[_StatRead], group_by: str) -> list[Bucket]:
    tallies: dict[str, _Tally] = {}
    for read in reads:
        for key in _keys(read, group_by):
            tallies.setdefault(key, _Tally()).add(read)
    unknown = tallies.pop(UNKNOWN, _Tally())
    if group_by == "month":
        order = sorted(tallies)
    elif group_by == "rating":
        order = sorted(tallies, key=float, reverse=True)
    else:
        order = sorted(tallies, key=lambda k: (-len(tallies[k].books), -tallies[k].reads, k))
    shown, rest = order[:MAX_BUCKETS], order[MAX_BUCKETS:]
    buckets = [Bucket(key=k, **tallies[k].totals()) for k in shown]
    if rest:
        other = _Tally()
        for k in rest:
            other.merge(tallies[k])
        buckets.append(Bucket(key="other", **other.totals()))
    buckets.append(Bucket(key=UNKNOWN, **unknown.totals()))
    return buckets


async def _goals(client: HardcoverClient) -> list[Goal] | None:
    """The profile's reading goals, live (1 request); ``None`` when Hardcover can't answer, so
    the stats still come back without them (§3.3.7)."""
    try:
        result = await client.execute(GOALS)
        [me] = result.data["me"]
        return [
            Goal(
                metric=g["metric"],
                goal=g["goal"],
                progress=g["progress"],
                start_date=g.get("start_date"),
                end_date=g.get("end_date"),
                state=g["state"],
            )
            for g in me["goals"]
        ]
    except HardcoverError, KeyError, TypeError, ValueError:
        log.info("reading goals unavailable", extra={"event": "goals_unavailable"})
        return None


# --- get_book -----------------------------------------------------------------------------

TOP_TAGS = 5
"""Genres, moods and tags shown per category in a full record (§3.3.8)."""
MAX_WARNINGS = 10
SIMILAR_SHOWN = 10


class BookRead(TypedDict):
    """One read of a book: its Hardcover id (for correct_read), dates and formats."""

    read_id: int
    started_on: str | None
    finished_on: str | None
    formats: list[str]


class Shelf(TypedDict):
    """One profile's shelf state for the book."""

    profile: str
    status: str
    rating: float | None
    date_added: str | None
    reads: list[BookRead]
    lists: list[str]
    review: NotRequired[str | None]
    """The caller's own review only; never another profile's (§2.8)."""
    review_has_spoilers: NotRequired[bool]


class SeriesEntry(TypedDict):
    """A series the book belongs to, and its position there."""

    series_id: int
    name: str
    position: float | None


class Contributor(TypedDict):
    """A person credited on the book, with Hardcover's role for them (Author, Illustrator…)."""

    name: str
    role: str | None


class SimilarBook(TypedDict):
    """A book Hardcover's readers of this one also read, compactly."""

    book_id: int
    title: str
    author: str | None
    release_year: int | None
    community_rating: float | None
    status: str | None
    """The caller's shelf for it, or null when it isn't shelved."""


class BookRecord(TypedDict):
    """``get_book``'s answer: the book, each requested shelf, and similar books."""

    book_id: int
    title: str
    subtitle: str | None
    release_year: int | None
    canonical_id: NotRequired[int]
    pages: int | None
    audio_hours: float | None
    community_rating: float | None
    contributors: list[Contributor]
    narrators: list[str]
    series: list[SeriesEntry]
    genres: list[str]
    moods: list[str]
    tags: list[str]
    content_warnings: list[str]
    shelves: list[Shelf]
    similar: list[SimilarBook]
    unavailable: NotRequired[list[str]]
    """Extras Hardcover couldn't give this time ("similar", "bob's shelf")."""


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_book(book: int, profile: str = ME) -> BookRecord:
    """Get one book's full record from Hardcover, live, with the caller's shelf for it.

    Use this for "have I read X, what did I rate it and what did I say?", "when did I read X,
    how many times?" and "tell me about X". It takes Hardcover's book_id: find it first with
    get_my_library (for a shelved book), search_books or check_books. Don't guess an id.

    Arguments:
    - book: the Hardcover book_id.
    - profile: "me" (the default), another profile's name, or "household" to see each
      sharing profile's shelf for the book too.

    The record has title, subtitle, release_year, pages, audio_hours (of the default
    audiobook), community_rating (Hardcover's average, not the user's), contributors (with
    roles such as Author or Illustrator), narrators, every series with the book's position,
    the top 5 genres, moods and tags, content_warnings, and similar (10 books Hardcover's
    readers also read, with the caller's shelf status for each: a seed for recommendations,
    not a verdict). canonical_id, when present, means Hardcover merged this book into
    another; prefer that id.

    shelves has one entry per profile that has the book shelved (empty when nobody has):
    status, rating (the user's own, 0.5–5), date_added, every read with its read_id, dates
    and formats (unknown when Hardcover has none), and the lists it's on. The caller's own
    entry also has review and review_has_spoilers; other people's reviews are never shown.
    Many books shelved as read have no reads logged: say so rather than guess a date. When
    Hardcover couldn't give an extra this time, unavailable names it (for example "similar"),
    and the rest of the record still stands.
    """
    caller = current_profile()
    bound = services()
    names = resolve_profiles(profile, caller, bound.profiles)
    # Lists and the similar books' shelf status come from the copy, so make it current first
    # (free within the TTL). A profile with no copy just has no lists to show.
    with contextlib.suppress(ToolError):
        await _refresh(bound.mirror, names, caller)
    first = bound.hardcover[names[0]]
    result = await first.execute(BOOK, {"book_id": book, "user_id": await first.me_id()})
    if not result.data.get("books"):
        raise ToolError(
            f"Hardcover has no book with id {book}. Find the id with search_books or "
            "check_books, or with get_my_library for a shelved book."
        )
    data: dict[str, Any] = result.data["books"][0]
    found: dict[str, dict[str, Any] | None] = {names[0]: _first(result.data.get("user_books"))}
    missing: list[str] = []
    # The other shelves and the similar books are extras: each is fetched at the same time,
    # and one that fails is named in `unavailable` rather than costing the whole record.
    async with asyncio.TaskGroup() as tg:
        others = {
            name: tg.create_task(_other_shelf(bound.hardcover[name], book)) for name in names[1:]
        }
        similar = tg.create_task(_similar(first, data.get("cached_similar_book_ids") or [], caller))
    for name, task in others.items():
        if isinstance(shelf := task.result(), HardcoverError):
            missing.append(f"{name}'s shelf")
        else:
            found[name] = shelf
    if isinstance(similar_rows := similar.result(), HardcoverError):
        missing.append("similar")
        similar_rows = []
    for name, user_book in found.items():
        # The copy catches up with what Hardcover just said (§3.3.4).
        stored = {**user_book, "book": data} if user_book else None
        await bound.mirror.write_through(name, book, stored)
    lists = await bound.db.read(_lists_for, book, tuple(found))
    shelves = [
        _shelf(name, ub, lists.get(name, []), own=name == caller)
        for name, ub in found.items()
        if ub is not None
    ]
    record = BookRecord(
        book_id=data["id"],
        title=data.get("title") or "",
        subtitle=data.get("subtitle"),
        release_year=data.get("release_year"),
        pages=data.get("pages") or (data.get("default_physical_edition") or {}).get("pages"),
        audio_hours=_hours((data.get("default_audio_edition") or {}).get("audio_seconds")),
        community_rating=_rounded(data.get("rating")),
        contributors=[
            Contributor(name=c["author"]["name"], role=c.get("contribution"))
            for c in data.get("cached_contributors") or []
            if (c.get("author") or {}).get("name")
        ],
        narrators=_narrators(data.get("narrated")),
        series=[
            SeriesEntry(
                series_id=s["series"]["id"], name=s["series"]["name"], position=s["position"]
            )
            for s in data.get("book_series") or []
            if s.get("series")
        ],
        genres=tag_names(data.get("genres"))[:TOP_TAGS],
        moods=tag_names(data.get("moods"))[:TOP_TAGS],
        tags=tag_names(data.get("tags"))[:TOP_TAGS],
        content_warnings=tag_names(data.get("warnings"))[:MAX_WARNINGS],
        shelves=shelves,
        similar=similar_rows,
    )
    if data.get("canonical_id"):
        record["canonical_id"] = data["canonical_id"]
    if missing:
        record["unavailable"] = missing
    return record


async def _other_shelf(
    client: HardcoverClient, book: int
) -> dict[str, Any] | None | HardcoverError:
    try:
        shelf = await client.execute(SHELF, {"book_id": book, "user_id": await client.me_id()})
    except HardcoverError as exc:
        return exc
    return _first(shelf.data.get("user_books"))


def _first(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    return rows[0] if rows else None


def _shelf(name: str, ub: dict[str, Any], lists: list[str], *, own: bool) -> Shelf:
    shelf = Shelf(
        profile=name,
        status=STATUSES.get(ub["status_id"], str(ub["status_id"])),
        rating=ub.get("rating"),
        date_added=ub.get("date_added"),
        reads=[
            BookRead(
                read_id=r["id"],
                started_on=r.get("started_at"),
                finished_on=r.get("finished_at"),
                formats=list(
                    FORMATS.get((r.get("edition") or {}).get("reading_format_id") or 0, (UNKNOWN,))
                ),
            )
            for r in ub.get("user_book_reads") or []
        ],
        lists=lists,
    )
    if own:
        shelf["review"] = ub.get("review_raw") or None
        shelf["review_has_spoilers"] = bool(ub.get("review_has_spoilers"))
    return shelf


def _lists_for(
    conn: sqlite3.Connection, book_id: int, profiles: tuple[str, ...]
) -> dict[str, list[str]]:
    """The names of the lists each profile has the book on, from the mirror."""
    out: dict[str, list[str]] = {}
    for profile in profiles:
        rows = conn.execute(
            "SELECT list_name FROM mirror_list_items WHERE profile = ? AND hc_book_id = ? "
            "ORDER BY list_name",
            (profile, book_id),
        )
        out[profile] = [name for (name,) in rows]
    return out


def _narrators(narrated: dict[str, Any] | None) -> list[str]:
    """Narrators are credited on the audio edition, as contribution "Narrator"."""
    people = (narrated or {}).get("cached_contributors") or []
    return [
        c["author"]["name"]
        for c in people
        if c.get("contribution") == "Narrator" and (c.get("author") or {}).get("name")
    ]


def _hours(seconds: int | None) -> float | None:
    return round(seconds / 3600, 1) if seconds else None


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


async def _similar(
    client: HardcoverClient, ids: list[int], caller: str
) -> list[SimilarBook] | HardcoverError:
    """The first similar ids, hydrated in one request, in Hardcover's own order, with the
    caller's shelf status for each; the error instead when Hardcover can't answer."""
    wanted = ids[:SIMILAR_SHOWN]
    if not wanted:
        return []
    try:
        result = await client.execute(SIMILAR, {"ids": wanted})
    except HardcoverError as exc:
        return exc
    by_id = {b["id"]: b for b in result.data.get("books") or []}
    statuses = await services().db.read(_statuses, caller, tuple(by_id))
    rows: list[SimilarBook] = []
    for book_id in wanted:  # the _in answer comes back in no particular order
        b = by_id.get(book_id)
        if b is None:
            continue
        people = b.get("cached_contributors") or []
        authors = [
            c["author"]["name"]
            for c in people
            if c.get("contribution") in AUTHOR_ROLES and (c.get("author") or {}).get("name")
        ]
        rows.append(
            SimilarBook(
                book_id=book_id,
                title=b.get("title") or "",
                author=authors[0] if authors else None,
                release_year=b.get("release_year"),
                community_rating=_rounded(b.get("rating")),
                status=statuses.get(book_id),
            )
        )
    return rows


def _statuses(conn: sqlite3.Connection, profile: str, book_ids: tuple[int, ...]) -> dict[int, str]:
    """The profile's shelf status for each of ``book_ids`` it has shelved, from the mirror."""
    if not book_ids:
        return {}
    marks = ",".join("?" * len(book_ids))
    rows = conn.execute(
        f"SELECT hc_book_id, status_id FROM mirror_user_books "  # noqa: S608
        f"WHERE profile = ? AND hc_book_id IN ({marks})",
        (profile, *book_ids),
    )
    return {book_id: STATUSES.get(status, str(status)) for book_id, status in rows}


# --- series_progress ----------------------------------------------------------------------

STARTED = frozenset({2, 3})
"""Statuses that mean a series is under way: currently reading or read."""
READ = 3
SERIES_PER_REQUEST = 20
"""Series fetched per live request when listing every started series."""
_SHOWN_FIRST = (3, 2, 4, 1, 5, 6)
"""When several books share a position, the status shown for it: read wins, then reading."""


class SeriesBook(TypedDict):
    """One position in a series' reading order, shown as its most-read book."""

    position: float
    book_id: int
    title: str
    status: NotRequired[str | None]
    """The profile's shelf at this position (any edition counts), on one-profile answers."""
    statuses: NotRequired[dict[str, str | None]]
    """The same, per profile, on answers for several."""


class SeriesProgress(TypedDict):
    """How far one profile is through a series."""

    profile: str
    read: int
    """Whole-number positions read."""
    next: SeriesBook | None
    """The first whole-number position not read, or null once all are."""


class SeriesAnswer(TypedDict):
    """One series: its main-entry count, each profile's progress, and its order if asked."""

    series_id: int
    name: str
    books: int
    """Whole-number positions: the main entries, not novellas (#128)."""
    progress: list[SeriesProgress]
    order: NotRequired[list[SeriesBook]]
    """The full reading order, novellas included, when one series was asked for."""


class SeriesPage(TypedDict):
    """``series_progress``'s answer: series by name, with the copy's freshness."""

    series: list[SeriesAnswer]
    as_of: str
    stale: bool
    stale_reason: NotRequired[str]


@dataclass(frozen=True)
class _Position:
    position: float
    book_ids: frozenset[int]
    shown: tuple[int, str]  # (book_id, title) of the most-read book at the position


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def series_progress(series: str | None = None, profile: str = ME) -> SeriesPage:
    """Show where the user is in their book series, and what to read next.

    Use this for "which series am I partway through?", "what's next in X?" and "what's the
    reading order of X?".

    Arguments:
    - series: leave it out for every series the user has started (a book in it read or being
      read) but not finished, each with the next book to read. Give a series name, or its
      Hardcover series id, for that series' full reading order with the user's status for
      each book. A name is matched exactly (ignoring case) against the user's own series,
      then exactly on Hardcover (where case matters), then as part of one of the user's own
      series' names.
    - profile: "me" (the default), another profile's name, or "household".

    Each series has series_id, name, books (main entries in it) and progress: per profile,
    read (main entries read) and next (the first main entry not read: position, book_id,
    title and the profile's status for it; null when finished). With a series asked for, order
    lists every position, novellas (fractional positions such as 1.5) included, each shown as
    its most-read edition with the profile's status (status, or statuses per profile). Any
    edition or translation shelved as read counts for its position. Box sets and entries with
    no position are left out. Series lists come from Hardcover live; shelf status from
    SyncShelf's copy (as_of, stale as in get_my_library).
    """
    caller = current_profile()
    bound = services()
    names = resolve_profiles(profile, caller, bound.profiles)
    freshness = await _refresh(bound.mirror, names, caller)
    shelves = {name: await bound.db.read(_series_shelf, name) for name in names}
    client = bound.hardcover[names[0]]
    series = (series or "").strip() or None  # a blank name means every started series
    if series is None:
        started = sorted({s for shelf in shelves.values() for s in shelf.started})
        found: list[dict[str, Any]] = []
        # In chunks: a series' rows include every edition and translation, so one request
        # for a long reader's every series could be tens of thousands of rows.
        for at in range(0, len(started), SERIES_PER_REQUEST):
            chunk = started[at : at + SERIES_PER_REQUEST]
            found += await _fetch_series(client, SERIES, {"ids": chunk})
    else:
        found = await _find_series(client, series, shelves)
    found.sort(key=lambda d: ((d.get("name") or "").casefold(), d["id"]))
    answers: list[SeriesAnswer] = []
    for data in found:
        order = _order(data.get("book_series") or [])
        whole = [p for p in order if p.position.is_integer()]
        progress = [_progress(name, whole, shelves[name].status) for name in names]
        if series is None:
            # Only what is under way and unfinished, for some profile asked about.
            progress = [
                p
                for p in progress
                if p["next"] is not None and data["id"] in shelves[p["profile"]].started
            ]
            if not progress:
                continue
        answer = SeriesAnswer(
            series_id=data["id"], name=data.get("name") or "", books=len(whole), progress=progress
        )
        if series is not None:
            answer["order"] = [_entry(p, names, shelves) for p in order]
        answers.append(answer)
    return SeriesPage(series=answers, **_freshness(freshness))


@dataclass(frozen=True)
class _SeriesShelf:
    status: dict[int, int]
    """book_id → status_id, for every shelved book."""
    started: frozenset[int]
    """Featured series ids of books read or being read."""
    names: dict[int, str]
    """Featured series ids → names, for matching a ``series`` argument."""


def _series_shelf(conn: sqlite3.Connection, profile: str) -> _SeriesShelf:
    status: dict[int, int] = {}
    started: set[int] = set()
    names: dict[int, str] = {}
    for book_id, status_id, series_id, series_name in conn.execute(
        "SELECT ub.hc_book_id, ub.status_id, b.series_id, b.series_name "
        "FROM mirror_user_books ub LEFT JOIN mirror_books b "
        "ON b.profile = ub.profile AND b.hc_book_id = ub.hc_book_id WHERE ub.profile = ?",
        (profile,),
    ):
        status[book_id] = status_id
        if series_id is not None:
            names[series_id] = series_name or ""
            if status_id in STARTED:
                started.add(series_id)
    return _SeriesShelf(status, frozenset(started), names)


async def _fetch_series(
    client: HardcoverClient, query: str, variables: dict[str, Any]
) -> list[dict[str, Any]]:
    result = await client.execute(query, variables)
    return list(result.data.get("series") or [])


async def _find_series(
    client: HardcoverClient, wanted: str, shelves: dict[str, _SeriesShelf]
) -> list[dict[str, Any]]:
    """The series ``wanted`` names, tried in order: an exact (case-insensitive) name among the
    profiles' own series; a Hardcover series id, for a number; an exact name on Hardcover
    (case-sensitive there); then a name among their own series that contains it."""
    known = {i: n for shelf in shelves.values() for i, n in shelf.names.items()}
    folded = wanted.casefold()
    exact = sorted(i for i, n in known.items() if n.casefold() == folded)
    if exact:
        return await _fetch_series(client, SERIES, {"ids": exact})
    numeric = wanted.isascii() and wanted.isdigit()
    if numeric and (found := await _fetch_series(client, SERIES, {"ids": [int(wanted)]})):
        return found
    if found := await _fetch_series(client, SERIES_BY_NAME, {"name": wanted}):
        return found
    partial = sorted(i for i, n in known.items() if folded in n.casefold())
    if partial:
        return await _fetch_series(client, SERIES, {"ids": partial})
    raise ToolError(
        f"No series matches {wanted!r}. Try its exact name, or find a book in it with "
        "search_books and pass the series id from get_book."
    )


def _order(book_series: list[dict[str, Any]]) -> list[_Position]:
    """One entry per position, box sets and unpositioned rows left out (#128)."""
    at: dict[float, list[dict[str, Any]]] = {}
    for row in book_series:
        book = row.get("book") or {}
        if row.get("position") is None or book.get("compilation") or "id" not in book:
            continue
        at.setdefault(float(row["position"]), []).append(book)
    order: list[_Position] = []
    for position in sorted(at):
        books = at[position]
        top = max(books, key=lambda b: (b.get("users_count") or 0, -b["id"]))
        order.append(
            _Position(
                position, frozenset(b["id"] for b in books), (top["id"], top.get("title") or "")
            )
        )
    return order


def _status_at(position: _Position, shelf: dict[int, int]) -> int | None:
    held = {shelf[b] for b in position.book_ids if b in shelf}
    return next((s for s in _SHOWN_FIRST if s in held), None)


def _series_book(position: _Position) -> SeriesBook:
    book_id, title = position.shown
    return SeriesBook(position=position.position, book_id=book_id, title=title)


def _progress(name: str, whole: list[_Position], shelf: dict[int, int]) -> SeriesProgress:
    done = [p for p in whole if _status_at(p, shelf) == READ]
    upcoming = next((p for p in whole if _status_at(p, shelf) != READ), None)
    nxt: SeriesBook | None = None
    if upcoming is not None:
        nxt = _series_book(upcoming)
        status = _status_at(upcoming, shelf)
        nxt["status"] = STATUSES.get(status) if status else None
    return SeriesProgress(profile=name, read=len(done), next=nxt)


def _entry(
    position: _Position, names: tuple[str, ...], shelves: dict[str, _SeriesShelf]
) -> SeriesBook:
    entry = _series_book(position)
    status = {
        name: (STATUSES.get(s) if (s := _status_at(position, shelves[name].status)) else None)
        for name in names
    }
    if len(names) == 1:
        entry["status"] = status[names[0]]
    else:
        entry["statuses"] = status
    return entry


# --- search_books -------------------------------------------------------------------------

SearchSort = Literal["relevance", "popular", "rating", "newest"]
SEARCH_SORTS: dict[str, str | None] = {
    "relevance": None,
    "popular": "users_count:desc",
    "rating": "rating:desc",
    "newest": "release_year:desc",
}
"""Each ``sort`` value and the Typesense sort it sends [assembly choice]."""
SEARCH_PAGES = 4
"""Pages of 25 fetched at most to fill a page when shelved books are left out."""
DESCRIPTION_CHARS = 280


class SearchHit(TypedDict):
    """One book found, compactly, with whether the user has it shelved."""

    book_id: int
    title: str
    subtitle: str | None
    author: str | None
    release_year: int | None
    series: Series | None
    community_rating: float | None
    pages: int | None
    has_audiobook: bool
    has_ebook: bool
    genres: list[str]
    description: str | None
    """The start of Hardcover's description, cut at about 280 characters."""
    status: NotRequired[str | None]
    statuses: NotRequired[dict[str, str | None]]


class SearchPage(TypedDict):
    """``search_books``' answer, with what was left out."""

    books: list[SearchHit]
    found: int
    """Books Hardcover matched, before any were left out."""
    excluded_shelved: int
    """Matches left out because they're already shelved (among those looked at)."""
    incomplete: NotRequired[bool]
    """True when Hardcover failed partway through looking past shelved books."""
    as_of: str
    stale: bool
    stale_reason: NotRequired[str]


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def search_books(
    query: str | None = None,
    author: str | None = None,
    genre: str | None = None,
    sort: SearchSort = "relevance",
    exclude_shelved: bool = True,
    limit: Annotated[int, Field(ge=1, le=SEARCH_PAGE)] = 10,
    profile: str = ME,
) -> SearchPage:
    """Search all of Hardcover's books, live, and say which are already on the user's shelves.

    Use this to discover books ("what should I read next in fantasy?", "popular books by
    Ursula K. Le Guin") or to find a book's id for get_book. To check a list of specific
    titles you have in mind, use check_books instead: it resolves each one carefully and never
    substitutes a different book.

    Arguments (at least one of query, author, genre):
    - query: words to look for in titles, authors and series.
    - author: an author's exact full name, as Hardcover writes it (e.g. Frank Herbert).
    - genre: a Hardcover genre, exactly (e.g. Fantasy, Science Fiction).
    - sort: relevance (the default), popular (most Hardcover readers), rating (highest
      community rating) or newest (latest release year).
    - exclude_shelved: true (the default) leaves out every book the user has shelved, in any
      status, did-not-finish and ignored included, so suggestions are new to them. Set false
      to see shelved books too, each with its status.
    - limit: books to return, 1 to 25 (default 10).
    - profile: "me" (the default), another profile's name, or "household"; with household,
      a book on anyone's shelf is left out.

    Each book has book_id, title, subtitle, author, release_year, series (name and position),
    community_rating (Hardcover's average), pages, has_audiobook and has_ebook (whether
    Hardcover lists those editions), genres (top 3) and a short description, plus status
    (statuses per profile on household answers). found is Hardcover's total match count.
    incomplete is true when Hardcover failed while looking past shelved books; the books
    found so far are still returned.
    """
    # Backticks quote Typesense filter values, so they can't be part of one.
    author = _text(author.replace("`", "")) if author else None
    genre = _text(genre.replace("`", "")) if genre else None
    query = _text(query)
    if not (query or author or genre):
        raise ToolError("Give at least one of query, author or genre to search for.")
    caller = current_profile()
    bound = services()
    names = resolve_profiles(profile, caller, bound.profiles)
    freshness = await _refresh(bound.mirror, names, caller)
    variables: dict[str, Any] = {
        "q": query or "*",
        "filter": _search_filter(author, genre),
        "sort": SEARCH_SORTS[sort],
        # Only a search that leaves books out may need more than the hits asked for.
        "per": SEARCH_PAGE if exclude_shelved else limit,
        "page": 1,
    }
    client = bound.hardcover[names[0]]
    books: list[SearchHit] = []
    found = excluded = 0
    incomplete = False
    for page in range(1, SEARCH_PAGES + 1):
        variables["page"] = page
        try:
            result = await client.execute(SEARCH, variables)
        except HardcoverError:
            if page == 1:
                raise
            incomplete = True  # keep what earlier pages found
            break
        found, docs = _search_results(result)
        page_ids = tuple(i for d in docs if (i := _doc_id(d)) is not None)
        shelves = {name: await bound.db.read(_statuses, name, page_ids) for name in names}
        for doc in docs:
            book_id = _doc_id(doc)
            if book_id is None:
                continue
            if exclude_shelved and any(book_id in shelves[n] for n in names):
                excluded += 1
                continue
            books.append(_search_hit(doc, book_id, names, shelves))
            if len(books) == limit:
                break
        if len(books) == limit or len(docs) < variables["per"] or not exclude_shelved:
            break
    answer = SearchPage(
        books=books, found=found, excluded_shelved=excluded, **_freshness(freshness)
    )
    if incomplete:
        answer["incomplete"] = True
    return answer


def _text(value: str | None) -> str | None:
    """A text argument, trimmed, or ``None`` when absent or blank."""
    trimmed = value.strip() if value is not None else ""
    return trimmed or None


def _search_filter(author: str | None, genre: str | None) -> str | None:
    """Typesense ``filter_by`` for exact author and genre (backticks quote, so none inside)."""
    parts = []
    if author:
        parts.append(f"author_names:=[`{author.replace('`', '')}`]")
    if genre:
        parts.append(f"genres:=[`{genre.replace('`', '')}`]")
    return " && ".join(parts) or None


def _doc_id(doc: dict[str, Any]) -> int | None:
    """A Typesense document's book id (sent as a string), or ``None`` when unusable."""
    try:
        return int(doc["id"])
    except KeyError, TypeError, ValueError:
        return None


def _search_hit(
    doc: dict[str, Any], book_id: int, names: tuple[str, ...], shelves: dict[str, dict[int, str]]
) -> SearchHit:
    """A compact row from a Typesense document, with each profile's shelf status."""
    featured = doc.get("featured_series") or {}
    series = featured.get("series") or {}
    description = doc.get("description") or None
    if description:
        description = " ".join(description.split())  # newlines and runs of spaces
        if len(description) > DESCRIPTION_CHARS:
            description = description[:DESCRIPTION_CHARS].rsplit(None, 1)[0] + "…"
    hit = SearchHit(
        book_id=book_id,
        title=doc.get("title") or "",
        subtitle=doc.get("subtitle") or None,
        author=(doc.get("author_names") or [None])[0],
        release_year=doc.get("release_year") or None,  # 0 means unknown
        series=Series(name=series["name"], position=featured.get("position"))
        if series.get("name")
        else None,
        community_rating=_rounded(doc.get("rating") or None),
        pages=doc.get("pages") or None,
        has_audiobook=bool(doc.get("has_audiobook")),
        has_ebook=bool(doc.get("has_ebook")),
        genres=list(doc.get("genres") or [])[:TOP_GENRES],
        description=description,
    )
    if len(names) == 1:
        hit["status"] = shelves[names[0]].get(book_id)
    else:
        hit["statuses"] = {n: shelves[n].get(book_id) for n in names}
    return hit


# --- check_books --------------------------------------------------------------------------

MAX_CANDIDATES = 20
CANDIDATE_HITS = 5
"""Search hits scored per candidate, as the matcher's fuzzy step takes (§3.4a.5)."""

Match = Literal["exact", "likely", "not_found"]
_AUTHOR_SEPARATORS = re.compile(r"\s*(?:&|;|\band\b|\bwith\b)\s*", re.IGNORECASE)


class Candidate(TypedDict, total=False):
    """A book to check: a title and its author, or a Hardcover book_id."""

    title: str
    author: str
    book_id: int


class CheckedBook(TypedDict):
    """One candidate, resolved to at most one book."""

    input: Candidate
    match: Match
    book: NotRequired[SearchHit]
    """The book, when the match is exact or likely; never a closest guess otherwise."""
    error: NotRequired[str]
    """Why a not_found row couldn't be checked, or why a given id was refused."""


class CheckPage(TypedDict):
    """``check_books``' answer: one row per candidate, in the order given."""

    books: list[CheckedBook]
    as_of: str
    stale: bool
    stale_reason: NotRequired[str]


@dataclass
class _Pick:
    match: Match = "not_found"
    book_id: int | None = None
    error: str | None = None


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def check_books(
    candidates: Annotated[list[Candidate], Field(min_length=1, max_length=MAX_CANDIDATES)],
    profile: str = ME,
) -> CheckPage:
    """Check specific books against Hardcover: is each a real book, and is it already shelved?

    Use this to verify recommendations before offering them ("three books like X"), or to
    look up several titles at once. Each candidate resolves to one book or to not_found; it
    never guesses, so a candidate that isn't clearly one book is not_found rather than the
    nearest hit. For open-ended discovery, use search_books instead.

    Arguments:
    - candidates: 1 to 20 items, each {"title": ..., "author": ...} or {"book_id": ...}.
      Give the author with a title: a title alone can't be matched. For several authors give
      the first (the others are ignored). A book_id with a title is checked against it.
    - profile: "me" (the default), another profile's name, or "household" to see everyone's
      shelf status for each book.

    Each row echoes its input and has match: exact (a book_id given, or the top hit's title and
    author match closely and nothing else comes near), likely (close, but not enough to be
    sure: say so), or not_found. A matched row has book: book_id, title, author, release_year,
    series and position, community_rating, pages, has_audiobook and has_ebook (whether
    Hardcover has those editions), genres, and status (statuses on household answers; null
    when not shelved). A not_found row with error couldn't be checked this time (try again),
    or names why a given book_id was refused.
    """
    for candidate in candidates:
        if "book_id" not in candidate and not _text(candidate.get("title")):
            raise ToolError("Each candidate needs a title (with its author) or a book_id.")
    caller = current_profile()
    bound = services()
    names = resolve_profiles(profile, caller, bound.profiles)
    freshness = await _refresh(bound.mirror, names, caller)
    client = bound.hardcover[names[0]]

    # Titles resolve through search at the same time (the limiter paces them), each distinct
    # (title, author) once.
    keys = {_key(c) for c in candidates if "book_id" not in c}
    resolved: dict[tuple[str, str], _Pick] = {}

    async def resolve(key: tuple[str, str]) -> None:
        try:
            resolved[key] = await _resolve(client, *key)
        except HardcoverError as exc:
            resolved[key] = _Pick(error=str(exc))

    async with asyncio.TaskGroup() as tg:
        for key in keys:
            tg.create_task(resolve(key))

    # Every book matched, title or id, is read the same way in one batched request (§3.4a.5:
    # hits are hydrated), so a book's row doesn't depend on how it was asked for.
    wanted = {c["book_id"] for c in candidates if "book_id" in c}
    wanted |= {p.book_id for p in resolved.values() if p.book_id is not None}
    books: dict[int, dict[str, Any]] = {}
    hydrate_error: str | None = None
    if wanted:
        try:
            result = await client.execute(BOOKS_BY_ID, {"ids": sorted(wanted)})
            books = {b["id"]: b for b in result.data.get("books") or []}
        except HardcoverError as exc:
            hydrate_error = str(exc)
    shelves = {name: await bound.db.read(_statuses, name, tuple(books)) for name in names}

    rows: list[CheckedBook] = []
    for candidate in candidates:
        pick = _id_pick(candidate, books) if "book_id" in candidate else resolved[_key(candidate)]
        if pick.book_id is not None and pick.book_id not in books:
            pick = _Pick(error=hydrate_error)  # matched, but its details couldn't be read
        row = CheckedBook(input=candidate, match=pick.match)
        if pick.book_id is not None:
            doc = _doc_from_book(books[pick.book_id])
            row["book"] = _search_hit(doc, pick.book_id, names, shelves)
        if pick.error is not None:
            row["error"] = pick.error
        rows.append(row)
    return CheckPage(books=rows, **_freshness(freshness))


def _key(candidate: Candidate) -> tuple[str, str]:
    """(title, primary author), as searched and scored."""
    author = _text(candidate.get("author")) or ""
    primary = _AUTHOR_SEPARATORS.split(author, maxsplit=1)[0]
    return (_text(candidate.get("title")) or "", primary)


def _id_pick(candidate: Candidate, books: dict[int, dict[str, Any]]) -> _Pick:
    """A given id is exact when Hardcover has it, unless a title given with it doesn't fit."""
    book_id = candidate.get("book_id")
    book = books.get(book_id) if book_id is not None else None
    if book is None:
        return _Pick()
    title = _text(candidate.get("title"))
    if title and title_score(title, book.get("title") or "") < TITLE_FLOOR:
        return _Pick(error=f"Book {book_id} is {book.get('title')!r}, not {title!r}.")
    return _Pick("exact", book_id)


async def _resolve(client: HardcoverClient, title: str, author: str) -> _Pick:
    """Search as the matcher's fuzzy step does (§3.4a.5): by normalised title, filtered to the
    primary author, once more without the filter if that finds nothing; then score the hits.
    With no author the author score is 0, so nothing matches."""
    variables: dict[str, Any] = {
        "q": normalise(title) or title,
        "filter": _search_filter(author or None, None),
        "sort": None,
        "per": CANDIDATE_HITS,
        "page": 1,
    }
    _, docs = _search_results(await client.execute(SEARCH, variables))
    if not docs and variables["filter"]:
        variables["filter"] = None
        _, docs = _search_results(await client.execute(SEARCH, variables))
    scored = sorted(
        (
            (
                Scored(
                    title_score(title, d.get("title") or ""),
                    author_score(author, d.get("author_names") or []),
                ),
                book_id,
            )
            for d in docs
            if (book_id := _doc_id(d)) is not None
        ),
        key=lambda pair: (pair[0].title, pair[0].author),
        reverse=True,
    )
    ranked = [s for s, _ in scored]
    if auto_accepts(ranked):
        return _Pick("exact", scored[0][1])
    if is_likely(ranked):
        return _Pick("likely", scored[0][1])
    return _Pick()


def _search_results(result: Result) -> tuple[int, list[dict[str, Any]]]:
    """A Search answer's total and its documents."""
    results = (result.data.get("search") or {}).get("results") or {}
    return int(results.get("found") or 0), [
        h.get("document") or {} for h in results.get("hits") or []
    ]


def _doc_from_book(book: dict[str, Any]) -> dict[str, Any]:
    """A ``books`` row in the shape of a Typesense document, so one row builder serves both."""
    people = book.get("cached_contributors") or []
    return {
        "id": str(book["id"]),
        "title": book.get("title"),
        "subtitle": book.get("subtitle"),
        "author_names": [
            c["author"]["name"]
            for c in people
            if c.get("contribution") in AUTHOR_ROLES and (c.get("author") or {}).get("name")
        ],
        "release_year": book.get("release_year"),
        "featured_series": book.get("cached_featured_series") or None,
        "rating": book.get("rating"),
        "pages": book.get("pages"),
        "has_audiobook": book.get("default_audio_edition") is not None,
        "has_ebook": book.get("default_ebook_edition") is not None,
        "genres": tag_names(book.get("genres")),
        "description": None,
    }
