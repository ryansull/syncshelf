"""The observation contract (SPEC §3.4a.3): every Goodreads feed becomes one shape.

Each run turns what it fetched into one :class:`Batch` per profile: its ``source``, when it
was observed, whether it is a **complete enumeration**, and one :class:`Observation` per source
row, keyed by the Goodreads **edition** ``book_id``. The diff (§3.4a.6) reads only batches, so
RSS, the session export (4b) and ``/ingest`` all reach Hardcover the same way.

**A field a feed doesn't carry is not observed, never cleared.** ``Observation.fields`` holds
only the baseline fields its feed has, so RSS rows have no ``spoiler`` or ``read_count`` key at
all, and the diff can never read their absence as a change.
"""

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from email.utils import parsedate_to_datetime
from typing import Literal

from syncshelf.clients.goodreads_rss import RssItem, RssPass

__all__ = [
    "BUILT_IN_SHELVES",
    "Batch",
    "Format",
    "Observation",
    "Source",
    "observe_rss",
    "review_text",
]

Source = Literal["gr_rss", "gr_csv_auto", "gr_csv_ingest"]
Format = Literal["audio", "ebook", "print"]

BUILT_IN_SHELVES = ("read", "currently-reading", "to-read")
"""Goodreads' own exclusive shelves. With the profile's ``shelf_map`` keys, they tell RSS's
exclusive shelf from its other shelves, since ``user_shelves`` lists both together."""

RSS_FIELDS = ("status", "rating", "date_read", "review")
"""The baseline fields RSS carries (§3.4a.3)."""


@dataclass(frozen=True)
class Observation:
    """One source row as one feed saw it, normalised (§3.4a.3).

    ``fields`` maps each baseline field the feed carries to its normalised ``TEXT`` value, or
    ``None`` when observed empty: ``status`` is the exclusive shelf name (``shelf_map`` turns
    it into a Hardcover status when pushing), ``rating`` 1–5 (unrated is ``None``),
    ``date_read`` an ISO date, ``review`` plain text.
    """

    source_id: str
    fields: Mapping[str, str | None]
    title: str
    author: str | None
    isbn10: str | None
    isbn13: str | None = None
    format: Format | None = None
    shelves: tuple[str, ...] = ()
    """The row's non-exclusive shelves. Carried for matching and display, never pushed."""
    date_added: date | None = None
    """When the book was shelved, as a date in the profile's time zone (§2.4)."""
    last_touched: datetime | None = None
    """RSS ``user_date_added``, with Goodreads' offset: the date source for an undated
    finish (§3.4a.6, rule 6)."""


@dataclass(frozen=True)
class Batch:
    """One run's observations for one profile."""

    source: Source
    observed_at: int | None
    """Epoch seconds: the RSS fetch start, or the CSV's generation time. ``None`` only for a
    stale ``/ingest`` upload, which only fills (§3.4a.8)."""
    complete: bool
    """A complete enumeration: only such a batch may conclude a row is gone (§3.4a.6)."""
    rows: tuple[Observation, ...]
    stale: bool = False


def observe_rss(
    rss: RssPass, shelf_map: Mapping[str, int], observed_at: int, zone: tzinfo
) -> Batch:
    """The batch for one RSS pass, observed at the fetch's start (``observed_at``). ``zone``
    is the profile's time zone, which calendar dates are given in (§2.4)."""
    exclusive = frozenset(BUILT_IN_SHELVES) | frozenset(shelf_map)
    rows = tuple(_rss_row(item, exclusive, zone) for item in rss.items)
    return Batch("gr_rss", observed_at, rss.complete, rows)


def _rss_row(item: RssItem, exclusive: frozenset[str], zone: tzinfo) -> Observation:
    listed = [s.strip() for s in item.user_shelves.split(",") if s.strip()]
    # RSS leaves out `read`: no exclusive shelf listed means the book is on it (§3.4a.1). The
    # feed doesn't mark which shelf is exclusive, so a custom exclusive shelf missing from
    # shelf_map reads as an ordinary one, and the book as Read (#182).
    status = next((s for s in listed if s in exclusive), "read")
    fields = {
        "status": status,
        "rating": _rating(item.user_rating),
        # Midnight +0000: the date part as written, never shifted to another zone.
        "date_read": d.date().isoformat() if (d := _rfc822(item.user_read_at)) else None,
        "review": review_text(item.user_review),
    }
    added = _rfc822(item.user_date_created)
    return Observation(
        source_id=item.book_id,
        fields=fields,
        title=item.title,
        author=item.author_name or None,
        isbn10=item.isbn or None,
        shelves=tuple(s for s in listed if s != status),
        date_added=added.astimezone(zone).date() if added else None,
        last_touched=_rfc822(item.user_date_added),
    )


def _rating(text: str) -> str | None:
    # 0 is "unrated"; anything that isn't 1–5 is treated the same rather than guessed at.
    return text if text in ("1", "2", "3", "4", "5") else None


def _rfc822(text: str) -> datetime | None:
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except TypeError, ValueError:
        return None
    # "-0000" (and a date with no zone) parses naive; RFC 5322 reads it as UTC.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]*>")


def review_text(markup: str) -> str | None:
    """A Goodreads review's HTML as plain text (§3.4a.6, rule 1): ``<br>`` becomes a newline,
    other tags are dropped, entities decoded, and whitespace collapsed within each line, so a
    review compares equal however Goodreads spaced it. Empty is ``None``."""
    text = html.unescape(_TAG.sub("", _BREAK.sub("\n", markup)))
    lines = (" ".join(line.split()) for line in text.split("\n"))
    return "\n".join(lines).strip("\n") or None
