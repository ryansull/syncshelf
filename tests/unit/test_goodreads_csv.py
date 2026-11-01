"""The library export parser (SPEC §3.4a.2) and its observations (§3.4a.3): columns by header
name, the recorded cell forms, refusal of anything it can't read in full, and Binding as a
format."""

import logging
from datetime import date

import pytest

from syncshelf.clients.goodreads_csv import BadExport, parse_export
from syncshelf.sync import observations
from syncshelf.sync.observations import binding_format, observe_csv

from fakes.goodreads_csv import HEADER, ExportBook, library_export


def test_the_cells_are_typed() -> None:
    (row,) = parse_export(
        library_export(
            ExportBook(
                10,
                "Dune",
                isbn="044101359X",
                isbn13="9780441013593",
                rating=4,
                date_read="2026-03-02",
                shelf="read",
                shelves=("sci-fi", "favourites"),
                review="Great.<br/>Really.",
                spoiler=True,
            )
        )
    )
    assert row.book_id == "10"
    assert (row.isbn10, row.isbn13) == ("044101359X", "9780441013593")  # X kept, as text
    assert (row.rating, row.date_read, row.exclusive_shelf) == (4, date(2026, 3, 2), "read")
    assert row.bookshelves == ("sci-fi", "favourites")
    assert (row.review, row.spoiler, row.read_count) == ("Great.<br/>Really.", True, 1)


def test_a_review_may_span_lines() -> None:
    (row,) = parse_export(library_export(ExportBook(10, "Dune", review="One,\nTwo")))
    assert row.review == "One,\nTwo"


def test_columns_are_found_by_name() -> None:
    header = ("Inserted Column", *reversed(HEADER))
    (row,) = parse_export(library_export(ExportBook(10, "Dune", rating=3), header=header))
    assert (row.book_id, row.title, row.rating) == ("10", "Dune", 3)


def test_a_missing_column_is_named() -> None:
    header = tuple(c for c in HEADER if c not in ("ISBN13", "Read Count"))
    with pytest.raises(BadExport) as raised:
        parse_export(library_export(ExportBook(10, "Dune"), header=header))
    assert raised.value.missing == ("ISBN13", "Read Count")
    assert "ISBN13, Read Count" in str(raised.value)


@pytest.mark.parametrize(
    ("data", "says"),
    [
        ("Dune — ébauche".encode("cp1252"), "isn't UTF-8"),
        (b"", "missing Book Id"),
        (library_export(ExportBook(10, "Dune", extra={"Book Id": "abc"})), "Book Id"),
        (library_export(ExportBook(10, "Dune", extra={"Date Read": "02/03/2026"})), "YYYY/MM/DD"),
        (library_export(ExportBook(10, "Dune", extra={"My Rating": "four"})), "isn't a number"),
        (library_export(ExportBook(10, "Dune", extra={"My Rating": "7.0"})), "0 to 5"),
        (library_export(ExportBook(10, "Dune", extra={"My Rating": "4.5"})), "0 to 5"),
        (library_export(ExportBook(10, "Dune", extra={"My Rating": "inf"})), "0 to 5"),
        (library_export(ExportBook(10, "Dune", extra={"Read Count": "-1"})), "negative"),
        (library_export(ExportBook(10, "Dune", shelf="")), "Exclusive Shelf is empty"),
        (library_export(ExportBook(10, "Dune")) + b"11,Cut Off Ti\n", "one cell per column"),
        (library_export(ExportBook(10, "Dune"))[:-1] + b",extra\n", "one cell per column"),
        (library_export(ExportBook(10, "Dune")) + b'11,"Unclosed\n', "not valid CSV"),
    ],
)
def test_an_export_it_cant_read_in_full_is_refused(data: bytes, says: str) -> None:
    with pytest.raises(BadExport, match=says):
        parse_export(data)


def test_a_byte_order_mark_is_tolerated() -> None:
    (row,) = parse_export(b"\xef\xbb\xbf" + library_export(ExportBook(10, "Dune")))
    assert row.book_id == "10"


def test_an_export_becomes_a_complete_batch() -> None:
    rows = parse_export(
        library_export(
            ExportBook(10, "Dune", rating=4, date_read="2026-03-02", binding="Kindle Edition"),
            ExportBook(11, "Emma", shelf="to-read", shelves=("classics",), binding=""),
        )
    )
    batch = observe_csv(rows, "gr_csv_ingest", 1_000)
    assert (batch.source, batch.observed_at, batch.complete, batch.stale) == (
        "gr_csv_ingest",
        1_000,
        True,
        False,
    )
    dune, emma = batch.rows
    assert dict(dune.fields) == {
        "status": "read",
        "rating": "4",
        "date_read": "2026-03-02",
        "review": None,
        "spoiler": "0",
        "read_count": "1",
    }
    assert (dune.format, dune.date_added) == ("ebook", date(2026, 10, 15))
    assert (emma.fields["rating"], emma.fields["read_count"]) == (None, "0")
    assert (emma.format, emma.shelves) == (None, ("classics",))  # its status isn't a tag


def test_a_stale_upload_has_no_time() -> None:
    batch = observe_csv([], "gr_csv_ingest", None, stale=True)
    assert (batch.observed_at, batch.stale) == (None, True)


@pytest.mark.parametrize(
    ("binding", "format"),
    [
        ("Kindle Edition", "ebook"),
        ("Nook", "ebook"),
        ("ebook", "ebook"),
        ("Audible Audio", "audio"),
        ("Audio CD", "audio"),
        ("MP3 CD", "audio"),
        ("Hardcover", "print"),
        ("Mass Market Paperback", "print"),
        ("", None),
        (None, None),
    ],
)
def test_binding_becomes_a_format(binding: str | None, format: str | None) -> None:
    assert binding_format(binding) == format


def test_an_unrecognised_binding_is_logged_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observations, "_unrecognised", set())
    with caplog.at_level(logging.INFO, logger="syncshelf.sync.observations"):
        assert binding_format("Unknown Binding") == "print"
        assert binding_format("unknown binding") == "print"
        assert binding_format("Paperback") == "print"
    assert [r.__dict__["event"] for r in caplog.records] == ["binding_unrecognised"]


def test_an_error_names_the_line_a_review_ends_on() -> None:
    data = library_export(
        ExportBook(10, "Dune", review="One\nTwo\nThree"),
        ExportBook(11, "Emma", extra={"Date Read": "bad"}),
    )
    with pytest.raises(BadExport, match="^line 5: "):
        parse_export(data)


def test_a_long_review_is_read() -> None:
    (row,) = parse_export(library_export(ExportBook(10, "Dune", review="x" * 200_000)))
    assert len(row.review) == 200_000


def test_a_batch_carries_only_csv_fields() -> None:
    rows = parse_export(library_export(ExportBook(10, "Dune")))
    (row,) = observe_csv(rows, "gr_csv_ingest", 1).rows
    assert set(row.fields) <= set(observations.CSV_FIELDS)
