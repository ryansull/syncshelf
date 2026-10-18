"""The per-profile mirror of each Hardcover library (SPEC §3.3.4).

Library tools read a local copy instead of querying Hardcover on every question: the library
query costs 2–3 requests and 2–3 s, and the answers (counts, stats, filters) need the whole
library anyway. The copy is refreshed on read once it's older than ``cadence.mirror_ttl``:

- **Full replace per profile**, in one transaction. A user_book deleted in Hardcover just
  disappears; there are no tombstones, and links and baselines (4a) are never touched.
- **One refresh at a time per profile.** A reader that finds a refresh in progress waits for
  it, then uses its result rather than starting another.
- **Refresh failure serves stale, flagged.** The tool answers from the copy with ``stale`` and
  a reason. Only a profile with no copy yet is refused (:class:`NoMirror`).

What's kept is what the read tools need: no review text and no private notes.
"""

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from syncshelf.clients.hardcover import HardcoverClient, HardcoverError, Library
from syncshelf.db import Database
from syncshelf.logging import scrub

__all__ = ["TAG_CATEGORIES", "Freshness", "Mirror", "NoMirror", "replace_library"]

log = logging.getLogger(__name__)

TAG_CATEGORIES = ("genres", "moods", "tags")
"""The ``cached_tags`` categories the library query fetches, as ``tags_json`` keys."""


class NoMirror(Exception):
    """The profile has no copy of its library yet, and fetching one failed. The message is
    safe to show (§2.7)."""


@dataclass(frozen=True)
class Freshness:
    """How current the copy a tool is about to read is."""

    as_of: int
    """Epoch seconds of the last successful full fetch."""
    stale: bool = False
    stale_reason: str | None = None
    """Why the refresh failed, safe to show, when ``stale``."""


class Mirror:
    """The mirrors of every profile, refreshed through each profile's Hardcover client."""

    def __init__(
        self,
        db: Database,
        clients: Mapping[str, HardcoverClient],
        *,
        ttl: int,
        clock: Callable[[], float],
    ) -> None:
        self._db = db
        self._clients = clients
        self._ttl = ttl
        self._clock = clock
        self._locks = {profile: asyncio.Lock() for profile in clients}
        self._attempts = dict.fromkeys(clients, 0)
        self._last: dict[str, Freshness | NoMirror] = {}

    async def fresh(self, profile: str) -> Freshness:
        """Make ``profile``'s copy current enough to read, refreshing it if it's older than
        the TTL, and say how current it is. Raises :class:`NoMirror` when there's no copy and
        the refresh failed.

        Readers that queue behind a refresh take its outcome, a failure included, rather than
        each trying again in turn.
        """
        # Counted before anything awaits, so an attempt that starts after this reader arrived
        # is always one it can share.
        attempts = self._attempts[profile]
        as_of = await self._db.read(_as_of, profile)
        if as_of is not None and self._clock() - as_of < self._ttl:
            return Freshness(as_of)
        async with self._locks[profile]:
            if self._attempts[profile] != attempts:
                # A refresh ran while this reader waited: share its outcome.
                return self._outcome(profile)
            # One that started before this reader arrived may have finished meanwhile.
            as_of = await self._db.read(_as_of, profile)
            if as_of is not None and self._clock() - as_of < self._ttl:
                return Freshness(as_of)
            self._attempts[profile] += 1
            try:
                library = await self._clients[profile].library()
                now = int(self._clock())
                await self._db.write(replace_library, profile, library, now)
            except (HardcoverError, sqlite3.Error, KeyError, TypeError) as exc:
                # A database or shape error means Hardcover sent something the copy can't
                # hold; like an unreachable Hardcover, the old copy is served meanwhile.
                self._last[profile] = await self._failed(profile, exc)
                return self._outcome(profile)
            log.info(
                "refreshed the Hardcover mirror",
                extra={
                    "event": "mirror_refreshed",
                    "profile": profile,
                    "user_books": len(library.user_books),
                    "cost": library.cost,
                },
            )
            done = Freshness(now)
            self._last[profile] = done
            return done

    def _outcome(self, profile: str) -> Freshness:
        last = self._last[profile]
        if isinstance(last, NoMirror):
            raise last
        return last

    async def _failed(self, profile: str, exc: Exception) -> Freshness | NoMirror:
        if isinstance(exc, HardcoverError):
            reason = scrub(str(exc))
        else:
            reason = f"Hardcover sent data SyncShelf couldn't store ({type(exc).__name__})"
            log.error(
                "the Hardcover mirror couldn't store a refresh",
                exc_info=True,
                extra={"event": "mirror_store_failed", "profile": profile},
            )
        await self._db.write(_record_failure, profile, int(self._clock()), reason)
        log.warning(
            "the Hardcover mirror refresh failed: %s",
            reason,
            extra={"event": "mirror_refresh_failed", "profile": profile},
        )
        as_of = await self._db.read(_as_of, profile)
        if as_of is None:
            return NoMirror(
                f"SyncShelf has no copy of this Hardcover library yet, and fetching it "
                f"failed: {reason}"
            )
        return Freshness(as_of, stale=True, stale_reason=reason)


# --- the database side --------------------------------------------------------------------


def _as_of(conn: sqlite3.Connection, profile: str) -> int | None:
    row = conn.execute("SELECT as_of FROM mirror_state WHERE profile = ?", (profile,)).fetchone()
    return row[0] if row else None


def _record_failure(conn: sqlite3.Connection, profile: str, now: int, error: str) -> None:
    conn.execute(
        "INSERT INTO mirror_state (profile, last_attempt_at, last_error) VALUES (?, ?, ?) "
        "ON CONFLICT (profile) DO UPDATE SET last_attempt_at = excluded.last_attempt_at, "
        "last_error = excluded.last_error",
        (profile, now, error),
    )


_TABLES = ("mirror_books", "mirror_user_books", "mirror_reads", "mirror_list_items")


def replace_library(conn: sqlite3.Connection, profile: str, library: Library, now: int) -> None:
    """Replace ``profile``'s whole copy with ``library``, inside the caller's transaction."""
    for table in _TABLES:
        conn.execute(f"DELETE FROM {table} WHERE profile = ?", (profile,))  # noqa: S608
    for ub in library.user_books:
        _insert_user_book(conn, profile, ub)
    for lst in library.lists:
        for item in lst.get("list_books") or []:
            conn.execute(
                "INSERT OR IGNORE INTO mirror_list_items VALUES (?, ?, ?, ?)",
                (profile, lst["id"], lst.get("name") or "", item["book_id"]),
            )
    conn.execute(
        "INSERT INTO mirror_state (profile, as_of, last_attempt_at, last_error) "
        "VALUES (?, ?, ?, NULL) ON CONFLICT (profile) DO UPDATE SET as_of = excluded.as_of, "
        "last_attempt_at = excluded.last_attempt_at, last_error = NULL",
        (profile, now, now),
    )


def _insert_user_book(conn: sqlite3.Connection, profile: str, ub: Mapping[str, Any]) -> None:
    conn.execute(
        "INSERT INTO mirror_user_books VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            profile,
            ub["id"],
            ub["book_id"],
            ub["status_id"],
            ub.get("rating"),
            ub.get("edition_id"),
            ub.get("date_added"),
        ),
    )
    for read in ub.get("user_book_reads") or []:
        edition = read.get("edition") or {}
        conn.execute(
            "INSERT INTO mirror_reads VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                profile,
                read["id"],
                ub["id"],
                read.get("started_at"),
                read.get("finished_at"),
                read.get("edition_id"),
                edition.get("reading_format_id"),
            ),
        )
    book = ub.get("book") or {"id": ub["book_id"]}
    series = book.get("cached_featured_series") or {}
    series_info = series.get("series") or {}
    physical = book.get("default_physical_edition") or {}
    audio = book.get("default_audio_edition") or {}
    conn.execute(
        "INSERT OR REPLACE INTO mirror_books VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile,
            book["id"],
            book.get("title") or "",
            json.dumps(_contributors(book.get("cached_contributors"))),
            series_info.get("id"),
            series_info.get("name"),
            series.get("position"),
            book.get("pages") or physical.get("pages"),
            audio.get("audio_seconds"),
            json.dumps({c: _tag_names(book.get(c)) for c in TAG_CATEGORIES}),
        ),
    )


def _contributors(cached: Any) -> list[dict[str, str | None]]:
    """``[{name, role}]`` from ``cached_contributors``; ``role`` is ``None`` for an author."""
    out: list[dict[str, str | None]] = []
    for entry in cached or []:
        author = entry.get("author") or {}
        if author.get("name"):
            out.append({"name": author["name"], "role": entry.get("contribution")})
    return out


def _tag_names(cached: Any) -> list[str]:
    """The tag names of one ``cached_tags`` category, in Hardcover's order (most used first)."""
    return [t["tag"] for t in cached or [] if isinstance(t, dict) and t.get("tag")]
