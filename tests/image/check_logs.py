"""Check a container's ``docker logs`` output (SPEC §3.1 "Done when", §2.7).

CI's image job runs this on the runner's own Python against the started container's logs:

    python3 tests/image/check_logs.py logs.txt

It passes only if every non-empty line is a JSON object carrying the stable fields, and a
``startup`` line gives the version and the SQLite version. ``docker logs`` merges stdout and
stderr, so a stray non-JSON line from anywhere (a library's own handler, a warning) fails it.
Standard library only, so it runs without the project installed.
"""

import json
import sys
from pathlib import Path
from typing import Any

STABLE_FIELDS = ("ts", "level", "logger", "event", "msg")
"""The fields §2.7 puts on every line."""


class LogCheckError(Exception):
    """The logs fail the check. The message says which line and why."""


def check_logs(text: str) -> dict[str, Any]:
    """Validate ``docker logs`` output and return the ``startup`` line.

    Raises :class:`LogCheckError` on the first line that isn't a JSON object with the stable
    fields, or when no ``startup`` line carries a non-empty ``version`` and ``sqlite_version``.
    """
    startup: dict[str, Any] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            raise LogCheckError(f"line {number} is not JSON: {raw[:200]}") from None
        if not isinstance(line, dict):
            raise LogCheckError(f"line {number} is not a JSON object")
        missing = [key for key in STABLE_FIELDS if key not in line]
        if missing:
            raise LogCheckError(f"line {number} lacks {', '.join(missing)}")
        if line["event"] == "startup" and startup is None:
            startup = line
    if startup is None:
        raise LogCheckError("no startup line")
    for key in ("version", "sqlite_version"):
        if not startup.get(key):
            raise LogCheckError(f"the startup line has no {key}")
    return startup


def main(argv: list[str]) -> int:
    """Check the log file named in ``argv[1]``. Returns the process exit code."""
    if len(argv) != 2:
        print("usage: check_logs.py <docker-logs-file>", file=sys.stderr)
        return 2
    try:
        startup = check_logs(Path(argv[1]).read_text(encoding="utf-8"))
    except LogCheckError as exc:
        print(f"log check failed: {exc}", file=sys.stderr)
        return 1
    print(f"ok: version {startup['version']}, SQLite {startup['sqlite_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
