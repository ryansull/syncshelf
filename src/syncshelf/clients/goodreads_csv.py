"""The Goodreads library export (SPEC §3.4a.2): the CSV a person downloads from "Import and
export", parsed for ``/ingest`` (4a) and the session export (4b).

The file is UTF-8 (never cp1252), RFC 4180 quoted, and reviews can make a quoted cell span
lines. **Columns are found by header name**, since Goodreads has inserted columns before, and
the export is refused unless the header has every column in :data:`REQUIRED`. A cell that
doesn't parse refuses the whole export too: a half-read library would look like removals.

Cell forms, as verified on a real export: ``Book Id`` is a plain integer (the edition id);
``ISBN`` and ``ISBN13`` are wrapped as ``="…"`` (``=""`` when empty), and ISBN-10 stays a
string, since ``X`` occurs; ``My Rating`` is ``0`` (unrated) or ``N.0``; dates are
``YYYY/MM/DD``; ``Spoiler`` is empty for false. ``Private Notes`` and ``Owned Copies`` are
never read.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date

__all__ = ["COLUMNS", "REQUIRED", "BadExport", "CsvRow", "parse_export"]

COLUMNS = (
    "Book Id", "Title", "Author", "Author l-f", "Additional Authors", "ISBN", "ISBN13",
    "My Rating", "Publisher", "Binding", "Number of Pages", "Year Published",
    "Original Publication Year", "Date Read", "Date Added", "Bookshelves",
    "Bookshelves with positions", "Exclusive Shelf", "My Review", "Spoiler", "Private Notes",
    "Read Count", "Owned Copies",
)  # fmt: skip
"""The export's header, in Goodreads' order (as recorded)."""
REQUIRED = (
    "Book Id", "Title", "Author", "ISBN", "ISBN13", "My Rating", "Binding", "Date Read",
    "Date Added", "Exclusive Shelf", "My Review", "Spoiler", "Read Count",
)  # fmt: skip
"""The columns an export must have (§3.4a.2)."""

_WRAPPED = re.compile(r'^="(.*)"$')
_DATE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
FIELD_LIMIT = 16 * 1024 * 1024
"""The longest cell read (a review), well past the csv module's 128 KiB default."""


class BadExport(ValueError):
    """Not a Goodreads library export SyncShelf can read: the message says why, and
    ``missing`` names the required columns the header lacks."""

    def __init__(self, message: str, missing: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True)
class CsvRow:
    """One book in the export, as Goodreads wrote it, typed."""

    book_id: str
    title: str
    author: str | None
    isbn10: str | None
    isbn13: str | None
    rating: int
    """0 is unrated."""
    binding: str | None
    date_read: date | None
    date_added: date | None
    exclusive_shelf: str
    """The authoritative status: ``Bookshelves`` leaves ``read`` out."""
    bookshelves: tuple[str, ...]
    review: str
    """As written: HTML, possibly over several lines."""
    spoiler: bool
    read_count: int | None
    """How often the book was *started*: 1 for read and currently-reading alike."""


def parse_export(data: bytes) -> list[CsvRow]:
    """The rows of a library export. Raises :class:`BadExport` when it isn't UTF-8, its
    header lacks a required column, a row is cut short, or a cell doesn't parse."""
    try:
        text = data.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError:
        raise BadExport("the export isn't UTF-8 text") from None
    csv.field_size_limit(max(csv.field_size_limit(), FIELD_LIMIT))
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        header = reader.fieldnames or []
        missing = tuple(c for c in REQUIRED if c not in header)
        if missing:
            raise BadExport(
                f"not a Goodreads library export: missing {', '.join(missing)}", missing
            )
        # A record's line is where it ends: a review can span several.
        return [_row(cells, reader.line_num) for cells in reader]
    except csv.Error as exc:
        raise BadExport(f"not valid CSV: {exc}") from None


def _row(cells: dict[str | None, str | list[str] | None], line: int) -> CsvRow:
    # DictReader pads a short row with None and files a long one's extra cells under None:
    # either is a cut-off or broken export, never a book.
    if None in cells or None in cells.values():
        raise BadExport(f"line {line}: the row doesn't have one cell per column")

    def cell(name: str) -> str:
        value = cells.get(name)
        return value.strip() if isinstance(value, str) else ""

    book_id = cell("Book Id")
    if not book_id.isdigit():
        raise BadExport(f"line {line}: Book Id {book_id!r} isn't a number")
    try:
        rated = float(cell("My Rating") or "0")
        count = cell("Read Count")
        read_count = int(count) if count else None
    except ValueError:
        raise BadExport(f"line {line}: My Rating or Read Count isn't a number") from None
    if rated not in (0, 1, 2, 3, 4, 5):  # also refuses 4.5, inf and nan
        raise BadExport(f"line {line}: My Rating {cell('My Rating')!r} isn't 0 to 5")
    rating = int(rated)
    if read_count is not None and read_count < 0:
        raise BadExport(f"line {line}: Read Count {read_count} is negative")
    if not cell("Exclusive Shelf"):
        raise BadExport(f"line {line}: Exclusive Shelf is empty")
    shelves = tuple(s.strip() for s in cell("Bookshelves").split(",") if s.strip())
    return CsvRow(
        book_id=book_id,
        title=cell("Title"),
        author=cell("Author") or None,
        isbn10=_unwrap(cell("ISBN")),
        isbn13=_unwrap(cell("ISBN13")),
        rating=rating,
        binding=cell("Binding") or None,
        date_read=_date(cell("Date Read"), line),
        date_added=_date(cell("Date Added"), line),
        exclusive_shelf=cell("Exclusive Shelf"),
        bookshelves=shelves,
        review=review if isinstance(review := cells.get("My Review"), str) else "",
        spoiler=bool(cell("Spoiler")),
        read_count=read_count,
    )


def _unwrap(text: str) -> str | None:
    # `="0441013597"` keeps spreadsheets from reading an ISBN as a number.
    match = _WRAPPED.match(text)
    return (match.group(1) if match else text).strip() or None


def _date(text: str, line: int) -> date | None:
    if not text:
        return None
    match = _DATE.match(text)
    try:
        if match is None:
            raise ValueError(text)
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        raise BadExport(f"line {line}: {text!r} isn't a YYYY/MM/DD date") from None
