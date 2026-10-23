"""Attention items (SPEC §3.4a.10, §2.8): every case in ``tests/golden/attention.yaml``, then
where they show: on read tools for the caller's own profile only, in ``sync_status``, in the
household health view and on ``/healthz``, none of them naming a book or a value."""

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml

from syncshelf.app import create_app
from syncshelf.attention import attention
from syncshelf.tools.sync import health

from clock import FakeClock
from fakes.hardcover import FakeHardcover
from serving import hardcover_token, make_config, mcp_client, running

pytestmark = pytest.mark.anyio

CASES = yaml.safe_load(
    (Path(__file__).parents[1] / "golden" / "attention.yaml").read_text(encoding="utf-8")
)["cases"]
NOW = 4_102_444_800
HOUR = 3600


def seed(conn: sqlite3.Connection, profile: str, state: dict[str, Any]) -> None:
    """A profile's state as a golden case gives it."""
    conn.execute("INSERT OR IGNORE INTO profiles (name, created_at) VALUES (?, 0)", (profile,))
    for n, kind in enumerate(state.get("quarantined") or []):
        conn.execute(
            "INSERT INTO source_rows (profile, source, source_id, state, hc_book_id, "
            "first_seen_at, last_seen_at) VALUES (?, 'goodreads', ?, 'linked', 7, 0, 0)",
            (profile, f"q{n}"),
        )
        conn.execute(
            "INSERT INTO baseline_fields (profile, source, source_id, field, value, "
            "pending_value, pending_since, attempts, last_error, last_error_class, "
            "quarantined_at) VALUES (?, 'goodreads', ?, 'rating', '4', '5', 1, 3, "
            "'The Secret Title was refused', ?, 2)",
            (profile, f"q{n}", kind),
        )
    if (ago := state.get("last_success_ago")) is not None:
        conn.execute(
            "INSERT INTO schedules (profile, source, next_due_at, last_success_at, last_result) "
            "VALUES (?, 'gr_rss', 0, ?, 'ok')",
            (profile, NOW - ago * HOUR),
        )
    if (ago := state.get("first_run_ago")) is not None:
        conn.execute(
            "INSERT INTO runs (profile, source, trigger, started_at, state) "
            "VALUES (?, 'gr_rss', 'schedule', ?, 'fetch_failed')",
            (profile, NOW - ago * HOUR),
        )
    for n in range(state.get("queued", 0)):
        conn.execute(
            "INSERT INTO source_rows (profile, source, source_id, state, queue_reason, "
            "queued_at, first_seen_at, last_seen_at) VALUES (?, 'goodreads', ?, 'queued', "
            "'no_hit', 0, 0, 0)",
            (profile, f"u{n}"),
        )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_case(schema: sqlite3.Connection, case: dict[str, Any]) -> None:
    state = case["state"]
    with schema:
        seed(schema, "alice", state)
    items = attention(schema, "alice", NOW, state.get("enabled", True))
    assert [i["code"] for i in items] == case["expect"]
    for item in items:
        assert item["message"] and item["action"]
        assert "Secret" not in item["message"]  # never a title or an error's text


def test_the_household_view_is_codes_and_results_only(schema: sqlite3.Connection) -> None:
    with schema:
        seed(schema, "alice", {"quarantined": ["bug"], "last_success_ago": 1})
        seed(schema, "bob", {"last_success_ago": 30})
    config = make_config_profiles()
    rows = health(schema, config, NOW)
    assert [(r["profile"], r["attention"]) for r in rows] == [
        ("alice", ["quarantined", "hardcover_bug"]),
        ("bob", ["source_stale"]),
    ]
    assert rows[0]["sources"]["gr_rss"]["last_result"] == "ok"
    assert "Secret" not in repr(rows)


def make_config_profiles() -> Any:
    from types import SimpleNamespace

    gr = SimpleNamespace(user_id="1")
    return {"alice": SimpleNamespace(goodreads=gr), "bob": SimpleNamespace(goodreads=gr)}


# --- through the app ----------------------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> Any:
    fake = FakeHardcover()
    for n, name in enumerate(("alice", "bob")):
        fake.add_user(hardcover_token(name), user_id=42 + n)
    config = make_config(
        tmp_path,
        profiles=["alice", "bob"],
        profile_yaml={"bob": "{household_visible: true}"},
    )
    return create_app(
        config,
        data_dir=data_dir,
        clock=clock.time,
        http_transport=fake.transport,
        limiter_clock=clock,
    )


def quarantine(data_dir: Path, profile: str) -> None:
    with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn, conn:
        seed(conn, profile, {"quarantined": ["permanent"]})


async def call(mcp: Any, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(tool, arguments)
    assert not result.is_error, result.content
    return result.structured_content


async def test_read_tools_carry_only_the_callers_own_attention(app: Any, data_dir: Path) -> None:
    async with running(app) as http, mcp_client(http) as mcp:
        assert "attention" not in await call(mcp, "get_my_library")
        quarantine(data_dir, "bob")
        # Bob's quarantine is his: alice's household answer doesn't carry it.
        assert "attention" not in await call(mcp, "get_my_library", profile="household")
        quarantine(data_dir, "alice")
        page = await call(mcp, "get_my_library", profile="household")
        assert [i["code"] for i in page["attention"]] == ["quarantined"]
        stats = await call(mcp, "reading_stats", period="all")
        assert [i["code"] for i in stats["attention"]] == ["quarantined"]


async def test_sync_status_carries_attention_and_the_household_view(
    app: Any, data_dir: Path
) -> None:
    async with running(app) as http, mcp_client(http) as mcp:
        quarantine(data_dir, "bob")
        mine = await call(mcp, "sync_status")
        assert "attention" not in mine
        view = await call(mcp, "sync_status", profile="household")
        assert set(view) == {"now", "version", "household"}
        assert [(r["profile"], r["attention"]) for r in view["household"]] == [
            ("alice", []),
            ("bob", ["quarantined"]),
        ]
        assert "Secret" not in repr(view)


async def test_healthz_reports_attention_without_naming_anything(app: Any, data_dir: Path) -> None:
    async with running(app) as http:
        assert (await http.get("/healthz")).json()["status"] == "ok"
        quarantine(data_dir, "bob")
        response = await http.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "attention"
        assert body["profiles"] == {"alice": [], "bob": ["quarantined"]}
        assert "Secret" not in response.text
