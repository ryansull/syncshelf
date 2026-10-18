"""``FakeHardcover`` answers in the shape the live API was recorded in (SPEC §2.9).

The shape is every key path with the JSON types seen there. Wherever both the fake and the
recording have an object, its keys must match exactly, and each non-null type the fake sends must
have been seen live. A path only one side reaches (an empty list, a null object) isn't compared,
and null is allowed anywhere: a handful of recorded rows can't show every field's nullability.
The ``jsonb`` columns
Hardcover caches (contributors, featured series, tags) are compared only as their top-level
type, because their insides vary from book to book and SyncShelf reads only a few keys.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from syncshelf.clients.hardcover import GOALS, LIBRARY, ME, HardcoverClient

from fakes.hardcover import URL, FakeHardcover

FIXTURES = Path(__file__).parents[1] / "fixtures" / "hardcover"
OPAQUE = frozenset({"cached_contributors", "cached_featured_series", "genres", "moods", "tags"})

pytestmark = pytest.mark.anyio

type Shape = dict[str, set[str]]
"""Each path's JSON types, plus ``<path>{}`` → the keys seen on the object at that path."""


def shape(value: Any, path: str = "$", out: Shape | None = None) -> Shape:
    out = {} if out is None else out
    kind = "null" if value is None else type(value).__name__
    out.setdefault(path, set()).add("number" if kind in {"int", "float"} else kind)
    if path.rsplit(".", 1)[-1] in OPAQUE:
        return out
    if isinstance(value, dict):
        out.setdefault(f"{path}{{}}", set()).update(value)
        for key, item in value.items():
            shape(item, f"{path}.{key}", out)
    elif isinstance(value, list):
        for item in value:
            shape(item, f"{path}[]", out)
    return out


def recorded(name: str) -> Shape:
    return shape(json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["body"])


def assert_same_shape(fake: Shape, live: Shape) -> None:
    for path in sorted(set(fake) & set(live)):
        if path.endswith("{}"):
            assert fake[path] == live[path], (
                f"{path[:-2]}: only in the fake {sorted(fake[path] - live[path])}, "
                f"only live {sorted(live[path] - fake[path])}"
            )
            continue
        live_kinds = live[path] - {"null"}
        extra = fake[path] - {"null"} - live_kinds
        assert not (live_kinds and extra), f"{path}: the fake sends {extra}, live sent {live_kinds}"


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user("t", user_id=42)
    fake.add_user_book(
        42,
        7,
        status_id=3,
        rating=4.5,
        user_book_reads=[
            {"id": 1, "started_at": "2026-01-02", "edition_id": 70, "reading_format_id": 2}
        ],
        book={
            "cached_featured_series": {"id": 1, "series": {"id": 2, "name": "S"}, "position": 1.0}
        },
    )
    fake.add_user_book(42, 8)
    fake.add_list(42, "Favourites", [7])
    return fake


async def test_me_matches_the_recording(fake: FakeHardcover) -> None:
    result = await HardcoverClient(URL, "t", gate=None, transport=fake.transport).execute(ME)
    assert_same_shape(shape({"data": result.data}), recorded("me"))


async def test_the_library_query_matches_the_recording(fake: FakeHardcover) -> None:
    client = HardcoverClient(URL, "t", gate=None, transport=fake.transport)
    variables = {"user_id": 42, "limit": 1000, "after": 0, "lists_after": 0}
    result = await client.execute(LIBRARY, variables)
    assert_same_shape(shape({"data": result.data}), recorded("library"))


async def test_goals_match_the_recording(fake: FakeHardcover) -> None:
    fake.goals[42] = [
        {"metric": "book", "goal": 30, "progress": 12.0, "start_date": "2026-01-01",
         "end_date": "2026-12-31", "state": "active"}
    ]  # fmt: skip
    client = HardcoverClient(URL, "t", gate=None, transport=fake.transport)
    result = await client.execute(GOALS)
    assert_same_shape(shape({"data": result.data}), recorded("goals"))
