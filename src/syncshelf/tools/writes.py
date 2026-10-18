"""Claude's direct edits to the caller's Hardcover shelves (SPEC §3.3.9).

Every edit goes through the write procedure (:mod:`syncshelf.sync.apply`): fresh state, no-op
skips, inserts with the profile's ``hardcover_privacy``, a write-through to the mirror, and the
prior values of whatever changed, so Claude can undo. Edits act on the first call; Claude asks
the user in conversation first. They touch only the caller's own profile and never a Goodreads
or ABS baseline (4a), so a later unchanged source row can't overwrite them.
"""

from typing import Any, Literal, TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from syncshelf.clients.hardcover import HardcoverError, HardcoverMutationError
from syncshelf.sync.apply import UNSET, Applied, NotShelved, Target
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
) -> Edit:
    """Change a book on the caller's Hardcover shelves: its status, rating or review.

    Use this when the user asks to shelve a book, move it to another shelf, rate it or review
    it. It acts at once, so confirm with the user first. Only the caller's own shelves can be
    changed. Give the Hardcover book_id (from get_my_library, get_book, search_books or
    check_books). A book not yet on a shelf is added; give a status for it.

    Arguments (each optional; leave out what shouldn't change):
    - status: want-to-read, currently-reading, read, paused, dnf (did not finish) or
      ignored. To take a book off the shelves entirely, use remove_book.
    - rating: 0.5 to 5 in half stars; 0 clears the rating.
    - review: the review text (Markdown); "" clears the review and its spoiler flag.
    - review_has_spoilers: whether the review gives the plot away.

    The result lists each field changed with its value before and after, so you can undo an
    edit by setting the old values back. A field that already had the value isn't written.
    """
    target = Target(
        status_id=STATUS_IDS[status] if status is not None else UNSET,
        rating=_rating(rating),
        review=(review or None) if review is not None else UNSET,
        review_has_spoilers=review_has_spoilers if review_has_spoilers is not None else UNSET,
    )
    if target == Target():
        raise ToolError("Nothing to change: give a status, rating or review.")
    try:
        applied = await services().applier.apply(current_profile(), book_id, target)
    except NotShelved:
        raise ToolError(
            f"Book {book_id} isn't on the shelves yet, so give a status to add it with "
            "(want-to-read, currently-reading, read, paused, dnf or ignored)."
        ) from None
    except ServeStale as exc:
        raise ToolError(f"{exc}. Nothing was changed; try again then.") from None
    except HardcoverMutationError as exc:
        raise ToolError(f"Hardcover refused the change, and nothing was changed: {exc}") from None
    except HardcoverError as exc:
        raise ToolError(f"Hardcover couldn't be reached, and nothing was changed: {exc}") from None
    return _edit(book_id, applied)


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
