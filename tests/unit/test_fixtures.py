"""Every committed fixture passes the scrubber's checker (SPEC §2.9)."""

from pathlib import Path

from scrub import check_tree

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_every_committed_fixture_is_scrubbed_and_labelled() -> None:
    problems = check_tree(FIXTURES)
    assert problems == {}, "\n".join(p for ps in problems.values() for p in ps)
