"""Pre-commit hook: refuse files that contain a value from the private denylist (SPEC §2.6).

``local/denylist.txt`` is git-ignored and lists one real value per line (a domain, an email, a
LAN hostname). Blank lines and lines starting with ``#`` are ignored, and matching ignores case.
Without the file the hook passes silently, so contributors and CI need nothing.

Findings name the file and the denylist line number, never the value, so the output itself can
be pasted anywhere.
"""

import sys
from pathlib import Path

DENYLIST = Path("local") / "denylist.txt"


def load_denylist(path: Path = DENYLIST) -> list[tuple[int, str]]:
    """Return ``(line number, lowercased value)`` for each entry, or nothing if absent."""
    if not path.is_file():
        return []
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        value = line.strip()
        if value and not value.startswith("#"):
            entries.append((number, value.lower()))
    return entries


def main(argv: list[str]) -> int:
    """Check each file named in ``argv``; return 1 if any contains a denylisted value."""
    entries = load_denylist()
    if not entries:
        return 0
    found = False
    for name in argv:
        path = Path(name)
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="replace").lower()
        for number, value in entries:
            if value in text:
                print(f"{name}: contains the value on line {number} of {DENYLIST}", file=sys.stderr)
                found = True
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
