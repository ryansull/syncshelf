"""``rebaseline`` (SPEC §3.4a.11, §3.4a.12, §3.3.9): a preview and a confirm token first, then
the backfill run again on that token: queued rows re-matched by the backfill's rules, every
pending change dropped into its baseline, the ``backfills`` row rewritten, nothing pushed."""

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import syncshelf.tools as tools
import syncshelf.tools.matching as matching
from syncshelf.app import create_app
from syncshelf.sync.scheduler import Scheduler
from syncshelf.tools.confirm import ConfirmTokens

from clock import FakeClock
from fakes.hardcover import FakeHardcover
from serving import hardcover_token, make_config, mcp_client, running
from sync_world import GR_USER, HC_USER, World, world

pytestmark = pytest.mark.anyio


@pytest.fixture
async def w(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[World]:
    async with world(tmp_path) as w:
        w.hardcover.add_user_book(HC_USER, 9, status_id=3)
        w.hardcover.add_mapping(900, 9, title="Book 900")
        w.hardcover.add_mapping(300, 7, title="Orphan")  # not on the shelves yet
        w.goodreads.add_item(GR_USER, 300, "Orphan", user_rating="4")
        w.goodreads.add_item(GR_USER, 900, "Book 900")
        await w.sync.run_rss("alice")  # the backfill: 300 queued (not_in_library)
        w.clock.advance(3600)
        w.goodreads.users[GR_USER].items[1]["user_rating"] = "2"  # 300 changed since
        await w.sync.run_rss("alice")
        w.clock.advance(3600)
        bound = SimpleNamespace(
            scheduler=Scheduler(
                db=w.db, sync=w.sync, profiles=w.config.profiles, interval=3600, clock=w.clock
            ),
            db=w.db,
            confirm=ConfirmTokens(w.clock.time),
        )
        monkeypatch.setattr(matching, "services", lambda: bound)
        monkeypatch.setattr(matching, "current_profile", lambda: "alice")

        async def no_attention() -> list[Any]:
            return []

        monkeypatch.setattr(tools, "caller_attention", no_attention)
        yield w


async def test_a_preview_counts_and_changes_nothing(w: World) -> None:
    before = await w.rows("SELECT * FROM baseline_fields ORDER BY id")
    requests = len(w.hardcover.requests)
    preview = await matching.rebaseline("goodreads")
    assert {k: v for k, v in preview.items() if k not in ("confirm", "note")} == {
        "source": "goodreads",
        "done": False,
        "rows_rematched": 1,
        "pending_dropped": 1,
        "human_links_kept": 0,
        "ignored_kept": 0,
    }
    assert preview["confirm"] and "Preview only" in preview["note"]
    assert await w.rows("SELECT * FROM baseline_fields ORDER BY id") == before
    assert len(w.hardcover.requests) == requests  # a preview reads SQLite only


async def test_confirmed_it_links_imported_books_and_drops_pending_changes(w: World) -> None:
    token = (await matching.rebaseline("goodreads"))["confirm"]
    w.hardcover.add_user_book(HC_USER, 7, status_id=3, rating=4.0)  # the hand import, done
    w.clock.advance(60)  # within the token's 10 minutes
    done = await matching.rebaseline("goodreads", token)
    assert done["done"] is True and done["confirm"] is None
    assert (done["rows_linked"], done["rows_queued"]) == (2, 0)
    assert w.hardcover.writes == []
    assert await w.rows(
        "SELECT state, hc_book_id, hc_user_book_id IS NOT NULL FROM source_rows "
        "WHERE source_id = '300'"
    ) == [("linked", 7, 1)]
    assert await w.rows(
        "SELECT value, pending_since FROM baseline_fields WHERE source_id = '300' "
        "AND field = 'rating'"
    ) == [("2", None)]
    assert await w.rows("SELECT done_at, run_id, rows_linked, rows_queued FROM backfills") == [
        (int(w.clock.time()), None, 2, 0)
    ]
    await w.sync.run_rss("alice")
    assert w.hardcover.writes == []  # the rating Goodreads had is the baseline now


async def test_a_row_still_unshelved_stays_queued_by_the_backfills_rules(w: World) -> None:
    token = (await matching.rebaseline("goodreads"))["confirm"]
    done = await matching.rebaseline("goodreads", token)
    assert (done["rows_linked"], done["rows_queued"]) == (1, 1)
    assert await w.rows(
        "SELECT state, queue_reason, backfill_queued FROM source_rows WHERE source_id = '300'"
    ) == [("queued", "not_in_library", 1)]


async def test_a_change_since_the_preview_gets_a_fresh_one(w: World) -> None:
    token = (await matching.rebaseline("goodreads"))["confirm"]
    w.goodreads.users[GR_USER].items[1]["user_rating"] = "5"
    await w.sync.run_rss("alice")  # supersedes: a new pending change, a new baseline id kept
    w.goodreads.add_item(GR_USER, 400, "Lost")
    w.clock.advance(60)
    await w.sync.run_rss("alice")  # a new queued row
    again = await matching.rebaseline("goodreads", token)
    assert again["done"] is False and "changed since" in again["note"]
    assert again["rows_rematched"] == 2
    assert again["confirm"] and again["confirm"] != token


async def test_a_token_from_elsewhere_doesnt_confirm(w: World) -> None:
    again = await matching.rebaseline("goodreads", "not-a-token")
    assert again["done"] is False and "expired, used" in again["note"]


async def test_refused_for_abs_before_the_backfill_and_during_a_run(w: World) -> None:
    with pytest.raises(ToolError, match="phase 5"):
        await matching.rebaseline("abs")
    await w.db.write(lambda conn: conn.execute("DELETE FROM backfills"))
    with pytest.raises(ToolError, match="hasn't finished yet"):
        await matching.rebaseline("goodreads")


async def test_refused_while_a_run_holds_the_lock(w: World) -> None:
    async with w.sync.lock("alice"):
        with pytest.raises(ToolError, match="A sync is running"):
            await matching.rebaseline("goodreads")


async def test_nothing_changes_when_the_library_cant_be_read(w: World) -> None:
    token = (await matching.rebaseline("goodreads"))["confirm"]
    w.hardcover.fail_next("Library", "unavailable")
    with pytest.raises(ToolError, match="nothing was changed"):
        await matching.rebaseline("goodreads", token)
    assert await w.rows("SELECT count(*) FROM baseline_fields WHERE pending_since IS NOT NULL") == [
        (1,)
    ]


async def test_through_the_app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> None:
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=42)
    app = create_app(
        make_config(tmp_path),
        data_dir=data_dir,
        clock=clock.time,
        http_transport=fake.transport,
        limiter_clock=clock,
    )
    async with running(app) as http, mcp_client(http) as mcp:
        tool = {t.name: t for t in (await mcp.list_tools()).tools}["rebaseline"]
        assert tool.annotations is not None and tool.annotations.destructive_hint is True
        assert set(tool.input_schema["properties"]) == {"source", "confirm"}
        refused = await mcp.call_tool("rebaseline", {"source": "goodreads"})
        assert refused.is_error
