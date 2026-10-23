"""``unmatched_books``: the books SyncShelf couldn't place on Hardcover by itself (SPEC
§3.4a.5, §3.4a.11).

Each entry is one queued source row: its opaque ``entry_id``, why it's queued, the book as
Goodreads gave it, what would reach Hardcover once it's resolved (the pending diff, in
words), and the candidates the matcher kept, with enough to tell them apart. Claude settles an
entry with ``resolve_match``, with a candidate or any book found through ``search_books``.
"""

import sqlite3
from typing import Any, Literal, NotRequired, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from syncshelf.clients.hardcover import BOOKS_BY_ID, HardcoverError
from syncshelf.config import ProfileConfig
from syncshelf.logging import scrub
from syncshelf.sync import identity
from syncshelf.sync.identity import SourceRow
from syncshelf.sync.mirror import AUTHOR_ROLES, FetchFailed
from syncshelf.tools import REGISTRY, current_profile, iso, services, with_attention

__all__ = ["SHOWN", "Entry", "Resolved", "resolve_match", "unmatched_books"]

SHOWN = 50
"""Entries listed, oldest first; ``total`` counts them all."""
BOOK_URL = "https://hardcover.app/books/{slug}"
STATUS_NAMES = {
    1: "Want to Read",
    2: "Currently Reading",
    3: "Read",
    4: "Paused",
    5: "Did Not Finish",
}


class Candidate(TypedDict):
    """A book an entry might be."""

    book_id: int
    title: str | None
    subtitle: str | None
    authors: list[str]
    release_year: int | None
    url: str | None
    method: str
    title_score: float | None
    author_score: float | None


class Seen(TypedDict):
    """The book as Goodreads gave it."""

    title: str
    author: str | None
    isbn: str | None
    format: str | None


class Entry(TypedDict):
    entry_id: str
    source: str
    state: Literal["queued", "ignored"]
    reason: str | None
    queued_at: str | None
    input: Seen
    pending: str
    """What resolving it would push, in words."""
    candidates: list[Candidate]


class Unmatched(TypedDict):
    total: int
    entries: list[Entry]
    error: NotRequired[str]
    """What went wrong this time: a refresh that couldn't run, or candidates whose details
    couldn't be read (they're listed by id)."""


def _rows(
    conn: sqlite3.Connection, profile: str, include_ignored: bool
) -> tuple[int, list[dict[str, Any]]]:
    """The entries listed, with their baselines and candidates: three queries in all."""
    states = ("queued", "ignored") if include_ignored else ("queued",)
    marks = ",".join("?" * len(states))
    total = conn.execute(
        f"SELECT count(*) FROM source_rows WHERE profile = ? AND source = 'goodreads' "  # noqa: S608
        f"AND state IN ({marks})",
        (profile, *states),
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT source_id, state, queue_reason, queued_at, match_input, backfill_queued "  # noqa: S608
        f"FROM source_rows WHERE profile = ? AND source = 'goodreads' AND state IN ({marks}) "
        "ORDER BY queued_at IS NULL, queued_at, source_id LIMIT ?",
        (profile, *states, SHOWN),
    ).fetchall()
    ids = [row[0] for row in rows]
    listed = ",".join("?" * len(ids))
    baselines: dict[str, dict[str, tuple[Any, Any, Any]]] = {}
    candidates: dict[str, list[tuple[Any, ...]]] = {}
    if ids:
        for source_id, field, value, pending, since in conn.execute(
            "SELECT source_id, field, value, pending_value, pending_since FROM baseline_fields "  # noqa: S608
            f"WHERE profile = ? AND source = 'goodreads' AND source_id IN ({listed})",
            (profile, *ids),
        ):
            baselines.setdefault(source_id, {})[field] = (value, pending, since)
        for source_id, *candidate in conn.execute(
            "SELECT source_id, hc_book_id, method, title_score, author_score "  # noqa: S608
            f"FROM match_candidates WHERE profile = ? AND source = 'goodreads' AND source_id IN "
            f"({listed}) ORDER BY rank",
            (profile, *ids),
        ):
            candidates.setdefault(source_id, []).append(tuple(candidate))
    return total, [
        {
            "source_id": source_id,
            "state": state,
            "reason": reason,
            "queued_at": queued_at,
            "match_input": given,
            "backfill": bool(backfill),
            "baselines": baselines.get(source_id, {}),
            "candidates": candidates.get(source_id, []),
        }
        for source_id, state, reason, queued_at, given, backfill in rows
    ]


def _queued(conn: sqlite3.Connection, profile: str) -> list[str]:
    """The queued entries a refresh tries, oldest first, as many as are listed."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT source_id FROM source_rows WHERE profile = ? AND source = 'goodreads' "
            "AND state = 'queued' ORDER BY queued_at, source_id LIMIT ?",
            (profile, SHOWN),
        )
    ]


def _words(row: dict[str, Any], profile: ProfileConfig) -> str:
    """The pending diff in words ("would mark Read, finished 2026-03-02, rate 4")."""
    shelf_map = profile.goodreads.shelf_map if profile.goodreads else {}
    pending = {f: p for f, (_, p, since) in row["baselines"].items() if since is not None}
    # A row the backfill queued has nothing pending; resolving it onto a book not yet on the
    # shelves adds it as it stands on Goodreads (§3.4a.12).
    current = {f: v for f, (v, _, _) in row["baselines"].items()}
    parts = _parts(pending, shelf_map)
    if parts:
        return "would " + ", ".join(parts)
    if row["backfill"]:
        added = _parts({f: v for f, v in current.items() if v is not None}, shelf_map)
        if added:
            return "nothing new; if the book chosen isn't on the shelves, adds it: " + ", ".join(
                added
            )
    return "nothing to push"


def _parts(fields: dict[str, Any], shelf_map: Any) -> list[str]:
    parts: list[str] = []
    if "status" in fields and fields["status"] is not None:
        status = shelf_map.get(fields["status"])
        parts.append(f"mark {STATUS_NAMES[status]}" if status else "leave the status")
    if fields.get("date_read"):
        parts.append(f"finished {fields['date_read']}")
    if "rating" in fields:
        parts.append(f"rate {fields['rating']}" if fields["rating"] else "clear the rating")
    if "review" in fields:
        parts.append("set the review" if fields["review"] else "clear the review")
    return parts


def _seen(row: dict[str, Any]) -> Seen:
    seen = identity.seen(
        SourceRow(row["source_id"], "queued", None, None, match_input=row["match_input"])
    )
    return Seen(
        title=seen.title or f"Goodreads book {row['source_id']}",
        author=seen.author,
        isbn=seen.isbn13 or seen.isbn10,
        format=seen.format,
    )


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
@with_attention
async def unmatched_books(
    source: Literal["goodreads", "abs"] | None = None,
    include_ignored: bool = False,
    refresh: bool = False,
) -> Unmatched:
    """The caller's books that SyncShelf couldn't place on a Hardcover book by itself: use it
    when the user asks what didn't sync, or to tidy up after sync_status shows unmatched
    books.

    Each entry has an entry_id, why it's queued (no_hit, id_title_mismatch, fuzzy_low,
    fuzzy_ambiguous, ambiguous_mapping, not_in_library, profile_conflict, stale), when, the
    book as Goodreads gave it, what resolving it would push ("would mark Read, finished
    2026-03-02, rate 4"), and the candidates SyncShelf found, each with its Hardcover link and
    scores. Settle an entry with resolve_match: with a candidate's book_id, with any book
    found through search_books when none fits, or with none to ignore it.

    - source: "goodreads" (the default and, for now, the only one).
    - include_ignored: also list entries already ignored.
    - refresh: match the listed entries again first (their books may have reached Hardcover
      since).

    At most 50 entries, oldest first; total counts them all.
    """
    if source == "abs":
        raise ToolError("Audiobookshelf syncing arrives in phase 5.")
    caller = current_profile()
    bound = services()
    error = None
    if refresh:
        try:
            linked = await bound.scheduler.sync.refresh(
                caller, await bound.db.read(_queued, caller)
            )
        except (HardcoverError, FetchFailed) as exc:
            linked = []
            error = f"matching again failed: {scrub(str(exc))}"
        if linked is None:
            error = "a sync is running, so nothing was matched again; try again shortly"
        elif linked:
            bound.scheduler.nudge(caller, "sync_now")  # their changes go out now
    total, rows = await bound.db.read(_rows, caller, include_ignored)
    ids = sorted({c[0] for r in rows for c in r["candidates"]})
    books: dict[int, dict[str, Any]] = {}
    if ids:
        try:
            result = await bound.hardcover[caller].execute(BOOKS_BY_ID, {"ids": ids})
            books = {b["id"]: b for b in result.data.get("books") or []}
        except HardcoverError as exc:
            error = f"the candidates' details couldn't be read: {scrub(str(exc))}"
    entries = [
        Entry(
            entry_id=f"goodreads:{r['source_id']}",
            source="goodreads",
            state=r["state"],
            reason=r["reason"],
            queued_at=iso(r["queued_at"]),
            input=_seen(r),
            pending=_words(r, bound.profiles[caller]),
            candidates=[_candidate(c, books.get(c[0])) for c in r["candidates"]],
        )
        for r in rows
    ]
    answer = Unmatched(total=total, entries=entries)
    if error is not None:
        answer["error"] = error
    return answer


def _candidate(row: tuple[Any, ...], book: dict[str, Any] | None) -> Candidate:
    book_id, method, title_score, author_score = row
    book = book or {}
    people = book.get("cached_contributors") or []
    slug = book.get("slug")
    return Candidate(
        book_id=book_id,
        title=book.get("title"),
        subtitle=book.get("subtitle"),
        authors=[
            c["author"]["name"]
            for c in people
            if c.get("contribution") in AUTHOR_ROLES and (c.get("author") or {}).get("name")
        ],
        release_year=book.get("release_year"),
        url=BOOK_URL.format(slug=slug) if slug else None,
        method=method,
        title_score=title_score,
        author_score=author_score,
    )


class Resolved(TypedDict):
    """What ``resolve_match`` did."""

    entry_id: str
    state: Literal["linked", "ignored"]
    book_id: NotRequired[int]
    title: NotRequired[str | None]
    shared: NotRequired[bool]
    """Whether both profiles now use this book for the Goodreads book, or only the caller."""
    pushed: NotRequired[list[str]]
    """The fields that reached Hardcover."""
    failed: NotRequired[str]
    note: NotRequired[str]


def _entry(entry_id: str) -> str:
    source, _, source_id = entry_id.partition(":")
    if source == "abs":
        raise ToolError("Audiobookshelf syncing arrives in phase 5.")
    if source != "goodreads" or not source_id.isdigit():
        raise ToolError(f"{entry_id!r} isn't an entry id from unmatched_books.")
    return source_id


def _state(conn: sqlite3.Connection, profile: str, source_id: str) -> str | None:
    found = conn.execute(
        "SELECT state FROM source_rows WHERE profile = ? AND source = 'goodreads' "
        "AND source_id = ?",
        (profile, source_id),
    ).fetchone()
    return found[0] if found else None


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=False))
async def resolve_match(entry_id: str, hardcover_book_id: int | None) -> Resolved:
    """Settle which Hardcover book a Goodreads book is, after the user has confirmed it:
    with an entry from unmatched_books, or for any synced book that was linked wrongly.

    - entry_id: as unmatched_books gives it ("goodreads:<id>").
    - hardcover_book_id: the book: a candidate's book_id, or one found with search_books.
      None ignores the entry: it's never matched or synced again until resolved with a book.

    The link is written as the user's own, never replaced by matching. What Goodreads has
    for the book then reaches Hardcover at once; the result says what was pushed, and if a
    change failed, the link stands and the next sync tries again. A book that was on the
    shelves under the old link keeps its shelf entry: the result names it, and remove_book
    can take it off, once the user confirms.
    """
    source_id = _entry(entry_id)
    caller = current_profile()
    bound = services()
    if await bound.db.read(_state, caller, source_id) is None:
        raise ToolError(f"{entry_id} isn't one of your Goodreads books.")
    if hardcover_book_id is None:
        await bound.db.write(
            identity.ignore_row, caller, "goodreads", source_id, int(bound.clock())
        )
        return Resolved(entry_id=entry_id, state="ignored")
    try:
        result = await bound.hardcover[caller].execute(BOOKS_BY_ID, {"ids": [hardcover_book_id]})
    except HardcoverError as exc:
        raise ToolError(f"Hardcover couldn't be asked about that book: {scrub(str(exc))}") from None
    (book,) = result.data.get("books") or [None]
    if book is None:
        raise ToolError(f"Hardcover has no book {hardcover_book_id}; find it with search_books.")
    book_id = book.get("canonical_id") or hardcover_book_id  # a merged book: the one it became
    done = await bound.scheduler.sync.resolve(caller, source_id, book_id)
    answer = Resolved(
        entry_id=entry_id,
        state="linked",
        book_id=book_id,
        title=book.get("title"),
        shared=done.shared,
        pushed=done.pushed,
    )
    if done.failed:
        answer["failed"] = done.failed
    if done.old_book is not None:
        answer["note"] = (
            f"Book {done.old_book} still has a shelf entry from the old link; remove_book can "
            "take it off if it shouldn't be there."
        )
    return answer
