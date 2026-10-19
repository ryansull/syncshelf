"""The Goodreads shelf RSS client (SPEC §3.4a.1): paging to the empty page, what makes a page
usable, never retrying past an unusable one, and a reader's pace."""

import logging

import httpx2
import pytest

from syncshelf.clients.goodreads_rss import ALL_SHELF, PER_PAGE, GoodreadsRss, Pacer

from clock import FakeClock
from fakes.goodreads import FakeGoodreads

pytestmark = pytest.mark.anyio

USER = "31415926"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(clock: FakeClock) -> FakeGoodreads:
    fake = FakeGoodreads(clock)
    fake.add_user(USER)
    return fake


def client(fake: FakeGoodreads, clock: FakeClock, key: str | None = None) -> GoodreadsRss:
    return GoodreadsRss(USER, key=key, pacer=Pacer(clock), transport=fake.transport)


def fill(fake: FakeGoodreads, count: int) -> None:
    for n in range(count):
        fake.add_item(USER, 1000 + n, f"Book {n}")


async def test_a_pass_pages_until_an_empty_page(fake: FakeGoodreads, clock: FakeClock) -> None:
    fill(fake, 450)
    result = await client(fake, clock).fetch_all()
    assert result.complete and result.usable and result.problem is None
    assert result.pages == 4  # 200 + 200 + 50, then the empty page
    assert len(result.items) == 450
    assert [r.params["page"] for r in fake.requests] == ["1", "2", "3", "4"]
    for request in fake.requests:
        assert request.params["shelf"] == ALL_SHELF
        assert request.params["per_page"] == str(PER_PAGE)


async def test_an_empty_library_is_one_empty_page_and_complete(
    fake: FakeGoodreads, clock: FakeClock
) -> None:
    result = await client(fake, clock).fetch_all()
    assert result.complete and result.items == [] and result.pages == 1


async def test_items_carry_the_feed_fields(fake: FakeGoodreads, clock: FakeClock) -> None:
    fake.add_item(
        USER,
        12345678,
        "Example Book (Example Series, #2)",
        shelf="currently-reading",
        shelves=("favourites",),
        author_name="Ann Author",
        isbn="080441234X",
        user_rating="4",
        user_read_at="Fri, 2 Oct 2026 00:00:00 +0000",
        user_review="Loved it.<br/>Would reread.",
    )
    (item,) = (await client(fake, clock).fetch_all()).items
    assert item.book_id == "12345678"
    assert item.title == "Example Book (Example Series, #2)"
    assert item.author_name == "Ann Author"
    assert item.isbn == "080441234X"
    assert item.user_rating == "4"
    assert item.user_read_at == "Fri, 2 Oct 2026 00:00:00 +0000"
    assert item.user_shelves == "currently-reading, favourites"
    assert item.user_review == "Loved it.<br/>Would reread."
    assert item.user_date_added.endswith("-0700")


async def test_the_key_is_sent_only_when_configured(fake: FakeGoodreads, clock: FakeClock) -> None:
    await client(fake, clock).fetch_all()
    await client(fake, clock, key="feed-key").fetch_all()
    assert "key" not in fake.requests[0].params
    assert fake.requests[1].params["key"] == "feed-key"


async def test_requests_are_at_least_two_seconds_apart(
    fake: FakeGoodreads, clock: FakeClock
) -> None:
    fill(fake, 450)
    pacer = Pacer(clock)
    rss = GoodreadsRss(USER, pacer=pacer, transport=fake.transport)
    await rss.fetch_all()
    await rss.fetch_all()  # the next pass waits for the last request of this one
    times = [r.at for r in fake.requests]
    assert all(b - a >= 2 for a, b in zip(times, times[1:], strict=False))


@pytest.mark.parametrize(
    ("fault", "problem", "challenged"),
    [
        ("challenge", "page 1: Goodreads answered with a WAF challenge (HTTP 202)", True),
        ("captcha", "page 1: Goodreads answered with a WAF CAPTCHA (HTTP 405)", True),
        ("server_error", "page 1: Goodreads returned HTTP 503", False),
        ("html", "page 1: the feed is not RSS with a channel", False),
        ("wrong_shelf", "page 1: the feed doesn't name the #ALL# shelf", False),
        ("timeout", "page 1: Goodreads is unreachable (ReadTimeout)", False),
    ],
)
async def test_an_unusable_first_page_fetches_nothing(
    fake: FakeGoodreads, clock: FakeClock, fault: str, problem: str, challenged: bool
) -> None:
    fill(fake, 3)
    fake.fail_next(fault)  # type: ignore[arg-type]
    result = await client(fake, clock).fetch_all()
    assert not result.usable and not result.complete
    assert result.items == []
    assert (result.problem, result.challenged) == (problem, challenged)
    assert len(fake.requests) == 1  # never retried past


async def test_an_unknown_user_is_a_failed_fetch(fake: FakeGoodreads, clock: FakeClock) -> None:
    rss = GoodreadsRss("999", pacer=Pacer(clock), transport=fake.transport)
    result = await rss.fetch_all()
    assert not result.usable
    assert result.problem == "page 1: Goodreads has no such user (HTTP 404)"


async def test_an_unusable_later_page_keeps_what_came_before(
    fake: FakeGoodreads, clock: FakeClock
) -> None:
    fill(fake, 450)
    fake.fail_next("challenge", page=2)
    result = await client(fake, clock).fetch_all()
    assert result.usable and not result.complete
    assert result.pages == 1 and len(result.items) == 200
    assert result.problem == "page 2: Goodreads answered with a WAF challenge (HTTP 202)"
    assert len(fake.requests) == 2


async def test_a_page_that_repeats_earlier_items_stops_the_pass(clock: FakeClock) -> None:
    fake = FakeGoodreads(clock)
    fake.add_user(USER)
    fake.add_item(USER, 1, "Only Book")
    first = fake.handle

    def ignore_paging(request: httpx2.Request) -> httpx2.Response:
        # Goodreads ignoring ?page= would serve page 1 forever.
        return first(httpx2.Request("GET", request.url.copy_set_param("page", "1")))

    rss = GoodreadsRss(USER, pacer=Pacer(clock), transport=httpx2.MockTransport(ignore_paging))
    result = await rss.fetch_all()
    assert not result.complete and len(result.items) == 1
    assert result.problem == "page 2: repeats earlier items"


async def test_the_log_carries_no_query_or_user_id(
    fake: FakeGoodreads, clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="syncshelf.clients.goodreads_rss"):
        await client(fake, clock, key="feed-key").fetch_all()
    (record,) = caplog.records
    assert record.getMessage() == "GET /review/list_rss 200"
    assert record.__dict__["service"] == "goodreads"
    text = record.getMessage() + repr(record.__dict__)
    assert "feed-key" not in text and USER not in text
