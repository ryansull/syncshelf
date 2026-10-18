"""Claude's direct edits to the caller's Hardcover shelves (SPEC §3.3.9).

Every edit goes through the write procedure (:mod:`syncshelf.sync.apply`): fresh state, no-op
skips, inserts with the profile's ``hardcover_privacy``, a write-through to the mirror, and the
prior values of whatever changed, so Claude can undo. Edits act on the first call; Claude asks
the user in conversation first. They touch only the caller's own profile and never a Goodreads
or ABS baseline (4a), so a later unchanged source row can't overwrite them.
"""

from datetime import date
from typing import Any, Literal, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from syncshelf.clients.hardcover import (
    HardcoverError,
    HardcoverHTTPError,
    HardcoverMutationError,
    HardcoverUnavailable,
)
from syncshelf.sync.apply import (
    UNSET,
    Applied,
    DateNeeded,
    Finish,
    FormatNeeded,
    Incomplete,
    NotShelved,
    OpenReadExists,
    ReadOp,
    Start,
    Target,
)
from syncshelf.sync.limiter import ServeStale
from syncshelf.tools import REGISTRY, current_profile, services

__all__ = ["STATUS_IDS", "Edit", "update_book"]

STATUS_IDS = {
    "want-to-read": 1,
    "currently-reading": 2,
    "read": 3,
    "paused": 4,
    "dnf": 5,
    "ignored": 6,
}
"""``update_book``'s status slugs and Hardcover's status ids (§3.3.9)."""

_SLUGS = {v: k for k, v in STATUS_IDS.items()}

StatusSlug = Literal["want-to-read", "currently-reading", "read", "paused", "dnf", "ignored"]
Format = Literal["audio", "ebook", "print"]


class FieldChange(TypedDict):
    """One field changed, with its value before and after, for undoing."""

    field: str
    before: Any
    after: Any


class Edit(TypedDict):
    """What an edit did."""

    book_id: int
    added_to_shelf: bool
    """True when the book wasn't on the shelf and was added."""
    changes: list[FieldChange]
    """Empty when everything already matched."""


@REGISTRY.tool(annotations=ToolAnnotations(destructive_hint=False))
async def update_book(
    book_id: int,
    status: StatusSlug | None = None,
    rating: float | None = None,
    review: str | None = None,
    review_has_spoilers: bool | None = None,
    started_on: date | None = None,
    finished_on: date | None = None,
    format: Format | None = None,  # noqa: A002 - the spec's argument name
    reread: bool = False,
) -> Edit:
    """Change a book on the caller's Hardcover shelves: status, rating, review, or a read.

    Use this when the user asks to shelve a book, move it to another shelf, rate or review it,
    or log that they started or finished it. It acts at once, so confirm with the user first.
    Only the caller's own shelves can be changed. Give the Hardcover book_id (from
    get_my_library, get_book, search_books or check_books). A book not yet on a shelf is added.

    Arguments (each optional; leave out what shouldn't change):
    - status: want-to-read, currently-reading, read, paused, dnf (did not finish) or
      ignored. To take a book off the shelves entirely, use remove_book.
    - rating: 0.5 to 5 in half stars; 0 clears the rating.
    - review: the review text (Markdown); "" clears the review and its spoiler flag.
    - review_has_spoilers: whether the review gives the plot away.
    - finished_on: the date they finished it (ISO). Implies status read. It completes the
      read they had open, or one already logged within 14 days of it; otherwise it logs a new
      read. started_on with it gives that read's start.
    - started_on: the date they started it (ISO). Alone, it implies currently-reading and
      opens a read; it's refused if a read is already open (use correct_read for that one).
    - format: audio, ebook or print, for the read written. Needed when a new read is logged
      and how they read it isn't known; the tool refuses with format_needed rather than guess.
    - reread: true to log a new read even if one near that date exists.

    Dates: only ever a date the user stated. For "I just finished it", pass today's date;
    resolve "yesterday" or "last Tuesday" to an ISO date in the user's own time zone. Setting
    read or currently-reading without a date logs no read. Setting read on a book with a read
    still open is refused with date_needed, naming when that read started: ask when they
    finished it. Paused and dnf leave reads alone.

    The result lists each field changed with its value before and after (a read as its
    read_id, dates and format), so you can undo an edit; correct_read edits a logged read. A
    field that already had the value isn't written.
    """
    if review == "" and review_has_spoilers:
        raise ToolError("A cleared review has no spoilers: leave out review_has_spoilers.")
    op, status = _read_op(status, started_on, finished_on, format, reread)
    target = Target(
        status_id=STATUS_IDS[status] if status is not None else UNSET,
        rating=_rating(rating),
        review=(review or None) if review is not None else UNSET,
        review_has_spoilers=review_has_spoilers if review_has_spoilers is not None else UNSET,
        read=op,
    )
    if target == Target():
        raise ToolError("Nothing to change: give a status, rating, review or a date.")
    try:
        applied = await services().applier.apply(current_profile(), book_id, target)
    except NotShelved:
        raise ToolError(
            f"Book {book_id} isn't on the shelves yet, so give a status to add it with "
            "(want-to-read, currently-reading, read, paused, dnf or ignored)."
        ) from None
    except DateNeeded as exc:
        raise ToolError(
            f"date_needed: this book has a read still open, started {exc.started_on}. Ask "
            "when they finished it and pass finished_on; nothing was changed."
        ) from None
    except OpenReadExists as exc:
        raise ToolError(
            f"A read is already open, started {exc.started_on}: change its start with "
            "correct_read instead; nothing was changed."
        ) from None
    except FormatNeeded:
        raise ToolError(
            "format_needed: a read will be logged, but whether it was audio, ebook or print "
            "isn't known. Ask, and pass format; nothing was changed."
        ) from None
    except Incomplete as exc:
        raise ToolError(
            f"Partly done: Hardcover failed while fixing the book's reads ({exc}). Call "
            "update_book again with the same values to finish it."
        ) from None
    except ServeStale as exc:
        raise ToolError(f"{exc}. Nothing was changed; try again then.") from None
    except HardcoverMutationError as exc:
        raise ToolError(f"Hardcover refused the change, and nothing was changed: {exc}") from None
    except HardcoverUnavailable as exc:
        raise ToolError(f"Hardcover couldn't be reached, and nothing was changed: {exc}") from None
    except HardcoverHTTPError as exc:
        raise ToolError(
            f"Hardcover turned the request away ({exc}); nothing was changed. If it says the "
            "token is invalid, the profile's hardcover_token needs replacing."
        ) from None
    except HardcoverError as exc:
        raise ToolError(
            f"Hardcover answered unexpectedly, so the edit may not have landed: {exc}"
        ) from None
    return _edit(book_id, applied)


def _read_op(
    status: StatusSlug | None,
    started_on: date | None,
    finished_on: date | None,
    fmt: str | None,
    reread: bool,
) -> tuple[ReadOp | None, StatusSlug | None]:
    """The read to write and the status it implies (§3.3.9): a finish date implies read, a
    start date alone currently-reading."""
    if finished_on is not None:
        if status not in (None, "read"):
            raise ToolError("finished_on means the book was read: leave status out or use read.")
        if started_on is not None and started_on > finished_on:
            raise ToolError("started_on is after finished_on; give the earlier date first.")
        start = started_on.isoformat() if started_on else None
        return Finish(finished_on.isoformat(), start, fmt, reread), "read"
    if started_on is not None:
        if status not in (None, "currently-reading"):
            raise ToolError(
                "started_on alone opens a read: leave status out or use currently-reading."
            )
        return Start(started_on.isoformat(), fmt), "currently-reading"
    if fmt is not None or reread:
        raise ToolError("format and reread go with a date: give finished_on or started_on.")
    return None, status


def _rating(rating: float | None) -> Any:
    if rating is None:
        return UNSET
    if rating == 0:
        return None  # 0 clears
    if not (0.5 <= rating <= 5 and (rating * 2).is_integer()):
        raise ToolError("rating must be 0.5 to 5 in half stars, or 0 to clear it.")
    return rating


def _edit(book_id: int, applied: Applied) -> Edit:
    def shown(field: str, value: Any) -> Any:
        return _SLUGS.get(value, value) if field == "status_id" else value

    return Edit(
        book_id=book_id,
        added_to_shelf=applied.inserted,
        changes=[
            FieldChange(
                field="status" if c.field == "status_id" else c.field,
                before=shown(c.field, c.before),
                after=shown(c.field, c.after),
            )
            for c in applied.changes
        ],
    )
