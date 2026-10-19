"""The observation contract for RSS rows (SPEC §3.4a.3): normalised fields keyed by the
Goodreads edition id, and a field the feed doesn't carry left out, never cleared."""

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from syncshelf.clients.goodreads_rss import RssItem, RssPass
from syncshelf.sync.observations import Batch, observe_rss, review_text

SHELF_MAP = {"read": 3, "currently-reading": 2, "to-read": 1, "did-not-finish": 5}
OBSERVED_AT = 4_102_444_800
LONDON = ZoneInfo("Europe/London")


def item(**fields: str) -> RssItem:
    values = {
        "book_id": "12345678",
        "title": "Example Book (Example Series, #2)",
        "author_name": "Ann Author",
        "isbn": "",
        "user_rating": "0",
        "user_read_at": "",
        "user_date_added": "Thu, 15 Oct 2026 08:09:10 -0700",
        "user_date_created": "Mon, 21 Sep 2026 19:20:21 -0700",
        "user_shelves": "",
        "user_review": "",
    }
    return RssItem(**(values | fields))


def observe(*items: RssItem, complete: bool = True) -> Batch:
    rss = RssPass(list(items), pages=1, complete=complete)
    return observe_rss(rss, SHELF_MAP, OBSERVED_AT, LONDON)


def test_the_batch_carries_its_source_time_and_completeness() -> None:
    batch = observe(item(), complete=False)
    assert batch.source == "gr_rss"
    assert batch.observed_at == OBSERVED_AT
    assert not batch.complete and not batch.stale
    assert [row.source_id for row in batch.rows] == ["12345678"]


def test_rss_carries_only_its_own_fields() -> None:
    (row,) = observe(item()).rows
    # spoiler, read_count and format aren't in the feed: not observed, never cleared.
    assert set(row.fields) == {"status", "rating", "date_read", "review"}
    assert row.isbn13 is None and row.format is None


@pytest.mark.parametrize(
    ("shelves", "status", "others"),
    [
        ("", "read", ()),
        ("favourites, kindle", "read", ("favourites", "kindle")),
        ("currently-reading", "currently-reading", ()),
        ("to-read, favourites", "to-read", ("favourites",)),
        ("favourites, did-not-finish", "did-not-finish", ("favourites",)),
    ],
)
def test_the_status_is_the_exclusive_shelf_and_none_means_read(
    shelves: str, status: str, others: tuple[str, ...]
) -> None:
    (row,) = observe(item(user_shelves=shelves)).rows
    assert row.fields["status"] == status
    assert row.shelves == others


def test_a_built_in_shelf_is_exclusive_even_when_unmapped() -> None:
    rss = RssPass([item(user_shelves="to-read")], pages=1, complete=True)
    (row,) = observe_rss(rss, {"read": 3}, OBSERVED_AT, LONDON).rows
    assert row.fields["status"] == "to-read"


@pytest.mark.parametrize(("rating", "value"), [("0", None), ("4", "4"), ("", None)])
def test_a_zero_rating_is_none(rating: str, value: str | None) -> None:
    (row,) = observe(item(user_rating=rating)).rows
    assert row.fields["rating"] == value


def test_the_read_date_is_the_date_part_never_shifted() -> None:
    (row,) = observe(item(user_read_at="Fri, 2 Oct 2026 00:00:00 +0000")).rows
    assert row.fields["date_read"] == "2026-10-02"
    (row,) = observe(item(user_read_at="")).rows
    assert row.fields["date_read"] is None


def test_the_added_date_is_in_the_profiles_zone() -> None:
    # 19:20 Pacific on the 21st is already the 22nd in London (§2.4: calendar dates are in
    # the profile's time zone).
    (row,) = observe(item()).rows
    assert row.date_added == date(2026, 9, 22)


def test_last_touched_keeps_goodreads_offset() -> None:
    (row,) = observe(item()).rows
    pacific = timezone(timedelta(hours=-7))
    assert row.last_touched == datetime(2026, 10, 15, 8, 9, 10, tzinfo=pacific)


def test_a_date_with_no_known_offset_is_read_as_utc() -> None:
    (row,) = observe(item(user_date_added="Thu, 15 Oct 2026 08:09:10 -0000")).rows
    assert row.last_touched == datetime(2026, 10, 15, 8, 9, 10, tzinfo=UTC)


def test_match_input_fields() -> None:
    (row,) = observe(item(isbn="080441234X")).rows
    assert row.title == "Example Book (Example Series, #2)"
    assert row.author == "Ann Author"
    assert row.isbn10 == "080441234X"
    (row,) = observe(item(isbn="", author_name="")).rows
    assert row.isbn10 is None and row.author is None


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("", None),
        ("Loved it.", "Loved it."),
        ("Loved it.<br/>Would reread.", "Loved it.\nWould reread."),
        ("One<br>two<BR />three", "One\ntwo\nthree"),
        ("<b>Bold</b> and <i>italic</i>", "Bold and italic"),
        ("Fish &amp; chips &lt;3", "Fish & chips <3"),
        ("  lots   of\tspace  <br/>  here ", "lots of space\nhere"),
        ("<br/><br/>", None),
    ],
)
def test_review_html_becomes_plain_text(html: str, text: str | None) -> None:
    assert review_text(html) == text
    (row,) = observe(item(user_review=html)).rows
    assert row.fields["review"] == text
