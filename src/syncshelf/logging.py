"""JSON-lines logging with request/run context, host aliasing and secret scrubbing (SPEC §2.7).

The goal is that any log line, and any stored error string, can be pasted into a public issue.
This module takes plain inputs (a level, secret values, a host → alias map) and never reads
config itself, so the caller wires it up once at startup::

    configure(level=cfg.log_level, secrets=cfg.secret_values, host_aliases=cfg.host_aliases)
    uvicorn.run(app, ..., log_config=None)  # uvicorn's loggers then propagate to our handler

``scrub`` is the same redaction for strings that are about to be stored, and ``add_secret``
registers a token that only exists at runtime (an issued access token, a session cookie).
"""

import json
import logging
import re
import sys
import threading
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "CONTEXT_FIELDS",
    "PROPAGATING_LOGGERS",
    "QUIET_LOGGERS",
    "JsonFormatter",
    "Redactor",
    "ScrubFilter",
    "add_secret",
    "configure",
    "configure_redaction",
    "log_context",
    "scrub",
]

REDACTED = "***"

CONTEXT_FIELDS: tuple[str, ...] = (
    "profile",
    "source",
    "run_id",
    "change_id",
    "tool",
    "request_id",
)
"""Fields added to every line from contextvars while ``log_context`` has them set."""

PROPAGATING_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
)
"""Third-party loggers stripped of their own handlers so their lines go through ours."""

QUIET_LOGGERS: tuple[str, ...] = ("httpx2", "httpcore2")
"""Held at WARNING at every level: httpx2 logs full request URLs, query strings included."""

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_context: dict[str, ContextVar[str | int | None]] = {
    name: ContextVar(f"syncshelf_log_{name}", default=None) for name in CONTEXT_FIELDS
}

# Everything a bare LogRecord carries; anything else on a record came from ``extra=``.
_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys() | {"message", "asctime"}
)
_STABLE = ("ts", "level", "logger", "event", "msg")


class Redactor:
    """Rewrites configured hosts to their alias and replaces secret values with ``***``.

    Secrets are replaced first, so a secret that happens to contain a host is still matched
    whole. Hosts match case-insensitively and only as a whole hostname, never inside a longer
    one. Empty secrets are ignored.
    """

    def __init__(
        self, secrets: Iterable[str] = (), host_aliases: Mapping[str, str] | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = {s for s in secrets if s}
        self._aliases = {
            host.lower(): alias for host, alias in (host_aliases or {}).items() if host
        }
        self._secret_re = self._compile_secrets()
        self._host_re = self._compile_hosts()

    def add_secret(self, value: str) -> None:
        """Start redacting ``value`` too. Safe to call from any thread."""
        if not value or value in self._secrets:
            return
        with self._lock:
            self._secrets.add(value)
            self._secret_re = self._compile_secrets()

    def scrub(self, text: str) -> str:
        """Return ``text`` with every secret replaced and every configured host aliased."""
        secret_re = self._secret_re
        if secret_re is not None:
            text = secret_re.sub(REDACTED, text)
        if self._host_re is not None:
            text = self._host_re.sub(lambda m: self._aliases[m.group(0).lower()], text)
        return text

    def _compile_secrets(self) -> re.Pattern[str] | None:
        if not self._secrets:
            return None
        # Longest first, so a secret that contains a shorter one is replaced whole.
        ordered = sorted(self._secrets, key=len, reverse=True)
        return re.compile("|".join(re.escape(s) for s in ordered))

    def _compile_hosts(self) -> re.Pattern[str] | None:
        if not self._aliases:
            return None
        ordered = sorted(self._aliases, key=len, reverse=True)
        hosts = "|".join(re.escape(h) for h in ordered)
        # A trailing dot that ends a sentence is fine; one that continues a hostname is not.
        return re.compile(rf"(?<![\w.-])(?:{hosts})(?![\w-]|\.[\w-])", re.IGNORECASE)


_redactor = Redactor()


def configure_redaction(
    secrets: Iterable[str] = (), host_aliases: Mapping[str, str] | None = None
) -> None:
    """Replace the process-wide redaction rules used by ``scrub`` and the log filter."""
    global _redactor
    _redactor = Redactor(secrets, host_aliases)


def add_secret(value: str) -> None:
    """Redact a value that only exists at runtime, such as a freshly issued token."""
    _redactor.add_secret(value)


def scrub(text: str) -> str:
    """Redact ``text`` with the process-wide rules: for log lines and for stored error strings."""
    return _redactor.scrub(text)


@contextmanager
def log_context(
    *,
    profile: str | None = None,
    source: str | None = None,
    run_id: str | int | None = None,
    change_id: str | int | None = None,
    tool: str | None = None,
    request_id: str | None = None,
) -> Iterator[None]:
    """Add the given fields to every line logged inside the block, in this task or thread.

    Unset (``None``) fields leave any outer value in place. Nested blocks stack.
    """
    given = {
        "profile": profile,
        "source": source,
        "run_id": run_id,
        "change_id": change_id,
        "tool": tool,
        "request_id": request_id,
    }
    tokens: list[tuple[ContextVar[str | int | None], Token[str | int | None]]] = []
    for name, value in given.items():
        if value is not None:
            var = _context[name]
            tokens.append((var, var.set(value)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _scrub_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {str(k): _scrub_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_scrub_value(v) for v in value]
    return scrub(str(value))


class ScrubFilter(logging.Filter):
    """The final handler filter: attaches context fields, then scrubs everything on the record.

    The message is rendered and scrubbed in place, the traceback is rendered to ``exc_text``
    and scrubbed, and every ``extra=`` value is scrubbed. It sits on the handler, not a logger,
    so records propagated from any child logger pass through it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _context.items():
            value = var.get()
            if value is not None and name not in record.__dict__:
                setattr(record, name, value)
        try:
            message = record.getMessage()
        except Exception:
            # A bad format call must still log, and must still be scrubbed.
            message = f"{record.msg} {record.args!r}"
        record.msg = scrub(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = scrub(record.exc_text)
        if record.stack_info:
            record.stack_info = scrub(record.stack_info)
        for key in record.__dict__.keys() - _RECORD_ATTRS:
            record.__dict__[key] = _scrub_value(record.__dict__[key])
        return True


def _event_name(logger_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", logger_name.lower()).strip("_") or "log"


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ``ts``, ``level``, ``logger``, ``event``, ``msg``, then extras.

    ``event`` comes from ``extra={"event": ...}``. Lines without one (uvicorn's, a library's)
    get the logger name in snake_case. Extras never override the stable fields. Pair it with
    ``ScrubFilter``; the formatter itself does no redaction.
    """

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": str(record.__dict__.get("event") or _event_name(record.name)),
            "msg": record.getMessage(),
        }
        for key in sorted(record.__dict__.keys() - _RECORD_ATTRS):
            if key not in _STABLE:
                line[key] = record.__dict__[key]
        if record.exc_text:
            line["exc"] = record.exc_text
        elif record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            line["stack"] = record.stack_info
        return json.dumps(line, ensure_ascii=False, default=str)


def configure(
    level: str = "INFO",
    secrets: Iterable[str] = (),
    host_aliases: Mapping[str, str] | None = None,
) -> None:
    """Send all logging to stdout as scrubbed JSON lines at ``level`` (``server.log_level``).

    Replaces root's handlers (``MCPServer`` adds a stderr one via ``basicConfig``), so call it
    after building the MCP server and pass ``log_config=None`` to uvicorn so it leaves logging
    alone. Calling it again replaces the previous setup. Raises ``ValueError`` for an unknown
    level name.
    """
    name = level.upper()
    if name not in _LEVELS:
        raise ValueError(f"unknown log level {level!r}; expected one of {', '.join(_LEVELS)}")
    configure_redaction(secrets, host_aliases)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ScrubFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(name)

    for logger_name in PROPAGATING_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
    for logger_name in QUIET_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
