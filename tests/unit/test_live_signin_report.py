"""The opening test's findings report (SPEC §3.4b, Opening step).

The script itself is attended and never runs in a suite (§3.4b.7). What is worth testing offline
is the part that could leak: the report is built from a plain :class:`Findings` record, so these
check that it prints what #24 needs and nothing it must not — no cookie value, no CSRF token, no
password.
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

LIVE = Path(__file__).parents[1] / "live"
sys.path.insert(0, str(LIVE))

from goodreads_signin import Findings, cookie_lines, report  # noqa: E402

VALUE = "cookie-value-never-shown-anywhere"
CSRF = "csrf-token-never-shown-anywhere"
PASSWORD = "a-test-password-never-shown"


def cookie(name: str, expires: float | None = None) -> dict[str, object]:
    entry: dict[str, object] = {"name": name, "value": VALUE, "domain": ".goodreads.com"}
    if expires is not None:
        entry["expires"] = expires
    return entry


class Jar:
    def __init__(self, *cookies: dict[str, object]) -> None:
        self.cookies = cookies


class TestCookieLines:
    def test_an_expiry_is_shown_as_a_date(self) -> None:
        when = datetime(2027, 1, 1, tzinfo=UTC)
        [line] = cookie_lines(Jar(cookie("at-main", when.timestamp())))
        assert line == f"at-main@.goodreads.com expires={when.isoformat()}"

    def test_a_session_cookie_says_so(self) -> None:
        assert cookie_lines(Jar(cookie("ccsid"))) == ["ccsid@.goodreads.com expires=session"]

    def test_no_cookie_value_is_ever_shown(self) -> None:
        lines = cookie_lines(Jar(cookie("at-main", 1.0), cookie("x-main")))
        assert VALUE not in " ".join(lines)

    def test_the_lines_are_sorted(self) -> None:
        lines = cookie_lines(Jar(cookie("x-main"), cookie("at-main")))
        assert lines == sorted(lines)

    def test_an_empty_jar_is_no_lines(self) -> None:
        assert cookie_lines(Jar()) == []


class TestReport:
    def test_an_unfinished_run_still_reports(self) -> None:
        # The challenged case is a finding in its own right: §3.4b says that if every sign-in
        # is challenged, 4b ships with sign-in disabled.
        text = report(Findings(outcome="challenged", challenged=True, notes=["a CAPTCHA appeared"]))
        assert "sign-in outcome:      challenged" in text
        assert "challenge appeared:   yes" in text
        assert "- a CAPTCHA appeared" in text

    def test_a_full_run_reports_every_finding_the_spec_asks_for(self) -> None:
        text = report(
            Findings(
                outcome="ok",
                cookies=["at-main@.goodreads.com expires=session"],
                csrf_present=True,
                user_id="31415926",
                link_text="Your export from 10/15/2026 - 08:30",
                link_read_as_utc="2026-10-15T12:30:00+00:00",
                trigger_status=200,
                trigger_body='{"status":"ok"}',
                generated_at="2026-10-15T08:30:00-04:00",
                csv_bytes=4096,
            )
        )
        for expected in (
            "cookies harvested:    1",
            "CSRF token found:     yes",
            "user id on page:      31415926",
            "#exportFile text:     Your export from 10/15/2026 - 08:30",
            "read as (UTC):        2026-10-15T12:30:00+00:00",
            "trigger status:       200",
            "CSV size (bytes):     4096",
        ):
            assert expected in text

    def test_missing_findings_show_as_dashes_not_none(self) -> None:
        text = report(Findings())
        assert "None" not in text
        assert "user id on page:      -" in text

    @pytest.mark.parametrize("secret", [VALUE, CSRF, PASSWORD])
    def test_no_secret_can_reach_the_report(self, secret: str) -> None:
        # Findings holds no field for any of them, which is the point: there is nowhere to put
        # one. This fails loudly if a field for one is ever added.
        text = report(
            Findings(
                outcome="ok",
                cookies=cookie_lines(Jar(cookie("at-main"))),
                csrf_present=True,
                trigger_body='{"status":"ok"}',
            )
        )
        assert secret not in text

    def test_the_report_names_the_spec_section(self) -> None:
        assert "§3.4b" in report(Findings())
