"""``unmatched_books``: the books SyncShelf couldn't place on Hardcover by itself (SPEC
§3.4a.5, §3.4a.11).

Each entry is one queued source row: its opaque ``entry_id``, why it's queued, the book as
Goodreads gave it, what would reach Hardcover once it's resolved (the pending diff, in
words), and the candidates the matcher kept, with enough to tell them apart. Claude settles an
entry with ``resolve_match``, with a candidate or any book found through ``search_books``.
"""

import json
import sqlite3
from typing import Any, Literal, NotRequired, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from syncshelf.clients.hardcover import BOOKS_BY_ID, HardcoverError
from syncshelf.config import ProfileConfig
from syncshelf.logging import scrub
from syncshelf.sync.mirror import AUTHOR_ROLES
from syncshelf.tools import REGISTRY, current_profile, iso, services, with_attention

__all__ = ["SHOWN", "Entry", "unmatched_books"]

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
    """Why the candidates' details couldn't be read this time (they're listed by id)."""


def _rows(
    conn: sqlite3.Connection, profile: str, include_ignored: bool
) -> tuple[int, list[dict[str, Any]]]:
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
    found = []
    for source_id, state, reason, queued_at, given, backfill in rows:
        baselines = {
            field: (value, pending, since)
            for field, value, pending, since in conn.execute(
                "SELECT field, value, pending_value, pending_since FROM baseline_fields WHERE "
                "profile = ? AND source = 'goodreads' AND source_id = ?",
                (profile, source_id),
            )
        }
        candidates = conn.execute(
            "SELECT hc_book_id, method, title_score, author_score FROM match_candidates WHERE "
            "profile = ? AND source = 'goodreads' AND source_id = ? ORDER BY rank",
            (profile, source_id),
        ).fetchall()
        found.append(
            {
                "source_id": source_id,
                "state": state,
                "reason": reason,
                "queued_at": queued_at,
                "input": json.loads(given) if given else {},
                "backfill": bool(backfill),
                "baselines": baselines,
                "candidates": candidates,
            }
        )
    return total, found


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


def _seen(given: dict[str, Any], source_id: str) -> Seen:
    isbns = given.get("isbns") or []
    return Seen(
        title=given.get("title") or f"Goodreads book {source_id}",
        author=(given.get("authors") or [None])[0],
        isbn=isbns[0] if isbns else None,
        format=given.get("format"),
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
    if refresh:
        _, listed = await bound.db.read(_rows, caller, False)
        await bound.scheduler.sync.refresh(caller, [r["source_id"] for r in listed])
    total, rows = await bound.db.read(_rows, caller, include_ignored)
    ids = sorted({c[0] for r in rows for c in r["candidates"]})
    books: dict[int, dict[str, Any]] = {}
    error = None
    if ids:
        try:
            result = await bound.hardcover[caller].execute(BOOKS_BY_ID, {"ids": ids})
            books = {b["id"]: b for b in result.data.get("books") or []}
        except HardcoverError as exc:
            error = scrub(str(exc))
    entries = [
        Entry(
            entry_id=f"goodreads:{r['source_id']}",
            source="goodreads",
            state=r["state"],
            reason=r["reason"],
            queued_at=iso(r["queued_at"]),
            input=_seen(r["input"], r["source_id"]),
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
