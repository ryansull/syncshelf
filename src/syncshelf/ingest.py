"""``POST /ingest/goodreads-csv`` (SPEC §3.4a.8): the LAN-only manual fallback for getting a
Goodreads library export into SyncShelf, whatever state the Goodreads session is in.

**The secret identifies the profile.** ``Authorization: Bearer <ingest secret>`` is compared in
constant time against every profile's secret, so the request names no user, and an unknown
secret is a 401. The route isn't behind MCP auth; it's LAN-only by routing, as ``/healthz``
is: the public hostname never routes it (§2.2).

The body is ``multipart/form-data``: the export as a file part ``file``, and optionally
``generated_at``, the export's generation time (ISO-8601). Without it the batch is **stale**
and only fills (§3.4a.8). With it the upload also counts as a **CSV success**, pushing the
next automatic export out five days (§3.4b.5). The export's header is checked (400 names
the missing columns), the batch stored, the profile's Goodreads loop nudged, and 202 returned
at once, without waiting on a sync: ``{"batch_id", "run": "started" | "already_running", "stale"}``.
"""

import hmac
import logging
from datetime import UTC, datetime

import anyio
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from syncshelf.clients.goodreads_csv import BadExport, parse_export
from syncshelf.config import ProfileConfig
from syncshelf.sync import batches

__all__ = ["ingest_goodreads_csv"]

log = logging.getLogger(__name__)

STALE_HINT = (
    "Without generated_at this upload only fills in what SyncShelf hasn't seen. Resend it with "
    "generated_at (when the export was made, ISO-8601) so its changes reach Hardcover."
)


async def ingest_goodreads_csv(request: Request) -> JSONResponse:
    """Store an uploaded library export for the profile its secret names, and nudge that
    profile's Goodreads loop."""
    profile = _caller(request.headers.get("authorization", ""), request.app.state.profiles)
    if profile is None:
        return _error(401, "unknown ingest secret")
    if profile.goodreads is None:
        return _error(400, f"profile {profile.name} has no goodreads section")
    try:
        payload, generated = await _parts(request)
    except _Refused as exc:
        return _error(400, str(exc))
    observed_at = None
    if isinstance(generated, str) and generated.strip():
        try:
            observed_at = _epoch(generated.strip())
        except ValueError:
            return _error(400, "generated_at must be an ISO-8601 time, with its offset")
    try:
        # A whole library parses in a worker thread, off the event loop MCP is served on.
        rows = await anyio.to_thread.run_sync(parse_export, payload)
    except BadExport as exc:
        body: dict[str, object] = {"error": str(exc)}
        if exc.missing:
            body["missing"] = list(exc.missing)
        return JSONResponse(body, status_code=400)
    now = int(request.app.state.clock())
    batch_id = await request.app.state.db.write(
        batches.store, profile.name, "gr_csv_ingest", now, observed_at, payload, len(rows)
    )
    scheduler = request.app.state.scheduler
    # The lock, not the run record: a run holds it a moment before it has an id.
    run = "already_running" if scheduler.sync.lock(profile.name).locked() else "started"
    scheduler.nudge(profile.name, "ingest")
    stale = observed_at is None
    if observed_at is not None:
        # A dated upload is a CSV success: the household has a fresh export whoever fetched
        # it, so the next automatic one is five days from the export's own generation time
        # (§3.4a.8, §3.4b.5). An undated one only fills, so it buys nothing.
        await scheduler.csv_uploaded(profile.name, observed_at)
    log.info(
        "stored an uploaded Goodreads export",
        extra={
            "event": "ingest_stored",
            "profile": profile.name,
            "batch_id": batch_id,
            "rows": len(rows),
            "stale": stale,
        },
    )
    answer: dict[str, object] = {"batch_id": batch_id, "run": run, "stale": stale}
    if stale:
        answer["hint"] = STALE_HINT
    return JSONResponse(answer, status_code=202)


class _Refused(ValueError):
    pass


async def _parts(request: Request) -> tuple[bytes, str | None]:
    try:
        async with request.form(max_files=1, max_fields=4) as form:
            upload, generated = form.get("file"), form.get("generated_at")
            if not isinstance(upload, UploadFile):
                raise _Refused("send the export as a file part named 'file'")
            if isinstance(generated, UploadFile):
                raise _Refused("send generated_at as a text field, not a file")
            return await upload.read(), generated
    except HTTPException as exc:  # Starlette's: more parts than an upload has, or a bad body
        raise _Refused(f"not a valid upload: {exc.detail}") from None


def _caller(header: str, profiles: dict[str, ProfileConfig]) -> ProfileConfig | None:
    scheme, _, secret = header.partition(" ")
    if scheme.lower() != "bearer" or not secret:
        return None
    given = secret.encode()
    found = None
    # Every secret is compared, so the time taken never says which profile came close.
    for profile in profiles.values():
        if profile.ingest_secret is not None and hmac.compare_digest(
            given, profile.ingest_secret.encode()
        ):
            found = profile
    return found


def _epoch(text: str) -> int:
    when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if when.tzinfo is None:
        raise ValueError("no offset")  # a time with no zone could be anyone's
    return int(when.astimezone(UTC).timestamp())


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)
