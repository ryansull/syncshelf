"""The outer app (SPEC §2.2, §3.1): route order, the Phase 1 lifespan, the MCP mount's host
allowlist, and ``/healthz``. Everything goes through the real ASGI app."""

import logging
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from starlette.routing import Mount, Route

from syncshelf import __version__
from syncshelf.app import StartupError, allowed_hosts, create_app
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
        assert response.json() == {"status": "ok", "version": __version__}

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
