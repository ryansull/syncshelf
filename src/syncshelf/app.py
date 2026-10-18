"""The outer Starlette app: route order, the lifespan, and ``/healthz`` (SPEC §2.2, §3.1).

Routes are in §2.2 order with ``Mount("/")`` last, because nothing after a root mount is
reachable. A mounted app's own lifespan never runs, so everything the process needs at
startup happens in :func:`create_app`'s lifespan, in §2.2's order:

1. config is already loaded and validated, because uvicorn needs it before the app starts
   (``__main__`` loads it and fails fast; #51 asks the spec to confirm this order);
2. the data directory and its ``goodreads/`` directory must be writable;
3. the database is opened and migrated;
4. the version, ``sqlite3.sqlite_version`` and the auth mode are logged, never a secret;
5. the MCP session manager runs for as long as the app does.

On shutdown the database is closed. Phase 1 serves MCP with no tools and no auth, which is
safe only because config refuses a ``server.public_url`` that isn't on loopback.
"""

import logging
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from syncshelf import __version__
from syncshelf.config import Config, ServerConfig
from syncshelf.db import Database

__all__ = ["DATA_DIR", "DB_FILENAME", "StartupError", "allowed_hosts", "create_app"]

DATA_DIR = Path("/data")
"""Where the database, its snapshots and the Goodreads cookie jars live (§2.3)."""

DB_FILENAME = "syncshelf.db"
"""The database file inside the data directory."""

GOODREADS_DIR = "goodreads"
"""The data subdirectory for the Goodreads cookie jars (phase 4b), checked from phase 1."""

LOOPBACK_HOSTS = ("localhost:*", "127.0.0.1:*")
"""Always allowed on the MCP mount, for local clients and the container health check."""

log = logging.getLogger(__name__)


class StartupError(Exception):
    """The process can't start. The message names what is wrong, and never a secret."""


def allowed_hosts(server: ServerConfig) -> list[str]:
    """The MCP mount's Host allowlist: the public host, then loopback on any port (§2.2).

    The public host is written as a Host header carries it: with its port only when
    ``public_url`` gives one. Without it in the list, every request through the public name
    gets 421.
    """
    parts = urlsplit(server.public_url)
    public = server.public_host if parts.port is None else f"{server.public_host}:{parts.port}"
    return [public, *LOOPBACK_HOSTS]


def create_app(config: Config, *, data_dir: Path = DATA_DIR) -> Starlette:
    """Build the app for a loaded config. Nothing touches the disk until its lifespan runs.

    Each call builds a fresh MCP server, because the SDK's session manager can be run only
    once per instance.
    """
    mcp = MCPServer("syncshelf", version=__version__)
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
            # Step 4's meta.last_started_version compare is left unbuilt until #50 decides it.
            _log_startup(config, db)
            app.state.db = db
            async with mcp.session_manager.run():
                yield
        finally:
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
                "snapshot": str(migration.snapshot),
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
