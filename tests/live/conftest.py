"""Credentials for live tests (SPEC §2.9): the repo-root ``.env``, or the file named by
``SYNCSHELF_LIVE_ENV`` (a worktree has no ``.env`` of its own)."""

import os
from pathlib import Path

import pytest
from record import ROOT, read_env


@pytest.fixture(scope="session")
def live_env() -> dict[str, str]:
    path = Path(os.environ.get("SYNCSHELF_LIVE_ENV") or ROOT / ".env")
    if not path.is_file():
        pytest.fail(f"live tests read credentials from {path}, which doesn't exist")
    return read_env(path)
