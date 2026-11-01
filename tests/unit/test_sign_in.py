"""The Goodreads sign-in state machine (SPEC §3.4b.2) and #230's startup reset.

Sign-in is never automated against Goodreads (§3.4b.7), so the machine is driven through
``FakeBrowser``. The outcome rules themselves are the real :func:`classify`, so what these
check is SyncShelf's decisions rather than a hand-written answer.
"""

import sqlite3

import pytest

from syncshelf.sync.goodreads_session import (
    INTERRUPTED,
    PENDING_STATES,
    Cookie,
    CookieJar,
    PageFacts,
    attempt_sign_in,
    classify,
    harvest,
    record,
    reset_pending,
)

from fakes.browser import FakeBrowser, prompting, signed_in
from invariants import assert_accepted

pytestmark = pytest.mark.anyio

NOW = 1_767_225_600
LATER = NOW + 3600
EMAIL = "reader@example.com"
PASSWORD = "a-test-password-never-shown"


def cookie(name: str, domain: str = ".goodreads.com") -> Cookie:
    return Cookie(name=name, value="v", domain=domain, path="/")


@pytest.fixture
def db(schema: sqlite3.Connection) -> sqlite3.Connection:
    for name in ("alice", "bob"):
        assert_accepted(
            schema, "INSERT INTO profiles (name, created_at) VALUES (?, ?)", (name, NOW)
        )
    return schema


def state_of(conn: sqlite3.Connection, profile: str) -> tuple[str, int, str | None] | None:
    return conn.execute(
        "SELECT state, state_since, detail FROM goodreads_sessions WHERE profile = ?", (profile,)
    ).fetchone()


class TestClassify:
    """§3.4b.2's outcome table, as the rules see it."""

    def test_back_on_goodreads_is_signed_in(self) -> None:
        assert classify(signed_in()) == "ok"

    def test_still_on_the_portal_is_not(self) -> None:
        # Nothing recognised, so: stop. §3.4b.2 puts an unknown page in the challenged row.
        assert classify(prompting()) == "challenged"

    def test_a_one_time_code_prompt(self) -> None:
        assert classify(prompting(has_otp_field=True)) == "awaiting_code"

    def test_an_approval_prompt(self) -> None:
        assert classify(prompting(has_approval_prompt=True)) == "awaiting_approval"

    def test_a_rejected_password(self) -> None:
        assert classify(prompting(has_auth_error=True)) == "bad_credentials"

    def test_a_captcha(self) -> None:
        assert classify(prompting(has_captcha=True)) == "challenged"

    def test_a_captcha_wins_over_every_other_signal(self) -> None:
        # A page can show a CAPTCHA and look otherwise ordinary; a challenge means stop.
        facts = PageFacts(
            url="https://www.goodreads.com/",
            has_captcha=True,
            has_otp_field=True,
            has_approval_prompt=True,
            has_auth_error=True,
        )
        assert classify(facts) == "challenged"

    def test_an_amazon_url_is_not_signed_in(self) -> None:
        assert classify(PageFacts(url="https://www.amazon.com/")) == "challenged"

    def test_the_portal_path_on_goodreads_is_not_signed_in(self) -> None:
        # The portal is hosted on goodreads.com, so the host alone cannot say we are in.
        assert classify(PageFacts(url="https://www.goodreads.com/ap/signin")) == "challenged"


class TestHarvest:
    def test_only_goodreads_cookies_are_kept(self) -> None:
        # The sign-in goes through Amazon, so the context can hold more than it should store.
        kept = harvest(
            [
                cookie("at-main"),
                cookie("session-id", "www.goodreads.com"),
                cookie("x-main", ".amazon.com"),
                cookie("SID", "accounts.google.com"),
                cookie("aasp", "appleid.apple.com"),
            ]
        )
        assert kept.names() == ["at-main@.goodreads.com", "session-id@www.goodreads.com"]

    def test_a_lookalike_domain_is_not_goodreads(self) -> None:
        assert harvest([cookie("at-main", "goodreads.com.evil.test")]).cookies == ()

    def test_the_bare_domain_counts(self) -> None:
        assert harvest([cookie("at-main", "goodreads.com")]).names() == ["at-main@goodreads.com"]


class TestAttemptSignIn:
    async def test_a_successful_sign_in_harvests_the_jar(self) -> None:
        browser = FakeBrowser(cookies=(cookie("at-main"), cookie("x-main", ".amazon.com")))
        result = await attempt_sign_in(browser, CookieJar(), EMAIL, PASSWORD)
        assert result.outcome == "ok"
        assert result.jar.names() == ["at-main@.goodreads.com"]

    async def test_the_jar_seeds_the_context(self) -> None:
        # ubid-main and session-id tell Amazon this is a known device (§3.4b.2).
        jar = CookieJar((cookie("ubid-main"), cookie("session-id")))
        browser = FakeBrowser()
        await attempt_sign_in(browser, jar, EMAIL, PASSWORD)
        assert browser.last.seeded is jar

    @pytest.mark.parametrize(
        ("facts", "outcome"),
        [
            (prompting(has_otp_field=True), "awaiting_code"),
            (prompting(has_approval_prompt=True), "awaiting_approval"),
            (prompting(has_captcha=True), "challenged"),
            (prompting(has_auth_error=True), "bad_credentials"),
            (prompting(), "challenged"),
        ],
    )
    async def test_each_outcome_is_reported(self, facts: PageFacts, outcome: str) -> None:
        result = await attempt_sign_in(FakeBrowser(facts), CookieJar(), EMAIL, PASSWORD)
        assert result.outcome == outcome
        assert result.state == outcome  # the state names line up with the outcomes

    @pytest.mark.parametrize(
        "facts",
        [
            prompting(has_otp_field=True),
            prompting(has_approval_prompt=True),
            prompting(has_captcha=True),
            prompting(has_auth_error=True),
        ],
    )
    async def test_nothing_is_harvested_unless_it_signed_in(self, facts: PageFacts) -> None:
        browser = FakeBrowser(facts, cookies=(cookie("at-main"),))
        result = await attempt_sign_in(browser, CookieJar(), EMAIL, PASSWORD)
        assert result.jar.cookies == ()

    async def test_the_browser_is_always_released(self) -> None:
        browser = FakeBrowser(prompting(has_captcha=True))
        await attempt_sign_in(browser, CookieJar(), EMAIL, PASSWORD)
        assert browser.last.closed is True

    async def test_every_outcome_carries_a_scrubbed_detail(self) -> None:
        for facts in (prompting(has_captcha=True), prompting(has_auth_error=True), prompting()):
            result = await attempt_sign_in(FakeBrowser(facts), CookieJar(), EMAIL, PASSWORD)
            assert result.detail
            assert PASSWORD not in result.detail
            assert EMAIL not in result.detail


class TestRecord:
    def test_a_state_is_written_with_its_stamp(self, db: sqlite3.Connection) -> None:
        record(db, "alice", "challenged", NOW, "a bot check appeared")
        assert state_of(db, "alice") == ("challenged", NOW, "a bot check appeared")

    def test_a_later_state_replaces_it(self, db: sqlite3.Connection) -> None:
        record(db, "alice", "needs_sign_in", NOW)
        record(db, "alice", "ok", LATER)
        assert state_of(db, "alice") == ("ok", LATER, None)


class TestResetPending:
    @pytest.mark.parametrize("pending", PENDING_STATES)
    def test_a_waiting_session_is_reset(self, db: sqlite3.Connection, pending: str) -> None:
        # Only a dead process can leave one of these behind (#230).
        record(db, "alice", pending, NOW)  # type: ignore[arg-type]
        assert reset_pending(db, LATER) == ["alice"]
        assert state_of(db, "alice") == ("needs_sign_in", LATER, INTERRUPTED)

    @pytest.mark.parametrize("held", ["ok", "needs_sign_in", "challenged", "bad_credentials"])
    def test_any_other_state_is_left_alone(self, db: sqlite3.Connection, held: str) -> None:
        # Stickiness survives a restart: only the sign-in machine clears those.
        record(db, "alice", held, NOW)  # type: ignore[arg-type]
        assert reset_pending(db, LATER) == []
        assert state_of(db, "alice") == (held, NOW, None)

    def test_both_profiles_are_reset_together(self, db: sqlite3.Connection) -> None:
        record(db, "alice", "awaiting_code", NOW)
        record(db, "bob", "awaiting_approval", NOW)
        assert reset_pending(db, LATER) == ["alice", "bob"]

    def test_nothing_waiting_is_a_no_op(self, db: sqlite3.Connection) -> None:
        record(db, "alice", "ok", NOW)
        assert reset_pending(db, LATER) == []

    def test_an_empty_table_is_a_no_op(self, db: sqlite3.Connection) -> None:
        assert reset_pending(db, LATER) == []
