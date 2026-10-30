"""A Goodreads library export in the recorded form (``fixtures/goodreads/library_export.csv``):
the same header, ``="…"``-wrapped ISBNs, ``N.0`` ratings, ``YYYY/MM/DD`` dates, minimal
quoting and LF endings. The contract test holds it to the recording."""

import csv
import io
from dataclasses import dataclass, field

from syncshelf.clients.goodreads_csv import COLUMNS

__all__ = ["HEADER", "ExportBook", "library_export"]

HEADER = COLUMNS


@dataclass
class ExportBook:
    """One row; dates are ``YYYY-MM-DD`` (written as Goodreads writes them)."""

    book_id: int
    title: str
    author: str = "Ann Author"
    isbn: str = ""
    isbn13: str = ""
    rating: int = 0
    binding: str = "Paperback"
    date_read: str = ""
    date_added: str = "2026-10-15"
    shelf: str = "read"
    shelves: tuple[str, ...] = ()
    review: str = ""
    spoiler: bool = False
    read_count: int | None = None
    """Goodreads' own default: 1 on read and currently-reading, 0 on to-read."""
    extra: dict[str, str] = field(default_factory=dict)
    """Cells to override by column name."""


def library_export(*books: ExportBook, header: tuple[str, ...] = HEADER) -> bytes:
    """The export of these books, newest first as Goodreads lists them."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for book in books:
        writer.writerow(_cells(book).get(name, "") for name in header)
    return out.getvalue().encode("utf-8")


def _cells(book: ExportBook) -> dict[str, str]:
    last, _, first = book.author.rpartition(" ")
    # Goodreads leaves `read` out of Bookshelves; the other exclusive shelves are listed.
    listed = [*([] if book.shelf == "read" else [book.shelf]), *book.shelves]
    count = book.read_count if book.read_count is not None else int(book.shelf != "to-read")
    return {
        "Book Id": str(book.book_id),
        "Title": book.title,
        "Author": book.author,
        "Author l-f": f"{first}, {last}" if last else book.author,
        "ISBN": f'="{book.isbn}"',
        "ISBN13": f'="{book.isbn13}"',
        "My Rating": f"{book.rating}.0" if book.rating else "0",
        "Publisher": "Some Publisher",
        "Binding": book.binding,
        "Number of Pages": "300",
        "Year Published": "2020",
        "Original Publication Year": "2020",
        "Date Read": book.date_read.replace("-", "/"),
        "Date Added": book.date_added.replace("-", "/"),
        "Bookshelves": ", ".join(listed),
        "Bookshelves with positions": ", ".join(f"{s} (#1)" for s in listed),
        "Exclusive Shelf": book.shelf,
        "My Review": book.review,
        "Spoiler": "true" if book.spoiler else "",
        "Read Count": str(count),
        "Owned Copies": "0",
        **book.extra,
    }
