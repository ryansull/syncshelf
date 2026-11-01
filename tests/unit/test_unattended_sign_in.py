"""Signing in again without a person (SPEC §3.4b.2).

Two rules here exist to protect the account rather than to make sync work: never past a sticky
state, and never more than once a day. A third protects the person: any prompt abandons the
attempt, because nobody is watching to answer it.
"""

from pathlib import Path

import pytest

from syncshelf.db import Database
from syncshelf.sync.goodreads_session import (
    AUTO_SIGN_IN_EVERY,
    STICKY_STATES,
    NoSidecar,
    SessionState,
    SignIns,
    auto_sign_in_state,
    jar_path,
    may_sign_in_again,
    record,
    stamp_auto_sign_in,
)

from clock import FakeClock
from fakes.browser import FakeBrowser, prompting, signed_in

pytestmark = pytest.mark.anyio

NOW = 1_767_225_600
EMAIL = "reader@example.com"
PASSWORD = "a-test-password-never-shown"


async def profile_row(db: Database) -> None:
    await db.write(
        lambda conn: conn.execute("INSERT INTO profiles (name, created_at) VALUES ('alice', 0)")
    )


def holder(browser: FakeBrowser | None, tmp_path: Path, db: Database, clock: FakeClock) -> SignIns:
    return SignIns(browser, data_dir=tmp_path, db=db, clock=clock.time)


async def state_of(db: Database) -> tuple[str, str | None] | None:
    return await db.read(
        lambda conn: conn.execute(
            "SELECT state, detail FROM goodreads_sessions WHERE profile = 'alice'"
        ).fetchone()
    )


class TestTheRules:
    """``may_sign_in_again`` on its own: the two account-protecting rules."""

    def test_a_fresh_profile_may_sign_in(self) -> None:
        assert may_sign_in_again(None, None, NOW) is None

    def test_an_expired_session_may_sign_in(self) -> None:
        assert may_sign_in_again("needs_sign_in", None, NOW) is None

    @pytest.mark.parametrize("sticky", STICKY_STATES)
    def test_a_sticky_state_may_not(self, sticky: SessionState) -> None:
        # Only a person clears these: a challenge must not be hammered and a rejected password
        # must never be retried, or SyncShelf could lock the account.
        refusal = may_sign_in_again(sticky, None, NOW)
        assert refusal is not None and sticky in refusal

    def test_a_second_attempt_inside_a_day_may_not(self) -> None:
        refusal = may_sign_in_again("needs_sign_in", NOW - 3600, NOW)
        assert refusal is not None and "24 hours" in refusal

    def test_a_day_later_may(self) -> None:
        assert may_sign_in_again("needs_sign_in", NOW - AUTO_SIGN_IN_EVERY, NOW) is None

    def test_one_second_short_of_a_day_may_not(self) -> None:
        assert may_sign_in_again("needs_sign_in", NOW - AUTO_SIGN_IN_EVERY + 1, NOW) is not None

    def test_a_sticky_state_wins_over_a_stale_attempt(self) -> None:
        # Both rules fail: the sticky one is the more useful thing to say.
        refusal = may_sign_in_again("challenged", NOW - AUTO_SIGN_IN_EVERY * 10, NOW)
        assert refusal is not None and "challenged" in refusal


class TestTheStamp:
    async def test_the_attempt_is_stamped_on_a_fresh_profile(self, db: Database) -> None:
        await profile_row(db)
        await db.write(stamp_auto_sign_in, "alice", NOW)
        assert await db.read(auto_sign_in_state, "alice") == ("needs_sign_in", NOW)

    async def test_the_stamp_leaves_an_existing_state_alone(self, db: Database) -> None:
        await profile_row(db)
        await db.write(record, "alice", "ok", NOW - 10, None)
        await db.write(stamp_auto_sign_in, "alice", NOW)
        assert await db.read(auto_sign_in_state, "alice") == ("ok", NOW)

    async def test_no_row_reads_as_nothing_known(self, db: Database) -> None:
        assert await db.read(auto_sign_in_state, "alice") == (None, None)


class TestAttemptingUnattended:
    async def test_a_clean_run_signs_in_and_saves_the_jar(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(
            signed_in(),
            cookies=({"name": "at-main", "value": "v", "domain": ".goodreads.com", "path": "/"},),
        )  # type: ignore[arg-type]
        result = await holder(browser, tmp_path, db, clock).attempt_unattended(
            "alice", EMAIL, PASSWORD, NOW
        )
        assert (result.state, result.attempted) == ("ok", True)
        assert jar_path(tmp_path, "alice").exists()

    @pytest.mark.parametrize("sticky", ["challenged", "bad_credentials"])
    async def test_a_sticky_state_is_never_touched(
        self, tmp_path: Path, db: Database, clock: FakeClock, sticky: str
    ) -> None:
        await profile_row(db)
        await db.write(record, "alice", sticky, NOW - 100_000, "earlier")
        browser = FakeBrowser(signed_in())
        result = await holder(browser, tmp_path, db, clock).attempt_unattended(
            "alice", EMAIL, PASSWORD, NOW
        )
        assert result.attempted is False
        assert browser.opened == []  # no browser was even opened
        assert await state_of(db) == (sticky, "earlier")

    async def test_a_second_attempt_inside_a_day_is_refused(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(prompting(has_captcha=True))
        signs = holder(browser, tmp_path, db, clock)
        await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW)
        # The first attempt ended challenged, so clear that to isolate the daily cap.
        await db.write(record, "alice", "needs_sign_in", NOW, None)
        opened = len(browser.opened)
        result = await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW + 3600)
        assert result.attempted is False
        assert len(browser.opened) == opened

    async def test_a_day_later_it_tries_again(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        signs = holder(FakeBrowser(signed_in()), tmp_path, db, clock)
        await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW)
        await db.write(record, "alice", "needs_sign_in", NOW, None)
        result = await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW + AUTO_SIGN_IN_EVERY)
        assert result.attempted is True

    @pytest.mark.parametrize(
        "facts", [prompting(has_otp_field=True), prompting(has_approval_prompt=True)]
    )
    async def test_a_prompt_abandons_the_attempt(
        self, tmp_path: Path, db: Database, clock: FakeClock, facts: object
    ) -> None:
        # Nobody is watching, so a wait that needs a person is let go rather than left hanging.
        await profile_row(db)
        browser = FakeBrowser(facts)  # type: ignore[arg-type]
        signs = holder(browser, tmp_path, db, clock)
        result = await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW)
        assert result.state == "needs_sign_in"
        assert signs.waiting("alice") is False
        assert browser.last.closed is True
        stored = await state_of(db)
        assert stored is not None and stored[0] == "needs_sign_in"

    async def test_an_abandoned_attempt_still_counts_against_the_cap(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        # Otherwise a profile that always prompts would be retried every night window.
        await profile_row(db)
        signs = holder(FakeBrowser(prompting(has_otp_field=True)), tmp_path, db, clock)
        await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW)
        _, last = await db.read(auto_sign_in_state, "alice")
        assert last == NOW

    async def test_a_challenge_becomes_sticky(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        signs = holder(FakeBrowser(prompting(has_captcha=True)), tmp_path, db, clock)
        assert (await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW)).state == "challenged"
        # And a second attempt a week later is still refused.
        later = await signs.attempt_unattended(
            "alice", EMAIL, PASSWORD, NOW + AUTO_SIGN_IN_EVERY * 7
        )
        assert later.attempted is False

    async def test_a_rejected_password_is_never_retried(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(prompting(has_auth_error=True))
        signs = holder(browser, tmp_path, db, clock)
        assert (
            await signs.attempt_unattended("alice", EMAIL, PASSWORD, NOW)
        ).state == "bad_credentials"
        opened = len(browser.opened)
        for days in (1, 7, 30):
            await signs.attempt_unattended(
                "alice", EMAIL, PASSWORD, NOW + AUTO_SIGN_IN_EVERY * days
            )
        assert len(browser.opened) == opened

    async def test_no_sidecar_refuses(self, tmp_path: Path, db: Database, clock: FakeClock) -> None:
        with pytest.raises(NoSidecar):
            await holder(None, tmp_path, db, clock).attempt_unattended(
                "alice", EMAIL, PASSWORD, NOW
            )

    async def test_the_refusal_reason_never_carries_a_secret(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        await db.write(record, "alice", "bad_credentials", NOW, None)
        result = await holder(FakeBrowser(), tmp_path, db, clock).attempt_unattended(
            "alice", EMAIL, PASSWORD, NOW
        )
        assert result.reason and PASSWORD not in result.reason and EMAIL not in result.reason
