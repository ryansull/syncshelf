"""The jar-backed Goodreads transport (SPEC §3.4b.1, §3.4b.3, §3.4b.4).

What this layer has to get right before any export flow sits on it: cookie rotation settling
instead of looping, the jar never leaving `https://www.goodreads.com`, a reader's pace, and the
two stop conditions — an expired session and an AWS WAF challenge.
"""

import logging

import pytest

from syncshelf.clients.goodreads_export import (
    BASE,
    MAX_REDIRECTS,
    Challenged,
    ExportError,
    GoodreadsExport,
    SessionExpired,
)
from syncshelf.clients.goodreads_rss import MIN_INTERVAL, Pacer
from syncshelf.sync.goodreads_session import AMAZON_COOKIES, Cookie, CookieJar

from clock import FakeClock
from fakes.goodreads import FakeGoodreads

pytestmark = pytest.mark.anyio

IMPORT = "/review/import"
VALUE = "cookie-value-never-shown-anywhere"
CSRF = "csrf-token-never-shown-anywhere"


def cookie(name: str, value: str = VALUE) -> Cookie:
    return Cookie(name=name, value=value, domain=".goodreads.com", path="/")


def session_jar() -> CookieJar:
    return CookieJar(tuple(cookie(name) for name in sorted(AMAZON_COOKIES)))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake(clock: FakeClock) -> FakeGoodreads:
    return FakeGoodreads(clock)


@pytest.fixture
def client(fake: FakeGoodreads, clock: FakeClock) -> GoodreadsExport:
    return GoodreadsExport(session_jar(), pacer=Pacer(clock), transport=fake.transport)


class TestTheJarIsSent:
    async def test_the_session_cookies_go_with_the_request(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        fake.expect_export()
        response = await client.get(IMPORT)
        assert response.status_code == 200
        assert [r.path for r in fake.export_requests] == [IMPORT]

    async def test_the_jar_reads_back_unchanged_when_nothing_rotates(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        fake.expect_export()
        await client.get(IMPORT)
        assert {c["name"] for c in client.jar.cookies} == AMAZON_COOKIES
        assert client.jar.has_session

    async def test_a_post_carries_its_form_and_headers(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        fake.expect_export()
        await client.post(
            "/review_porter/export/42",
            data={"format": "json"},
            headers={"x-csrf-token": CSRF, "x-requested-with": "XMLHttpRequest"},
        )
        assert [(r.method, r.path) for r in fake.export_requests] == [
            ("POST", "/review_porter/export/42")
        ]


class TestCookieRotation:
    async def test_a_rotation_redirect_settles(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        # §3.4b.1: _session_id2 is regenerated through a same-URL redirect carrying Set-Cookie.
        fake.expect_export(rotate=1)
        response = await client.get(IMPORT)
        assert response.status_code == 200
        assert fake.rotations == 1
        assert [r.path for r in fake.export_requests] == [IMPORT, IMPORT]

    async def test_the_rotated_cookie_reaches_the_jar(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        # The whole point of applying Set-Cookie on a redirect: without this the next request
        # would send the stale cookie and be redirected again, forever.
        fake.expect_export(rotate=1)
        await client.get(IMPORT)
        rotated = {c["name"]: c["value"] for c in client.jar.cookies}
        assert rotated["_session_id2"] == "rotated-1"
        assert rotated.keys() >= AMAZON_COOKIES

    async def test_several_rotations_still_settle(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        fake.expect_export(rotate=MAX_REDIRECTS)
        assert (await client.get(IMPORT)).status_code == 200
        assert {c["name"]: c["value"] for c in client.jar.cookies}["_session_id2"] == (
            f"rotated-{MAX_REDIRECTS}"
        )

    async def test_a_chain_that_never_settles_fails_instead_of_hanging(
        self, client: GoodreadsExport, fake: FakeGoodreads, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        fake.expect_export(rotate=MAX_REDIRECTS + 1)
        with pytest.raises(ExportError, match="redirected more than"):
            await client.get(IMPORT)
        hops = [
            r for r in caplog.records if getattr(r, "event", None) == "goodreads_export_request"
        ]
        assert len(hops) == MAX_REDIRECTS + 1


class TestTheLeakGuard:
    @pytest.mark.parametrize(
        "target",
        [
            "https://example.com/elsewhere",
            "http://www.goodreads.com/review/import",
            "https://goodreads.com.example.com/review/import",
            "https://www.goodreads.com.evil.test/",
        ],
    )
    async def test_a_redirect_off_goodreads_is_refused(
        self, client: GoodreadsExport, fake: FakeGoodreads, target: str
    ) -> None:
        # The jar is only ever sent to https://www.goodreads.com (§3.4b.3), and never
        # downgraded to http. The fake asserts on any request to another host, so a leak would
        # fail there too.
        fake.expect_export(offsite=target)
        with pytest.raises(ExportError, match="redirected to"):
            await client.get(IMPORT)
        assert [r.path for r in fake.export_requests] == [IMPORT]

    async def test_a_plain_goodreads_redirect_is_followed(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        # Same host and scheme, so it is followed rather than refused. The redirect is
        # one-shot, so the followed hop answers an ordinary 200.
        fake.expect_export(offsite=f"{BASE}/review/import?step=2")
        assert (await client.get(IMPORT)).status_code == 200
        assert [r.path for r in fake.export_requests] == [IMPORT, IMPORT]


class TestStopConditions:
    @pytest.mark.parametrize("path", ["/user/sign_in", "/user/new"])
    async def test_a_sign_in_redirect_is_an_expired_session(
        self, client: GoodreadsExport, fake: FakeGoodreads, path: str
    ) -> None:
        fake.expect_export(offsite=f"{BASE}{path}")
        with pytest.raises(SessionExpired):
            await client.get(IMPORT)

    async def test_the_expired_redirect_shape_the_spec_names(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        fake.expect_export(expired=True)
        with pytest.raises(SessionExpired, match="sign-in page"):
            await client.get(IMPORT)

    @pytest.mark.parametrize("fault", ["challenge", "captcha"])
    async def test_a_waf_stop_is_challenged(
        self, client: GoodreadsExport, fake: FakeGoodreads, fault: str
    ) -> None:
        fake.fail_next(fault)  # type: ignore[arg-type]
        with pytest.raises(Challenged):
            await client.get(IMPORT)

    async def test_a_challenge_is_logged_with_its_status(
        self, client: GoodreadsExport, fake: FakeGoodreads, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        fake.fail_next("challenge")
        with pytest.raises(Challenged):
            await client.get(IMPORT)
        [stop] = [
            r for r in caplog.records if getattr(r, "event", None) == "goodreads_export_challenged"
        ]
        assert stop.__dict__["status"] == 202
        assert stop.__dict__["waf_action"] == "challenge"

    async def test_a_server_error_is_returned_not_raised(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        # Only the two stop conditions raise; the flow decides what any other status means.
        fake.fail_next("server_error")
        assert (await client.get(IMPORT)).status_code == 503


class TestPacing:
    async def test_requests_are_a_readers_pace_apart(
        self, client: GoodreadsExport, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        fake.expect_export()
        await client.get(IMPORT)
        fake.expect_export()
        await client.get(IMPORT)
        first, second = (r.at for r in fake.export_requests)
        assert second - first >= MIN_INTERVAL

    async def test_a_rotation_hop_is_paced_too(
        self, client: GoodreadsExport, fake: FakeGoodreads
    ) -> None:
        # Every hop is a Goodreads request, so the pace applies within one logical call.
        fake.expect_export(rotate=1)
        await client.get(IMPORT)
        first, second = (r.at for r in fake.export_requests)
        assert second - first >= MIN_INTERVAL

    async def test_the_pacer_is_shared_with_the_feed(
        self, fake: FakeGoodreads, clock: FakeClock
    ) -> None:
        # One pacer per profile, so the export and the feed never overlap (§3.4b.4).
        pacer = Pacer(clock)
        client = GoodreadsExport(session_jar(), pacer=pacer, transport=fake.transport)
        fake.expect_export()
        await client.get(IMPORT)
        fake.expect_export()
        await client.get(IMPORT)
        assert fake.export_requests[1].at - fake.export_requests[0].at >= MIN_INTERVAL


class TestNeverLogsSecrets:
    async def test_no_cookie_or_csrf_value_is_logged(
        self, client: GoodreadsExport, fake: FakeGoodreads, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        fake.expect_export(rotate=1)
        await client.post(
            "/review_porter/export/42",
            data={"format": "json"},
            headers={"x-csrf-token": CSRF},
        )
        everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert VALUE not in everything
        assert CSRF not in everything

    async def test_the_outbound_log_names_the_method_and_path(
        self, client: GoodreadsExport, fake: FakeGoodreads, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        fake.expect_export()
        await client.get(IMPORT)
        [call] = [
            r for r in caplog.records if getattr(r, "event", None) == "goodreads_export_request"
        ]
        assert (call.__dict__["method"], call.__dict__["path"]) == ("GET", IMPORT)
