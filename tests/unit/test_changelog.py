"""scripts/changelog.py: fragments render in CHANGELOG.md's existing format (#139)."""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "changelog.py"


@pytest.fixture(scope="module")
def changelog() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changelog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _released_section(text: str, version: str) -> str:
    """The section from its heading to the next one, ending in exactly one blank line."""
    start = text.index(f"## [{version}]")
    end = text.find("\n## [", start)
    return text[start : end if end != -1 else len(text)].rstrip("\n") + "\n\n"


def _bullets(body: str) -> list[str]:
    """Split a section body into its bullets, each with its continuation lines."""
    return [b.rstrip("\n") for b in re.split(r"\n(?=- )", body.strip("\n"))]


def _explode(section: str, directory: Path) -> tuple[str | None, int]:
    """Write one fragment per bullet of a released section; return its title and count."""
    lines = section.split("\n")
    title = lines[2] if not lines[2].startswith("### ") else None
    count = 0
    for heading, body in re.findall(r"^### (\w+)\n\n(.*?)(?=\n### |\Z)", section, re.S | re.M):
        for bullet in _bullets(body):
            count += 1
            (directory / f"{count}.{heading.lower()}.md").write_text(
                bullet + "\n", encoding="utf-8"
            )
    return title, count


@pytest.mark.parametrize("version", ["0.1.0", "0.2.0"])
def test_released_sections_round_trip_through_fragments(
    changelog: ModuleType, tmp_path: Path, version: str
) -> None:
    section = _released_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    date = section.split("\n", 1)[0].rsplit(" - ", 1)[1]
    title, count = _explode(section, tmp_path)
    fragments, errors = changelog.read_fragments(tmp_path)
    assert errors == []
    assert len(fragments) == count
    assert changelog.render(fragments, version, date, title) == section


def test_sections_follow_keep_a_changelog_order(changelog: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "3.added.md").write_text("- Third.\n", encoding="utf-8")
    (tmp_path / "1.fixed.md").write_text("- First.\n", encoding="utf-8")
    (tmp_path / "2.added.md").write_text("- Second.\n", encoding="utf-8")
    (tmp_path / "4.removed.md").write_text("- Fourth.\n", encoding="utf-8")
    fragments, _ = changelog.read_fragments(tmp_path)
    assert changelog.render(fragments, "0.3.0", "2026-11-01", None) == (
        "## [0.3.0] - 2026-11-01\n\n"
        "### Added\n\n- Second.\n- Third.\n\n"
        "### Removed\n\n- Fourth.\n\n"
        "### Fixed\n\n- First.\n\n"
    )


@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        ("77.feature.md", "- x\n", "name must be"),
        ("pr.added.md", "- x\n", "name must be"),
        ("77.added.txt", "- x\n", "name must be"),
        ("77.added.md", "\n", "is empty"),
        ("77.added.md", "Text\n", 'must start with a "- " bullet'),
        ("77.added.md", "- a\n\n- b\n", "is blank"),
        ("77.added.md", "- a\nb\n", "two-space continuation"),
        ("77.added.md", "- a \n", "trailing whitespace"),
    ],
)
def test_malformed_fragments_are_reported(
    changelog: ModuleType, tmp_path: Path, name: str, text: str, problem: str
) -> None:
    (tmp_path / name).write_text(text, encoding="utf-8")
    _, errors = changelog.read_fragments(tmp_path)
    assert len(errors) == 1 and problem in errors[0] and errors[0].startswith(name)


def test_the_readme_is_not_a_fragment(changelog: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# How to write a fragment\n", encoding="utf-8")
    assert changelog.read_fragments(tmp_path) == ([], [])


def test_build_writes_the_release_and_deletes_the_fragments(
    changelog: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "README.md").write_text("kept\n", encoding="utf-8")
    (fragments / "9.added.md").write_text("- New thing.\n", encoding="utf-8")
    log = tmp_path / "CHANGELOG.md"
    log.write_text(
        "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-17\n\nOld.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(changelog, "FRAGMENT_DIR", fragments)
    monkeypatch.setattr(changelog, "CHANGELOG", log)
    changelog.build("0.2.0", "2026-10-18", "Phase 2: Front door and profiles.")
    assert log.read_text(encoding="utf-8") == (
        "# Changelog\n\nIntro.\n\n## [Unreleased]\n\n"
        "## [0.2.0] - 2026-10-18\n\nPhase 2: Front door and profiles.\n\n"
        "### Added\n\n- New thing.\n\n"
        "## [0.1.0] - 2026-10-17\n\nOld.\n"
    )
    assert [p.name for p in fragments.iterdir()] == ["README.md"]


def test_build_refuses_a_non_empty_unreleased(
    changelog: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "9.added.md").write_text("- New thing.\n", encoding="utf-8")
    log = tmp_path / "CHANGELOG.md"
    log.write_text("## [Unreleased]\n\n### Added\n\n- Stray.\n", encoding="utf-8")
    monkeypatch.setattr(changelog, "FRAGMENT_DIR", fragments)
    monkeypatch.setattr(changelog, "CHANGELOG", log)
    with pytest.raises(SystemExit, match="isn't empty"):
        changelog.build("0.2.0", "2026-10-18", None)
    assert (fragments / "9.added.md").exists()


def test_the_repo_passes_its_own_check(changelog: ModuleType) -> None:
    changelog.check()
