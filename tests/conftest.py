"""Shared test wiring: the anyio backend, the injectable clock and live-test gating.

Later phases add the temp SQLite database, the fakes and ``authed_client`` here (SPEC §2.5).
"""

import os

import pytest

from clock import FakeClock

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
def clock() -> FakeClock:
    """A fresh clock at ``FakeClock.START`` that moves only when the test advances it."""
    return FakeClock()
