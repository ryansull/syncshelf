"""Starting and finishing an attended Goodreads sign-in (SPEC §3.4b.2, §3.4b.6).

``SignIns`` is where the work is, so it is tested directly against the fake browser: §3.4b.7
forbids automating a sign-in against Goodreads, and the repo keeps no test-only seam in
production code, so there is no way to hand the real app a fake browser. The tools themselves
are thin, and what they add — the refusals and the words handed back — is tested through the
real ASGI app.
"""

from pathlib import Path

import pytest

from syncshelf.db import Database
from syncshelf.sync.goodreads_session import (
    Cookie,
    NoSidecar,
    NotWaiting,
    SignIns,
    jar_path,
    load_jar,
    record,
)
from syncshelf.tools.goodreads import _NEXT

from clock import FakeClock
from fakes.browser import FakeBrowser, prompting, signed_in
from serving import AuthedClient

pytestmark = pytest.mark.anyio

EMAIL = "reader@example.com"
PASSWORD = "a-test-password-never-shown"
GOODREADS = (
    '{goodreads: {user_id: "1", shelf_map: {read: 3}, '
    'email: "${GR_EMAIL}", password: "${GR_PASSWORD}"}}'
)
CREDENTIAL_ENV = {"GR_EMAIL": EMAIL, "GR_PASSWORD": PASSWORD}


def cookie(name: str, domain: str = ".goodreads.com") -> Cookie:
    return Cookie(name=name, value="v", domain=domain, path="/")


async def profile_row(db: Database) -> None:
    await db.write(
        lambda conn: conn.execute("INSERT INTO profiles (name, created_at) VALUES ('alice', 0)")
    )


async def state_of(db: Database, profile: str = "alice") -> tuple[str, str | None] | None:
    return await db.read(
        lambda conn: conn.execute(
            "SELECT state, detail FROM goodreads_sessions WHERE profile = ?", (profile,)
        ).fetchone()
    )


def holder(browser: FakeBrowser | None, tmp_path: Path, db: Database, clock: FakeClock) -> SignIns:
    return SignIns(browser, data_dir=tmp_path, db=db, clock=clock.time)


class TestStartingASignIn:
    async def test_a_successful_sign_in_is_recorded_and_the_jar_saved(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(cookies=(cookie("at-main"), cookie("x-main", ".amazon.com")))
        result = await holder(browser, tmp_path, db, clock).start("alice", EMAIL, PASSWORD)
        assert result.state == "ok"
        assert await state_of(db) == ("ok", None)
        # Only the Goodreads cookie reaches the jar on disk.
        assert load_jar(jar_path(tmp_path, "alice")).names() == ["at-main@.goodreads.com"]

    @pytest.mark.parametrize(
        ("facts", "state"),
        [
            (prompting(has_otp_field=True), "awaiting_code"),
            (prompting(has_approval_prompt=True), "awaiting_approval"),
            (prompting(has_captcha=True), "challenged"),
            (prompting(has_auth_error=True), "bad_credentials"),
        ],
    )
    async def test_every_outcome_is_recorded_with_a_scrubbed_detail(
        self, tmp_path: Path, db: Database, clock: FakeClock, facts: object, state: str
    ) -> None:
        await profile_row(db)
        result = await holder(FakeBrowser(facts), tmp_path, db, clock).start(  # type: ignore[arg-type]
            "alice", EMAIL, PASSWORD
        )
        assert result.state == state
        stored = await state_of(db)
        assert stored is not None and stored[0] == state
        assert stored[1] and PASSWORD not in stored[1] and EMAIL not in stored[1]

    async def test_nothing_is_written_to_the_jar_unless_it_signed_in(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(prompting(has_captcha=True), cookies=(cookie("at-main"),))
        await holder(browser, tmp_path, db, clock).start("alice", EMAIL, PASSWORD)
        assert not jar_path(tmp_path, "alice").exists()

    async def test_a_waiting_sign_in_is_held_and_others_are_not(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        waiting = holder(FakeBrowser(prompting(has_otp_field=True)), tmp_path, db, clock)
        await waiting.start("alice", EMAIL, PASSWORD)
        assert waiting.waiting("alice") is True

        done = holder(FakeBrowser(signed_in()), tmp_path, db, clock)
        await done.start("alice", EMAIL, PASSWORD)
        assert done.waiting("alice") is False

    async def test_a_finished_attempt_releases_the_browser(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(prompting(has_captcha=True))
        await holder(browser, tmp_path, db, clock).start("alice", EMAIL, PASSWORD)
        assert browser.last.closed is True

    async def test_a_waiting_attempt_keeps_its_browser_open(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(prompting(has_otp_field=True))
        await holder(browser, tmp_path, db, clock).start("alice", EMAIL, PASSWORD)
        assert browser.last.closed is False

    async def test_signing_in_again_replaces_a_held_attempt(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        # This is also how a sticky challenged or bad_credentials is cleared (§3.4b.6).
        await profile_row(db)
        browser = FakeBrowser(prompting(has_otp_field=True))
        signs = holder(browser, tmp_path, db, clock)
        await signs.start("alice", EMAIL, PASSWORD)
        first = browser.last
        await signs.start("alice", EMAIL, PASSWORD)
        assert first.closed is True
        assert len(browser.opened) == 2

    async def test_a_sticky_state_does_not_block_a_fresh_attempt(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        await db.write(record, "alice", "bad_credentials", 0, "rejected")
        signs = holder(FakeBrowser(signed_in()), tmp_path, db, clock)
        assert (await signs.start("alice", EMAIL, PASSWORD)).state == "ok"

    async def test_no_sidecar_refuses_to_start(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        with pytest.raises(NoSidecar):
            await holder(None, tmp_path, db, clock).start("alice", EMAIL, PASSWORD)

    async def test_availability_says_whether_a_sign_in_is_possible(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        assert holder(None, tmp_path, db, clock).available is False
        assert holder(FakeBrowser(), tmp_path, db, clock).available is True


class TestVerifying:
    async def test_a_code_finishes_a_waiting_sign_in(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(
            prompting(has_otp_field=True), cookies=(cookie("at-main"),), after_code=signed_in()
        )
        signs = holder(browser, tmp_path, db, clock)
        assert (await signs.start("alice", EMAIL, PASSWORD)).state == "awaiting_code"
        assert (await signs.verify("alice", "123456")).state == "ok"
        assert await state_of(db) == ("ok", None)
        assert signs.waiting("alice") is False

    async def test_a_wrong_code_can_leave_it_still_waiting(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(
            prompting(has_otp_field=True), after_code=prompting(has_otp_field=True)
        )
        signs = holder(browser, tmp_path, db, clock)
        await signs.start("alice", EMAIL, PASSWORD)
        assert (await signs.verify("alice", "000000")).state == "awaiting_code"
        assert signs.waiting("alice") is True

    async def test_the_code_reaches_the_browser(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(prompting(has_otp_field=True), after_code=signed_in())
        signs = holder(browser, tmp_path, db, clock)
        await signs.start("alice", EMAIL, PASSWORD)
        await signs.verify("alice", "123456")
        assert browser.last.submissions == ["credentials", "code:123456"]

    async def test_a_code_with_nothing_waiting_is_refused(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        # The restart case (#230): the process holding it has gone, so start again.
        with pytest.raises(NotWaiting):
            await holder(FakeBrowser(), tmp_path, db, clock).verify("alice", "123456")

    async def test_closing_releases_everything_held(
        self, tmp_path: Path, db: Database, clock: FakeClock
    ) -> None:
        await profile_row(db)
        browser = FakeBrowser(prompting(has_otp_field=True))
        signs = holder(browser, tmp_path, db, clock)
        await signs.start("alice", EMAIL, PASSWORD)
        await signs.close()
        assert browser.last.closed is True
        assert signs.waiting("alice") is False


class TestWhatThePersonIsTold:
    def test_every_state_has_something_to_say(self) -> None:
        # Including the states a sign-in never returns, since sync_status reports them too.
        for state in (
            "ok",
            "needs_sign_in",
            "awaiting_approval",
            "awaiting_code",
            "challenged",
            "bad_credentials",
            "disabled",
        ):
            assert _NEXT[state]  # type: ignore[index]

    def test_a_challenge_points_at_the_manual_fallback(self) -> None:
        # §3.4a.10's action for `goodreads_session` when challenged.
        assert "by hand" in _NEXT["challenged"]

    def test_a_rejected_password_says_it_will_not_be_retried(self) -> None:
        assert "not try it again" in _NEXT["bad_credentials"]


class TestThroughTheApp:
    """What the tools add over the holder: the refusals, through the real ASGI app."""

    @staticmethod
    async def refusal(mcp: object, tool: str, **arguments: object) -> str:
        result = await mcp.call_tool(tool, arguments)  # type: ignore[attr-defined]
        assert result.is_error, result.content
        return str(result.content[0].text)

    async def test_a_profile_without_credentials_is_told_so(
        self, authed_client: AuthedClient
    ) -> None:
        async with authed_client("alice") as session:
            says = await self.refusal(session.mcp, "goodreads_sign_in")
        assert "no Goodreads email and password" in says
        # And it says the RSS sync is unaffected, so nobody thinks Goodreads has stopped.
        assert "RSS" in says

    async def test_verifying_with_nothing_waiting_says_to_start_again(
        self, authed_client: AuthedClient
    ) -> None:
        # The restart case (#230) looks the same from here: nothing is waiting.
        async with authed_client("alice") as session:
            says = await self.refusal(session.mcp, "goodreads_verify", code="123456")
        assert "Run goodreads_sign_in" in says

    async def test_neither_tool_echoes_the_code_back(self, authed_client: AuthedClient) -> None:
        async with authed_client("alice") as session:
            says = await self.refusal(session.mcp, "goodreads_verify", code="123456")
        assert "123456" not in says

    async def test_both_tools_are_served(self, authed_client: AuthedClient) -> None:
        async with authed_client("alice") as session:
            names = {tool.name for tool in (await session.mcp.list_tools()).tools}
        assert {"goodreads_sign_in", "goodreads_verify"} <= names
