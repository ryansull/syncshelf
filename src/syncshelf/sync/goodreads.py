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

Phase 4a builds this up ticket by ticket: here a run pushes **rating** changes. Other fields'
changes are recorded as pending and wait for the tickets that push them, and rows first seen
after the backfill are left for #162.
"""

import logging
import time
from collections.abc import Mapping
from typing import Protocol

import httpx2

from syncshelf.clients.goodreads_rss import GoodreadsRss, Pacer
from syncshelf.clients.hardcover import HardcoverClient, HardcoverError
from syncshelf.config import ProfileConfig
from syncshelf.db import Database
from syncshelf.logging import scrub
from syncshelf.sync import identity, runs
from syncshelf.sync.apply import Applier, Incomplete, Target
from syncshelf.sync.identity import Pending, Placement
from syncshelf.sync.matcher import goodreads_mappings
from syncshelf.sync.mirror import Mirror
from syncshelf.sync.observations import Batch, observe_rss
from syncshelf.sync.runs import RunCounts, RunState, Trigger

__all__ = ["Clock", "GoodreadsSync"]

log = logging.getLogger(__name__)

SOURCE = "goodreads"
TRIGGER = "goodreads"
"""Who the write procedure's change lines name as asking."""
LOGGED = frozenset({"status", "rating", "date_read", "read_count", "spoiler"})
"""The fields whose values a change line may carry (§2.7)."""


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
        for name, profile in profiles.items():
            if profile.goodreads is None:
                continue
            self._rss[name] = GoodreadsRss(
                profile.goodreads.user_id,
                key=profile.goodreads.rss_key,
                pacer=Pacer(clock),
                transport=transport,
            )

    async def aclose(self) -> None:
        """Close every profile's Goodreads connection."""
        for rss in self._rss.values():
            await rss.aclose()

    def enabled(self, profile: str) -> bool:
        """Whether the profile has a Goodreads source."""
        return profile in self._rss

    async def run_rss(self, profile: str, trigger: Trigger = "schedule") -> int:
        """Run the profile's RSS source once and return the run's id. Every outcome is on the
        run's row; this raises only for a profile with no Goodreads source."""
        rss = self._rss[profile]
        started = int(self._clock.time())
        began = time.monotonic()
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
                    "duration_s": round(time.monotonic() - began, 3),
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
        work = [change for change in observed.pending if _target(change) is not None]
        if not work:
            return "ok" if batch.complete else "partial"
        self._started(profile, run_id)

        # Stages 3 and 4, under the profile's apply lock.
        async with self._applier.lock(profile):
            try:
                await self._mirror.fetch(profile)
            except HardcoverError as exc:
                counts.error(scrub(str(exc)))
                return "deferred"
            for change in work:
                await self._apply(profile, change, batch, run_id, counts)
        if counts.failed or not batch.complete:
            return "partial"
        return "ok"

    async def _backfill(
        self, profile: str, batch: Batch, run_id: int, counts: RunCounts
    ) -> RunState:
        """The map-only backfill (§3.4a.12): reads Hardcover, never writes it."""
        ids = [row.source_id for row in batch.rows]
        async with self._applier.lock(profile):
            try:
                library = await self._mirror.fetch(profile)
                shared = await self._db.read(identity.shared_links, SOURCE, ids)
                unlinked = [i for i in ids if i not in shared]
                hits = {**await goodreads_mappings(self._hardcover[profile], unlinked), **shared}
            except HardcoverError as exc:
                counts.error(scrub(str(exc)))
                return "deferred"
        shelved = {ub["book_id"]: ub["id"] for ub in library.user_books}
        placements = []
        for row in batch.rows:
            hit = hits.get(row.source_id)
            user_book = shelved.get(hit.book_id) if hit is not None else None
            placements.append(Placement(row, hit, user_book is not None, user_book))
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
        self, profile: str, change: Pending, batch: Batch, run_id: int, counts: RunCounts
    ) -> None:
        target = _target(change)
        assert target is not None
        try:
            applied = await self._applier.apply(
                profile, change.hc_book_id, target, trigger=TRIGGER, locked=True
            )
        except (HardcoverError, Incomplete) as exc:
            # The baseline stays put, so the next run tries again (§3.4a.6, rule 9).
            counts.failed += 1
            counts.error(scrub(str(exc)))
            log.warning(
                "a Goodreads change didn't reach Hardcover: %s",
                scrub(str(exc)),
                extra={
                    "event": "change_failed",
                    "profile": profile,
                    "change_id": change.baseline_id,
                    "hc_book_id": change.hc_book_id,
                    "field": change.field,
                },
            )
            return
        assert batch.observed_at is not None
        await self._db.write(
            identity.commit,
            profile,
            SOURCE,
            change,
            batch.observed_at,
            applied.user_book_id,
            run_id,
            int(self._clock.time()),
        )
        counts.applied += 1
        log.info(
            "applied a Goodreads change",
            extra={
                "event": "change_applied",
                "profile": profile,
                "change_id": change.baseline_id,
                "hc_book_id": change.hc_book_id,
                "field": change.field,
                # Change lines carry status, rating and date values only, never text (§2.7).
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


def _target(change: Pending) -> Target | None:
    """What Hardcover should hold for a change, or ``None`` for a field this phase doesn't
    push yet."""
    if change.field == "rating":
        # Goodreads rates in whole stars; unrated clears the Hardcover rating.
        return Target(rating=None if change.new is None else float(change.new))
    return None
