"""The write procedure: "make Hardcover match" (SPEC §3.3.6).

One procedure serves every SyncShelf write to Hardcover: Claude's direct edits now, and the
Goodreads (4a), ABS (5) and Chaptarr (6) syncs later. Given a profile, a book and a
:class:`Target`, it:

- holds the profile's **apply lock** (§2.2), so two writers never act on each other's stale
  reads;
- reads the user_book **fresh** from Hardcover, never from the mirror;
- **skips no-ops**: a field already equal in Hardcover isn't written;
- **inserts** an unshelved book in one call, with ``privacy_setting_id`` from the profile's
  ``hardcover_privacy`` (inserts only: an update never changes privacy), or **updates** only
  the fields that differ;
- checks every mutation's ``error`` (Hardcover refuses with a 200);
- **writes through** to the mirror after success, and returns what changed with the prior
  values, so a caller can undo.

Read operations (start, finish, correct) and undoing the reads Hardcover invents on a status
change land with #91.
"""

import asyncio
import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from syncshelf.clients.hardcover import (
    INSERT_USER_BOOK,
    UPDATE_USER_BOOK,
    USER_BOOK,
    HardcoverClient,
    HardcoverQueryError,
    check_mutation,
)
from syncshelf.config import ProfileConfig
from syncshelf.sync.mirror import Mirror

__all__ = ["UNSET", "Applied", "Applier", "Change", "NotShelved", "Target"]

log = logging.getLogger(__name__)


class _Unset(Enum):
    UNSET = "unset"


UNSET: Final = _Unset.UNSET
"""A :class:`Target` field left as it is, as opposed to ``None``, which clears it."""

type Settable[T] = T | _Unset


@dataclass(frozen=True)
class Target:
    """What a book's shelf entry should be. Fields left :data:`UNSET` aren't touched."""

    status_id: Settable[int] = UNSET
    rating: Settable[float | None] = UNSET
    """0.5–5 in half steps, or ``None`` to clear."""
    review: Settable[str | None] = UNSET
    """Markdown, or ``None`` to clear the review and its spoiler flag."""
    review_has_spoilers: Settable[bool] = UNSET


class NotShelved(Exception):
    """The book isn't on the profile's shelves and the target has no status to add it with."""


@dataclass(frozen=True)
class Change:
    """One field written, with its value before and after."""

    field: str
    before: Any
    after: Any


@dataclass(frozen=True)
class Applied:
    """What a write did."""

    user_book_id: int
    inserted: bool
    changes: list[Change] = field(default_factory=list)


# Each Target field: (user_book field it compares with, mutation input field it writes).
_FIELDS = {
    "status_id": ("status_id", "status_id"),
    "rating": ("rating", "rating"),
    "review": ("review_raw", "review_markdown"),
    "review_has_spoilers": ("review_has_spoilers", "review_has_spoilers"),
}


class Applier:
    """Applies targets to each profile's Hardcover shelves, one write at a time per profile."""

    def __init__(
        self,
        clients: Mapping[str, HardcoverClient],
        mirror: Mirror,
        profiles: Mapping[str, ProfileConfig],
    ) -> None:
        self._clients = clients
        self._mirror = mirror
        self._profiles = profiles
        self._locks = {profile: asyncio.Lock() for profile in clients}

    def lock(self, profile: str) -> asyncio.Lock:
        """The profile's apply lock (§2.2). A sync run holds it from its Hardcover fetch through
        its writes, then calls :meth:`apply` with ``locked=True``."""
        return self._locks[profile]

    async def apply(
        self,
        profile: str,
        book_id: int,
        target: Target,
        *,
        trigger: str = "claude",
        locked: bool = False,
    ) -> Applied:
        """Make ``profile``'s shelf entry for ``book_id`` match ``target`` (§3.3.6).

        ``trigger`` names who asked, for the change log (§2.7). ``locked`` says the caller
        already holds :meth:`lock`; otherwise this call takes it.

        Raises :class:`NotShelved` when the book isn't shelved and ``target`` has no status,
        and a :class:`~syncshelf.clients.hardcover.HardcoverError` when Hardcover refuses or
        can't be reached before anything is written. Once a write has landed, nothing after it
        (the write-through) can make this call fail: the changes are always returned.
        """
        if locked:
            return await self._apply(profile, book_id, target, trigger)
        async with self._locks[profile]:
            return await self._apply(profile, book_id, target, trigger)

    async def _apply(self, profile: str, book_id: int, target: Target, trigger: str) -> Applied:
        client = self._clients[profile]
        current = await self._fetch(client, book_id)
        wanted = _wanted(target)
        if current is None:
            if "status_id" not in wanted:
                raise NotShelved(book_id)
            applied = await self._insert(client, profile, book_id, wanted)
        else:
            applied = await self._update(client, current, wanted)
        # Logged as soon as Hardcover has them, whatever happens next.
        for change in applied.changes:
            _log_change(profile, book_id, change, trigger)
        if applied.changes:
            await self._write_through(client, profile, book_id)
        return applied

    async def _write_through(self, client: HardcoverClient, profile: str, book_id: int) -> None:
        """Re-fetch the user_book and upsert it into the copy (§3.3.4, Write-through). Best
        effort: the write already landed, and a copy that missed it catches up at the TTL."""
        try:
            await self._mirror.write_through(profile, book_id, await self._fetch(client, book_id))
        except Exception:
            log.warning(
                "the write landed, but the copy couldn't be brought up to date with it",
                exc_info=True,
                extra={"event": "write_through_failed", "profile": profile, "hc_book_id": book_id},
            )

    async def _fetch(self, client: HardcoverClient, book_id: int) -> dict[str, Any] | None:
        result = await client.execute(
            USER_BOOK, {"book_id": book_id, "user_id": await client.me_id()}
        )
        rows = result.data.get("user_books") or []
        return rows[0] if rows else None

    async def _insert(
        self, client: HardcoverClient, profile: str, book_id: int, wanted: dict[str, Any]
    ) -> Applied:
        obj: dict[str, Any] = {
            "book_id": book_id,
            "privacy_setting_id": self._profiles[profile].hardcover_privacy,
        }
        for name, value in wanted.items():
            obj[_FIELDS[name][1]] = value
        result = await client.execute(INSERT_USER_BOOK, {"object": obj})
        inserted = check_mutation(result.data, "insert_user_book")
        if not isinstance(inserted.get("id"), int):
            raise HardcoverQueryError("Hardcover added the book but returned no id for it")
        # A field set to nothing on a new entry isn't a change.
        changes = [
            Change(name, None, value)
            for name, value in wanted.items()
            if value not in (None, False)
        ]
        return Applied(inserted["id"], inserted=True, changes=changes)

    async def _update(
        self, client: HardcoverClient, current: dict[str, Any], wanted: dict[str, Any]
    ) -> Applied:
        changes = [
            Change(name, current.get(_FIELDS[name][0]), value)
            for name, value in wanted.items()
            if _differs(current.get(_FIELDS[name][0]), value)
        ]
        if changes:
            obj = {_FIELDS[c.field][1]: c.after for c in changes}
            result = await client.execute(UPDATE_USER_BOOK, {"id": current["id"], "object": obj})
            check_mutation(result.data, "update_user_book")
        return Applied(current["id"], inserted=False, changes=changes)


def _wanted(target: Target) -> dict[str, Any]:
    wanted = {name: getattr(target, name) for name in _FIELDS if getattr(target, name) is not UNSET}
    if "review" in wanted and wanted["review"] is None:
        wanted["review_has_spoilers"] = False  # a cleared review has no spoilers to flag
    return wanted


def _differs(current: Any, wanted: Any) -> bool:
    """Whether writing ``wanted`` would change Hardcover. Empty and absent text are equal."""
    if isinstance(current, str | None) and isinstance(wanted, str | None):
        return (current or None) != (wanted or None)
    if isinstance(current, int | float) and isinstance(wanted, int | float):
        return float(current) != float(wanted)
    return current != wanted


def _log_change(profile: str, book_id: int, change: Change, trigger: str) -> None:
    """One INFO line per applied change (§2.7): values for status and rating; a review only as
    its length and a hash prefix, never its text."""
    if change.field == "review":
        shown = {
            "before": _review_mark(change.before),
            "after": _review_mark(change.after),
        }
    else:
        shown = {"before": change.before, "after": change.after}
    log.info(
        "applied a change to Hardcover",
        extra={
            "event": "change_applied",
            "profile": profile,
            "hc_book_id": book_id,
            "field": change.field,
            "trigger": trigger,
            **shown,
        },
    )


def _review_mark(text: Any) -> str | None:
    if not text:
        return None
    digest = hashlib.sha256(str(text).encode()).hexdigest()[:8]
    return f"{len(str(text))} chars {digest}"
