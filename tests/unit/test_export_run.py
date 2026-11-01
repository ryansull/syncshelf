"""The Goodreads export run (SPEC §3.4b.4 steps 2 to 4): trigger, poll, download, and the
batch the 4a engine consumes.

The decision on #231 shapes most of this: a refused trigger falls back to whatever export
Goodreads already has, because a refused trigger deleted nothing and older real data beats none.
"""

from datetime import UTC, datetime, timedelta

import pytest

from syncshelf.clients.goodreads_csv import parse_export
from syncshelf.clients.goodreads_export import (
    DOWNLOAD_PATH,
    POLL_INTERVAL,
    POLL_TIMEOUT,
    Challenged,
    ExportError,
    GoodreadsExport,
    SessionExpired,
    fetch_export,
    parse_import_page,
)
from syncshelf.clients.goodreads_rss import Pacer
from syncshelf.sync.batches import store
from syncshelf.sync.goodreads_session import AMAZON_COOKIES, Cookie, CookieJar

from clock import FakeClock
from fakes.goodreads import FakeGoodreads, import_page
from fakes.goodreads_csv import ExportBook, library_export
from invariants import assert_accepted

pytestmark = pytest.mark.anyio

USER = "31415926"
CSRF = "csrf-token-never-shown-anywhere"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
EXPORT = library_export(
    ExportBook(book_id=10, title="Some Book"), ExportBook(book_id=11, title="Another Book")
)


def session_jar() -> CookieJar:
    return CookieJar(
        tuple(
            Cookie(name=name, value="v", domain=".goodreads.com", path="/")
            for name in sorted(AMAZON_COOKIES)
        )
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(clock: FakeClock) -> FakeGoodreads:
    return FakeGoodreads(clock)


@pytest.fixture
def client(fake: FakeGoodreads, clock: FakeClock) -> GoodreadsExport:
    return GoodreadsExport(session_jar(), pacer=Pacer(clock), transport=fake.transport)


def page(**kwargs: object):
    return parse_import_page(import_page(user_id=USER, csrf=CSRF, **kwargs), UTC)  # type: ignore[arg-type]


def stamp(age: timedelta) -> str:
    return (NOW - age).strftime("%m/%d/%Y - %H:%M")


class TestTriggeringAndPolling:
    async def test_the_happy_path_triggers_polls_and_downloads(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(not_ready=2, csv=EXPORT)
        fetched = await fetch_export(client, page(), now=NOW, clock=clock)
        assert fetched.payload == EXPORT
        assert fetched.reused is False
        # Triggered, so the export is current as of when we asked, not of any link text.
        assert fetched.observed_at == NOW
        assert fake.triggers == 1
        paths = [r.path for r in fake.export_requests]
        assert paths.count(DOWNLOAD_PATH.format(user_id=USER)) == 4  # two 404s, ready, download

    async def test_the_trigger_carries_what_the_spec_requires(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(csv=EXPORT)
        await fetch_export(client, page(), now=NOW, clock=clock)
        [trigger] = [r for r in fake.export_requests if r.method == "POST"]
        assert trigger.path == f"/review_porter/export/{USER}"

    async def test_a_302_counts_as_a_successful_trigger(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        # §3.4b.4: the prior-art tools accept a 2xx or a 302.
        fake.expect_export(trigger_status=302, csv=EXPORT)
        fetched = await fetch_export(client, page(), now=NOW, clock=clock)
        assert fetched.payload == EXPORT and fetched.reused is False

    async def test_polling_gives_up_after_ten_minutes(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(not_ready=10_000, csv=EXPORT)
        with pytest.raises(ExportError, match="had not finished"):
            await fetch_export(client, page(), now=NOW, clock=clock)
        assert clock.monotonic() >= POLL_TIMEOUT

    async def test_polling_waits_between_attempts(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(not_ready=1, csv=EXPORT)
        await fetch_export(client, page(), now=NOW, clock=clock)
        assert clock.monotonic() >= POLL_INTERVAL

    async def test_a_download_that_fails_is_an_error(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(download_status=500, csv=EXPORT)
        with pytest.raises(ExportError, match="could not be downloaded"):
            await fetch_export(client, page(), now=NOW, clock=clock)


class TestReuse:
    async def test_a_fresh_export_is_reused_and_nothing_is_triggered(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(csv=EXPORT)
        fetched = await fetch_export(
            client, page(export_at=stamp(timedelta(hours=2))), now=NOW, clock=clock
        )
        assert fetched.reused is True
        assert fake.triggers == 0
        # Reused, so observed_at is the link's timestamp, not now.
        assert fetched.observed_at == NOW - timedelta(hours=2)

    async def test_a_stale_export_is_re_triggered(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(csv=EXPORT)
        fetched = await fetch_export(
            client, page(export_at=stamp(timedelta(hours=30))), now=NOW, clock=clock
        )
        assert fetched.reused is False and fake.triggers == 1
        assert fetched.observed_at == NOW


class TestARefusedTrigger:
    """#231: a refused trigger deleted nothing, so fall back to the export that is still there."""

    async def test_an_existing_export_is_used_whatever_its_age(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(trigger_status=429, csv=EXPORT)
        old = stamp(timedelta(days=9))
        fetched = await fetch_export(client, page(export_at=old), now=NOW, clock=clock)
        assert fetched.payload == EXPORT
        assert fetched.reused is True
        assert fetched.observed_at == NOW - timedelta(days=9)

    async def test_with_no_export_at_all_it_is_a_failed_fetch(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(trigger_status=429)
        with pytest.raises(ExportError, match="refused to start"):
            await fetch_export(client, page(), now=NOW, clock=clock)

    async def test_the_refusal_is_logged_with_its_status(
        self,
        client: GoodreadsExport,
        fake: FakeGoodreads,
        clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.DEBUG)
        fake.expect_export(trigger_status=429)
        with pytest.raises(ExportError):
            await fetch_export(client, page(), now=NOW, clock=clock)
        [refused] = [
            r for r in caplog.records if getattr(r, "event", None) == "goodreads_export_refused"
        ]
        assert refused.__dict__["status"] == 429

    async def test_a_refusal_never_polls(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        # Nothing was started, so there is nothing to wait for.
        fake.expect_export(trigger_status=429, csv=EXPORT)
        await fetch_export(client, page(export_at=stamp(timedelta(days=9))), now=NOW, clock=clock)
        assert clock.monotonic() < POLL_INTERVAL


class TestStopConditions:
    async def test_a_challenge_mid_flow_stops_the_run(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(csv=EXPORT)
        fake.fail_next("challenge")
        with pytest.raises(Challenged):
            await fetch_export(client, page(), now=NOW, clock=clock)

    async def test_an_expired_session_mid_flow_stops_the_run(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export(expired=True)
        with pytest.raises(SessionExpired):
            await fetch_export(client, page(), now=NOW, clock=clock)


class TestTheStoredBatch:
    async def test_the_export_is_stored_as_a_complete_gr_csv_auto_batch(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock, schema: object
    ) -> None:
        conn = schema  # a migrated connection
        assert_accepted(
            conn,  # type: ignore[arg-type]
            "INSERT INTO profiles (name, created_at) VALUES ('alice', 0)",
        )
        fake.expect_export(csv=EXPORT)
        fetched = await fetch_export(client, page(), now=NOW, clock=clock)
        rows = parse_export(fetched.payload)
        batch_id = store(
            conn,  # type: ignore[arg-type]
            "alice",
            "gr_csv_auto",
            int(NOW.timestamp()),
            int(fetched.observed_at.timestamp()),
            fetched.payload,
            len(rows),
        )
        stored = conn.execute(  # type: ignore[attr-defined]
            "SELECT source, observed_at, stale, complete, payload, row_count FROM ingest_batches"
            " WHERE id = ?",
            (batch_id,),
        ).fetchone()
        assert stored == (
            "gr_csv_auto",
            int(NOW.timestamp()),
            0,
            1,
            EXPORT,
            2,
        )

    async def test_the_payload_parses_with_the_4a_parser(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        # §3.4b.4 step 4: the same parser /ingest uses, so a bad header fails the same way.
        fake.expect_export(csv=EXPORT)
        fetched = await fetch_export(client, page(), now=NOW, clock=clock)
        assert [row.book_id for row in parse_export(fetched.payload)] == ["10", "11"]
