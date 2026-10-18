"""The package installs and reports its version."""

from importlib.metadata import version

import syncshelf


def test_version_matches_installed_metadata() -> None:
    assert syncshelf.__version__ == version("syncshelf")
