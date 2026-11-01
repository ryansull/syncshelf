"""The Goodreads import page and the 24-hour reuse decision (SPEC §3.4b.4 step 1 and 2).

Triggering a new export **deletes the previous file**, so reading this page correctly is what
stops SyncShelf destroying a good export to make an equivalent one. The page is synthesised
rather than recorded (see #239), so these tests pin the parser to what §3.4b.4 documents.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from syncshelf.clients.goodreads_export import (
    IMPORT_PATH,
    REUSE_WITHIN,
    Challenged,
    ExistingExport,
    GoodreadsExport,
    ImportPage,
    ImportPageError,
    SessionExpired,
    parse_import_page,
)
from syncshelf.clients.goodreads_rss import Pacer
from syncshelf.sync.goodreads_session import AMAZON_COOKIES, Cookie, CookieJar

from clock import FakeClock
from fakes.goodreads import FakeGoodreads, import_page

pytestmark = pytest.mark.anyio

USER = "31415926"
CSRF = "csrf-token-never-shown-anywhere"
NEW_YORK = ZoneInfo("America/New_York")


def parsed(tz: object = UTC, **page: object) -> ImportPage:
    return parse_import_page(import_page(csrf=CSRF, **page), tz)  # type: ignore[arg-type]


class TestWhatThePageCarries:
    def test_the_csrf_token_comes_from_the_meta_tag(self) -> None:
        assert parsed(user_id=USER).csrf_token == CSRF

    def test_a_hidden_authenticity_token_works_too(self) -> None:
        # §3.4b.4 names either form.
        assert parsed(user_id=USER, authenticity=True).csrf_token == CSRF

    def test_the_user_id_comes_from_the_form_action(self) -> None:
        assert parsed(user_id=USER).user_id == USER

    def test_the_token_is_not_in_the_repr(self) -> None:
        # It authorises the trigger, so it is a credential like any other (§3.4b.3).
        assert CSRF not in repr(parsed(user_id=USER))


class TestTheExistingExport:
    def test_no_link_means_no_existing_export(self) -> None:
        assert parsed(user_id=USER).export is None

    def test_a_link_is_read_into_a_path_and_a_timestamp(self) -> None:
        page = parsed(user_id=USER, export_at="10/15/2026 - 08:30")
        assert page.export is not None
        assert page.export.path == f"/review_porter/export/{USER}/goodreads_export.csv"
        assert page.export.generated_at == datetime(2026, 10, 15, 8, 30, tzinfo=UTC)

    def test_the_timestamp_is_read_in_the_profiles_timezone(self) -> None:
        # The link text carries no offset, so it can only be read against a timezone the
        # caller supplies. §3.4b.4 says to confirm which one in the opening test (#219).
        page = parsed(NEW_YORK, user_id=USER, export_at="10/15/2026 - 08:30")
        assert page.export is not None
        assert page.export.generated_at == datetime(2026, 10, 15, 8, 30, tzinfo=NEW_YORK)

    def test_the_user_id_can_come_from_the_link_when_the_form_is_gone(self) -> None:
        html = import_page(user_id=USER, export_at="10/15/2026 - 08:30")
        html = html.replace(f'<form action="/review_porter/export/{USER}" method="post">', "<form>")
        assert parse_import_page(html, UTC).user_id == USER


class TestTheReuseDecision:
    @staticmethod
    def page_with(age: timedelta) -> tuple[ImportPage, datetime]:
        now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
        generated = now - age
        page = parsed(user_id=USER, export_at=generated.strftime("%m/%d/%Y - %H:%M"))
        return page, now

    def test_an_export_younger_than_24_hours_is_reused(self) -> None:
        page, now = self.page_with(timedelta(hours=23, minutes=59))
        reused = page.reuse(now)
        assert reused is not None and reused is page.export

    def test_an_export_older_than_24_hours_is_not(self) -> None:
        page, now = self.page_with(timedelta(hours=24, minutes=1))
        assert page.reuse(now) is None

    def test_exactly_24_hours_old_is_not_reused(self) -> None:
        # "Younger than 24 h" (§3.4b.4), so the boundary itself triggers.
        page, now = self.page_with(REUSE_WITHIN)
        assert page.reuse(now) is None

    def test_no_existing_export_means_trigger(self) -> None:
        page = parsed(user_id=USER)
        assert page.reuse(datetime(2026, 10, 16, 12, 0, tzinfo=UTC)) is None

    def test_fresh_at_is_the_same_judgement(self) -> None:
        export = ExistingExport("/p", datetime(2026, 10, 16, 12, 0, tzinfo=UTC))
        assert export.fresh_at(datetime(2026, 10, 17, 11, 59, tzinfo=UTC))
        assert not export.fresh_at(datetime(2026, 10, 17, 12, 0, tzinfo=UTC))


class TestWhatBreaksLoudly:
    def test_a_page_with_no_token_names_the_token(self) -> None:
        html = import_page(user_id=USER).replace('<meta name="csrf-token" content="', '<meta x="')
        with pytest.raises(ImportPageError, match="CSRF token"):
            parse_import_page(html, UTC)

    def test_a_page_with_no_user_id_says_so(self) -> None:
        html = import_page(user_id=USER).replace(
            f'<form action="/review_porter/export/{USER}" method="post">', "<form>"
        )
        with pytest.raises(ImportPageError, match="user id"):
            parse_import_page(html, UTC)

    @pytest.mark.parametrize(
        "text",
        [
            "Your export from yesterday",
            "Your export from 15/10/2026 - 08:30",
            "Your export from 10/15/2026",
            "Download",
            "",
        ],
    )
    def test_an_unreadable_date_is_an_error_not_a_missing_export(self, text: str) -> None:
        # Treating it as "no export" would trigger a new one and delete the file the link
        # describes, which is the one outcome worth failing to avoid.
        with pytest.raises(ImportPageError, match="date could not be read"):
            parse_import_page(import_page(user_id=USER, link_text=text), UTC)

    def test_an_impossible_date_is_an_error(self) -> None:
        with pytest.raises(ImportPageError, match="date could not be read"):
            parse_import_page(import_page(user_id=USER, export_at="02/30/2026 - 08:30"), UTC)

    def test_an_empty_page_names_what_was_missing(self) -> None:
        with pytest.raises(ImportPageError, match="CSRF token"):
            parse_import_page("<html><body>Goodreads</body></html>", UTC)


class TestThroughTheClient:
    """Fetching the page for real, so an expired session is surfaced as such (§3.4b.4 step 1)."""

    @staticmethod
    def client(fake: FakeGoodreads, clock: FakeClock) -> GoodreadsExport:
        jar = CookieJar(
            tuple(
                Cookie(name=name, value="v", domain=".goodreads.com", path="/")
                for name in sorted(AMAZON_COOKIES)
            )
        )
        return GoodreadsExport(jar, pacer=Pacer(clock), transport=fake.transport)

    async def test_the_page_is_fetched_and_parsed(self) -> None:
        clock = FakeClock()
        fake = FakeGoodreads(clock)
        fake.expect_export(body=import_page(user_id=USER, csrf=CSRF))
        response = await self.client(fake, clock).get(IMPORT_PATH)
        assert parse_import_page(response.text, UTC).user_id == USER

    async def test_an_expired_session_is_not_a_parse_failure(self) -> None:
        # The transport raises before any HTML is read, so "sign in again" never arrives
        # disguised as "the markup changed" (#221, §3.4b.1).
        clock = FakeClock()
        fake = FakeGoodreads(clock)
        fake.expect_export(expired=True)
        with pytest.raises(SessionExpired):
            await self.client(fake, clock).get(IMPORT_PATH)

    async def test_a_challenge_is_not_a_parse_failure_either(self) -> None:
        clock = FakeClock()
        fake = FakeGoodreads(clock)
        fake.fail_next("challenge")
        with pytest.raises(Challenged):
            await self.client(fake, clock).get(IMPORT_PATH)
