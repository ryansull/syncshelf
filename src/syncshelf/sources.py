"""Which of a profile's sources are switched on (SPEC §3.4a.10, §3.4b.5).

One place, because three readers have to agree: ``sync_status`` (which suppresses
``next_due_at`` for a source that no longer runs), the household health view and ``/healthz``
(which list only the sources a profile has), and ``attention`` (which raises ``source_stale``
only for an enabled source).

The rule for ``gr_csv`` is **derived, never stored** (#232): it needs the profile's Goodreads
credentials *and* a browser sidecar on the stack, because without a sidecar there is no way to
sign in and so no way to export. A stack with no ``services.browser_ws`` therefore has the CSV
source off rather than permanently stale, and raises no attention for it at all.
"""

from collections.abc import Mapping

from syncshelf.config import ProfileConfig

__all__ = ["SOURCES", "enabled"]

SOURCES: Mapping[str, str] = {"gr_rss": "goodreads", "gr_csv": "goodreads"}
"""The run sources shipped so far, and the identity source each belongs to."""


def enabled(profile: ProfileConfig, *, sidecar: bool) -> dict[str, bool]:
    """Which run sources ``profile`` has switched on, by run source.

    ``sidecar`` is whether the stack has a browser sidecar configured at all
    (``services.browser_ws``), which only ``gr_csv`` depends on.
    """
    goodreads = profile.goodreads
    return {
        "gr_rss": goodreads is not None,
        "gr_csv": goodreads is not None and goodreads.password is not None and sidecar,
    }
