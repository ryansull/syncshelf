"""The outer app (SPEC §2.2, §3.1): route order, the Phase 1 lifespan, the MCP mount's host
allowlist, and ``/healthz``. Everything goes through the real ASGI app."""

import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import anyio
import pytest
from starlette.routing import Mount, Route

import syncshelf.app
from syncshelf import __version__
from syncshelf.app import PURGE_TASK, SYNC_TASK, StartupError, allowed_hosts, create_app
from syncshelf.tools import ToolRegistry

from clock import FakeClock
from serving import MCP_HEADERS, SECRET, make_config, mcp_client, running

pytestmark = pytest.mark.anyio

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


class TestRoutes:
    def test_healthz_comes_first_and_the_mcp_mount_last(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        app = create_app(make_config(tmp_path), data_dir=data_dir)
        routes = app.router.routes
        assert isinstance(routes[0], Route) and routes[0].path == "/healthz"
        assert isinstance(routes[-1], Mount) and routes[-1].path == ""

    async def test_register_is_not_offered(self, tmp_path: Path, data_dir: Path) -> None:
        async with running(create_app(make_config(tmp_path), data_dir=data_dir)) as client:
            assert (await client.get("/register")).status_code == 404
            assert (await client.post("/register", json={})).status_code == 404


class TestHealthz:
    async def test_ok_when_the_database_answers(self, tmp_path: Path, data_dir: Path) -> None:
        async with running(create_app(make_config(tmp_path), data_dir=data_dir)) as client:
            response = await client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {
            "status": "ok",
            "version": __version__,
            "profiles": {"alice": []},
        }

    async def test_503_when_the_database_does_not_answer(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = create_app(make_config(tmp_path), data_dir=data_dir)
        async with running(app) as client:
            await app.state.db.close()
            response = await client.get("/healthz")
        assert response.status_code == 503
        assert response.json()["version"] == __version__
        assert response.json()["status"] != "ok"
        assert any(r.levelno == logging.WARNING for r in caplog.records)


class TestMcpMount:
    async def test_a_disallowed_host_gets_421(self, tmp_path: Path, data_dir: Path) -> None:
        async with running(create_app(make_config(tmp_path), data_dir=data_dir)) as client:
            response = await client.post(
                "/mcp", json=INIT, headers={**MCP_HEADERS, "host": "evil.example.net"}
            )
        assert response.status_code == 421

    async def test_an_allowed_host_reaches_the_server(self, tmp_path: Path, data_dir: Path) -> None:
        app = create_app(make_config(tmp_path), data_dir=data_dir, registry=ToolRegistry())
        async with running(app) as client:
            response = await client.post("/mcp", json=INIT, headers=MCP_HEADERS)
            assert response.status_code == 200
            async with mcp_client(client) as mcp:
                assert (await mcp.list_tools()).tools == []

    def test_the_allowlist_is_the_public_host_and_loopback(self, tmp_path: Path) -> None:
        server = make_config(tmp_path, "http://127.0.0.1:8000").server
        assert allowed_hosts(server) == ["127.0.0.1:8000", "localhost:*", "127.0.0.1:*"]
        server = make_config(tmp_path, "http://localhost").server
        assert allowed_hosts(server) == ["localhost", "localhost:*", "127.0.0.1:*"]

    async def test_a_public_url_without_a_port_admits_the_bare_host(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        # Config refuses a default port (#98), so the portless form is the one clients send.
        app = create_app(make_config(tmp_path, "http://localhost"), data_dir=data_dir)
        async with running(app) as client:
            response = await client.post(
                "/mcp", json=INIT, headers={**MCP_HEADERS, "host": "localhost"}
            )
        assert response.status_code == 200


class TestLifespan:
    async def test_startup_prepares_data_and_migrates(self, tmp_path: Path, data_dir: Path) -> None:
        async with running(create_app(make_config(tmp_path), data_dir=data_dir)):
            assert (data_dir / "goodreads").is_dir()
            db_file = data_dir / "syncshelf.db"
            with sqlite3.connect(db_file) as conn:
                assert conn.execute("PRAGMA user_version").fetchone()[0] >= 1
            conn.close()

    async def test_startup_logs_versions_and_auth_mode_but_no_secret(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        async with running(create_app(make_config(tmp_path), data_dir=data_dir)):
            pass
        [startup] = [r for r in caplog.records if getattr(r, "event", None) == "startup"]
        assert startup.__dict__["version"] == __version__
        assert startup.__dict__["sqlite_version"] == sqlite3.sqlite_version
        assert startup.__dict__["auth_mode"] == "dev"
        everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert SECRET not in everything

    async def test_startup_disables_a_profile_with_no_goodreads_credentials(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        # No credentials means nothing to sign in to, so the session says so rather than
        # having no row at all (SPEC §3.4b, #232).
        caplog.set_level(logging.INFO)
        config = make_config(tmp_path, profiles=("alice", "bob"))
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            pass
        with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn:
            rows = dict(conn.execute("SELECT profile, state FROM goodreads_sessions"))
        assert rows == {"alice": "disabled", "bob": "disabled"}
        events = [getattr(r, "event", None) for r in caplog.records]
        assert "goodreads_export_disabled" in events
        assert "goodreads_sessions_reconciled" in events

    async def test_startup_offers_a_sign_in_once_credentials_are_configured(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        caplog.set_level(logging.INFO)
        goodreads = (
            '{goodreads: {user_id: "1", shelf_map: {read: 3}, '
            'email: "${GR_EMAIL}", password: "${GR_PASSWORD}"}}'
        )
        config = make_config(
            tmp_path,
            profiles=("alice",),
            profile_yaml={"alice": goodreads},
            browser_ws="ws://browser:3000/",
            environ={"GR_EMAIL": "reader@example.com", "GR_PASSWORD": "a-test-password"},
        )
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            pass
        with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn:
            rows = dict(conn.execute("SELECT profile, state FROM goodreads_sessions"))
        assert rows == {"alice": "needs_sign_in"}
        # The sidecar is configured, so nothing reports 4b as off.
        assert "goodreads_export_disabled" not in [
            getattr(r, "event", None) for r in caplog.records
        ]
        everything = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
        assert "a-test-password" not in everything

    async def test_startup_syncs_the_profiles_table_with_the_config(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        caplog.set_level(logging.INFO)

        def rows() -> dict[str, bool]:
            with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn:
                query = "SELECT name, removed_at IS NOT NULL FROM profiles"
                return {name: bool(removed) for name, removed in conn.execute(query)}

        async def start(*profiles: str) -> None:
            config = make_config(tmp_path, profiles=profiles)
            async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
                pass

        await start("alice", "bob")
        assert rows() == {"alice": False, "bob": False}
        with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn:
            created = {t for (t,) in conn.execute("SELECT created_at FROM profiles")}
        assert created == {int(clock.time())}
        clock.advance(60)

        caplog.clear()
        await start("bob")
        assert rows() == {"alice": True, "bob": False}
        [synced] = [r for r in caplog.records if getattr(r, "event", None) == "profiles_synced"]
        assert synced.__dict__["removed"] == ["alice"]

        await start("alice", "bob")
        assert rows() == {"alice": False, "bob": False}

    async def test_startup_purges_expired_oauth_rows_and_logs_the_counts(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        caplog.set_level(logging.INFO)
        config = make_config(tmp_path)
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            pass
        assert not [r for r in caplog.records if getattr(r, "event", None) == "oauth_purged"]
        _expired_oauth_rows(data_dir / "syncshelf.db", int(clock.time()))
        clock.advance(2 * 86400)

        caplog.clear()
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            assert _oauth_rows(data_dir / "syncshelf.db") == (0, 0, 1)
        [purged] = [r for r in caplog.records if getattr(r, "event", None) == "oauth_purged"]
        counts = {k: purged.__dict__[k] for k in ("codes", "access_tokens", "refresh_tokens")}
        assert counts == {"codes": 1, "access_tokens": 1, "refresh_tokens": 0}

    async def test_startup_marks_runs_left_running_as_aborted(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        caplog.set_level(logging.INFO)
        config = make_config(tmp_path)
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            pass
        with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn, conn:
            conn.execute(
                "INSERT INTO runs (profile, source, trigger, started_at, state) "
                "VALUES ('alice', 'gr_rss', 'schedule', ?, 'running')",
                (int(clock.time()),),  # recent: runs over 90 days old are purged at startup
            )
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            pass
        with closing(sqlite3.connect(data_dir / "syncshelf.db")) as conn:
            assert conn.execute("SELECT state, ended_at FROM runs").fetchall() == [
                ("aborted", int(clock.time()))
            ]
        [aborted] = [r for r in caplog.records if getattr(r, "event", None) == "runs_aborted"]
        assert aborted.__dict__["count"] == 1

    async def test_a_new_version_releases_quarantined_changes_once(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture, clock: FakeClock
    ) -> None:
        caplog.set_level(logging.INFO)
        config = make_config(tmp_path)
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            pass  # the first start records the version (a missing row counts as new)
        db = data_dir / "syncshelf.db"
        with closing(sqlite3.connect(db)) as conn, conn:
            assert conn.execute("SELECT value FROM meta").fetchall() == [(__version__,)]
            conn.execute(
                "INSERT INTO source_rows (profile, source, source_id, state, first_seen_at, "
                "last_seen_at) VALUES ('alice', 'goodreads', '1', 'ignored', 0, 0)"
            )
            conn.execute(
                "INSERT INTO baseline_fields (profile, source, source_id, field, attempts, "
                "quarantined_at) VALUES ('alice', 'goodreads', '1', 'rating', 3, 1)"
            )
            conn.execute("UPDATE meta SET value = '0.0.1' WHERE key = 'last_started_version'")
        caplog.clear()
        async with running(create_app(config, data_dir=data_dir, clock=clock.time)):
            pass
        with closing(sqlite3.connect(db)) as conn:
            assert conn.execute(
                "SELECT attempts, quarantined_at FROM baseline_fields"
            ).fetchall() == [(0, None)]
            assert conn.execute("SELECT value FROM meta").fetchall() == [(__version__,)]
        [released] = [
            r for r in caplog.records if getattr(r, "event", None) == "quarantine_released"
        ]
        assert released.__dict__["count"] == 1

    async def test_the_purge_repeats_while_running_and_stops_at_shutdown(
        self, tmp_path: Path, data_dir: Path, clock: FakeClock
    ) -> None:
        app = create_app(
            make_config(tmp_path), data_dir=data_dir, clock=clock.time, purge_interval=0.01
        )
        async with running(app):
            assert [t for t in asyncio.all_tasks() if t.get_name() == PURGE_TASK]
            _expired_oauth_rows(data_dir / "syncshelf.db", int(clock.time()))
            clock.advance(2 * 86400)
            with anyio.fail_after(10):
                while _oauth_rows(data_dir / "syncshelf.db")[:2] != (0, 0):
                    await anyio.sleep(0.01)
        assert not [t for t in asyncio.all_tasks() if t.get_name() == PURGE_TASK]

    async def test_the_sync_loops_run_with_the_app_and_stop_at_shutdown(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        async with running(create_app(make_config(tmp_path), data_dir=data_dir)):
            assert [t for t in asyncio.all_tasks() if t.get_name() == SYNC_TASK]
        assert not [t for t in asyncio.all_tasks() if t.get_name() == SYNC_TASK]

    async def test_a_failing_purge_is_logged_and_never_stops_the_app(
        self,
        tmp_path: Path,
        data_dir: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = 0

        def broken(conn: sqlite3.Connection, now: int) -> None:
            nonlocal calls
            calls += 1
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(syncshelf.app, "purge_expired", broken)
        app = create_app(make_config(tmp_path), data_dir=data_dir, purge_interval=0.01)
        async with running(app) as client:
            with anyio.fail_after(10):
                while calls < 3:
                    await anyio.sleep(0.01)
            assert (await client.get("/healthz")).status_code == 200
        failures = [r for r in caplog.records if getattr(r, "event", None) == "oauth_purge_failed"]
        assert len(failures) >= 3

    async def test_shutdown_closes_the_database(self, tmp_path: Path, data_dir: Path) -> None:
        app = create_app(make_config(tmp_path), data_dir=data_dir)
        async with running(app):
            db = app.state.db
        with pytest.raises(RuntimeError, match="closed"):
            await db.read(lambda c: c.execute("SELECT 1").fetchone())

    async def test_an_unusable_data_dir_fails_naming_the_path(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        # A file where the directory should be can't be written into on any platform.
        (data_dir / "goodreads").write_text("", encoding="utf-8")
        app = create_app(make_config(tmp_path), data_dir=data_dir)
        with pytest.raises(StartupError, match=r"goodreads"):
            async with running(app):
                pass
        assert not (data_dir / "syncshelf.db").exists()

    async def test_the_data_dir_comes_from_syncshelf_data_dir(
        self, tmp_path: Path, data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNCSHELF_DATA_DIR", str(data_dir))
        async with running(create_app(make_config(tmp_path))):
            assert (data_dir / "syncshelf.db").is_file()

    async def test_a_missing_data_dir_fails_naming_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nowhere" / "data"
        app = create_app(make_config(tmp_path), data_dir=missing)
        with pytest.raises(StartupError, match=r"nowhere"):
            async with running(app):
                pass


def _expired_oauth_rows(db_file: Path, now: int) -> None:
    """One grant, live at ``now``, with a code, an access token and a refresh token that are
    all fresh at ``now``; two days later only the refresh token is still worth keeping."""
    with closing(sqlite3.connect(db_file)) as conn, conn:
        conn.execute(
            "INSERT INTO oauth_grant (id, profile, email, client_id, client_label, resource, "
            "scopes, created_at, last_used_at, refresh_expires_at) VALUES (1, 'alice', "
            "'alice@example.com', 'c', 'claude.ai', 'r', 'syncshelf', ?, ?, ?)",
            (now, now, now + 90 * 86400),
        )
        conn.execute(
            "INSERT INTO oauth_code (code_hash, client_id, redirect_uri, code_challenge, "
            "resource, scopes, profile, email, created_at, expires_at, grant_id) VALUES "
            "('h', 'c', 'u', 'x', 'r', 'syncshelf', 'alice', 'alice@example.com', ?, ?, 1)",
            (now, now + 120),
        )
        conn.execute(
            "INSERT INTO oauth_access_token (token_hash, grant_id, created_at, expires_at) "
            "VALUES ('a', 1, ?, ?)",
            (now, now + 3600),
        )
        conn.execute(
            "INSERT INTO oauth_refresh_token (token_hash, grant_id, created_at) VALUES ('r', 1, ?)",
            (now,),
        )


def _oauth_rows(db_file: Path) -> tuple[int, int, int]:
    """How many code, access-token and refresh-token rows are left."""
    with closing(sqlite3.connect(db_file)) as conn:
        tables = ("oauth_code", "oauth_access_token", "oauth_refresh_token")
        codes, access, refresh = (
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
        )
        return codes, access, refresh
