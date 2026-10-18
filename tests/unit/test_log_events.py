"""Every SyncShelf log call names its ``event`` explicitly (SPEC §2.7, issue #47).

``JsonFormatter`` falls back to the logger name in snake_case when a record has no ``event``.
That fallback is for third-party loggers (uvicorn, libraries). A ``syncshelf.*`` line that
relied on it would get an event like ``syncshelf_app``, which names a module, not what
happened, and would change if the module moved.

Why a static scan rather than a runtime check in tests: a runtime check only sees the log
calls a test happens to drive, and the calls most likely to be missed are on error branches
that tests rarely reach. Scanning the source checks every call, run or not, and leaves
production behaviour alone: a missing event still logs with the fallback, it just fails CI.
The cost is that the event must be visible in the source, so ``extra`` has to be a dict
literal at the call site with an ``"event"`` key.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "syncshelf"

LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "fatal", "log"}
)
# Receivers that are loggers by name: ``log``, ``logger``, ``_log``, ``self._logger``,
# ``auth_log``, and the ``logging`` module itself (whose calls go to the root logger).
# Anchored so ``catalog.error(...)`` is not mistaken for one.
LOGGER_NAME = re.compile(r"(?:^|_)(?:log|logger)$|^logging$", re.IGNORECASE)
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Problem:
    path: str
    line: int
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}"


def _receiver_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_get_logger(node: ast.expr) -> bool:
    return isinstance(node, ast.Call) and _receiver_name(node.func) == "getLogger"


def _is_log_call(call: ast.Call) -> bool:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in LOG_METHODS:
        return False
    if _is_get_logger(func.value):
        return True
    name = _receiver_name(func.value)
    return name is not None and bool(LOGGER_NAME.search(name))


def _check_call(call: ast.Call) -> str | None:
    extra = next((kw.value for kw in call.keywords if kw.arg == "extra"), None)
    if extra is None:
        return 'no extra={"event": ...}'
    if not isinstance(extra, ast.Dict):
        return 'extra is not a dict literal, so its "event" cannot be checked'
    for key, value in zip(extra.keys, extra.values, strict=True):
        if isinstance(key, ast.Constant) and key.value == "event":
            if isinstance(value, ast.Constant) and not (
                isinstance(value.value, str) and SNAKE_CASE.match(value.value)
            ):
                return f"event {value.value!r} is not a snake_case name"
            return None
    return 'extra has no "event" key'


def scan(source: str, path: str = "<string>") -> tuple[int, list[Problem]]:
    """Return how many log calls ``source`` makes and the ones without an explicit event."""
    calls = 0
    problems: list[Problem] = []
    for node in ast.walk(ast.parse(source, filename=path)):
        if isinstance(node, ast.Call) and _is_log_call(node):
            calls += 1
            reason = _check_call(node)
            if reason is not None:
                problems.append(Problem(path, node.lineno, reason))
    return calls, problems


def test_every_syncshelf_log_call_has_an_explicit_event() -> None:
    calls = 0
    problems: list[Problem] = []
    for file in sorted(SRC.rglob("*.py")):
        found, bad = scan(file.read_text(encoding="utf-8"), str(file.relative_to(SRC.parent)))
        calls += found
        problems += bad
    # Guards against the scan silently matching nothing (a renamed logger, a moved tree).
    assert calls > 0, f"found no log calls under {SRC}"
    assert not problems, "log calls without an explicit event:\n" + "\n".join(map(str, problems))


@pytest.mark.parametrize(
    "source",
    [
        'log.info("started")',
        'logger.warning("x %s", 1, exc_info=True)',
        'self._log.error("x", extra={"profile": "p"})',
        'logging.info("to root")',
        'logging.getLogger(__name__).debug("chained")',
        'log.log(20, "x", extra=fields)',
        'log.info("x", extra={**base})',
        'log.info("x", extra={"event": "Not Snake"})',
        'log.info("x", extra={"event": ""})',
    ],
)
def test_scan_flags_a_call_without_an_explicit_event(source: str) -> None:
    calls, problems = scan(source)
    assert calls == 1
    assert len(problems) == 1


@pytest.mark.parametrize(
    "source",
    [
        'log.info("x", extra={"event": "startup"})',
        'auth_log.exception("x", extra={"event": "token_refresh_failed", "grant_id": 3})',
        'log.info("x", extra={"event": name, "n": 1})',
    ],
)
def test_scan_accepts_an_explicit_event(source: str) -> None:
    assert scan(source) == (1, [])


@pytest.mark.parametrize(
    "source", ['catalog.error("x")', "math.log(2)", 'dialog.info("x")', 'errors.warning("x")']
)
def test_scan_ignores_calls_on_things_that_are_not_loggers(source: str) -> None:
    assert scan(source) == (0, [])
