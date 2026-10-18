"""The private denylist pre-commit hook (SPEC §2.6)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_denylist.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_denylist", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_denylist = load()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clean.py").write_text("HOST = 'shelf.example.com'\n", encoding="utf-8")
    (tmp_path / "leaky.md").write_text("Deploy to Shelf.RealDomain.Test today\n", encoding="utf-8")
    return tmp_path


def write_denylist(repo: Path, text: str) -> None:
    (repo / "local").mkdir()
    (repo / "local" / "denylist.txt").write_text(text, encoding="utf-8")


def test_passes_silently_without_a_denylist(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_denylist.main(["clean.py", "leaky.md"]) == 0
    assert capsys.readouterr() == ("", "")


def test_passes_files_without_a_listed_value(repo: Path) -> None:
    write_denylist(repo, "shelf.realdomain.test\n")
    assert check_denylist.main(["clean.py"]) == 0


def test_blocks_a_file_containing_a_listed_value_case_insensitively(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_denylist(repo, "shelf.realdomain.test\n")
    assert check_denylist.main(["clean.py", "leaky.md"]) == 1
    err = capsys.readouterr().err
    assert "leaky.md" in err
    assert "clean.py" not in err


def test_never_prints_the_listed_value(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_denylist(repo, "shelf.realdomain.test\n")
    check_denylist.main(["leaky.md"])
    out, err = capsys.readouterr()
    assert "realdomain" not in (out + err).lower()
    assert "line 1" in err


def test_ignores_blank_lines_and_comments(repo: Path) -> None:
    write_denylist(repo, "# my real values\n\n   \n")
    assert check_denylist.main(["clean.py", "leaky.md"]) == 0


def test_reads_binary_files_without_crashing(repo: Path) -> None:
    write_denylist(repo, "realdomain\n")
    (repo / "blob.bin").write_bytes(b"\xff\xfe realdomain \x00")
    assert check_denylist.main(["blob.bin"]) == 1


def test_skips_paths_that_are_not_files(repo: Path) -> None:
    write_denylist(repo, "realdomain\n")
    assert check_denylist.main(["missing.txt"]) == 0
