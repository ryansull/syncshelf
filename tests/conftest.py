"""Shared test wiring: the anyio backend, the injectable clock, temp SQLite, live gating and
``authed_client``.

Later phases add the fakes here (SPEC §2.5).
"""

import logging
import os
import sqlite3
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import AbstractAsyncContextManager, closing
from pathlib import Path

import pytest

from syncshelf import logging as slog
from syncshelf.app import create_app
from syncshelf.db import Database, connect
from syncshelf.db.migrate import migrate
from syncshelf.tools import REGISTRY, ToolRegistry

from clock import FakeClock
from serving import Authed, AuthedClient, authed, make_config

LIVE_ENV = "SYNCSHELF_LIVE"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help=f"also collect tests marked live; needs {LIVE_ENV}=1 as well",
    )


def live_enabled(config: pytest.Config) -> bool:
    """Live tests need both the ``--live`` flag and ``SYNCSHELF_LIVE=1``, so neither alone
    (a stray env var in a shell, a copied command line) can reach a real service."""
    return bool(config.getoption("--live")) and os.environ.get(LIVE_ENV) == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Deselect rather than skip: an ungated live test should not even appear as collected.
    if live_enabled(config):
        return
    live = [item for item in items if item.get_closest_marker("live")]
    if live:
        config.hook.pytest_deselected(items=live)
        items[:] = [item for item in items if not item.get_closest_marker("live")]


@pytest.fixture
def anyio_backend() -> str:
    # The service runs on asyncio under uvicorn; trio is not a target.
    return "asyncio"


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """``syncshelf.logging.configure`` rewires global logging state; put it back afterwards so
    tests stay independent."""
    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    names = [*slog.PROPAGATING_LOGGERS, *slog.QUIET_LOGGERS]
    saved = {
        n: (lg.handlers[:], lg.filters[:], lg.level, lg.propagate)
        for n in names
        for lg in [logging.getLogger(n)]
    }
    yield
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])
    for n, (handlers, filters, level, propagate) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.filters[:] = filters
        lg.setLevel(level)
        lg.propagate = propagate
    slog.configure_redaction()


@pytest.fixture
def clock() -> FakeClock:
    """A fresh clock at ``FakeClock.START`` that moves only when the test advances it."""
    return FakeClock()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """An existing, empty data directory for ``create_app``."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    """A migrated database in a temp directory, opened as the service opens it.

    Closed afterwards, so Windows can delete the temp files. Needs an ``anyio`` test.
    """
    database = await Database.open(tmp_path / "syncshelf.db", snapshot_dir=tmp_path)
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def schema(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A plain connection to a temp database migrated by the shipped migrations.

    For SQL-invariant tests (``invariants.py``), which talk to SQLite directly so that what
    they prove is the database's own refusal.
    """
    with closing(connect(tmp_path / "schema.db")) as conn:
        migrate(conn, snapshot_dir=tmp_path)
        yield conn


@pytest.fixture
def authed_client(tmp_path: Path, data_dir: Path, clock: FakeClock) -> AuthedClient:
    """Sign in to the real app's ``/mcp`` as a profile, through the production verifier
    (SPEC §2.9)::

        async with authed_client("alice") as session:
            result = await session.mcp.call_tool("my_connections", {})

    It runs an ``oauth``-mode app on ``data_dir`` and the ``clock`` fixture, puts a grant and
    an access token for the profile into the store, and yields an
    :class:`serving.Authed`: the MCP client, the raw HTTP client, the database, the grant id
    and the raw token. Keyword arguments: ``profiles`` (the configured names, default just
    the one signing in), ``profile_yaml`` (as for ``make_config``) and ``registry`` (the
    tools served, the production registry by default).
    """

    def connect_as(
        profile: str = "alice",
        *,
        profiles: Iterable[str] | None = None,
        profile_yaml: dict[str, str] | None = None,
        registry: ToolRegistry = REGISTRY,
    ) -> AbstractAsyncContextManager[Authed]:
        config = make_config(
            tmp_path,
            profiles=profiles if profiles is not None else (profile,),
            profile_yaml=profile_yaml,
            auth_mode="oauth",
        )
        app = create_app(config, data_dir=data_dir, clock=clock.time, registry=registry)
        return authed(app, profile, int(clock.time()))

    return connect_as
