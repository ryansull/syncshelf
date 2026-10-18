"""JSON-lines logging and redaction (SPEC §2.7)."""

import json
import logging
from datetime import datetime

import pytest
from mcp.server import MCPServer

from syncshelf import logging as slog

SECRET = "fake-secret-value"
HOSTS = {"abs.home.arpa": "abs", "chaptarr.home.arpa": "chaptarr", "192.0.2.10": "browser"}


pytestmark = pytest.mark.usefixtures("restore_logging")


def lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line]


def test_every_line_carries_the_stable_fields(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure()
    logging.getLogger("syncshelf.app").info("started %s", "ok", extra={"event": "startup"})

    [line] = lines(capsys)
    assert line["level"] == "INFO"
    assert line["logger"] == "syncshelf.app"
    assert line["event"] == "startup"
    assert line["msg"] == "started ok"
    ts = datetime.fromisoformat(str(line["ts"]))
    assert ts.utcoffset() is not None and ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_event_defaults_to_the_logger_name_in_snake_case(
    capsys: pytest.CaptureFixture[str],
) -> None:
    slog.configure()
    logging.getLogger("uvicorn.error").info("Application startup complete.")

    [line] = lines(capsys)
    assert line["event"] == "uvicorn_error"


def test_context_fields_appear_only_when_set(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure()
    log = logging.getLogger("syncshelf.sync")
    with slog.log_context(profile="alice", source="abs", run_id=7):
        with slog.log_context(change_id=42):
            log.info("applied", extra={"event": "change_applied", "hc_book_id": 123})
        log.info("run end", extra={"event": "run_end"})
    log.info("outside", extra={"event": "idle"})

    inner, outer, bare = lines(capsys)
    assert inner["profile"] == "alice"
    assert inner["source"] == "abs"
    assert inner["run_id"] == 7
    assert inner["change_id"] == 42
    assert inner["hc_book_id"] == 123
    assert "change_id" not in outer
    assert outer["run_id"] == 7
    for key in slog.CONTEXT_FIELDS:
        assert key not in bare


def test_log_context_rejects_unknown_fields() -> None:
    with pytest.raises(TypeError), slog.log_context(email="alice@example.com"):  # type: ignore[call-arg]
        pass


def test_stable_fields_cannot_be_overridden_by_extras(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure()
    logging.getLogger("syncshelf.x").warning("m", extra={"event": "e", "level": "DEBUG"})

    [line] = lines(capsys)
    assert line["level"] == "WARNING"


def test_a_configured_secret_in_an_exception_becomes_stars(
    capsys: pytest.CaptureFixture[str],
) -> None:
    slog.configure(secrets=[SECRET])
    log = logging.getLogger("syncshelf.clients.hardcover")
    try:
        raise RuntimeError(f"401 for token {SECRET}")
    except RuntimeError:
        log.exception("call failed with %s", SECRET, extra={"event": "hc_failed"})

    out = capsys.readouterr().out
    assert SECRET not in out
    [line] = [json.loads(x) for x in out.splitlines()]
    assert line["msg"] == "call failed with ***"
    assert "Traceback" in str(line["exc"])
    assert "401 for token ***" in str(line["exc"])


def test_a_configured_host_becomes_its_alias(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure(host_aliases=HOSTS)
    log = logging.getLogger("syncshelf.clients.abs")
    try:
        raise ConnectionError("cannot reach https://ABS.home.arpa:13378/api/me")
    except ConnectionError:
        log.exception("GET to chaptarr.home.arpa failed", extra={"event": "fetch_failed"})

    out = capsys.readouterr().out
    assert "home.arpa" not in out.lower()
    [line] = [json.loads(x) for x in out.splitlines()]
    assert line["msg"] == "GET to chaptarr failed"
    assert "https://abs:13378/api/me" in str(line["exc"])


def test_extras_and_nested_values_are_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure(secrets=[SECRET], host_aliases=HOSTS)
    logging.getLogger("syncshelf.x").warning(
        "m",
        extra={
            "event": "e",
            "error": f"via abs.home.arpa with {SECRET}",
            "detail": {"t": [SECRET]},
        },
    )

    [line] = lines(capsys)
    assert line["error"] == "via abs with ***"
    assert line["detail"] == {"t": ["***"]}


def test_scrub_is_reusable_for_strings_that_will_be_stored() -> None:
    slog.configure_redaction(secrets=[SECRET], host_aliases=HOSTS)
    assert slog.scrub(f"POST http://192.0.2.10:3000/ said {SECRET}") == (
        "POST http://browser:3000/ said ***"
    )


def test_a_live_token_added_later_is_scrubbed() -> None:
    slog.configure_redaction(secrets=[SECRET])
    slog.add_secret("fake-live-token")
    assert slog.scrub("Bearer fake-live-token and " + SECRET) == "Bearer *** and ***"


def test_a_discarded_secret_is_no_longer_scrubbed() -> None:
    slog.configure_redaction(secrets=[SECRET])
    slog.add_secret("fake-live-token")
    slog.discard_secret("fake-live-token")
    slog.discard_secret("never-added")  # a no-op, not an error
    assert slog.scrub("Bearer fake-live-token and " + SECRET) == "Bearer fake-live-token and ***"
    slog.discard_secret(SECRET)
    assert slog.scrub(SECRET) == SECRET


def test_overlapping_secrets_redact_the_longest_match() -> None:
    redactor = slog.Redactor(secrets=["abc", "abcdef", ""])
    assert redactor.scrub("x abcdef y abc") == "x *** y ***"


def test_host_aliasing_does_not_touch_longer_hostnames() -> None:
    redactor = slog.Redactor(host_aliases={"abs.home.arpa": "abs"})
    assert redactor.scrub("xabs.home.arpa.example and abs.home.arpa.") == (
        "xabs.home.arpa.example and abs."
    )


def test_a_secret_containing_a_host_is_still_redacted() -> None:
    redactor = slog.Redactor(secrets=["key@abs.home.arpa"], host_aliases={"abs.home.arpa": "abs"})
    assert redactor.scrub("key@abs.home.arpa") == "***"


def test_level_defaults_to_info(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure()
    log = logging.getLogger("syncshelf.x")
    log.debug("hidden")
    log.info("shown")

    assert [line["msg"] for line in lines(capsys)] == ["shown"]


def test_level_is_configurable(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure(level="debug")
    logging.getLogger("syncshelf.x").debug("shown")

    assert [line["msg"] for line in lines(capsys)] == ["shown"]


def test_an_unknown_level_is_refused() -> None:
    with pytest.raises(ValueError, match="LOUD"):
        slog.configure(level="LOUD")


def test_uvicorn_loggers_reach_the_json_handler(capsys: pytest.CaptureFixture[str]) -> None:
    # As uvicorn would leave them with its default config: own handlers, no propagation.
    for name in ("uvicorn", "uvicorn.access"):
        stray = logging.getLogger(name)
        stray.addHandler(logging.StreamHandler())
        stray.propagate = False
    slog.configure(secrets=[SECRET])
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "192.0.2.1:5", "GET", "/healthz", "1.1", 200
    )
    logging.getLogger("uvicorn.error").error("boom %s", SECRET)

    access, error = lines(capsys)
    assert access["logger"] == "uvicorn.access"
    assert access["msg"] == '192.0.2.1:5 - "GET /healthz HTTP/1.1" 200'
    assert error["msg"] == "boom ***"


def access_record(path: str) -> logging.LogRecord:
    """An access record built exactly as uvicorn's h11 and httptools protocols build it."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("192.0.2.1:5", "GET", path, "1.1", 200),
        None,
    )


def test_the_access_log_drops_query_strings(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure()
    logging.getLogger("uvicorn.access").handle(access_record("/healthz?token=abc&x=1"))

    [line] = lines(capsys)
    assert line["msg"] == '192.0.2.1:5 - "GET /healthz HTTP/1.1" 200'


def test_uvicorns_colour_copy_of_the_message_is_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    slog.configure()
    logging.getLogger("uvicorn.error").info(
        "Started server process [%d]", 7, extra={"color_message": "Started [\x1b[36m%d\x1b[0m]"}
    )

    [line] = lines(capsys)
    assert line["msg"] == "Started server process [7]"
    assert "color_message" not in line


def test_the_query_filter_is_installed_once(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure()
    slog.configure()

    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, slog.AccessQueryFilter) for f in filters) == 1


def test_the_query_filter_leaves_other_records_alone() -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "odd %s", ("a?b",), None)
    assert slog.AccessQueryFilter().filter(record)
    assert record.getMessage() == "odd a?b"


def test_configure_replaces_existing_root_handlers(capsys: pytest.CaptureFixture[str]) -> None:
    # MCPServer() calls logging.basicConfig, which leaves a plain stderr handler on root.
    logging.getLogger().addHandler(logging.StreamHandler())
    slog.configure()
    slog.configure()

    assert len(logging.getLogger().handlers) == 1
    logging.getLogger("x").warning("once")
    assert len(lines(capsys)) == 1


def test_building_the_mcp_server_after_configure_leaves_logging_alone() -> None:
    # __main__ configures logging before create_app builds the MCPServer. Its basicConfig has
    # no force=True, so with a root handler already in place it must change nothing.
    slog.configure(level="WARNING")
    root = logging.getLogger()
    handlers = root.handlers[:]
    MCPServer("test")
    assert root.handlers == handlers
    assert root.level == logging.WARNING


def test_http_client_loggers_stay_quiet_even_at_debug(capsys: pytest.CaptureFixture[str]) -> None:
    # httpx2 logs full request URLs, query strings included, at INFO.
    slog.configure(level="DEBUG")
    logging.getLogger("httpx2").info("HTTP Request: GET https://x.example/?key=1")
    logging.getLogger("httpcore2.http11").debug("send_request_headers")

    assert lines(capsys) == []


def test_a_bad_format_call_still_logs_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    slog.configure(secrets=[SECRET])
    logging.getLogger("syncshelf.x").info("needs %s %s " + SECRET, "one")

    out = capsys.readouterr().out
    assert SECRET not in out
