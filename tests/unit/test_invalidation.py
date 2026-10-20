"""Links staying right when Hardcover or a profile disagrees (SPEC §3.4a.4 rules 6 and 7): a
merge followed on any fetch, a vanished book re-queued, and a per-profile conflict queued for
that profile only."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from syncshelf.sync import identity

from sync_world import GR_USERS, HC_USERS, World, world

pytestmark = pytest.mark.anyio

DUNE = 100


@pytest.fixture
async def w(tmp_path: Path) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USERS["alice"], 7, status_id=3)
        w.hardcover.add_mapping(DUNE, 7, title="Dune")
        w.goodreads.add_item(GR_USERS["alice"], DUNE, "Dune")
        await w.sync.run_rss("alice")
        w.clock.advance(3600)
        yield w


async def where(w: World) -> tuple[list[tuple[int, ...]], list[tuple[int, ...]]]:
    links = await w.rows("SELECT hc_book_id FROM links")
    rows = await w.rows("SELECT hc_book_id FROM source_rows")
    return links, rows


def merge(w: World, old: int, into: int) -> None:
    (ub,) = [u for u in w.hardcover.user_books[HC_USERS["alice"]] if u["book_id"] == old]
    ub["book"]["canonical_id"] = into


async def test_a_mirror_refresh_follows_a_merge(w: World) -> None:
    merge(w, 7, 70)
    await w.mirror.fetch("alice")
    assert await where(w) == ([(70,)], [(70,)])
    assert await w.rows(
        "SELECT source_id, kind, old_value, new_value FROM change_log WHERE kind = 'merge'"
    ) == [(str(DUNE), "merge", "7", "70")]
    # Followed, not queued: the row is still linked.
    assert await w.rows("SELECT state FROM source_rows") == [("linked",)]


async def test_a_write_through_follows_a_merge_too(w: World) -> None:
    merge(w, 7, 70)
    (ub,) = w.hardcover.user_books[HC_USERS["alice"]]
    await w.mirror.write_through("alice", 7, ub)
    assert await where(w) == ([(70,)], [(70,)])


async def test_a_merge_seen_twice_is_followed_once(w: World) -> None:
    merge(w, 7, 70)
    await w.mirror.fetch("alice")
    await w.mirror.fetch("alice")
    assert await w.rows("SELECT count(*) FROM change_log WHERE kind = 'merge'") == [(1,)]


async def test_a_vanished_book_makes_its_link_stale_and_queues_the_row(w: World) -> None:
    assert await w.db.write(identity.mark_stale, {7}, int(w.clock.time())) == 1
    assert await w.rows("SELECT state FROM links") == [("stale",)]
    assert await w.rows("SELECT state, hc_book_id, queue_reason FROM source_rows") == [
        ("queued", None, "stale")
    ]
    assert await w.rows("SELECT kind, hc_book_id FROM change_log") == [("stale", 7)]


async def test_a_hydrate_that_misses_a_linked_book_queues_it(w: World) -> None:
    # A new book's fuzzy search shows Dune, but Hardcover no longer has it by id.
    w.hardcover.add_search_doc(7, "Dune Again", author_names=["Ann Author"])
    del w.hardcover.catalog[7]
    w.goodreads.add_item(GR_USERS["alice"], 500, "Dune Again")
    await w.sync.run_rss("alice")
    assert await w.rows("SELECT source_id, queue_reason FROM source_rows ORDER BY source_id") == [
        (str(DUNE), "stale"),
        ("500", "no_hit"),
    ]


async def test_a_profile_whose_shelves_contradict_the_shared_link_is_queued_alone(
    tmp_path: Path,
) -> None:
    async with world(tmp_path, others=["bob"]) as w:
        # A person linked Goodreads' Dune to book 7; bob's own shelves have it as book 9.
        await w.db.write(
            lambda conn: conn.execute(
                "INSERT INTO links (source, source_id, hc_book_id, method, linked_at, "
                "last_seen_at) VALUES ('goodreads', ?, 7, 'human', 0, 0)",
                (str(DUNE),),
            )
        )
        w.hardcover.add_user_book(HC_USERS["alice"], 7, status_id=3)
        w.hardcover.add_user_book(HC_USERS["bob"], 9, status_id=3)
        w.hardcover.add_mapping(DUNE, 9, title="Dune")
        for name in ("alice", "bob"):
            w.goodreads.add_item(GR_USERS[name], DUNE, "Dune")
            await w.sync.run_rss(name)
        assert await w.rows(
            "SELECT profile, state, hc_book_id, queue_reason FROM source_rows ORDER BY profile"
        ) == [("alice", "linked", 7, None), ("bob", "queued", None, "profile_conflict")]
        assert await w.rows("SELECT hc_book_id, method FROM links") == [(7, "human")]
        assert await w.rows(
            "SELECT rank, hc_book_id FROM match_candidates WHERE profile = 'bob' ORDER BY rank"
        ) == [(1, 9), (2, 7)]


async def test_a_merge_the_mapping_step_shows_is_followed(tmp_path: Path) -> None:
    async with world(tmp_path, others=["bob"]) as w:
        # Alice linked Goodreads' Dune to book 7; Hardcover has since merged 7 into 70.
        w.hardcover.add_user_book(HC_USERS["alice"], 7, status_id=3)
        w.hardcover.add_mapping(DUNE, 7, title="Dune")
        w.goodreads.add_item(GR_USERS["alice"], DUNE, "Dune")
        await w.sync.run_rss("alice")
        w.hardcover.catalog[7]["canonical_id"] = 70
        # Bob's first sighting of Dune Messiah looks the mapping step up, which shows it.
        w.hardcover.add_user_book(HC_USERS["bob"], 70, status_id=3)
        w.hardcover.add_mapping(200, 7, title="Dune")
        w.goodreads.add_item(GR_USERS["bob"], 200, "Dune")
        await w.sync.run_rss("bob")
        assert await w.rows("SELECT DISTINCT hc_book_id FROM source_rows") == [(70,)]
        assert await w.rows("SELECT hc_book_id FROM links ORDER BY source_id") == [(70,), (70,)]


async def test_a_link_on_a_merged_away_book_is_not_a_conflict(tmp_path: Path) -> None:
    async with world(tmp_path, others=["bob"]) as w:
        await w.db.write(
            lambda conn: conn.execute(
                "INSERT INTO links (source, source_id, hc_book_id, method, linked_at, "
                "last_seen_at) VALUES ('goodreads', ?, 7, 'gr_mapping', 0, 0)",
                (str(DUNE),),
            )
        )
        # Book 7 was merged into 70, which is what bob has shelved.
        w.hardcover.add_mapping(DUNE, 7, title="Dune")
        w.hardcover.catalog[7]["canonical_id"] = 70
        w.hardcover.add_user_book(HC_USERS["bob"], 70, status_id=3)
        w.goodreads.add_item(GR_USERS["bob"], DUNE, "Dune")
        await w.sync.run_rss("bob")
        assert await w.rows("SELECT state, hc_book_id FROM source_rows") == [("linked", 70)]


def test_a_merge_moves_candidates_too(schema: Any) -> None:
    with schema:
        schema.execute("INSERT INTO profiles (name, created_at) VALUES ('alice', 0)")
        schema.execute(
            "INSERT INTO source_rows (profile, source, source_id, state, queue_reason, "
            "queued_at, first_seen_at, last_seen_at) VALUES ('alice', 'goodreads', '1', "
            "'queued', 'fuzzy_low', 0, 0, 0)"
        )
        schema.execute(
            "INSERT INTO match_candidates (profile, source, source_id, rank, hc_book_id, method,"
            " fetched_at) VALUES ('alice', 'goodreads', '1', 1, 7, 'fuzzy', 0)"
        )
        identity.follow_merges(schema, {7: 70}, 1)
    assert schema.execute("SELECT hc_book_id FROM match_candidates").fetchall() == [(70,)]
