"""Attention items (SPEC §3.4a.10, §2.8): every case in ``tests/golden/attention.yaml``, then
where they show: on read tools for the caller's own profile only, in ``sync_status``, in the
household health view and on ``/healthz``, none of them naming a book or a value."""

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from syncshelf.app import create_app
from syncshelf.attention import attention
from syncshelf.tools.sync import health

from clock import FakeClock
from fakes.hardcover import FakeHardcover
from serving import (
    SIDECAR,
    credential_env,
    hardcover_token,
    make_config,
    mcp_client,
    running,
)

pytestmark = pytest.mark.anyio

CASES = yaml.safe_load(
    (Path(__file__).parents[1] / "golden" / "attention.yaml").read_text(encoding="utf-8")
)["cases"]
NOW = 4_102_444_800
GOODREADS = SimpleNamespace(user_id="1", password=None)
WITH_PASSWORD = SimpleNamespace(user_id="1", password="a-test-password-never-shown")
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
    for source, key in (("gr_rss", "last_success_ago"), ("gr_csv", "csv_success_ago")):
        if (ago := state.get(key)) is not None:
            conn.execute(
                "INSERT INTO schedules (profile, source, next_due_at, last_success_at, "
                "last_result) VALUES (?, ?, 0, ?, 'ok')",
                (profile, source, NOW - ago * HOUR),
            )
    for source, key in (("gr_rss", "due_ago"), ("gr_csv", "csv_due_ago")):
        if (ago := state.get(key)) is not None:
            conn.execute(
                "INSERT INTO schedules (profile, source, next_due_at) VALUES (?, ?, ?)",
                (profile, source, NOW - ago * HOUR),
            )
    if (what := state.get("session")) is not None:
        conn.execute(
            "INSERT INTO goodreads_sessions (profile, state, state_since) VALUES (?, ?, 0)",
            (profile, what),
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
    goodreads = WITH_PASSWORD if state.get("credentials") else GOODREADS
    config = SimpleNamespace(goodreads=goodreads if state.get("enabled", True) else None)
    items = attention(  # type: ignore[arg-type]
        schema, "alice", NOW, config, bool(state.get("sidecar"))
    )
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
        ("carol", []),
    ]
    assert rows[0]["sources"]["gr_rss"]["last_result"] == "ok"
    assert rows[2]["sources"] == {}  # no Goodreads section: no source shown
    assert "Secret" not in repr(rows)


def make_config_profiles() -> Any:
    return {
        "alice": SimpleNamespace(goodreads=GOODREADS),
        "bob": SimpleNamespace(goodreads=GOODREADS),
        "carol": SimpleNamespace(goodreads=None),
    }


def with_credentials() -> Any:
    return {
        "alice": SimpleNamespace(goodreads=WITH_PASSWORD),
        "bob": SimpleNamespace(goodreads=GOODREADS),
        "carol": SimpleNamespace(goodreads=None),
    }


class TestTheHouseholdViewAndTheSession:
    """§3.4a.10: the session's **state** travels with the health view, and nothing else of it."""

    def test_a_csv_profile_shows_its_session_state_and_no_detail(
        self, schema: sqlite3.Connection
    ) -> None:
        with schema:
            seed(schema, "alice", {"last_success_ago": 1, "csv_success_ago": 1})
            schema.execute(
                "INSERT INTO goodreads_sessions (profile, state, state_since, detail) "
                "VALUES ('alice', 'challenged', 0, 'The Secret bot check')"
            )
            seed(schema, "bob", {"last_success_ago": 1})
        rows = health(schema, with_credentials(), NOW, True)
        assert rows[0]["goodreads_session"] == {"state": "challenged"}
        assert rows[0]["attention"] == ["goodreads_session"]
        assert set(rows[0]["sources"]) == {"gr_rss", "gr_csv"}
        assert "Secret" not in repr(rows)  # the detail never travels with the view

    def test_a_profile_without_credentials_shows_no_session(
        self, schema: sqlite3.Connection
    ) -> None:
        with schema:
            seed(schema, "bob", {"last_success_ago": 1})
        rows = health(schema, with_credentials(), NOW, True)
        bob = next(r for r in rows if r["profile"] == "bob")
        assert "goodreads_session" not in bob
        assert set(bob["sources"]) == {"gr_rss"}

    def test_without_a_sidecar_the_csv_source_is_off_everywhere(
        self, schema: sqlite3.Connection
    ) -> None:
        # #232: no sidecar means no CSV source at all, not a permanently stale one.
        with schema:
            seed(schema, "alice", {"last_success_ago": 1, "csv_success_ago": 400})
            schema.execute(
                "INSERT INTO goodreads_sessions (profile, state, state_since) "
                "VALUES ('alice', 'challenged', 0)"
            )
        (alice, *_) = health(schema, with_credentials(), NOW, False)
        assert alice["attention"] == []
        assert "goodreads_session" not in alice
        assert set(alice["sources"]) == {"gr_rss"}


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


async def test_an_attention_failure_never_costs_the_answer(
    app: Any, data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import syncshelf.tools as tools

    async def broken() -> list[Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(tools, "caller_attention", broken)
    async with running(app) as http, mcp_client(http) as mcp:
        page = await call(mcp, "get_my_library")
        assert "attention" not in page and "books" in page


@pytest.fixture
def csv_app(tmp_path: Path, data_dir: Path, clock: FakeClock) -> Any:
    """An app whose profile has Goodreads credentials and a sidecar, so it has a CSV source."""
    fake = FakeHardcover()
    fake.add_user(hardcover_token("alice"), user_id=42)
    body = (
        '{goodreads: {user_id: "1", shelf_map: {read: 3}, '
        'email: "${GR_EMAIL}", password: "${GR_PASSWORD}"}, timezone: Etc/UTC}'
    )
    config = make_config(
        tmp_path,
        profile_yaml={"alice": body},
        browser_ws=SIDECAR,
        environ=credential_env(),
    )
    return create_app(
        config,
        data_dir=data_dir,
        clock=clock.time,
        http_transport=fake.transport,
        limiter_clock=clock,
    )


async def test_healthz_reflects_the_goodreads_session(csv_app: Any) -> None:
    """§3.4a.10: /healthz carries the codes, so a lapsed session shows there too."""
    async with running(csv_app) as http:
        body = (await http.get("/healthz")).json()
        # Nothing has signed in yet, so the session needs a person.
        assert body["status"] == "attention"
        assert body["profiles"] == {"alice": ["goodreads_session"]}


async def test_a_session_that_is_fine_leaves_healthz_ok(csv_app: Any, data_dir: Path) -> None:
    async with running(csv_app) as http:
        with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn, conn:
            conn.execute(
                "INSERT INTO goodreads_sessions (profile, state, state_since, detail) "
                "VALUES ('alice', 'ok', 0, NULL) ON CONFLICT (profile) DO UPDATE "
                "SET state = 'ok'"
            )
        body = (await http.get("/healthz")).json()
        assert body == {"status": "ok", "version": body["version"], "profiles": {"alice": []}}


async def test_without_a_sidecar_healthz_says_nothing_about_a_session(
    app: Any, data_dir: Path
) -> None:
    # #232: the CSV source is off, so a stored state raises nothing.
    async with running(app) as http:
        # Startup already made the row (disabled, since the profile has no credentials).
        with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn, conn:
            conn.execute(
                "INSERT INTO goodreads_sessions (profile, state, state_since) "
                "VALUES ('alice', 'challenged', 0) ON CONFLICT (profile) DO UPDATE "
                "SET state = 'challenged'"
            )
        body = (await http.get("/healthz")).json()
        assert body["profiles"] == {"alice": [], "bob": []}
