"""FakeGoodreads answers in the recorded feed's shape (SPEC §2.9 contract tests): the same
channel elements, the same item elements in the same order, and the same empty past-the-end
page. When Goodreads changes, re-record; this fails; fix the fake."""

import csv
import io
import re
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import httpx2
import pytest

from syncshelf.clients.goodreads_csv import COLUMNS, parse_export
from syncshelf.clients.goodreads_rss import GoodreadsRss, Pacer

from clock import FakeClock
from fakes.goodreads import FakeGoodreads
from fakes.goodreads_csv import ExportBook, library_export

FIXTURES = Path(__file__).parents[1] / "fixtures" / "goodreads"


def channel(text: str) -> ET.Element:
    found = ET.fromstring(text).find("channel")
    assert found is not None
    return found


def shape(element: ET.Element) -> list[str]:
    return [child.tag for child in element]


def served(page: int) -> str:
    fake = FakeGoodreads()
    fake.add_user("1")
    fake.add_item("1", 10, "Some Book", user_rating="3")
    request = httpx2.Request(
        "GET", f"https://www.goodreads.com/review/list_rss/1?shelf=%23ALL%23&page={page}"
    )
    return fake.handle(request).text


def test_the_channel_has_the_recorded_elements() -> None:
    recorded = channel((FIXTURES / "rss_page.xml").read_text(encoding="utf-8"))
    fake = channel(served(1))
    assert [t for t in shape(fake) if t != "item"] == [t for t in shape(recorded) if t != "item"]


def test_an_item_has_the_recorded_elements_in_order() -> None:
    recorded = channel((FIXTURES / "rss_page.xml").read_text(encoding="utf-8")).find("item")
    fake = channel(served(1)).find("item")
    assert recorded is not None and fake is not None
    assert shape(fake) == shape(recorded)
    assert shape(fake.find("book")) == shape(recorded.find("book"))  # type: ignore[arg-type]


def test_a_page_past_the_end_matches_the_recorded_empty_page() -> None:
    recorded = channel((FIXTURES / "rss_end.xml").read_text(encoding="utf-8"))
    fake = channel(served(2))
    assert shape(fake) == shape(recorded)
    assert fake.findall("item") == []


@pytest.mark.anyio
async def test_a_body_that_is_not_xml_is_unusable() -> None:
    transport = httpx2.MockTransport(lambda _: httpx2.Response(200, text="<<not xml"))
    result = await GoodreadsRss("1", pacer=Pacer(FakeClock()), transport=transport).fetch_all()
    assert result.problem == "page 1: the feed is not XML"


EXPORT = FIXTURES / "library_export.csv"


def test_the_fake_export_has_the_recorded_header() -> None:
    recorded = EXPORT.read_text(encoding="utf-8").splitlines()[0]
    fake = library_export(ExportBook(10, "Some Book")).decode("utf-8").splitlines()[0]
    assert fake == recorded
    assert tuple(recorded.split(",")) == COLUMNS


def test_the_fake_export_writes_cells_in_the_recorded_forms() -> None:
    recorded = list(csv.DictReader(io.StringIO(EXPORT.read_text(encoding="utf-8"), newline="")))
    fake = next(
        csv.DictReader(
            io.StringIO(
                library_export(ExportBook(10, "Some Book", isbn13="9780000000002")).decode(),
                newline="",
            )
        )
    )
    forms = {
        "Book Id": r"^\d+$",
        "ISBN": r'^="[0-9X]*"$',
        "ISBN13": r'^="\d*"$',
        "My Rating": r"^(0|[1-5]\.0)$",
        "Date Added": r"^\d{4}/\d{2}/\d{2}$",
        "Read Count": r"^\d+$",
    }
    for column, form in forms.items():
        assert all(re.match(form, row[column]) for row in recorded), column
        assert re.match(form, fake[column]), column


def test_the_recorded_export_parses() -> None:
    rows = parse_export(EXPORT.read_bytes())
    assert len(rows) == 8
    kindle = rows[0]
    assert (kindle.book_id, kindle.binding, kindle.isbn10, kindle.isbn13) == (
        "247031753",
        "Kindle Edition",
        None,
        None,
    )
    assert kindle.date_added == date(2000, 1, 1)
    with_isbn = next(r for r in rows if r.isbn10)
    assert with_isbn.isbn10 is not None and len(with_isbn.isbn10) == 10
    assert with_isbn.isbn13 is not None and with_isbn.isbn13.startswith("978")
    assert any(r.date_read for r in rows) and any(not r.date_read for r in rows)
