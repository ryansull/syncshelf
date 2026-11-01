"""A Goodreads CSV run, start to finish (SPEC §3.4b.4, §3.4b.5).

A CSV run fetches a library export and then applies it exactly as a run applies an upload
(§3.4a.8): the batch it stores is a ``gr_csv_auto`` one, and everything after that is 4a's
machinery, already covered by ``test_ingest_runs.py``. So what is tested here is the part
that is new — getting the export, and what each way of failing does to the session.

**An automatic export is always dated**, so unlike an undated upload it pushes.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from syncshelf.sync import batches
from syncshelf.sync.goodreads_session import record

from fakes.browser import prompting
from fakes.goodreads import FakeGoodreads, import_page
from fakes.goodreads_csv import ExportBook, library_export
from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio

DUNE = ExportBook(100, "Dune", rating=5)


@pytest.fixture
async def w(tmp_path: Path) -> World:
    async with world(tmp_path, csv=True) as w:
        w.hardcover.add_user_book(HC_USER, 7, status_id=3, rating=4.0)
        w.hardcover.add_mapping(100, 7, title="Dune")
        w.goodreads.expect_export(body=import_page(user_id=GR_USER))
        yield w  # type: ignore[misc]


def page(fake: FakeGoodreads, **what: object) -> None:
    """Re-arm the fake with an import page and whatever else the case needs."""
    fake.expect_export(body=import_page(user_id=GR_USER), **what)  # type: ignore[arg-type]


async def session(w: World) -> tuple[str, str | None] | None:
    return await w.db.read(
        lambda conn: conn.execute(
            "SELECT state, detail FROM goodreads_sessions WHERE profile = 'alice'"
        ).fetchone()
    )


async def backfill_by_rss(w: World) -> None:
    w.goodreads.add_item(GR_USER, 100, "Dune", user_rating="4")
    await w.sync.run_rss("alice")
    w.clock.advance(3600)
    page(w.goodreads)


class TestWhatACsvRunDoes:
    async def test_the_export_becomes_a_dated_batch_and_is_applied(self, w: World) -> None:
        page(w.goodreads, csv=library_export(DUNE))
        result = await w.sync.run_csv("alice")
        assert (result.state, result.fetched) == ("ok", True)
        assert await w.rows(
            "SELECT source, stale, complete, processed_at IS NOT NULL, run_id FROM ingest_batches"
        ) == [("gr_csv_auto", 0, 1, 1, result.run_id)]

    async def test_the_run_is_recorded_against_the_csv_source(self, w: World) -> None:
        page(w.goodreads, csv=library_export(DUNE))
        result = await w.sync.run_csv("alice")
        assert await w.rows("SELECT source, state FROM runs WHERE id = ?", result.run_id) == [
            ("gr_csv", "ok")
        ]

    async def test_a_dated_export_pushes_its_change(self, w: World) -> None:
        # The point of the CSV source: an undated upload would only have filled.
        await backfill_by_rss(w)
        page(w.goodreads, csv=library_export(DUNE))
        await w.sync.run_csv("alice")
        (shelved,) = [ub for ub in w.hardcover.user_books[HC_USER] if ub["book_id"] == 7]
        assert shelved["rating"] == 5.0

    async def test_an_export_already_there_is_reused_rather_than_re_triggered(
        self, w: World
    ) -> None:
        # Triggering deletes the previous file (§3.4b.4 step 2), so reuse is not just cheaper.
        at = w.clock.now().strftime("%m/%d/%Y - %H:%M")
        w.goodreads.expect_export(
            body=import_page(user_id=GR_USER, export_at=at), csv=library_export(DUNE)
        )
        assert (await w.sync.run_csv("alice")).state == "ok"
        assert w.goodreads.triggers == 0

    async def test_an_upload_waiting_is_applied_in_the_same_run(self, w: World) -> None:
        # Both batches are unprocessed, so one run clears both, oldest observation first.
        now = int(w.clock.time())
        await w.db.write(
            batches.store,
            "alice",
            "gr_csv_ingest",
            now,
            now - 600,
            library_export(ExportBook(100, "Dune", rating=3)),
            1,
        )
        page(w.goodreads, csv=library_export(DUNE))
        await w.sync.run_csv("alice")
        assert await w.rows("SELECT COUNT(*) FROM ingest_batches WHERE processed_at IS NULL") == [
            (0,)
        ]

    async def test_a_csv_run_and_an_rss_run_share_the_profile_lock(self, w: World) -> None:
        # One run of a profile's Goodreads source at a time, whichever source it is (§2.2).
        assert w.sync.lock("alice") is w.sync.lock("alice")
        async with w.sync.lock("alice"):
            assert w.sync.lock("alice").locked()


class TestWhenTheExportCannotBeFetched:
    async def test_a_waf_stop_sets_challenged_and_fails_the_run(self, w: World) -> None:
        w.goodreads.fail_next("captcha")
        result = await w.sync.run_csv("alice")
        assert result.state == "fetch_failed"
        stored = await session(w)
        assert stored is not None and stored[0] == "challenged"
        assert w.browser is not None and w.browser.opened == []  # never signs in past a WAF

    async def test_a_refused_trigger_with_no_export_at_all_fails_the_run(self, w: World) -> None:
        # #231: with an export to fall back on it succeeds; with none there is nothing to get.
        page(w.goodreads, trigger_status=429)
        result = await w.sync.run_csv("alice")
        assert result.state == "fetch_failed"
        assert await w.rows("SELECT COUNT(*) FROM ingest_batches") == [(0,)]

    async def test_a_refused_trigger_falls_back_to_the_export_that_is_there(self, w: World) -> None:
        old = (w.clock.now() - timedelta(days=9)).strftime("%m/%d/%Y - %H:%M")
        w.goodreads.expect_export(
            body=import_page(user_id=GR_USER, export_at=old),
            trigger_status=429,
            csv=library_export(DUNE),
        )
        assert (await w.sync.run_csv("alice")).state == "ok"

    async def test_an_unreadable_import_page_fails_the_run_without_a_sign_in(
        self, w: World
    ) -> None:
        w.goodreads.expect_export(body="<html><body>Next.js</body></html>")
        assert (await w.sync.run_csv("alice")).state == "fetch_failed"
        assert w.browser is not None and w.browser.opened == []

    async def test_the_failure_is_on_the_run_as_an_error(self, w: World) -> None:
        page(w.goodreads, trigger_status=429)
        result = await w.sync.run_csv("alice")
        said = await w.rows("SELECT first_error FROM runs WHERE id = ?", result.run_id)
        assert said[0][0] and "429" in said[0][0]


class TestAnExpiredSession:
    async def test_it_signs_in_again_and_the_retry_gets_the_export(self, w: World) -> None:
        page(w.goodreads, expire_next=1, csv=library_export(DUNE))
        result = await w.sync.run_csv("alice")
        assert result.state == "ok"
        assert await session(w) == ("ok", None)
        assert w.browser is not None and len(w.browser.opened) == 1

    async def test_a_session_a_sign_in_cannot_recover_fails_the_run(self, w: World) -> None:
        page(w.goodreads, expired=True, csv=library_export(DUNE))
        result = await w.sync.run_csv("alice")
        assert result.state == "fetch_failed"
        assert w.browser is not None and len(w.browser.opened) == 1  # one attempt, not a loop

    async def test_a_sign_in_that_needs_a_person_leaves_needs_sign_in(self, w: World) -> None:
        assert w.browser is not None
        w.browser.facts = prompting(has_otp_field=True)
        page(w.goodreads, expire_next=1, csv=library_export(DUNE))
        result = await w.sync.run_csv("alice")
        assert result.state == "fetch_failed"
        stored = await session(w)
        assert stored is not None and stored[0] == "needs_sign_in"

    async def test_a_sticky_state_is_never_signed_in_past(self, w: World) -> None:
        await w.db.write(record, "alice", "bad_credentials", int(w.clock.time()), "rejected")
        page(w.goodreads, expire_next=1, csv=library_export(DUNE))
        result = await w.sync.run_csv("alice")
        assert result.state == "fetch_failed"
        assert w.browser is not None and w.browser.opened == []

    async def test_nothing_in_the_run_carries_the_password(self, w: World) -> None:
        page(w.goodreads, expired=True)
        result = await w.sync.run_csv("alice")
        said = await w.rows("SELECT first_error FROM runs WHERE id = ?", result.run_id)
        stored = await session(w)
        for text in [said[0][0] or "", (stored or ("", ""))[1] or ""]:
            assert "a-test-password-never-shown" not in text


class TestWhichProfilesHaveACsvSource:
    async def test_a_profile_with_credentials_and_a_sidecar_does(self, w: World) -> None:
        assert w.sync.csv_enabled("alice") is True

    async def test_a_profile_without_credentials_does_not(self, tmp_path: Path) -> None:
        async with world(tmp_path) as plain:
            assert plain.sync.csv_enabled("alice") is False
            with pytest.raises(KeyError):
                await plain.sync.run_csv("alice")
