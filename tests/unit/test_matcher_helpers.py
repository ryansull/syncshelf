"""The matcher's normalisation and scoring helpers, run through ``tests/golden/matcher.yaml``
(SPEC §3.4a.5, §2.9 golden sets)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from syncshelf.sync import matcher
from syncshelf.sync.matcher import Scored, author_score, auto_accepts, is_likely, normalise

GOLDEN = yaml.safe_load(
    (Path(__file__).parents[1] / "golden" / "matcher.yaml").read_text(encoding="utf-8")
)


def ids(cases: list[dict[str, Any]], key: str) -> list[str]:
    return [str(case[key]) for case in cases]


@pytest.mark.parametrize("case", GOLDEN["normalise"], ids=ids(GOLDEN["normalise"], "in"))
def test_normalise(case: dict[str, Any]) -> None:
    assert normalise(case["in"]) == case["out"]


def check_bound(score: float, case: dict[str, Any]) -> None:
    if "at_least" in case:
        assert score >= case["at_least"], score
    if "below" in case:
        assert score < case["below"], score


@pytest.mark.parametrize("case", GOLDEN["title_score"], ids=ids(GOLDEN["title_score"], "source"))
def test_title_score(case: dict[str, Any]) -> None:
    check_bound(matcher.title_score(case["source"], case["candidate"]), case)


@pytest.mark.parametrize(
    "case", GOLDEN["author_score"], ids=ids(GOLDEN["author_score"], "candidates")
)
def test_author_score(case: dict[str, Any]) -> None:
    check_bound(author_score(case["source"], case["candidates"]), case)


@pytest.mark.parametrize("case", GOLDEN["decide"], ids=ids(GOLDEN["decide"], "candidates"))
def test_decisions(case: dict[str, Any]) -> None:
    ranked = [Scored(title, author) for title, author in case["candidates"]]
    assert auto_accepts(ranked) is case["auto_accept"]
    assert is_likely(ranked) is case["likely"]


def test_the_thresholds_are_the_spec_starting_values() -> None:
    # Pinned until 4a calibrates them; a change here is a decision, not a refactor.
    assert (matcher.TITLE_FLOOR, matcher.AUTO_ACCEPT_TITLE, matcher.AUTO_ACCEPT_AUTHOR) == (
        60,
        95,
        90,
    )
    assert (matcher.LIKELY_TITLE, matcher.LIKELY_AUTHOR, matcher.MARGIN) == (85, 80, 10)
