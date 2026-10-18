"""Claude's direct edits to the caller's Hardcover shelves (SPEC §3.3.9).

Every edit goes through :mod:`syncshelf.sync.apply` (``correct_read`` beside the procedure, as
it changes no status): fresh state, no-op skips, inserts with the profile's
``hardcover_privacy``, a write-through to the mirror, and the prior values of whatever changed,
so Claude can undo. Edits act on the first call; Claude asks
the user in conversation first. They touch only the caller's own profile and never a Goodreads
or ABS baseline (4a), so a later unchanged source row can't overwrite them.
"""

from collections.abc import Iterator
from contextlib import contextmanager
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
    READ_FIELDS,
    UNSET,
    Applied,
    DateNeeded,
    DatesOutOfOrder,
    Finish,
    FormatNeeded,
    Incomplete,
    NotShelved,
    OpenReadExists,
    ReadNotFound,
    ReadOp,
    ReadStillOpen,
    Start,
    Target,
)
from syncshelf.sync.limiter import ServeStale
from syncshelf.tools import REGISTRY, current_profile, services
from syncshelf.tools.confirm import Redeemed, snapshot_of

__all__ = [
    "STATUS_IDS",
    "Edit",
    "ReadEdit",
    "Removal",
    "correct_read",
    "remove_book",
    "update_book",
]

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
_FORMAT_NAMES = {1: "print", 2: "audio", 3: "both", 4: "ebook"}

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


class ShownRead(TypedDict):
    """One read: its id, dates and format."""

    read_id: int
    started_on: str | None
    finished_on: str | None
    format: str | None


class ReadEdit(Edit):
    """What correct_read did, or, for a deletion not yet confirmed, what it would delete."""

    deleted: bool
    preview: ShownRead | None
    """The read a deletion would remove, until it's confirmed."""
    confirm_token: str | None
    """For a deletion's preview: pass it back to delete, within 10 minutes."""
    note: str | None


class Removal(TypedDict):
    """What remove_book found on the shelf, and whether it removed it."""

    book_id: int
    title: str | None
    removed: bool
    status: str | None
    rating: float | None
    has_review: bool
    review_has_spoilers: bool
    reads: list[ShownRead]
    has_journal: bool
    """Whether Hardcover holds reading-journal rows (its activity history) for the book."""
    linked_source_rows: list[str]
    """Goodreads or Audiobookshelf rows still linked to the book: none until those syncs land."""
    confirm_token: str | None
    """For a preview: pass it back to remove the book, within 10 minutes."""
    note: str


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
        with _hardcover_errors():
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
    except ReadStillOpen as exc:
        raise ToolError(
            f"A read is still open, started {exc.started_on}, and this finish wouldn't "
            "complete it (it started later, or reread was asked). Finish that read, or change "
            "it with correct_read first; nothing was changed."
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
    return _edit(book_id, applied)


@REGISTRY.tool(annotations=ToolAnnotations(destructive_hint=True))
async def correct_read(
    book_id: int,
    read_id: int,
    started_on: date | None = None,
    finished_on: date | None = None,
    format: Format | None = None,  # noqa: A002 - the spec's argument name
    delete: bool = False,
    confirm_token: str | None = None,
) -> ReadEdit:
    """Fix one read already logged on the caller's Hardcover shelf: its dates or its format,
    or delete it.

    Use this when the user says a logged read is wrong ("I actually finished it on the 3rd",
    "that was the audiobook"), or to change the start of a read still open. It acts at once,
    so confirm with the user first. Give the book_id and the read_id; get_book lists each
    read with its read_id. Only the caller's own reads can be changed. It changes no status:
    to log a new read, use update_book.

    Arguments (give at least one; leave out what shouldn't change):
    - started_on: the date they started that read (ISO).
    - finished_on: the date they finished it (ISO). On an open read, this finishes it.
    - format: audio, ebook or print; the read moves to the book's default edition for it.

    Dates: only ever a date the user stated, resolved to an ISO date in the user's own time
    zone ("yesterday", "last Tuesday"). A read can't start after it finished.

    The result gives the read before and after (read_id, dates, format), so you can undo it
    with another correct_read. A value the read already had isn't written.

    Deleting: delete=true alone (no dates or format) first returns a preview of the read and a
    confirm_token, and deletes nothing. Show the user what will go; once they agree, call again
    with delete=true and that confirm_token, within 10 minutes. If the book changed since the
    preview, or the token is stale, you get a fresh preview instead, and nothing is deleted.
    A deleted read can be logged again with update_book, from the values the result returns.
    """
    if delete:
        if started_on is not None or finished_on is not None or format is not None:
            raise ToolError("delete goes alone: leave out started_on, finished_on and format.")
        return await _delete_read(book_id, read_id, confirm_token)
    if confirm_token is not None:
        raise ToolError("Edits act at once: confirm_token goes only with delete=true.")
    if started_on is None and finished_on is None and format is None:
        raise ToolError("Nothing to change: give started_on, finished_on or format, or delete.")
    if started_on is not None and finished_on is not None and started_on > finished_on:
        raise ToolError("started_on is after finished_on; give the earlier date first.")
    try:
        with _hardcover_errors():
            applied = await services().applier.correct_read(
                current_profile(),
                book_id,
                read_id,
                started_on=started_on.isoformat() if started_on else None,
                finished_on=finished_on.isoformat() if finished_on else None,
                format=format,
            )
    except NotShelved:
        raise ToolError(f"Book {book_id} isn't on the shelves, so it has no reads.") from None
    except ReadNotFound:
        raise ToolError(
            f"Read {read_id} isn't one of book {book_id}'s reads: get_book lists them with "
            "their read_ids. Nothing was changed."
        ) from None
    except DatesOutOfOrder as exc:
        raise ToolError(
            f"The read would start on {exc.started_on}, after it finished on "
            f"{exc.finished_on}: give both dates. Nothing was changed."
        ) from None
    except Incomplete as exc:
        raise ToolError(
            f"Partly done: the read changed, but Hardcover failed updating the book's first "
            f"start date ({exc}). Call correct_read again with the same values to finish it."
        ) from None
    return ReadEdit(
        **_edit(book_id, applied), deleted=False, preview=None, confirm_token=None, note=None
    )


async def _delete_read(book_id: int, read_id: int, token: str | None) -> ReadEdit:
    profile, tokens = current_profile(), services().confirm
    subject = (book_id, read_id)
    outcome: list[Redeemed] = []

    def confirm(user_book: dict[str, Any]) -> bool:
        if token is None:
            return False
        outcome.append(tokens.redeem(token, profile, "correct_read", subject, _snapshot(user_book)))
        return outcome[0] is Redeemed.OK

    try:
        with _hardcover_errors():
            seen, read, deleted = await services().applier.delete_read(
                profile, book_id, read_id, confirm
            )
    except NotShelved:
        raise ToolError(f"Book {book_id} isn't on the shelves, so it has no reads.") from None
    except ReadNotFound:
        raise ToolError(
            f"Read {read_id} isn't one of book {book_id}'s reads: get_book lists them with "
            "their read_ids. Nothing was deleted."
        ) from None
    except Incomplete as exc:
        raise ToolError(
            f"The read was deleted, but Hardcover failed updating the book's first start date "
            f"({exc}). A correct_read on another of its reads sets it again."
        ) from None
    shown = _shown_read(read)
    if deleted:
        change = FieldChange(field="read", before=shown, after=None)
        return ReadEdit(
            book_id=book_id,
            added_to_shelf=False,
            changes=[change],
            deleted=True,
            preview=None,
            confirm_token=None,
            note="Deleted. To log it again, use update_book with its dates.",
        )
    fresh = tokens.issue(profile, "correct_read", subject, _snapshot(seen))
    return ReadEdit(
        book_id=book_id,
        added_to_shelf=False,
        changes=[],
        deleted=False,
        preview=shown,
        confirm_token=fresh,
        note=_preview_note(outcome, "deleted"),
    )


@REGISTRY.tool(annotations=ToolAnnotations(destructive_hint=True))
async def remove_book(book_id: int, confirm_token: str | None = None) -> Removal:
    """Take a book off the caller's Hardcover shelves entirely: its status, rating, review,
    every logged read, and its reading-journal history.

    Use this only when the user asks to remove a book, not to change its shelf (that's
    update_book). It takes two calls:
    1. Without confirm_token, it previews what would go and returns a confirm_token, removing
       nothing. Show the user the preview (status, rating, whether there's a review, each read
       with its dates, whether there's journal history).
    2. Once they agree, call again with that confirm_token, within 10 minutes. If the book
       changed since the preview, or the token is stale, you get a fresh preview instead, and
       nothing is removed.

    Removal can't be undone from here, but the result keeps every value the book had, so
    update_book can shelve it again with its status, rating, review and reads (not its journal
    history). Only the caller's own shelves can be changed.
    """
    profile, tokens = current_profile(), services().confirm
    outcome: list[Redeemed] = []

    def confirm(user_book: dict[str, Any], has_journal: bool) -> bool:
        if confirm_token is None:
            return False
        snapshot = _snapshot(user_book, has_journal)
        outcome.append(tokens.redeem(confirm_token, profile, "remove_book", book_id, snapshot))
        return outcome[0] is Redeemed.OK

    try:
        with _hardcover_errors():
            removal = await services().applier.remove_book(profile, book_id, confirm)
    except NotShelved:
        raise ToolError(
            f"Book {book_id} isn't on the shelves; there's nothing to remove."
        ) from None
    ub = removal.user_book
    if removal.removed:
        token = None
        note = "Removed."
        if removal.journal_error is not None:
            note += (
                " Hardcover failed deleting its journal history, which stays (it shows only on "
                f"Hardcover): {removal.journal_error}"
            )
    else:
        token = tokens.issue(profile, "remove_book", book_id, _snapshot(ub, removal.has_journal))
        note = _preview_note(outcome, "removed")
    return Removal(
        book_id=book_id,
        title=(ub.get("book") or {}).get("title"),
        removed=removal.removed,
        status=_SLUGS.get(ub.get("status_id") or 0),
        rating=ub.get("rating"),
        has_review=bool(ub.get("has_review") or ub.get("review_raw")),
        review_has_spoilers=bool(ub.get("review_has_spoilers")),
        reads=[_shown_read(r) for r in ub.get("user_book_reads") or []],
        has_journal=removal.has_journal,
        linked_source_rows=[],
        confirm_token=token,
        note=note,
    )


def _preview_note(outcome: list[Redeemed], done: str) -> str:
    """What a deletion's preview tells Claude, after a token that didn't confirm, if any."""
    ask = (
        f"Show the user what would be {done}; once they agree, call again with this "
        "confirm_token within 10 minutes."
    )
    if not outcome:
        return f"Preview only: nothing was {done}. {ask}"
    if outcome[0] is Redeemed.CHANGED:
        return f"The book changed since that preview, so nothing was {done}. This is it now. {ask}"
    return (
        f"That confirm_token is expired, used or for something else, so nothing was {done}. {ask}"
    )


def _snapshot(user_book: dict[str, Any], has_journal: bool | None = None) -> str:
    """What a confirm token is bound to: everything on the shelf a deletion would lose."""
    keys = ("id", "status_id", "rating", "review_raw", "review_has_spoilers", "edition_id")
    reads = [
        {k: r.get(k) for k in ("id", *READ_FIELDS)} for r in user_book.get("user_book_reads") or []
    ]
    shelf = {k: user_book.get(k) for k in keys}
    return snapshot_of({**shelf, "reads": reads, "journal": has_journal})


def _shown_read(read: dict[str, Any]) -> ShownRead:
    format_id = (read.get("edition") or {}).get("reading_format_id")
    return ShownRead(
        read_id=read["id"],
        started_on=(read.get("started_at") or "")[:10] or None,
        finished_on=(read.get("finished_at") or "")[:10] or None,
        format=_FORMAT_NAMES.get(format_id or 0),
    )


@contextmanager
def _hardcover_errors() -> Iterator[None]:
    """Hardcover's failures as tool errors that say whether anything landed."""
    try:
        yield
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
