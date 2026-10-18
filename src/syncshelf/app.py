"""The outer Starlette app: route order, the lifespan, and ``/healthz`` (SPEC §2.2, §3.1).

Routes are in §2.2 order with ``Mount("/")`` last, because nothing after a root mount is
reachable. A mounted app's own lifespan never runs, so everything the process needs at
startup happens in :func:`create_app`'s lifespan, in §2.2's order:

1. config is already loaded and validated, because uvicorn needs it before the app starts
   (``__main__`` loads it and fails fast; #51 asks the spec to confirm this order);
2. the data directory and its ``goodreads/`` directory must be writable;
3. the database is opened and migrated, and the ``profiles`` table is synced with the
   config (:mod:`syncshelf.profiles`);
4. the version, ``sqlite3.sqlite_version`` and the auth mode are logged, never a secret,
   and dev mode logs a warning at every start (§3.2.5);
5. the MCP session manager runs for as long as the app does.

On shutdown the database is closed.

The MCP mount is the SDK's resource server: every ``/mcp`` request needs a bearer token that
:class:`syncshelf.auth.tokens.TokenVerifier` accepts, and gets a 401 naming the
protected-resource metadata otherwise (§3.2.1). The SDK serves that metadata on the mount. The
verifier reads the token store, so the lifespan binds it to the database once it is open.
"""

import logging
import sqlite3
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from syncshelf import __version__
from syncshelf.auth.tokens import TokenVerifier
from syncshelf.config import Config, ServerConfig
from syncshelf.config import data_dir as default_data_dir
from syncshelf.db import Database
from syncshelf.profiles import ProfileSync, sync_profiles
from syncshelf.tools import REGISTRY, SyncShelfServer, ToolRegistry

__all__ = ["DB_FILENAME", "StartupError", "allowed_hosts", "create_app"]

DB_FILENAME = "syncshelf.db"
"""The database file inside the data directory."""

GOODREADS_DIR = "goodreads"
"""The data subdirectory for the Goodreads cookie jars (phase 4b), checked from phase 1."""

LOOPBACK_HOSTS = ("localhost:*", "127.0.0.1:*")
"""Always allowed on the MCP mount, for local clients and the container health check."""

_DEFAULT_PORTS = {"http": 80, "https": 443}

log = logging.getLogger(__name__)


class StartupError(Exception):
    """The process can't start. The message names what is wrong, and never a secret."""


def allowed_hosts(server: ServerConfig) -> list[str]:
    """The MCP mount's Host allowlist: the public host, then loopback on any port (§2.2).

    The public host is written as a Host header carries it: with its port only when
    ``public_url`` gives one other than the scheme's default. Clients leave a default port
    out of the header, so ``http://localhost:80`` is listed as ``localhost``. Without the right
    entry, every request through the public name gets 421.
    """
    parts = urlsplit(server.public_url)
    port = parts.port
    if port is None or port == _DEFAULT_PORTS.get(parts.scheme):
        public = server.public_host
    else:
        public = f"{server.public_host}:{port}"
    return [public, *LOOPBACK_HOSTS]


def create_app(
    config: Config,
    *,
    data_dir: Path | None = None,
    clock: Callable[[], float] = time.time,
    registry: ToolRegistry = REGISTRY,
) -> Starlette:
    """Build the app for a loaded config. Nothing touches the disk until its lifespan runs.

    ``data_dir`` holds the database, its snapshots and the Goodreads cookie jars (§2.3). It
    defaults to :func:`syncshelf.config.data_dir`: ``SYNCSHELF_DATA_DIR``, or ``/data``.

    ``clock`` gives wall-clock epoch seconds for every timestamp the app writes; tests pass the
    one injectable clock (§2.9).

    ``registry`` holds the tools served, :data:`syncshelf.tools.REGISTRY` by default.

    Each call builds a fresh MCP server, because the SDK's session manager can be run only
    once per instance.
    """
    if data_dir is None:
        data_dir = default_data_dir()
    verifier = TokenVerifier(config, clock=clock)
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
        try:
            synced = await db.write(sync_profiles, list(config.profiles), int(clock()))
            _log_profile_sync(synced)
            # Step 4's meta.last_started_version compare is left unbuilt until #50 decides it.
            _log_startup(config, db)
            if (dev := config.server.dev_mode) is not None:
                _warn_dev_mode(dev.profile)
            app.state.db = db
            verifier.bind(db)
            async with mcp.session_manager.run():
                yield
        finally:
            verifier.bind(None)
            await db.close()

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            # Phase 2+ routes go here, before the mount.
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


async def healthz(request: Request) -> JSONResponse:
    """Liveness: 200 when ``SELECT 1`` succeeds on a read connection, 503 otherwise.

    The 503 body is provisional until #51 confirms it.
    """
    try:
        db: Database = request.app.state.db
        await db.read(_select_one)
    except Exception:
        log.warning("health check failed", exc_info=True, extra={"event": "healthz_failed"})
        return JSONResponse({"status": "unavailable", "version": __version__}, status_code=503)
    return JSONResponse({"status": "ok", "version": __version__})


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
