"""Fetching a profile's Goodreads library export and storing it as a batch (SPEC §3.4b.4).

This is the seam between the signed-in HTTP layer (:mod:`syncshelf.clients.goodreads_export`)
and the 4a engine. It owns one job: get an export, and put it in ``ingest_batches`` where a
run can consume it exactly as it consumes an upload (§3.4a.8). Nothing here diffs, matches or
pushes — the stored batch is indistinguishable from one a person uploaded, bar its ``source``.

**The jar is read and written around every fetch.** Goodreads regenerates ``_session_id2``
through a redirect on almost every request (§3.4b.1), so the jar that comes back is the one
worth keeping, and it is saved whether the fetch succeeded or not: a rotated cookie from a
request that then failed is still the session.

**An export is always dated.** Either SyncShelf triggered it, in which case the trigger time
is when its contents were true, or it read the ``#exportFile`` timestamp. So an automatic
export is never a *stale* batch, and unlike an undated upload it pushes (§3.4a.8).
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Protocol

import httpx2

from syncshelf.clients.goodreads_csv import parse_export
from syncshelf.clients.goodreads_export import (
    IMPORT_PATH,
    ExportError,
    FetchedExport,
    GoodreadsExport,
    fetch_export,
    parse_import_page,
)
from syncshelf.clients.goodreads_rss import Pacer
from syncshelf.db import Database
from syncshelf.sync import batches
from syncshelf.sync.goodreads_session import jar_path, load_jar, save_jar

__all__ = ["Clock", "Exported", "fetch_and_store"]

log = logging.getLogger(__name__)

SOURCE = "gr_csv_auto"
"""What an export SyncShelf fetched itself is stored as (§3.4b.4)."""


class Clock(Protocol):
    """The engine's clock, declared here so this module doesn't import the engine that uses it.

    Wall time dates the batch and reads the ``#exportFile`` timestamp; monotonic time and
    ``sleep`` are what the export poll and the pacer run on (§3.4b.4).
    """

    def time(self) -> float: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class Exported:
    """The batch one export became."""

    batch_id: int
    observed_at: int
    """When the export's contents were true of the library, as epoch seconds."""
    rows: int
    reused: bool
    """Whether an export Goodreads already had was used instead of triggering a new one."""


async def fetch_and_store(
    db: Database,
    profile: str,
    *,
    tz: tzinfo,
    data_dir: Path,
    pacer: Pacer,
    clock: Clock,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> Exported:
    """Fetch ``profile``'s library export and store it as a ``gr_csv_auto`` batch.

    ``tz`` is the profile's timezone, which the ``#exportFile`` link text is read in (§3.4b.4).
    ``pacer`` is the profile's Goodreads pacer, shared with its RSS feed, so these requests
    and the feed's stay ≥ 2 s apart and single-flight (§3.4b.4).

    Raises :class:`~syncshelf.clients.goodreads_export.SessionExpired` when the jar no longer
    carries a session, :class:`~syncshelf.clients.goodreads_export.Challenged` on a WAF stop,
    and :class:`~syncshelf.clients.goodreads_export.ExportError` for anything else that kept
    the export from being fetched, including a page that could not be read. The caller decides
    what each means for the session state. A CSV whose header does not validate raises
    :class:`~syncshelf.clients.goodreads_csv.BadExport`.
    """
    path = jar_path(data_dir, profile)
    client = GoodreadsExport(load_jar(path), pacer=pacer, transport=transport)
    try:
        fetched = await _fetch(client, tz, clock)
    finally:
        # Saved even when the fetch failed: a cookie Goodreads rotated in on the way is still
        # the session, and dropping it would cost a sign-in for nothing.
        save_jar(client.jar, path)
        await client.aclose()
    rows = await asyncio.to_thread(parse_export, fetched.payload)
    observed_at = int(fetched.observed_at.timestamp())
    batch_id = await db.write(
        batches.store,
        profile,
        SOURCE,
        int(clock.time()),
        observed_at,
        fetched.payload,
        len(rows),
    )
    log.info(
        "stored a Goodreads library export",
        extra={
            "event": "goodreads_export_stored",
            "profile": profile,
            "batch_id": batch_id,
            "rows": len(rows),
            "reused": fetched.reused,
        },
    )
    return Exported(batch_id, observed_at, len(rows), fetched.reused)


async def _fetch(client: GoodreadsExport, tz: tzinfo, clock: Clock) -> FetchedExport:
    """Read the import page, then run §3.4b.4 steps 2 to 4 against it."""
    response = await client.get(IMPORT_PATH)
    if not response.is_success:
        raise ExportError(f"the Goodreads import page could not be read ({response.status_code})")
    page = parse_import_page(response.text, tz)
    now = datetime.fromtimestamp(clock.time(), tz)
    return await fetch_export(client, page, now=now, clock=clock)
