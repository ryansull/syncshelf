"""The Goodreads cookie jar (SPEC §3.4b.3).

The jar is the only session state, so these cover what makes it safe to keep on disk: it is
written 0600 and atomically, it never stores a `jwt_token`, it survives a corrupt or missing
file without taking the service down, and no cookie value ever reaches a log record.
"""

import json
import logging
import os
from pathlib import Path

import pytest

from syncshelf.sync.goodreads_session import (
    AMAZON_COOKIES,
    JAR_MODE,
    Cookie,
    CookieJar,
    jar_path,
    load_jar,
    save_jar,
)

VALUE = "cookie-value-never-shown-anywhere"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="Windows has no POSIX file mode; the deployment target is Linux"
)


def cookie(name: str, value: str = VALUE, domain: str = ".goodreads.com") -> Cookie:
    return Cookie(name=name, value=value, domain=domain, path="/")


def amazon_set() -> tuple[Cookie, ...]:
    return tuple(cookie(name) for name in sorted(AMAZON_COOKIES))


@pytest.fixture
def jar(tmp_path: Path) -> Path:
    return jar_path(tmp_path, "alice")


def test_the_jar_is_named_for_the_profile_under_goodreads(tmp_path: Path) -> None:
    assert jar_path(tmp_path, "alice") == tmp_path / "goodreads" / "alice.cookies.json"


class TestRoundTrip:
    def test_the_amazon_set_and_the_rails_session_survive(self, jar: Path) -> None:
        stored = CookieJar(amazon_set() + (cookie("_session_id2"),))
        save_jar(stored, jar)
        assert load_jar(jar).cookies == stored.cookies

    @pytest.mark.parametrize("name", ["ccsid", "aws-waf-token"])
    def test_the_optional_cookies_are_kept_when_present(self, jar: Path, name: str) -> None:
        save_jar(CookieJar(amazon_set() + (cookie(name),)), jar)
        assert f"{name}@.goodreads.com" in load_jar(jar).names()

    def test_a_jwt_token_is_never_stored(self, jar: Path) -> None:
        saved = save_jar(CookieJar(amazon_set() + (cookie("jwt_token"),)), jar)
        # Dropped from what is returned as well as from the file, so no caller keeps one.
        assert "jwt_token" not in {c["name"] for c in saved.cookies}
        assert "jwt_token" not in jar.read_text(encoding="utf-8")
        assert "jwt_token" not in {c["name"] for c in load_jar(jar).cookies}

    def test_the_optional_playwright_fields_survive(self, jar: Path) -> None:
        rich = Cookie(
            name="at-main",
            value=VALUE,
            domain=".goodreads.com",
            path="/",
            expires=1_800_000_000.0,
            httpOnly=True,
            secure=True,
            sameSite="Lax",
        )
        save_jar(CookieJar((rich,)), jar)
        assert load_jar(jar).cookies == (rich,)

    def test_a_cookie_without_a_path_reads_back_with_the_default(self, jar: Path) -> None:
        jar.parent.mkdir(parents=True)
        jar.write_text(
            json.dumps([{"name": "at-main", "value": VALUE, "domain": ".goodreads.com"}]),
            encoding="utf-8",
        )
        assert load_jar(jar).cookies[0]["path"] == "/"


class TestHasSession:
    def test_the_whole_amazon_set_makes_a_jar_reusable(self) -> None:
        assert CookieJar(amazon_set()).has_session

    def test_a_missing_amazon_cookie_does_not(self) -> None:
        assert not CookieJar(amazon_set()[1:]).has_session

    def test_the_rails_session_alone_does_not(self) -> None:
        # _session_id2 is regenerated from the Amazon set, so it is no credential by itself.
        assert not CookieJar((cookie("_session_id2"),)).has_session

    def test_the_rails_session_is_not_required(self) -> None:
        assert CookieJar(amazon_set()).has_session
        assert not CookieJar(()).has_session


class TestOnDisk:
    @POSIX_ONLY
    def test_the_jar_is_written_0600(self, jar: Path) -> None:
        save_jar(CookieJar(amazon_set()), jar)
        assert jar.stat().st_mode & 0o777 == JAR_MODE

    @POSIX_ONLY
    def test_rewriting_keeps_the_mode(self, jar: Path) -> None:
        save_jar(CookieJar(amazon_set()), jar)
        os.chmod(jar, 0o644)
        save_jar(CookieJar(amazon_set()), jar)
        assert jar.stat().st_mode & 0o777 == JAR_MODE

    def test_the_directory_is_created_on_first_write(self, jar: Path) -> None:
        assert not jar.parent.exists()
        save_jar(CookieJar(amazon_set()), jar)
        assert jar.is_file()

    def test_no_temporary_file_is_left_behind(self, jar: Path) -> None:
        save_jar(CookieJar(amazon_set()), jar)
        assert [p.name for p in jar.parent.iterdir()] == [jar.name]

    def test_a_failed_write_leaves_the_previous_jar_intact(self, jar: Path) -> None:
        # The atomic rename's whole point: a crash part-way through must not destroy a session.
        save_jar(CookieJar(amazon_set()), jar)
        before = jar.read_text(encoding="utf-8")

        # A value json cannot encode: the name filter passes it through, then the dump fails
        # with the temporary file already open.
        unserialisable = {"name": "at-main", "value": object(), "domain": ".goodreads.com"}
        with pytest.raises(TypeError):
            save_jar(CookieJar((unserialisable,)), jar)  # type: ignore[arg-type]
        assert jar.read_text(encoding="utf-8") == before
        assert [p.name for p in jar.parent.iterdir()] == [jar.name]


class TestUnusableJars:
    def test_a_missing_jar_is_no_session(self, jar: Path) -> None:
        assert load_jar(jar).cookies == ()
        assert not load_jar(jar)

    def test_a_missing_jar_is_not_logged_as_a_problem(
        self, jar: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The ordinary first run: nothing has signed in yet.
        caplog.set_level(logging.DEBUG)
        load_jar(jar)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @pytest.mark.parametrize(
        "text",
        [
            "not json at all",
            "{}",
            '"a string"',
            "[1, 2, 3]",
            '[{"value": "v", "domain": "d"}]',
            '[{"name": "at-main", "domain": "d"}]',
            '[{"name": "at-main", "value": "v"}]',
            '[{"name": "at-main", "value": 1, "domain": "d"}]',
        ],
    )
    def test_a_malformed_jar_is_no_session_rather_than_a_crash(self, jar: Path, text: str) -> None:
        jar.parent.mkdir(parents=True)
        jar.write_text(text, encoding="utf-8")
        assert load_jar(jar).cookies == ()

    def test_one_bad_cookie_rejects_the_whole_jar(self, jar: Path) -> None:
        jar.parent.mkdir(parents=True)
        good = {"name": "at-main", "value": VALUE, "domain": ".goodreads.com"}
        jar.write_text(json.dumps([good, {"name": "broken"}]), encoding="utf-8")
        assert load_jar(jar).cookies == ()

    def test_a_malformed_jar_is_logged_without_its_contents(
        self, jar: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        jar.parent.mkdir(parents=True)
        jar.write_text(json.dumps([{"name": "at-main", "value": VALUE}]), encoding="utf-8")
        load_jar(jar)
        [record] = [r for r in caplog.records if getattr(r, "event", None)]
        assert record.__dict__["event"] == "goodreads_jar_malformed"
        assert VALUE not in f"{record.getMessage()} {record.__dict__}"


class TestNeverLogsValues:
    def test_reading_and_writing_log_names_only(
        self, jar: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        save_jar(CookieJar(amazon_set() + (cookie("_session_id2"),)), jar)
        load_jar(jar)
        everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert VALUE not in everything
        # The names are what a log line may carry, and they did reach it.
        assert "at-main@.goodreads.com" in everything

    def test_the_jar_itself_never_reprs_a_value(self) -> None:
        stored = CookieJar(amazon_set())
        assert VALUE not in repr(stored)
        assert VALUE not in f"{stored}"

    def test_names_are_sorted_name_at_domain(self) -> None:
        stored = CookieJar((cookie("x-main"), cookie("at-main", domain="www.goodreads.com")))
        assert stored.names() == ["at-main@www.goodreads.com", "x-main@.goodreads.com"]
