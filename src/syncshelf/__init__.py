"""SyncShelf: a remote MCP server and one-way syncs that keep Hardcover as the reading log."""

from importlib.metadata import version

__version__: str = version("syncshelf")
"""The installed package version, as reported by ``/healthz`` and the startup log."""
