"""The read tools over a profile's Hardcover library (SPEC §3.3.7, §3.3.8).

``get_my_library`` reads the mirror (§3.3.4), refreshed first when it's older than the TTL,
and answers in compact **library rows** of a fixed shape, so a page of 25 stays small in
Claude's context. Every answer carries ``as_of``, and ``stale`` with a reason when the refresh
failed and the copy was served anyway.

Reads without a format are reported in an explicit ``unknown`` bucket, never guessed (§3.3.2).
"""

import base64
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from syncshelf.sync.mirror import Freshness, NoMirror
from syncshelf.tools import REGISTRY, current_profile, services

__all__ = ["FORMATS", "STATUSES", "LibraryPage", "LibraryRow", "get_my_library"]

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
class _Book:
    user_book_id: int
    date_added: str | None
    row: LibraryRow


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def get_my_library(
    status: Status | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
    cursor: str | None = None,
) -> LibraryPage:
    """List books on the caller's Hardcover shelves, newest additions first.

    Use this for questions about the user's own library: what they're currently reading,
    what's on their want-to-read, what they've read, and how many of each. For one book's full
    record (reads with dates, review, series, tags) use get_book instead.

    Arguments:
    - status: one shelf, as a slug: want-to-read, currently-reading, read, paused,
      did-not-finish or ignored. Leave it out for every shelf.
    - limit: rows per page, 1 to 100 (default 25).
    - cursor: the next_cursor from the previous page, to get the next one.

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
    profile = current_profile()
    after = _after(cursor)
    bound = services()
    try:
        freshness = await bound.mirror.fresh(profile)
    except NoMirror as exc:
        raise ToolError(str(exc)) from None
    books = await bound.db.read(_library, profile)
    if status is not None:
        books = [b for b in books if b.row["status"] == status]
    # Newest first; books with no date_added go last, then by Hardcover's id.
    books.sort(key=_sort_key, reverse=True)
    # The cursor is the last row's sort key, not an offset, so a refresh between pages
    # (books added or removed) never repeats or skips a row.
    rest = books if after is None else [b for b in books if _sort_key(b) < after]
    page = rest[:limit]
    result = LibraryPage(
        books=[b.row for b in page],
        total=len(books),
        next_cursor=_cursor(_sort_key(page[-1])) if len(rest) > limit else None,
        **_freshness(freshness),
    )
    if len(books) > len(page):
        counts = Counter(b.row["status"] for b in books)
        result["counts_by_status"] = {s: counts[s] for s in STATUSES.values() if counts[s]}
    return result


# --- reading the mirror -------------------------------------------------------------------


def _library(conn: sqlite3.Connection, profile: str) -> list[_Book]:
    reads: dict[int, list[tuple[str | None, int | None]]] = {}
    for user_book_id, finished_on, format_id in conn.execute(
        "SELECT hc_user_book_id, finished_on, reading_format_id FROM mirror_reads "
        "WHERE profile = ?",
        (profile,),
    ):
        reads.setdefault(user_book_id, []).append((finished_on, format_id))
    rows = conn.execute(
        "SELECT ub.hc_user_book_id, ub.hc_book_id, ub.status_id, ub.rating, ub.date_added, "
        "b.title, b.contributors_json, b.series_name, b.series_position, b.tags_json "
        "FROM mirror_user_books ub LEFT JOIN mirror_books b "
        "ON b.profile = ub.profile AND b.hc_book_id = ub.hc_book_id "
        "WHERE ub.profile = ?",
        (profile,),
    )
    return [_book(row, reads.get(row[0], [])) for row in rows]


def _book(row: tuple[Any, ...], reads: list[tuple[str | None, int | None]]) -> _Book:
    (user_book_id, book_id, status_id, rating, date_added, title, contributors, series_name,
     series_position, tags) = row  # fmt: skip
    finished = [f for f, _ in reads if f]
    formats = sorted({fmt for _, fid in reads for fmt in FORMATS.get(fid or 0, (UNKNOWN,))})
    return _Book(
        user_book_id,
        date_added,
        LibraryRow(
            book_id=book_id,
            title=title or "",
            author=_first_author(json.loads(contributors) if contributors else []),
            status=STATUSES.get(status_id, str(status_id)),
            rating=rating,
            formats=formats,
            last_finished=max(finished) if finished else None,
            read_count=len(reads),
            series=Series(name=series_name, position=series_position) if series_name else None,
            genres=(json.loads(tags).get("genres", []) if tags else [])[:TOP_GENRES],
        ),
    )


def _first_author(contributors: list[dict[str, Any]]) -> str | None:
    """The first contributor credited as author: ``cached_contributors`` gives authors no
    role, and narrators, editors or translators one."""
    for c in contributors:
        if not c.get("role"):
            return c["name"]
    # Only narrators, editors and the like: naming one as the author would mislead.
    return None


# --- paging and freshness -----------------------------------------------------------------


type _Key = tuple[str, int]


def _sort_key(book: _Book) -> _Key:
    return (book.date_added or "", book.user_book_id)


def _cursor(key: _Key) -> str:
    return base64.urlsafe_b64encode(json.dumps(list(key)).encode()).decode().rstrip("=")


def _after(cursor: str | None) -> _Key | None:
    if cursor is None:
        return None
    try:
        key = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        date_added, user_book_id = key
        if not isinstance(date_added, str) or type(user_book_id) is not int:
            raise ValueError
        return (date_added, user_book_id)
    except ValueError, TypeError:  # binascii.Error and JSON/Unicode errors are ValueErrors
        raise ToolError(
            "That cursor isn't one this tool gave out. Pass next_cursor from the previous "
            "page, or leave cursor out to start from the beginning."
        ) from None


def _freshness(freshness: Freshness) -> dict[str, Any]:
    out: dict[str, Any] = {
        "as_of": datetime.fromtimestamp(freshness.as_of, UTC).isoformat(),
        "stale": freshness.stale,
    }
    if freshness.stale_reason is not None:
        out["stale_reason"] = freshness.stale_reason
    return out
