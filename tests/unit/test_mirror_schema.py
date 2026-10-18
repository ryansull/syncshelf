"""SQL invariants of ``0003_mirror`` (SPEC §2.4, Appendix A): the database itself refuses a
status or rating Hardcover can't hold, a second user_book for one book, and a row for a
profile that doesn't exist."""

import sqlite3

import pytest

from invariants import assert_accepted, assert_rejected

NOW = 1_767_225_600
USER_BOOK = "INSERT INTO mirror_user_books VALUES (?, ?, ?, ?, ?, NULL, NULL)"


@pytest.fixture
def mirror(schema: sqlite3.Connection) -> sqlite3.Connection:
    assert_accepted(schema, "INSERT INTO profiles (name, created_at) VALUES ('alice', ?)", (NOW,))
    return schema


def test_the_migration_creates_every_mirror_table(schema: sqlite3.Connection) -> None:
    tables = {row[0] for row in schema.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    assert {
        "mirror_state",
        "mirror_books",
        "mirror_user_books",
        "mirror_reads",
        "mirror_list_items",
    } <= tables


@pytest.mark.parametrize("status", [0, 7])
def test_a_status_outside_1_to_6_is_refused(mirror: sqlite3.Connection, status: int) -> None:
    assert_rejected(mirror, USER_BOOK, ("alice", 1, 10, status, None), match="CHECK")
    assert_accepted(mirror, USER_BOOK, ("alice", 1, 10, 6, None))


@pytest.mark.parametrize("rating", [0, 0.25, 4.7, 5.5])
def test_a_rating_off_the_half_star_grid_is_refused(
    mirror: sqlite3.Connection, rating: float
) -> None:
    assert_rejected(mirror, USER_BOOK, ("alice", 1, 10, 3, rating), match="CHECK")


@pytest.mark.parametrize("rating", [None, 0.5, 3, 4.5, 5])
def test_half_star_ratings_and_none_are_accepted(
    mirror: sqlite3.Connection, rating: float | None
) -> None:
    assert_accepted(mirror, USER_BOOK, ("alice", 1, 10, 3, rating))


def test_one_user_book_per_book(mirror: sqlite3.Connection) -> None:
    assert_accepted(mirror, USER_BOOK, ("alice", 1, 10, 3, None))
    assert_rejected(mirror, USER_BOOK, ("alice", 2, 10, 1, None), match="UNIQUE")


def test_a_row_for_an_unknown_profile_is_refused(mirror: sqlite3.Connection) -> None:
    assert_rejected(mirror, USER_BOOK, ("carol", 1, 10, 3, None), match="FOREIGN KEY")
