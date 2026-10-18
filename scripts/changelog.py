"""Assemble ``changelog.d/`` fragments into ``CHANGELOG.md`` at release (SPEC §2.10, #139).

Each PR with a user- or operator-visible change adds one fragment instead of editing
``CHANGELOG.md``, so parallel PRs never conflict on it. A fragment is named
``<issue-or-pr>.<type>.md``, where the type is the *Keep a Changelog* section it lands in, and
holds the bullet(s) exactly as they should read. The release-prep PR runs ``build``, which
writes them under the new version heading and deletes them, so ``## [Unreleased]`` stays
empty on ``main``.

Usage::

    python scripts/changelog.py check
    python scripts/changelog.py build --version 0.3.0 --date 2026-11-01 --title "Phase 3: Hardcover"
"""

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"

SECTIONS = ("added", "changed", "removed", "fixed")
"""The fragment types, in the order *Keep a Changelog* lists its sections."""

UNRELEASED = "## [Unreleased]"
NAME = re.compile(r"^(?P<number>[1-9][0-9]*)\.(?P<type>[a-z]+)\.md$")
IGNORED = {"README.md", ".gitkeep"}


@dataclass(frozen=True)
class Fragment:
    path: Path
    number: int
    type: str
    text: str
    """The bullet lines, with no trailing newline."""


def read_fragments(directory: Path | None = None) -> tuple[list[Fragment], list[str]]:
    """Every fragment in ``directory`` (``changelog.d/``), and a message per malformed one."""
    directory = FRAGMENT_DIR if directory is None else directory
    fragments: list[Fragment] = []
    errors: list[str] = []
    for path in sorted(directory.iterdir()) if directory.is_dir() else []:
        if path.name in IGNORED:
            continue
        match = NAME.match(path.name)
        if match is None or match["type"] not in SECTIONS:
            errors.append(f"{path.name}: name must be <issue-or-pr>.<{'|'.join(SECTIONS)}>.md")
            continue
        text = path.read_text(encoding="utf-8").strip("\n")
        errors.extend(f"{path.name}: {problem}" for problem in _problems(text))
        fragments.append(Fragment(path, int(match["number"]), match["type"], text))
    return fragments, errors


def _problems(text: str) -> list[str]:
    lines = text.split("\n")
    if not text.strip():
        return ["is empty"]
    problems = []
    if not lines[0].startswith("- "):
        problems.append('must start with a "- " bullet')
    for n, line in enumerate(lines, 1):
        if not line.strip():
            problems.append(f"line {n} is blank; bullets in a section aren't separated")
        elif n > 1 and not line.startswith(("- ", "  ")):
            problems.append(f'line {n} must be a "- " bullet or a two-space continuation')
        if line != line.rstrip():
            problems.append(f"line {n} has trailing whitespace")
    return problems


def render(fragments: list[Fragment], version: str, date: str, title: str | None) -> str:
    """The ``## [version] - date`` section the fragments make, ending in a blank line."""
    parts = [f"## [{version}] - {date}\n"]
    if title:
        parts.append(f"{title}\n")
    ordered = sorted(fragments, key=lambda f: (f.number, f.path.name))
    for section in SECTIONS:
        bullets = [f.text for f in ordered if f.type == section]
        if bullets:
            parts.append(f"### {section.capitalize()}\n")
            parts.append("\n".join(bullets) + "\n")
    return "\n".join(parts) + "\n"


def unreleased_body(changelog: str) -> str:
    """What sits between ``## [Unreleased]`` and the next version heading."""
    start = changelog.index(UNRELEASED) + len(UNRELEASED)
    end = changelog.find("\n## [", start)
    return changelog[start : end if end != -1 else len(changelog)]


def build(version: str, date: str, title: str | None) -> None:
    fragments, errors = read_fragments()
    if errors:
        sys.exit("\n".join(errors))
    if not fragments:
        sys.exit("changelog.d/ has no fragments to release")
    changelog = CHANGELOG.read_text(encoding="utf-8")
    if unreleased_body(changelog).strip():
        sys.exit(f"{UNRELEASED} in CHANGELOG.md isn't empty; move its lines into fragments")
    if f"## [{version}]" in changelog:
        sys.exit(f"CHANGELOG.md already has a [{version}] section")
    head, _, tail = changelog.partition(UNRELEASED + "\n")
    section = render(fragments, version, date, title)
    CHANGELOG.write_text(
        head + UNRELEASED + "\n\n" + section + tail.lstrip("\n"), encoding="utf-8", newline="\n"
    )
    for fragment in fragments:
        fragment.path.unlink()
    print(f"wrote [{version}] from {len(fragments)} fragments and deleted them")


def check() -> None:
    _, errors = read_fragments()
    if unreleased_body(CHANGELOG.read_text(encoding="utf-8")).strip():
        errors.append(f"CHANGELOG.md: {UNRELEASED} must stay empty; add a fragment instead")
    if errors:
        sys.exit("\n".join(errors))
    print("changelog fragments OK")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate fragments and the empty [Unreleased]")
    release = commands.add_parser("build", help="write a release section from the fragments")
    release.add_argument("--version", required=True, help="e.g. 0.3.0 (no v)")
    release.add_argument("--date", required=True, help="YYYY-MM-DD")
    release.add_argument("--title", help='the phase line, e.g. "Phase 3: Hardcover."')
    args = parser.parse_args(argv)
    if args.command == "check":
        check()
    else:
        build(args.version, args.date, args.title)


if __name__ == "__main__":
    main()
