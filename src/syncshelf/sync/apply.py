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

A target may also carry one read operation: :class:`Start`, :class:`Finish` or
:class:`Correct`. After a status
write, every read Hardcover invented (a "today" read it opened, a "today" finish it stamped) is
undone against a snapshot taken first, and the intended read is then written explicitly, with
full objects only. A finish adopts a read already logged within 14 days, or completes an open
read; ``first_started_reading_date`` is kept to the earliest start (rules 4–6).
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Final

from syncshelf.clients.hardcover import (
    BOOK_EDITIONS,
    DELETE_JOURNALS,
    DELETE_READ,
    DELETE_USER_BOOK,
    INSERT_READ,
    INSERT_USER_BOOK,
    JOURNAL,
    UPDATE_READ,
    UPDATE_USER_BOOK,
    USER_BOOK,
    HardcoverClient,
    HardcoverError,
    HardcoverQueryError,
    check_mutation,
)
from syncshelf.config import ProfileConfig
from syncshelf.sync.mirror import Mirror

__all__ = [
    "UNSET",
    "Applied",
    "Applier",
    "Change",
    "Correct",
    "DateNeeded",
    "DatesOutOfOrder",
    "Finish",
    "FormatNeeded",
    "Incomplete",
    "NotShelved",
    "OpenReadExists",
    "ReadNotFound",
    "Removal",
    "ReadStillOpen",
    "Start",
    "Target",
]

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
    read: ReadOp | None = None
    """A read to start or finish, with its dates (rule 5)."""


class Incomplete(Exception):
    """A change partly landed: its status was written, but fixing the reads after it failed.
    The same call again completes it (rule 9)."""


class ReadStillOpen(Exception):
    """A finish that wouldn't complete the read left open (it started later, or ``reread``),
    which would then dangle under Read."""

    def __init__(self, started_on: str) -> None:
        super().__init__(started_on)
        self.started_on = started_on


class NotYetShown(HardcoverError):
    """Hardcover took a write but hasn't shown it within :data:`SETTLE_TIMEOUT` (trap 5)."""


SETTLE_TIMEOUT = 3.0
"""How long to wait for Hardcover to show a write before calling the change partly done."""
SETTLE_POLL = 0.25
"""How often to look meanwhile (seconds)."""

PENDING_TTL = 3600.0
"""How long an unfinished change waits for the same call to complete it (seconds)."""


@dataclass
class _Pending:
    target: Target
    snapshot: list[dict[str, Any]]
    landed: list[Change]
    """What the earlier attempt(s) already changed, reported again when the change completes."""
    at: float


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
    "review": ("review_markdown", "review_markdown"),
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
        self._pending: dict[tuple[str, int], _Pending] = {}
        """Changes whose read fix-up failed, by (profile, book): asking the same change again
        completes it against the reads from before it began (rule 9). Kept in memory only, so
        a restart in between loses it, and the invented reads stay until edited."""

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

        Raises :class:`NotShelved`, :class:`DateNeeded`, :class:`OpenReadExists` or
        :class:`FormatNeeded` before anything is written, and a
        :class:`~syncshelf.clients.hardcover.HardcoverError` when Hardcover refuses or can't be
        reached. A failure after the first write leaves the change to a rerun, which completes
        it (rule 9). Nothing after the last write (the write-through) can make this call fail.
        """
        if locked:
            return await self._apply(profile, book_id, target, trigger)
        async with self._locks[profile]:
            return await self._apply(profile, book_id, target, trigger)

    async def _apply(self, profile: str, book_id: int, target: Target, trigger: str) -> Applied:
        client = self._clients[profile]
        key = (profile, book_id)
        current = await self._fetch(client, book_id)
        wanted = _wanted(target)
        op = target.read
        pending = self._replay(key, target)
        # A replay is judged against the reads before the change began, not against what
        # Hardcover invented when its first attempt's status write landed.
        basis = pending.snapshot if pending else list((current or {}).get("user_book_reads") or [])
        if isinstance(op, Correct) and _finished_on(basis, op.old) is None:
            op = Finish(op.on, format=op.format)  # nothing logged on the old date (rule 5)
        edition = await self._edition(client, book_id, current, op, basis)
        if current is None:
            if "status_id" not in wanted:
                raise NotShelved(book_id)
            applied = await self._insert(client, profile, book_id, wanted, op, edition)
        else:
            _check(target, op, basis)
            applied = await self._update(client, current, wanted)
        for change in applied.changes:  # logged as soon as Hardcover has them
            _log_change(profile, book_id, change, trigger)
        earlier = pending.landed if pending else []
        status_written = current is None or any(c.field == "status_id" for c in applied.changes)
        if status_written or op is not None or pending:
            # Undo every read Hardcover invented, then write the one asked for (rule 4).
            landed = earlier + applied.changes
            self._pending[key] = _Pending(target, basis, landed, time.monotonic())
            fixes: list[Change] = []
            try:
                after = await self._settled(client, book_id, wanted.get("status_id", UNSET))
                await self._reconcile(client, after, basis, op, edition, fixes)
            except HardcoverError as exc:
                for change in fixes:
                    _log_change(profile, book_id, change, trigger)
                self._pending[key].landed = landed + fixes
                raise Incomplete(str(exc)) from exc
            del self._pending[key]
            for change in fixes:
                _log_change(profile, book_id, change, trigger)
            applied.changes[:] = earlier + applied.changes + fixes
        if applied.changes:
            await self._write_through(client, profile, book_id)
        return applied

    def _replay(self, key: tuple[str, int], target: Target) -> _Pending | None:
        """The unfinished change for this book, when ``target`` is that same change asked
        again in time; otherwise it's dropped, and its leftovers stay as they are."""
        pending = self._pending.get(key)
        if pending is None:
            return None
        if pending.target == target and time.monotonic() - pending.at < PENDING_TTL:
            return pending
        del self._pending[key]
        log.warning(
            "a partly done change to a book was replaced by a different one before it finished",
            extra={"event": "change_abandoned", "profile": key[0], "hc_book_id": key[1]},
        )
        return None

    async def _edition(
        self,
        client: HardcoverClient,
        book_id: int,
        current: dict[str, Any] | None,
        op: ReadOp | None,
        reads: list[dict[str, Any]],
    ) -> int | None:
        """The edition a read written for ``op`` carries (§3.3.5): the book's default edition
        for the format. With no format, a finish may complete an open read or adopt a nearby
        one, which keep their own editions; otherwise the format is needed."""
        if op is None:
            return None
        if op.format is None:
            if isinstance(op, Correct):
                return None  # the read it moves keeps its edition
            adoptable = isinstance(op, Finish) and not op.reread and current is not None
            if adoptable and (_open_read(reads, op.on) or _near(reads, op.on)):
                return None
            raise FormatNeeded
        if current is not None:
            book = current.get("book") or {}
        else:
            result = await client.execute(BOOK_EDITIONS, {"book_id": book_id})
            books = result.data.get("books") or []
            book = books[0] if books else {}
        return (book.get(EDITION_FOR[op.format]) or {}).get("id")

    async def _settled(
        self, client: HardcoverClient, book_id: int, status_id: Any
    ) -> dict[str, Any]:
        """The user_book once Hardcover shows the write just made: it exists, with
        ``status_id`` when one was written. Hardcover's reads can lag its writes, and
        reconciling against the state before the status write would miss the reads it
        invents (§3.3.1 trap 5, #150). Raises :class:`NotYetShown` after
        :data:`SETTLE_TIMEOUT` seconds."""
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while True:
            row = await self._fetch(client, book_id)
            if row is not None and (status_id is UNSET or row.get("status_id") == status_id):
                return row
            if time.monotonic() >= deadline:
                raise NotYetShown("Hardcover hasn't shown the change yet")
            await asyncio.sleep(SETTLE_POLL)

    async def _reconcile(
        self,
        client: HardcoverClient,
        after: dict[str, Any],
        snapshot: list[dict[str, Any]],
        op: ReadOp | None,
        edition: int | None,
        changes: list[Change],
    ) -> None:
        """Bring ``after``'s reads back to ``snapshot`` and write the read ``op`` asks for, in
        as few full-object writes as possible (rules 4–6). Each read change made is appended
        to ``changes`` as it lands, so a failure part-way still accounts for it."""
        before = {r["id"]: r for r in snapshot}
        # The read the op rewrites (the open read a finish completes, the read a correction
        # moves) becomes the intended read in one write, stamped by Hardcover or not.
        completes = None
        if isinstance(op, Finish) and not op.reread:
            completes = _open_read(snapshot, op.on)
        elif isinstance(op, Correct):
            completes = _finished_on(snapshot, op.old)
        done = False
        kept: list[dict[str, Any]] = []
        for read in after.get("user_book_reads") or []:
            old = before.get(read["id"])
            if old is not None and completes is not None and read["id"] == completes["id"]:
                assert isinstance(op, Finish | Correct)
                started = op.started if isinstance(op, Finish) else None
                intended = _full(
                    old,
                    finished_at=op.on,
                    started_at=started or old.get("started_at"),
                    edition_id=edition if op.format else old.get("edition_id"),
                )
                if intended != _full(read):
                    await self._write_read(client, UPDATE_READ, {"id": read["id"]}, intended)
                new = {**read, **intended}
                if intended != _full(old):
                    changes.append(Change("read", _shown(old), _shown(new, op.format)))
                kept.append(new)
                done = True
            elif old is not None:
                if any(read.get(k) != old.get(k) for k in READ_FIELDS):
                    await self._write_read(client, UPDATE_READ, {"id": read["id"]}, _full(old))
                kept.append({**read, **{k: old.get(k) for k in READ_FIELDS}})
            elif not done and op is not None and _is_intended(read, op, edition):
                kept.append(read)  # the read an insert with dates created, as asked
                done = True
                changes.append(Change("read", None, _shown(read, op.format)))
            else:
                await self._write_read(client, DELETE_READ, {"id": read["id"]}, None)
        if op is not None and not done:
            if isinstance(op, Finish) and not op.reread and _near(kept, op.on) is not None:
                pass  # already logged, within days of this finish
            else:
                assert not isinstance(op, Correct)  # one with nothing to move became a Finish
                started = op.started if isinstance(op, Finish) else op.on
                fields = {
                    "started_at": started,
                    "finished_at": op.on if isinstance(op, Finish) else None,
                    "edition_id": edition,
                    "progress_pages": None,
                    "progress_seconds": None,
                }
                result = await self._write_read(
                    client, INSERT_READ, {"user_book_id": after["id"]}, fields
                )
                new = {"id": result["id"], **fields}
                kept.append(new)
                changes.append(Change("read", None, _shown(new, op.format)))
        await self._first_started(client, after, kept)

    async def _first_started(
        self, client: HardcoverClient, user_book: dict[str, Any], reads: list[dict[str, Any]]
    ) -> None:
        """Set ``first_started_reading_date`` to the earliest read's start (rule 6). Hardcover
        never recomputes it, and stamps it on the reads it invents. With no dated start left,
        it stays as Hardcover has it: Hardcover ignores a null there (trap 1, #148)."""
        starts = [r["started_at"][:10] for r in reads if r.get("started_at")]
        earliest = min(starts, default=None)
        if earliest is not None and earliest != user_book.get("first_started_reading_date"):
            result = await client.execute(
                UPDATE_USER_BOOK,
                {"id": user_book["id"], "object": {"first_started_reading_date": earliest}},
            )
            check_mutation(result.data, "update_user_book")

    async def correct_read(
        self,
        profile: str,
        book_id: int,
        read_id: int,
        *,
        started_on: str | None = None,
        finished_on: str | None = None,
        format: str | None = None,  # noqa: A002 - matches the read ops
        trigger: str = "claude",
    ) -> Applied:
        """Edit one logged read in place (§3.3.9 ``correct_read``): the given dates, and the
        book's default edition for ``format``, over its current values, in one full-object
        write. It runs outside the procedure, as it changes no status and Hardcover invents
        nothing for it; ``first_started_reading_date`` still follows (rule 6).

        Raises :class:`NotShelved`, :class:`ReadNotFound` or :class:`DatesOutOfOrder` before
        anything is written, and :class:`Incomplete` when the read changed but the first-start
        date didn't: the same call again completes it.
        """
        async with self._locks[profile]:
            client = self._clients[profile]
            current = await self._fetch(client, book_id)
            if current is None:
                raise NotShelved(book_id)
            reads = list(current.get("user_book_reads") or [])
            old = next((r for r in reads if r["id"] == read_id), None)
            if old is None:
                raise ReadNotFound(read_id)
            changed: dict[str, Any] = {}
            if started_on is not None:
                changed["started_at"] = started_on
            if finished_on is not None:
                changed["finished_at"] = finished_on
            if format is not None:
                book = current.get("book") or {}
                changed["edition_id"] = (book.get(EDITION_FOR[format]) or {}).get("id")
            intended = _full(old, **changed)
            start, finish = intended["started_at"], intended["finished_at"]
            if start and finish and start[:10] > finish[:10]:
                raise DatesOutOfOrder(start[:10], finish[:10])
            applied = Applied(current["id"], inserted=False, changes=[])
            if any(_differs(old.get(k), intended[k]) for k in changed):
                await self._write_read(client, UPDATE_READ, {"id": read_id}, intended)
                new = {**old, **intended}
                if format is not None:
                    new["edition"] = None  # shown by ``format``, not the old edition's
                change = Change("read", _shown(old), _shown(new, format))
                applied.changes.append(change)
                _log_change(profile, book_id, change, trigger)
                reads[reads.index(old)] = new
            try:
                # Even with the read already right: a retry after this failed completes it.
                await self._first_started(client, current, reads)
            except HardcoverError as exc:
                if applied.changes:
                    raise Incomplete(str(exc)) from exc
                raise
            finally:
                if applied.changes:
                    await self._write_through(client, profile, book_id)
            return applied

    async def remove_book(
        self,
        profile: str,
        book_id: int,
        confirm: Callable[[dict[str, Any], bool], bool],
        *,
        trigger: str = "claude",
    ) -> Removal:
        """Take a book off ``profile``'s shelves (§3.3.9 ``remove_book``), if ``confirm`` says
        so on seeing it fresh: the user_book (reads included) and whether it has journal rows.
        A deletion runs outside the procedure (rule 8); the confirm runs under the apply lock,
        so nothing changes between what it saw and what is deleted.

        Raises :class:`NotShelved`. Once the book is gone, a failure to delete its journal
        doesn't raise: the :class:`Removal` says so.
        """
        async with self._locks[profile]:
            client = self._clients[profile]
            current = await self._fetch(client, book_id)
            if current is None:
                raise NotShelved(book_id)
            result = await client.execute(
                JOURNAL, {"book_id": book_id, "user_id": await client.me_id()}
            )
            has_journal = bool(result.data.get("reading_journals"))
            if not confirm(current, has_journal):
                return Removal(current, has_journal, removed=False)
            result = await client.execute(DELETE_USER_BOOK, {"id": current["id"]})
            check_mutation(result.data, "delete_user_book")
            _log_change(profile, book_id, Change("user_book", current["id"], None), trigger)
            try:
                await self._mirror.write_through(profile, book_id, None)
            except Exception:
                log.warning(
                    "the book was removed, but the copy still has it until its refresh",
                    exc_info=True,
                    extra={
                        "event": "write_through_failed",
                        "profile": profile,
                        "hc_book_id": book_id,
                    },
                )
            removal = Removal(current, has_journal, removed=True)
            if has_journal:
                try:
                    result = await client.execute(DELETE_JOURNALS, {"book_id": book_id})
                    check_mutation(result.data, "delete_reading_journals_for_book")
                except HardcoverError as exc:
                    # Trap 4's leftovers: harmless history, shown on Hardcover only.
                    log.warning(
                        "the book was removed, but its reading journal wasn't",
                        exc_info=True,
                        extra={"event": "journal_left", "profile": profile, "hc_book_id": book_id},
                    )
                    removal.journal_error = str(exc)
            return removal

    async def delete_read(
        self,
        profile: str,
        book_id: int,
        read_id: int,
        confirm: Callable[[dict[str, Any]], bool],
        *,
        trigger: str = "claude",
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Delete one read (§3.3.9 ``correct_read(delete=true)``), if ``confirm`` says so on
        seeing the user_book fresh, under the apply lock. ``first_started_reading_date``
        follows the reads left (rule 6).

        Returns the user_book seen, the read, and whether it was deleted. Raises
        :class:`NotShelved` or :class:`ReadNotFound`; and :class:`Incomplete` when the read
        is gone but the first-start date couldn't follow.
        """
        async with self._locks[profile]:
            client = self._clients[profile]
            current = await self._fetch(client, book_id)
            if current is None:
                raise NotShelved(book_id)
            reads = list(current.get("user_book_reads") or [])
            old = next((r for r in reads if r["id"] == read_id), None)
            if old is None:
                raise ReadNotFound(read_id)
            if not confirm(current):
                return current, old, False
            await self._write_read(client, DELETE_READ, {"id": read_id}, None)
            _log_change(profile, book_id, Change("read", _shown(old), None), trigger)
            reads.remove(old)
            try:
                await self._first_started(client, current, reads)
            except HardcoverError as exc:
                raise Incomplete(str(exc)) from exc
            finally:
                await self._write_through(client, profile, book_id)
            return current, old, True

    async def _write_read(
        self,
        client: HardcoverClient,
        mutation: str,
        ids: dict[str, int],
        read: dict[str, Any] | None,
    ) -> dict[str, Any]:
        variables: dict[str, Any] = dict(ids)
        if read is not None:
            variables["read"] = read
        result = await client.execute(mutation, variables)
        name = {
            INSERT_READ: "insert_user_book_read",
            UPDATE_READ: "update_user_book_read",
            DELETE_READ: "delete_user_book_read",
        }[mutation]
        return check_mutation(result.data, name)

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
        self,
        client: HardcoverClient,
        profile: str,
        book_id: int,
        wanted: dict[str, Any],
        op: ReadOp | None,
        edition: int | None,
    ) -> Applied:
        obj: dict[str, Any] = {
            "book_id": book_id,
            "privacy_setting_id": self._profiles[profile].hardcover_privacy,
        }
        for name, value in wanted.items():
            obj[_FIELDS[name][1]] = value
        # The read's dates go in the same call (rule 3): Read with user_date, Currently
        # Reading with user_start_date, and its edition.
        if isinstance(op, Finish):
            obj["user_date"] = op.on
            if op.started:
                obj["user_start_date"] = op.started
        elif isinstance(op, Start):
            obj["user_start_date"] = op.on
        if op is not None and edition is not None:
            obj["edition_id"] = edition
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


def _check(target: Target, op: ReadOp | None, reads: list[dict[str, Any]]) -> None:
    """Refuse what §3.3.9 refuses, before anything is written. A Read status never sits over a
    dangling open read."""
    open_read = _open_read(reads)
    if open_read is None:
        return
    started = open_read["started_at"][:10]
    if target.status_id == 3 and op is None:
        raise DateNeeded(started)
    if isinstance(op, Start):
        raise OpenReadExists(started)
    if isinstance(op, Finish) and (op.reread or _open_read(reads, op.on) is None):
        # This finish wouldn't complete the open read, which would then dangle under Read.
        raise ReadStillOpen(started)


def _is_intended(read: dict[str, Any], op: ReadOp, edition: int | None) -> bool:
    """Whether a read the write created is exactly the one ``op`` asks for, edition too."""
    started = (read.get("started_at") or "")[:10] or None
    finished = (read.get("finished_at") or "")[:10] or None
    if edition is not None and read.get("edition_id") != edition:
        return False
    if isinstance(op, Start):
        return started == op.on and finished is None
    if isinstance(op, Correct):
        return False  # it moves a read already logged, so a new one is never asked for
    return finished == op.on and started == op.started


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


# --- reads (#91) --------------------------------------------------------------------------

READ_FIELDS = ("started_at", "finished_at", "edition_id", "progress_pages", "progress_seconds")
"""A full ``DatesReadInput``: every read write sends all of them (§3.3.1 trap 2)."""

ADOPT_DAYS = 14
"""A finish this close to an existing read's is that read, logged again (§3.3.6 rule 5)."""

EDITION_FOR = {
    "audio": "default_audio_edition",
    "ebook": "default_ebook_edition",
    "print": "default_physical_edition",
}
"""The book's default edition each format's read is written with (§3.3.5)."""


@dataclass(frozen=True)
class Start:
    """Open a read on ``on`` (an ISO date)."""

    on: str
    format: str | None = None


@dataclass(frozen=True)
class Finish:
    """Finish a read on ``on``: complete or adopt an adoptable read, or add one."""

    on: str
    started: str | None = None
    format: str | None = None
    reread: bool = False
    """Never adopt: always add a new read."""


@dataclass(frozen=True)
class Correct:
    """Move a logged finish from ``old`` to ``on``: the read finished on ``old`` gets the new
    date. With none, it's a finish on ``on``, adopting rather than duplicating (rule 5)."""

    old: str
    on: str
    format: str | None = None


type ReadOp = Start | Finish | Correct


class DateNeeded(Exception):
    """Setting Read with no finish date over an open read: ask when it was finished."""

    def __init__(self, started_on: str) -> None:
        super().__init__(started_on)
        self.started_on = started_on


class OpenReadExists(Exception):
    """A start asked for while a read is already open: correct that read instead."""

    def __init__(self, started_on: str) -> None:
        super().__init__(started_on)
        self.started_on = started_on


class DatesOutOfOrder(Exception):
    """An edit would leave a read starting after it finished."""

    def __init__(self, started_on: str, finished_on: str) -> None:
        super().__init__(started_on, finished_on)
        self.started_on = started_on
        self.finished_on = finished_on


@dataclass
class Removal:
    """What :meth:`Applier.remove_book` saw, and what it did."""

    user_book: dict[str, Any]
    """The user_book as it was, reads included: the values to restore it from."""
    has_journal: bool
    removed: bool
    journal_error: str | None = None
    """Why the journal rows were left, when deleting them failed after the book went."""


class ReadNotFound(Exception):
    """The read id isn't one of this book's reads on the caller's shelf."""


class FormatNeeded(Exception):
    """A read must be written but its format can't be told: ask how it was read."""


def _open_read(
    reads: list[dict[str, Any]], on_or_before: str | None = None
) -> dict[str, Any] | None:
    """The latest read that has a start and no finish (started on or before a date, if given)."""
    candidates = [
        r
        for r in reads
        if r.get("started_at")
        and not r.get("finished_at")
        and (on_or_before is None or r["started_at"][:10] <= on_or_before)
    ]
    return max(candidates, key=lambda r: (r["started_at"], r["id"]), default=None)


def _finished_on(reads: list[dict[str, Any]], on: str) -> dict[str, Any] | None:
    """The read finished on exactly ``on`` (the earliest such, if two are)."""
    matches = [r for r in reads if (r.get("finished_at") or "")[:10] == on]
    return min(matches, key=lambda r: r["id"], default=None)


def _near(reads: list[dict[str, Any]], on: str) -> dict[str, Any] | None:
    """A read finished within :data:`ADOPT_DAYS` of ``on``. An undated read never is."""
    day = date.fromisoformat(on)
    for r in reads:
        finished = r.get("finished_at")
        if finished and abs((date.fromisoformat(finished[:10]) - day).days) <= ADOPT_DAYS:
            return r
    return None


def _full(read: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A read as a full ``DatesReadInput``, with ``changes`` over its current values."""
    return {key: changes.get(key, read.get(key)) for key in READ_FIELDS}


def _shown(read: dict[str, Any] | None, fmt: str | None = None) -> dict[str, Any] | None:
    """A read as a change reports it: id, dates and format."""
    if read is None:
        return None
    format_id = (read.get("edition") or {}).get("reading_format_id")
    return {
        "read_id": read["id"],
        "started_on": (read.get("started_at") or None) and read["started_at"][:10],
        "finished_on": (read.get("finished_at") or None) and read["finished_at"][:10],
        "format": fmt or _FORMAT_NAMES.get(format_id or 0),
    }


_FORMAT_NAMES = {1: "print", 2: "audio", 3: "both", 4: "ebook"}
