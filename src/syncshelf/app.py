"""The outer Starlette app: route order, the lifespan, and ``/healthz`` (SPEC §2.2, §3.1).

Routes are in §2.2 order with ``Mount("/")`` last, because nothing after a root mount is
reachable. A mounted app's own lifespan never runs, so everything the process needs at
startup happens in :func:`create_app`'s lifespan, in §2.2's order:

1. config is already loaded and validated, because uvicorn needs it before the app starts
   (``__main__`` loads it and fails fast; #51 asks the spec to confirm this order);
2. the data directory and its ``goodreads/`` directory must be writable;
3. the database is opened and migrated, and the ``profiles`` table is synced with the
   config (:mod:`syncshelf.profiles`), every run still ``running`` is marked ``aborted``
   (:func:`syncshelf.sync.runs.abort_running`), and expired OAuth rows are purged
   (:func:`syncshelf.auth.tokens.purge_expired`, #77);
4. the version, ``sqlite3.sqlite_version`` and the auth mode are logged, never a secret,
   and dev mode logs a warning at every start (§3.2.5);
5. the MCP session manager runs for as long as the app does;
6. the sync loops resume from their schedules (:class:`syncshelf.sync.scheduler.Scheduler`),
   and the scheduler's housekeeping purges expired OAuth rows (#77) and old runs again every
   :data:`PURGE_INTERVAL`.

On shutdown the loops and the housekeeping are cancelled and the database is closed.

The MCP mount is the SDK's resource server: every ``/mcp`` request needs a bearer token that
:class:`syncshelf.auth.tokens.TokenVerifier` accepts, and gets a 401 naming the
protected-resource metadata otherwise (§3.2.1). That metadata is served before the mount, by
:func:`syncshelf.auth.oauth.protected_resource_route`, because the SDK's own can't advertise
the scopes (§3.2.2). The verifier reads the token store, and tools reach the database, the
clock and the refresh-grace cache through :func:`syncshelf.tools.services`, so the lifespan
binds both to the database once it is open. ``/token`` and the tools share one
:class:`syncshelf.auth.tokens.RefreshGrace`, so a revoked connection's held refresh pairs go
with it.

Importing a tool module registers its tools on :data:`syncshelf.tools.REGISTRY`, so the
production tool modules are imported here, where the registry is served.

The authorization server's routes (:func:`syncshelf.auth.oauth.oauth_routes`) are served only
when ``server.auth_mode`` is ``oauth``: dev mode disables them (§3.2.5), and with the key
absent no token is accepted (#73), so there is nothing to sign in to. ``/authorize`` checks
clients against their documents (:class:`syncshelf.auth.cimd.ClientDocuments`), which the
lifespan also binds to the database, with the HTTP client that fetches them, and checks the
Cloudflare Access login (:class:`syncshelf.auth.access.AccessVerifier`), whose key fetches
get an HTTP client from the lifespan too.
"""

import logging
import sqlite3
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import syncshelf.tools.connections  # noqa: F401  # registers its tools on REGISTRY
import syncshelf.tools.library  # noqa: F401  # registers its tools on REGISTRY
import syncshelf.tools.sync  # noqa: F401  # registers its tools on REGISTRY
import syncshelf.tools.writes  # noqa: F401  # registers its tools on REGISTRY
from syncshelf import __version__
from syncshelf.attention import attention
from syncshelf.auth.access import AccessVerifier
from syncshelf.auth.cimd import ClientDocuments
from syncshelf.auth.oauth import oauth_routes, protected_resource_route
from syncshelf.auth.tokens import RefreshGrace, TokenVerifier, purge_expired
from syncshelf.clients.hardcover import HardcoverClient
from syncshelf.config import Config, ProfileConfig, ServerConfig
from syncshelf.config import data_dir as default_data_dir
from syncshelf.db import Database
from syncshelf.profiles import ProfileSync, sync_profiles
from syncshelf.sync.apply import Applier
from syncshelf.sync.goodreads import GoodreadsSync
from syncshelf.sync.identity import release_on_new_version
from syncshelf.sync.limiter import Clock, HardcoverLimiter, SystemClock
from syncshelf.sync.mirror import Mirror
from syncshelf.sync.runs import abort_running
from syncshelf.sync.scheduler import Scheduler, every, purge_runs
from syncshelf.tools import REGISTRY, Services, SyncShelfServer, ToolRegistry
from syncshelf.tools.confirm import ConfirmTokens

__all__ = [
    "DB_FILENAME",
    "PURGE_INTERVAL",
    "PURGE_TASK",
    "StartupError",
    "allowed_hosts",
    "create_app",
]

DB_FILENAME = "syncshelf.db"
"""The database file inside the data directory."""

GOODREADS_DIR = "goodreads"
"""The data subdirectory for the Goodreads cookie jars (phase 4b), checked from phase 1."""

PURGE_INTERVAL = 24 * 3600
"""Seconds between purges of expired OAuth rows after the one at startup (#77)."""

LOOPBACK_HOSTS = ("localhost:*", "127.0.0.1:*")
"""Always allowed on the MCP mount, for local clients and the container health check."""

log = logging.getLogger(__name__)


class StartupError(Exception):
    """The process can't start. The message names what is wrong, and never a secret."""


def allowed_hosts(server: ServerConfig) -> list[str]:
    """The MCP mount's Host allowlist: the public host, then loopback on any port (§2.2).

    The public host is ``public_url``'s host and port exactly as a Host header carries it.
    Config only accepts the canonical form (#98): a lowercase host and no default port, which
    clients leave out of the header. Without the right entry, every request through the public
    name gets 421.
    """
    return [urlsplit(server.public_url).netloc, *LOOPBACK_HOSTS]


def create_app(
    config: Config,
    *,
    data_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
    registry: ToolRegistry = REGISTRY,
    http_transport: httpx2.AsyncBaseTransport | None = None,
    purge_interval: float = PURGE_INTERVAL,
    limiter_clock: Clock | None = None,
) -> Starlette:
    """Build the app for a loaded config. Nothing touches the disk until its lifespan runs.

    ``data_dir`` holds the database, its snapshots and the Goodreads cookie jars (§2.3). It
    defaults to :func:`syncshelf.config.data_dir`: ``SYNCSHELF_DATA_DIR``, or ``/data``.

    ``clock`` gives wall-clock epoch seconds for every timestamp the app writes; tests pass the
    one injectable clock (§2.9).

    ``registry`` holds the tools served, :data:`syncshelf.tools.REGISTRY` by default.

    ``http_transport`` carries the app's outbound HTTP calls (the client-document fetch, the
    Access key fetch and Hardcover) in place of the network; tests pass a ``MockTransport``
    (§2.9).

    ``purge_interval`` is the seconds between purges of expired OAuth rows,
    :data:`PURGE_INTERVAL` by default; tests shorten it rather than wait a day.

    ``limiter_clock`` paces the Hardcover limiters (it sleeps as well as tells the time); the
    real clock by default.

    Each call builds a fresh MCP server, because the SDK's session manager can be run only
    once per instance.
    """
    if data_dir is None:
        data_dir = default_data_dir()
    verifier = TokenVerifier(config, clock=clock)
    # In memory only, so a new app (a restart) starts with none held (§3.2.4).
    grace = RefreshGrace()
    oauth = config.server.oauth
    clients = ClientDocuments(clock=clock, transport=http_transport)
    access = (
        AccessVerifier(
            oauth.access_team_domain, oauth.access_aud, clock=clock, transport=http_transport
        )
        if oauth is not None
        else None
    )
    mcp = SyncShelfServer(
        registry,
        config.profiles,
        "syncshelf",
        version=__version__,
        token_verifier=verifier,
        # Validated from strings so the SDK keeps the issuer's exact form: AnyHttpUrl() alone
        # would add a trailing slash, and issuer comparison is exact (RFC 8414, RFC 9207).
        auth=AuthSettings.model_validate(
            {
                "issuer_url": config.server.public_url,
                "resource_server_url": config.server.mcp_url,
                "validate_token_resource": True,
            }
        ),
    )
    # streamable_http_app() creates the session manager, so it must run before the lifespan.
    mcp_app = mcp.streamable_http_app(
        stateless_http=True,
        transport_security=TransportSecuritySettings(allowed_hosts=allowed_hosts(config.server)),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        _check_writable(data_dir)
        _check_writable(data_dir / GOODREADS_DIR, create=True)
        db = await Database.open(data_dir / DB_FILENAME, snapshot_dir=data_dir)
        hardcover: dict[str, HardcoverClient] = {}
        goodreads: GoodreadsSync | None = None
        scheduler: Scheduler | None = None
        try:
            hardcover = hardcover_clients(config, transport=http_transport, clock=limiter_clock)
            synced = await db.write(sync_profiles, list(config.profiles), int(clock()))
            _log_profile_sync(synced)
            # A run still `running` belonged to a process that died (§3.4a.7).
            if aborted := await db.write(abort_running, int(clock())):
                log.info(
                    "marked runs left running by the last process as aborted",
                    extra={"event": "runs_aborted", "count": aborted},
                )
            # A new version releases every quarantined change once (§2.2 step 4, #50).
            if released := await db.write(release_on_new_version, __version__):
                log.info(
                    "a new SyncShelf version released quarantined changes",
                    extra={"event": "quarantine_released", "count": released},
                )
            _log_startup(config, db)
            await _housekeeping(db, clock)
            if (dev := config.server.dev_mode) is not None:
                _warn_dev_mode(dev.profile)
            app.state.db = db
            app.state.grace = grace
            app.state.profiles = config.profiles
            app.state.clock = clock
            verifier.bind(db)
            mirror = Mirror(db, hardcover, ttl=config.cadence.mirror_ttl, clock=clock)
            applier = Applier(hardcover, mirror, config.profiles)
            sync_clock = limiter_clock or SystemClock()
            goodreads = GoodreadsSync(
                db=db,
                profiles=config.profiles,
                hardcover=hardcover,
                mirror=mirror,
                applier=applier,
                clock=sync_clock,
                transport=http_transport,
            )
            scheduler = Scheduler(
                db=db,
                sync=goodreads,
                profiles=config.profiles,
                interval=config.cadence.rss_interval,
                clock=sync_clock,
            )
            mcp.bind(
                Services(
                    db=db,
                    clock=clock,
                    grace=grace,
                    mirror=mirror,
                    profiles=config.profiles,
                    hardcover=hardcover,
                    applier=applier,
                    confirm=ConfirmTokens(clock),
                    scheduler=scheduler,
                )
            )
            async with AsyncExitStack() as stack:
                tasks = await stack.enter_async_context(anyio.create_task_group())
                # The scheduler's housekeeping: expired OAuth rows and old runs (§2.4).
                tasks.start_soon(
                    every, purge_interval, lambda: _housekeeping(db, clock), name=PURGE_TASK
                )
                # Unwound first, so the tasks are cancelled before the group waits for them.
                stack.callback(tasks.cancel_scope.cancel)
                if access is not None:  # only /authorize fetches documents and keys
                    await stack.enter_async_context(clients.running(db))
                    await stack.enter_async_context(access.running())
                await stack.enter_async_context(mcp.session_manager.run())
                # Step 6: the sync loops, resumed from their schedules (§3.4a.9).
                await scheduler.resume()
                tasks.start_soon(scheduler.run_forever, name=SYNC_TASK)
                yield
        finally:
            mcp.bind(None)
            verifier.bind(None)
            if scheduler is not None:
                await scheduler.close()
            try:
                if goodreads is not None:
                    await goodreads.aclose()
                for client in hardcover.values():
                    await client.aclose()
            finally:
                await db.close()

    authorization_server = (
        oauth_routes(config, clients, access, grace, clock=clock) if access is not None else []
    )
    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            *authorization_server,
            protected_resource_route(config.server),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def hardcover_clients(
    config: Config,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Clock | None = None,
) -> dict[str, HardcoverClient]:
    """One Hardcover client per configured profile, each with that profile's token and gated
    by its own limiter (§3.3.3). ``transport`` and ``clock`` are for tests."""
    # Config validation requires the URL from phase 3; the fallback only satisfies the type.
    url = config.services.hardcover_url or ""
    reserve = config.cadence.hardcover_daily_reserve
    return {
        name: HardcoverClient(
            url,
            profile.hardcover_token,
            gate=HardcoverLimiter(name, reserve=reserve, clock=clock),
            transport=transport,
        )
        for name, profile in config.profiles.items()
    }


async def healthz(request: Request) -> JSONResponse:
    """Liveness and health (§3.4a.10): 200 whenever the database answers, with each
    profile's attention codes and ``status`` ``attention`` when any profile has one, so
    Uptime Kuma or Home Assistant can alert on it; 503 only when the database doesn't. No
    titles or values, and a CAPTCHA never fails liveness."""
    try:
        db: Database = request.app.state.db
        await db.read(_select_one)
        codes = await db.read(_codes, request.app.state.profiles, int(request.app.state.clock()))
    except Exception:
        log.warning("health check failed", exc_info=True, extra={"event": "healthz_failed"})
        return JSONResponse({"status": "unavailable", "version": __version__}, status_code=503)
    state = "attention" if any(codes.values()) else "ok"
    return JSONResponse({"status": state, "version": __version__, "profiles": codes})


def _codes(
    conn: sqlite3.Connection, profiles: Mapping[str, ProfileConfig], now: int
) -> dict[str, list[str]]:
    return {
        name: [i["code"] for i in attention(conn, name, now, profile.goodreads is not None)]
        for name, profile in sorted(profiles.items())
    }


def _select_one(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT 1").fetchone()


def _check_writable(path: Path, *, create: bool = False) -> None:
    """Raise :class:`StartupError` naming ``path`` unless a file can be created in it."""
    try:
        if create:
            path.mkdir(exist_ok=True)
        # Actually writing is the only check that holds for ACLs, read-only mounts and uids.
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as exc:
        message = f"the data directory {path} is not writable ({type(exc).__name__})"
        log.error(message, extra={"event": "startup_failed"})
        raise StartupError(message) from None


PURGE_TASK = "oauth_purge"
"""The name of the scheduler's housekeeping task, so a test can find it."""
SYNC_TASK = "sync_loops"
"""The name of the task running the sync loops."""


async def _purge(db: Database, clock: Callable[[], float]) -> None:
    """Purge expired OAuth rows once, logging what went. A failure is logged and swallowed:
    the rows only cost space, so a failed purge must never stop the service."""
    try:
        purged = await db.write(purge_expired, int(clock()))
    except Exception:
        log.exception("purging expired OAuth rows failed", extra={"event": "oauth_purge_failed"})
        return
    if purged:
        log.info(
            "purged expired OAuth rows: %d codes, %d access tokens, %d refresh tokens",
            purged.codes,
            purged.access_tokens,
            purged.refresh_tokens,
            extra={
                "event": "oauth_purged",
                "codes": purged.codes,
                "access_tokens": purged.access_tokens,
                "refresh_tokens": purged.refresh_tokens,
            },
        )


async def _housekeeping(db: Database, clock: Callable[[], float]) -> None:
    """The daily purges: expired OAuth rows, and runs older than 90 days (§2.4). A failure is
    logged and swallowed, like the OAuth purge's."""
    await _purge(db, clock)
    try:
        if purged := await db.write(purge_runs, int(clock())):
            log.info("purged old runs", extra={"event": "runs_purged", "count": purged})
    except Exception:
        log.exception("purging old runs failed", extra={"event": "runs_purge_failed"})


def _log_profile_sync(synced: ProfileSync) -> None:
    if not (synced.added or synced.removed or synced.restored or synced.revoked):
        return
    log.info(
        "profiles synced with the config: %d added, %d removed, %d restored, %d grants revoked",
        len(synced.added),
        len(synced.removed),
        len(synced.restored),
        synced.revoked,
        extra={
            "event": "profiles_synced",
            "added": synced.added,
            "removed": synced.removed,
            "restored": synced.restored,
            "revoked_grants": synced.revoked,
        },
    )


def _warn_dev_mode(profile: str) -> None:
    log.warning(
        "DEV MODE: /mcp accepts the static dev token as profile %s. "
        "Never expose this instance beyond this machine.",
        profile,
        extra={"event": "dev_mode", "profile": profile},
    )


def _log_startup(config: Config, db: Database) -> None:
    migration = db.migration
    if migration.applied:
        log.info(
            "migrated the database from version %d to %d",
            migration.from_version,
            migration.to_version,
            extra={
                "event": "migrated",
                "from_version": migration.from_version,
                "to_version": migration.to_version,
                "snapshot": str(migration.snapshot) if migration.snapshot else None,
            },
        )
    log.info(
        "syncshelf %s starting on SQLite %s",
        __version__,
        sqlite3.sqlite_version,
        extra={
            "event": "startup",
            "version": __version__,
            "sqlite_version": sqlite3.sqlite_version,
            "auth_mode": config.server.auth_mode,
            "schema_version": migration.to_version,
        },
    )
