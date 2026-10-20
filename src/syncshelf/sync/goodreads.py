"""The Goodreads sync: one run of a profile's RSS source, start to finish (SPEC §3.4a).

A run has four stages (§3.4a.7):

1. **Fetch the source** in full: the whole ``#ALL#`` feed, to its empty page.
2. **Diff against baselines**, which needs no Hardcover data.
3. **Fetch the Hardcover library**, only if there's something to push or a row to match, so
   an idle run costs no Hardcover request. The fetch also refreshes the mirror.
4. **Apply** each change through the write procedure (§3.3.6), committing each with its
   baseline only after Hardcover took it.

Stages 3 and 4 hold the profile's apply lock, so no other writer acts on this run's view.

**The first complete enumeration is the backfill** (§3.4a.12): it reads Hardcover, links the
rows the Goodreads mapping places in the profile's own library, queues the rest, takes every
observed value as the baseline, and pushes nothing. A run before that whose feed came back
incomplete stores nothing and pushes nothing; the next one tries again.

**What a change becomes** (§3.4a.6). Each pending change is planned into one write:

- a **status** change carries its read edits as one change (rule 6): going Read finishes a
  read, on Goodreads' Date Read, else the date part of RSS ``user_date_added``, else the day
  SyncShelf first saw the change, and ``change_log.date_source`` says which. Going Currently
  Reading opens one the same way (none is opened over a read already open). Read →
  Currently Reading → Read is a re-read. An exclusive shelf missing from ``shelf_map`` is
  logged and settles its baseline without a push;
- any other **Date Read** change is a correction of the read finished on the old date, or a
  finish when there was none. A vanished Date Read is never pushed, but its baseline moves on;
- a **rating**, **review** or **spoiler** change sets or clears that field; an emptied
  review clears the review and its spoiler flag (rule 5).

A new read carries the link's edition when Hardcover gives that edition a format, else no
edition (§3.3.5, an RSS-only row), and inserts take the profile's ``hardcover_privacy``.

**Books first seen after the backfill** are matched and added from an empty baseline, so
every non-empty field of their first observation is pushed (rule 3); unlike the backfill, a
match links whether or not the book is on the profile's shelves. A change with no status, on
a row with no Hardcover shelf entry known, waits for the row's status to be observed (#182).
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, tzinfo
from typing import Any, Protocol

import httpx2

from syncshelf.clients.goodreads_rss import GoodreadsRss, Pacer
from syncshelf.clients.hardcover import EDITION_FORMATS, HardcoverClient, HardcoverError
from syncshelf.config import ProfileConfig
from syncshelf.db import Database
from syncshelf.logging import scrub
from syncshelf.sync import identity, runs
from syncshelf.sync.apply import (
    UNSET,
    Applied,
    Applier,
    Correct,
    DateNeeded,
    DatesOutOfOrder,
    Finish,
    FormatNeeded,
    Incomplete,
    NotShelved,
    OpenReadExists,
    ReadStillOpen,
    Start,
    Target,
)
from syncshelf.sync.identity import LogRow, Pending, Placement, SourceRow
from syncshelf.sync.matcher import Candidate, MatchInput, Outcome, match
from syncshelf.sync.mirror import FetchFailed, Mirror
from syncshelf.sync.observations import Batch, Observation, observe_rss
from syncshelf.sync.runs import RunCounts, RunState, Trigger

__all__ = ["Clock", "GoodreadsSync"]

log = logging.getLogger(__name__)

SOURCE = "goodreads"
TRIGGER = "goodreads"
"""Who the write procedure's change lines name as asking."""
LOGGED = frozenset({"status", "rating", "date_read", "read_count", "spoiler"})
"""The fields whose values a change line may carry (§2.7)."""
PUSHED = frozenset({"status", "date_read", "rating", "review", "spoiler"})
"""The fields whose changes reach Hardcover. ``read_count`` is a re-read signal (#168)."""
READ, CURRENTLY_READING = 3, 2
"""The Hardcover statuses that carry read edits."""


class Clock(Protocol):
    """Wall time for timestamps, and monotonic time and sleep for the Goodreads pacer: the
    limiter's clock, or the injectable test clock (§2.9)."""

    def time(self) -> float: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class GoodreadsSync:
    """Runs each profile's Goodreads source against its Hardcover shelves.

    One per process. Profiles without a ``goodreads`` section have no source here.
    ``transport`` carries Goodreads requests in tests (§2.9).
    """

    def __init__(
        self,
        *,
        db: Database,
        profiles: Mapping[str, ProfileConfig],
        hardcover: Mapping[str, HardcoverClient],
        mirror: Mirror,
        applier: Applier,
        clock: Clock,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._db = db
        self._profiles = profiles
        self._hardcover = hardcover
        self._mirror = mirror
        self._applier = applier
        self._clock = clock
        self._rss: dict[str, GoodreadsRss] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        for name, profile in profiles.items():
            if profile.goodreads is None:
                continue
            self._rss[name] = GoodreadsRss(
                profile.goodreads.user_id,
                key=profile.goodreads.rss_key,
                pacer=Pacer(clock),
                transport=transport,
            )
            self._locks[name] = asyncio.Lock()

    async def aclose(self) -> None:
        """Close every profile's Goodreads connection."""
        for rss in self._rss.values():
            await rss.aclose()

    def enabled(self, profile: str) -> bool:
        """Whether the profile has a Goodreads source."""
        return profile in self._rss

    def lock(self, profile: str) -> asyncio.Lock:
        """The profile's Goodreads sync lock (§2.2): one run of its source at a time. A run
        takes it; the scheduler skips a tick, and ``sync_now`` reports the run in progress,
        when it's held."""
        return self._locks[profile]

    async def run_rss(self, profile: str, trigger: Trigger = "schedule") -> int:
        """Run the profile's RSS source once and return the run's id, waiting for a run in
        progress to finish first. Every outcome is on the run's row; this raises only for a
        profile with no Goodreads source."""
        rss = self._rss[profile]
        async with self._locks[profile]:
            return await self._locked_run(profile, rss, trigger)

    async def _locked_run(self, profile: str, rss: GoodreadsRss, trigger: Trigger) -> int:
        started = int(self._clock.time())
        began = self._clock.monotonic()
        run_id = await self._db.write(runs.start, profile, "gr_rss", trigger, started)
        counts = RunCounts()
        state: RunState = "aborted"
        try:
            state = await self._run(profile, rss, run_id, started, counts)
        except Exception as exc:
            # Never let one run's bug kill the loop that called it; the row says it died.
            counts.error(f"the run stopped on an internal error ({type(exc).__name__})")
            log.exception(
                "the Goodreads run stopped on an internal error",
                extra={"event": "run_crashed", "profile": profile, "run_id": run_id},
            )
        finally:
            await self._db.write(runs.finish, run_id, state, counts, int(self._clock.time()))
            log.info(
                "Goodreads run %s",
                state,
                extra={
                    "event": "run_finished",
                    "profile": profile,
                    "source": "gr_rss",
                    "run_id": run_id,
                    "state": state,
                    "applied": counts.applied,
                    "failed": counts.failed,
                    "quarantined": counts.quarantined,
                    "duration_s": round(self._clock.monotonic() - began, 3),
                },
            )
        return run_id

    async def _run(
        self, profile: str, rss: GoodreadsRss, run_id: int, started: int, counts: RunCounts
    ) -> RunState:
        goodreads = self._profiles[profile].goodreads
        assert goodreads is not None
        # Stage 1: the whole feed.
        fetched = await rss.fetch_all()
        if fetched.problem is not None:
            counts.error(scrub(fetched.problem))
        if not fetched.usable:
            return "fetch_failed"
        zone = self._profiles[profile].tzinfo()
        batch = observe_rss(fetched, goodreads.shelf_map, started, zone)

        if not await self._db.read(identity.backfilled, profile, SOURCE):
            if not batch.complete:
                return "partial"  # the backfill waits for a complete enumeration
            self._started(profile, run_id)
            return await self._backfill(profile, batch, run_id, counts)

        # Stage 2: the diff, against baselines only.
        now = int(self._clock.time())
        observed = await self._db.write(identity.observe, profile, SOURCE, batch, now)
        if observed.gone:
            log.info(
                "Goodreads no longer lists some books; recorded, never pushed",
                extra={"event": "rows_gone", "profile": profile, "count": observed.gone},
            )
        rows = await self._db.read(identity.load_rows, profile, SOURCE)
        work = _ready_to_push(observed.pending, rows)
        if not work and not observed.new and not observed.rematch:
            return "ok" if batch.complete else "partial"
        self._started(profile, run_id)
        assert batch.observed_at is not None
        observations = {obs.source_id: obs for obs in batch.rows}

        if observed.new or observed.rematch:
            # Matching only reads Hardcover, so it doesn't hold the apply lock.
            try:
                added = await self._add(profile, batch, observed.new, observed.rematch, rows)
            except (HardcoverError, FetchFailed) as exc:
                # The books stay as they were, so the next run matches them; the rest goes on.
                counts.error(str(exc) if isinstance(exc, FetchFailed) else scrub(str(exc)))
                log.warning(
                    "couldn't match books on Goodreads; the next run tries again",
                    extra={"event": "match_failed", "profile": profile},
                )
            else:
                rows = await self._db.read(identity.load_rows, profile, SOURCE)
                work = _ready_to_push([*observed.pending, *added], rows)

        # Stages 3 and 4, under the profile's apply lock.
        async with self._applier.lock(profile):
            plans = _plans(work, observations, rows, goodreads.shelf_map, zone)
            # What needs no Hardcover write settles first, never costing the fetch.
            for plan in plans:
                if plan.target is None:
                    await self._settle(profile, plan, batch.observed_at, None, run_id)
            writes = [plan for plan in plans if plan.target is not None]
            if writes:
                try:
                    library = await self._mirror.fetch(profile)
                    writes = await self._ready(profile, writes, library.user_books, counts)
                except FetchFailed as exc:
                    counts.error(str(exc))
                    return "deferred"
                except HardcoverError as exc:
                    counts.error(scrub(str(exc)))
                    return "deferred"
            for plan in _shelf_first(writes, rows):
                await self._apply(profile, plan, batch.observed_at, run_id, counts)
        if counts.failed or not batch.complete:
            return "partial"
        return "ok"

    async def _add(
        self,
        profile: str,
        batch: Batch,
        new: list[Observation],
        rematch: list[Observation],
        rows: Mapping[str, SourceRow],
    ) -> list[Pending]:
        """Match the books first seen now and add them from an empty baseline (§3.4a.6,
        rule 3), and re-match the queued rows whose match input changed, returning the
        pending changes on the rows now linked. Unlike the backfill, a match links whether or
        not the book is on the profile's shelves, and a clear fuzzy match links too; but a
        row the backfill queued is re-matched by the backfill's rules (§3.4a.12)."""
        later = [
            obs for obs in [*new, *rematch] if not rows.get(obs.source_id, _NEW).backfill_queued
        ]
        backfilled = [obs for obs in rematch if rows[obs.source_id].backfill_queued]
        placements = await self._placements(profile, later, shelved=None)
        if backfilled:
            library = await self._mirror.fetch(profile)
            shelved = _shelved(library.user_books)
            placements += await self._placements(profile, backfilled, shelved=shelved)
        changed = {obs.source_id for obs in rematch}
        now = int(self._clock.time())
        added = await self._db.write(
            identity.add_rows,
            profile,
            SOURCE,
            batch,
            [p for p in placements if p.observation.source_id not in changed],
            now,
        )
        if rematch:
            await self._db.write(
                identity.rematch_rows,
                profile,
                SOURCE,
                [p for p in placements if p.observation.source_id in changed],
                now,
            )
        log.info(
            "matched books first seen on Goodreads, or whose details changed",
            extra={
                "event": "rows_matched",
                "profile": profile,
                "new": len(new),
                "rematched": len(rematch),
                "linked": sum(p.linked for p in placements),
                "queued": sum(not p.linked for p in placements),
            },
        )
        relinked = await self._db.read(identity.pending_for, profile, SOURCE, changed)
        return [*added, *relinked]

    async def _placements(
        self,
        profile: str,
        observations: list[Observation],
        *,
        shelved: Mapping[int, int] | None,
    ) -> list[Placement]:
        """Where each row goes: an active shared link is reused, a stale one queues the row,
        and the rest go through the matcher chain (§3.4a.5). ``shelved`` (the profile's shelf
        entries by book) makes it the backfill: a link only to a book on those shelves, and
        no fuzzy auto-link (§3.4a.12)."""
        ids = [obs.source_id for obs in observations]
        shared, stale = await self._db.read(identity.shared_links, SOURCE, ids)
        inputs = [
            MatchInput(obs.source_id, obs.title, obs.author, obs.isbn13, obs.isbn10)
            for obs in observations
            if obs.source_id not in shared and obs.source_id not in stale
        ]
        outcomes = (
            await match(self._hardcover[profile], inputs, accept_fuzzy=shelved is None)
            if inputs
            else {}
        )
        placements = []
        for obs in observations:
            if obs.source_id in stale:
                # A link Hardcover no longer backs: a person resolves it (§3.4a.4, rule 7).
                placements.append(Placement(obs, None, False, reason="stale"))
                continue
            shared_hit = shared.get(obs.source_id)
            outcome = Outcome(shared_hit) if shared_hit else outcomes[obs.source_id]
            placements.append(_place(obs, outcome, shelved))
        return placements

    async def _ready(
        self,
        profile: str,
        plans: list[_Plan],
        user_books: list[dict[str, Any]],
        counts: RunCounts,
    ) -> list[_Plan]:
        """Finish the plans against this run's Hardcover fetch (§3.3.5, §3.4a.6 rule 6): no
        read is opened over one already open, a re-read with no open read to complete is
        always a new read, and each new read gets its edition. A plan whose edition can't be
        looked up waits for the next run; the others go ahead."""
        reads = _reads_by_book(user_books)
        for i, plan in enumerate(plans):
            assert plan.target is not None
            read, open_now = plan.target.read, _has_open_read(reads.get(plan.hc_book_id, []))
            if isinstance(read, Start) and open_now:
                plans[i] = replace(plan, target=replace(plan.target, read=None))
            elif isinstance(read, Finish) and plan.trigger == "reread_cycle" and not open_now:
                read = replace(read, reread=True)
                plans[i] = replace(plan, target=replace(plan.target, read=read))
        reading = {p.source_id: p.hc_book_id for p in plans if p.reads}
        try:
            editions = await self._editions(profile, reading)
        except HardcoverError as exc:
            reason = scrub(str(exc))
            counts.failed += len(reading)
            counts.error(reason)
            log.warning(
                "couldn't look up the editions of new reads; they wait for the next run: %s",
                reason,
                extra={"event": "editions_failed", "profile": profile, "count": len(reading)},
            )
            return [p for p in plans if not p.reads]
        for i, plan in enumerate(plans):
            if plan.target is not None and plan.target.read is not None:
                read = replace(plan.target.read, edition=editions.get(plan.source_id))
                plans[i] = replace(plan, target=replace(plan.target, read=read))
        return plans

    async def _editions(self, profile: str, books: Mapping[str, int]) -> dict[str, int]:
        """The edition each row's new read is written with (``books``: source id → the book
        the read goes on): its link's, when Hardcover says that edition is of this book and
        has a format. Otherwise the row is left out and its read has no edition, so it counts
        as ``unknown`` rather than a guessed format (§3.3.5)."""
        linked = await self._db.read(identity.link_editions, SOURCE, books)
        if not linked:
            return {}
        result = await self._hardcover[profile].execute(
            EDITION_FORMATS, {"ids": sorted(set(linked.values()))}
        )
        known = {
            e["id"]: e.get("book_id")
            for e in result.data.get("editions") or []
            if e.get("reading_format_id")
        }
        return {
            source_id: edition
            for source_id, edition in linked.items()
            if edition in known and known[edition] == books[source_id]
        }

    async def _backfill(
        self, profile: str, batch: Batch, run_id: int, counts: RunCounts
    ) -> RunState:
        """The map-only backfill (§3.4a.12): reads Hardcover, never writes it."""
        # Matching only reads Hardcover, so it doesn't hold the apply lock.
        try:
            library = await self._mirror.fetch(profile)
            placements = await self._placements(
                profile, list(batch.rows), shelved=_shelved(library.user_books)
            )
        except FetchFailed as exc:
            counts.error(str(exc))
            return "deferred"
        except HardcoverError as exc:
            counts.error(scrub(str(exc)))
            return "deferred"
        now = int(self._clock.time())
        linked, queued = await self._db.write(
            identity.record_backfill, profile, SOURCE, batch, placements, run_id, now
        )
        log.info(
            "Goodreads backfill done",
            extra={
                "event": "backfill_done",
                "profile": profile,
                "source": SOURCE,
                "run_id": run_id,
                "rows_linked": linked,
                "rows_queued": queued,
            },
        )
        return "ok"

    async def _apply(
        self, profile: str, plan: _Plan, observed_at: int, run_id: int, counts: RunCounts
    ) -> None:
        assert plan.target is not None
        try:
            applied = await self._applier.apply(
                profile, plan.hc_book_id, plan.target, trigger=TRIGGER, locked=True
            )
        except (HardcoverError, *REFUSALS) as exc:
            # The baselines stay put, so the next run tries again (§3.4a.6, rule 9).
            reason = _failure(exc, plan.hc_book_id)
            counts.failed += 1
            counts.error(reason)
            for change in plan.settles:
                log.warning(
                    "a Goodreads change didn't reach Hardcover: %s",
                    reason,
                    extra={
                        "event": "change_failed",
                        "profile": profile,
                        "change_id": change.baseline_id,
                        "hc_book_id": plan.hc_book_id,
                        "field": change.field,
                    },
                )
            return
        await self._settle(profile, plan, observed_at, applied, run_id)
        counts.applied += 1

    async def _settle(
        self,
        profile: str,
        plan: _Plan,
        observed_at: int,
        applied: Applied | None,
        run_id: int,
    ) -> None:
        """Advance the plan's baselines, with its change log rows when Hardcover took a
        write (§3.4a.6, rule 9)."""
        logs = list(plan.logs) if applied is not None else []
        read = plan.target.read if plan.target is not None else None
        for change in applied.changes if applied is not None else []:
            # A correction that moved the read finished on the old date says so in its push
            # row; one that found nothing there finished (and maybe adopted) a read instead.
            moved = isinstance(read, Correct) and _finished_on(change.before) == read.old
            if change.field == "read" and not moved:
                kind = "read_insert" if change.before is None else "read_adopt"
                logs.append(LogRow(kind, None, None, None, plan.trigger, plan.date_source))
        await self._db.write(
            identity.settle,
            profile,
            SOURCE,
            plan.source_id,
            plan.hc_book_id,
            plan.settles,
            observed_at,
            applied.user_book_id if applied is not None else None,
            logs,
            run_id,
            int(self._clock.time()),
        )
        for change in plan.settles:
            log.info(
                "applied a Goodreads change" if applied else "settled a Goodreads change",
                extra={
                    "event": "change_applied" if applied else "change_settled",
                    "profile": profile,
                    "change_id": change.baseline_id,
                    "hc_book_id": plan.hc_book_id,
                    "field": change.field,
                    "trigger": plan.trigger,
                    "date_source": plan.date_source,
                    # Change lines carry status, rating and date values only, never text.
                    **({"old": change.old, "new": change.new} if change.field in LOGGED else {}),
                },
            )

    def _started(self, profile: str, run_id: int) -> None:
        # An idle run logs only its end line (§2.7).
        log.info(
            "Goodreads run started",
            extra={
                "event": "run_started",
                "profile": profile,
                "source": "gr_rss",
                "run_id": run_id,
            },
        )


_NEW = SourceRow("", "queued", None, None)
"""What a row not yet in the database looks like to :meth:`GoodreadsSync._add`."""


def _place(obs: Observation, outcome: Outcome, shelved: Mapping[int, int] | None) -> Placement:
    """A row's place from the chain's outcome. In the backfill (``shelved`` given) a hit
    links only a book on the profile's shelves; otherwise it's queued ``not_in_library``,
    with the hit as its candidate (§3.4a.12)."""
    hit = outcome.hit
    if hit is None:
        return Placement(obs, None, False, reason=outcome.reason, candidates=outcome.candidates)
    if shelved is None:
        return Placement(obs, hit, True)
    if (user_book := shelved.get(hit.book_id)) is not None:
        return Placement(obs, hit, True, user_book)
    candidate = Candidate(hit.book_id, hit.edition_id, hit.method, None)
    return Placement(obs, hit, False, reason="not_in_library", candidates=(candidate,))


def _shelved(user_books: list[dict[str, Any]]) -> dict[int, int]:
    """Each shelved book's user_book id, by book id and by the book it was merged into, so
    a hit (which follows ``canonical_id``) finds a shelf entry still on the merged book."""
    shelved: dict[int, int] = {}
    for ub in user_books:
        shelved.setdefault(ub["book_id"], ub["id"])
        if canonical := (ub.get("book") or {}).get("canonical_id"):
            shelved.setdefault(canonical, ub["id"])
    return shelved


REFUSALS = (
    Incomplete,
    NotShelved,
    DateNeeded,
    OpenReadExists,
    FormatNeeded,
    ReadStillOpen,
    DatesOutOfOrder,
)
"""What the write procedure raises for a change it can't make as asked: each fails that one
change and leaves its baseline, never the run."""


def _failure(exc: Exception, book_id: int) -> str:
    """A failed change's reason, safe to show and store (§2.7)."""
    if isinstance(exc, NotShelved):
        return f"book {book_id} is not on the profile's Hardcover shelves"
    if isinstance(exc, HardcoverError | Incomplete):
        return scrub(str(exc))
    return f"the write procedure refused the change ({type(exc).__name__})"


@dataclass(frozen=True)
class _Plan:
    """One write to Hardcover for one row (or none), and the baselines it settles."""

    source_id: str
    hc_book_id: int
    settles: list[Pending]
    target: Target | None = None
    """``None``: the baselines settle with no write (an unmapped shelf, a vanished date)."""
    logs: list[LogRow] = field(default_factory=list[LogRow])
    trigger: str | None = None
    date_source: str | None = None

    @property
    def reads(self) -> bool:
        """Whether the write may add a read, which needs its edition."""
        return self.target is not None and self.target.read is not None


def _plans(
    changes: list[Pending],
    observations: Mapping[str, Observation],
    rows: Mapping[str, SourceRow],
    shelf_map: Mapping[str, int],
    zone: tzinfo,
) -> list[_Plan]:
    """Each row's pending changes as writes: its status (with the read edits it carries and
    any Date Read change), a Date Read correction, its rating."""
    by_row: dict[str, dict[str, Pending]] = {}
    for change in changes:
        by_row.setdefault(change.source_id, {})[change.field] = change
    plans: list[_Plan] = []
    for source_id, fields in by_row.items():
        row, obs = rows[source_id], observations[source_id]
        status, date_read = fields.get("status"), fields.get("date_read")
        if status is not None:
            plans.extend(_status_plan(status, date_read, obs, row, shelf_map, zone))
        elif date_read is not None:
            plans.append(_date_plan(date_read, _value(row, "status"), shelf_map))
        if (rating := fields.get("rating")) is not None:
            # Goodreads rates in whole stars; unrated clears the Hardcover rating.
            target = Target(rating=None if rating.new is None else float(rating.new))
            plans.append(_push(rating, target))
        review, spoiler = fields.get("review"), fields.get("spoiler")
        text = review.new if review is not None else _value(row, "review")
        if review is not None:
            # Written as review_markdown; an emptied review clears it and its spoiler flag
            # (rule 5), so a spoiler change alongside settles with it.
            plan = _push(review, Target(review=review.new))
            if spoiler is not None and text is None:
                plan.settles.append(spoiler)
                spoiler = None
            plans.append(plan)
        if spoiler is not None:
            if text is None:
                plans.append(_Plan(source_id, spoiler.hc_book_id, [spoiler]))  # no review
            else:
                plans.append(_push(spoiler, Target(review_has_spoilers=spoiler.new == "1")))
    return plans


def _push(change: Pending, target: Target) -> _Plan:
    """A plain field push: one change, one write, one ``push`` row."""
    push = LogRow("push", change.field, change.old, change.new)
    return _Plan(change.source_id, change.hc_book_id, [change], target, [push])


def _shelf_first(plans: list[_Plan], rows: Mapping[str, SourceRow]) -> list[_Plan]:
    """The writes for a row with no shelf entry known go in one insert (§3.3.6 rule 3): its
    status plan takes every other field's target, so the book is added with them. Without a
    status plan that writes, nothing can add the book, so its other changes wait (#182)."""
    by_row: dict[str, list[_Plan]] = {}
    for plan in plans:
        by_row.setdefault(plan.source_id, []).append(plan)
    ready: list[_Plan] = []
    for source_id, group in by_row.items():
        if rows[source_id].hc_user_book_id is not None:
            ready.extend(group)
            continue
        status = next((p for p in group if any(c.field == "status" for c in p.settles)), None)
        if status is None or status.target is None:
            continue  # they wait for the row's status
        target, settles, logs = status.target, list(status.settles), list(status.logs)
        for other in group:
            if other is status or other.target is None:
                continue
            target = _merged(target, other.target)
            settles.extend(other.settles)
            logs.extend(other.logs)
        ready.append(replace(status, target=target, settles=settles, logs=logs))
    return ready


def _merged(target: Target, other: Target) -> Target:
    """``target`` with every field ``other`` sets."""
    fields = ("status_id", "rating", "review", "review_has_spoilers")
    changes = {f: getattr(other, f) for f in fields if getattr(other, f) is not UNSET}
    if other.read is not None:
        changes["read"] = other.read
    return replace(target, **changes)


def _ready_to_push(changes: list[Pending], rows: Mapping[str, SourceRow]) -> list[Pending]:
    """The changes to plan this run: the fields that reach Hardcover, less those that must
    wait for a status. A change with no status, on a row with no shelf entry known, can't be
    written (the write procedure adds a book only with a status), so it waits for the row's
    status to be observed rather than failing every run (#182)."""
    with_status = {c.source_id for c in changes if c.field == "status"}
    return [
        c
        for c in changes
        if c.field in PUSHED
        and (c.source_id in with_status or rows[c.source_id].hc_user_book_id is not None)
    ]


def _status_plan(
    status: Pending,
    date_read: Pending | None,
    obs: Observation,
    row: SourceRow,
    shelf_map: Mapping[str, int],
    zone: tzinfo,
) -> list[_Plan]:
    """A status change and the read edits it carries, as one change (§3.4a.6, rule 6). A
    Date Read change on the same row is the finish date when the change finishes a read;
    otherwise it's planned on its own, against the new status."""
    new = shelf_map.get(status.new) if status.new is not None else None
    old = shelf_map.get(status.old) if status.old is not None else None
    finishes = new == READ and old != READ
    rest = [] if date_read is None or finishes else [_date_plan(date_read, status.new, shelf_map)]
    settles = [status, *([date_read] if date_read is not None and finishes else [])]
    if new is None:
        log.info(
            "a Goodreads exclusive shelf isn't in shelf_map, so its status isn't pushed",
            extra={"event": "unmapped_shelf", "shelf": status.new, "change_id": status.baseline_id},
        )
        return [_Plan(status.source_id, status.hc_book_id, settles), *rest]
    trigger = "status"
    op: Start | Finish | None = None
    date_source: str | None = None
    if finishes:
        reread = status.old == "currently-reading" and status.prev_value == "read"
        trigger = "reread_cycle" if reread else "status"
        # The Date Read already on the baseline is the last read's: a re-read finishes on a
        # date of its own unless Goodreads' date moved with it.
        finished = date_read.new if date_read is not None else None
        if finished is None and not reread:
            finished = _value(row, "date_read")
        on, date_source = _when(finished, obs, status, zone)
        op = Finish(on)
    elif new == CURRENTLY_READING and old != CURRENTLY_READING:
        on, date_source = _when(None, obs, status, zone)
        op = Start(on)
    push = LogRow("push", "status", status.old, status.new, trigger, date_source)
    target = Target(status_id=new, read=op)
    plan = _Plan(status.source_id, status.hc_book_id, settles, target, [push], trigger, date_source)
    return [plan, *rest]


def _date_plan(change: Pending, status: str | None, shelf_map: Mapping[str, int]) -> _Plan:
    """A Date Read change that finishes nothing: a correction (§3.4a.6, rule 6). ``status``
    is the row's exclusive shelf, as of this run."""
    if change.new is None or status is None or shelf_map.get(status) != READ:
        # A vanished Date Read is never pushed (rule 5), and a date on a book that isn't Read
        # isn't a finish; either way the baseline moves on.
        return _Plan(change.source_id, change.hc_book_id, [change])
    op = Correct(change.old, change.new) if change.old is not None else Finish(change.new)
    push = LogRow("push", "date_read", change.old, change.new, None, "goodreads_date_read")
    target = Target(read=op)
    return _Plan(
        change.source_id,
        change.hc_book_id,
        [change],
        target,
        [push],
        date_source="goodreads_date_read",
    )


def _when(
    date_read: str | None, obs: Observation, change: Pending, zone: tzinfo
) -> tuple[str, str]:
    """A read's date and where it came from (§3.4a.6, rule 6): Goodreads' Date Read, else the
    date of RSS ``user_date_added`` in the profile's zone, else the day SyncShelf first saw
    the change."""
    if date_read is not None:
        return date_read, "goodreads_date_read"
    if obs.last_touched is not None:
        return obs.last_touched.astimezone(zone).date().isoformat(), "user_date_added"
    seen = change.pending_since if change.pending_since is not None else 0
    return datetime.fromtimestamp(seen, UTC).astimezone(zone).date().isoformat(), "first_seen"


def _value(row: SourceRow, name: str) -> str | None:
    baseline = row.baselines.get(name)
    return baseline.value if baseline is not None else None


def _reads_by_book(user_books: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """Each shelved book's reads, by book id and by the book it was merged into."""
    reads: dict[int, list[dict[str, Any]]] = {}
    for ub in user_books:
        found = ub.get("user_book_reads") or []
        reads.setdefault(ub["book_id"], found)
        if canonical := (ub.get("book") or {}).get("canonical_id"):
            reads.setdefault(canonical, found)
    return reads


def _finished_on(read: Any) -> str | None:
    """A read change's finish date, as the write procedure reports reads."""
    return read.get("finished_on") if isinstance(read, dict) else None


def _has_open_read(reads: list[dict[str, Any]]) -> bool:
    return any(r.get("started_at") and not r.get("finished_at") for r in reads)
