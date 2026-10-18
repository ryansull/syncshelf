"""Dev mode and the authenticated ``/mcp`` (SPEC §3.2.1, §3.2.4, §3.2.5): the 401 that starts
sign-in, the ``TokenVerifier`` dev branch, and the dev-mode startup warning."""

import logging
from dataclasses import replace
from pathlib import Path

import pytest
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.auth.tokens import TokenVerifier
from syncshelf.config import Config
from syncshelf.tools import ToolRegistry

from serving import DEV_TOKEN, MCP_HEADERS, make_config, running

pytestmark = pytest.mark.anyio

PRM_URL = "http://localhost:8000/.well-known/oauth-protected-resource/mcp"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def app_for(config: Config, data_dir: Path) -> Starlette:
    return create_app(config, data_dir=data_dir, registry=ToolRegistry())


class TestUnauthenticated:
    @pytest.mark.parametrize("token", [None, "wrong-token", DEV_TOKEN + "x", ""])
    async def test_mcp_without_the_dev_token_is_401_naming_the_prm(
        self, tmp_path: Path, data_dir: Path, token: str | None
    ) -> None:
        app = app_for(make_config(tmp_path), data_dir)
        async with running(app, token=token) as client:
            response = await client.post("/mcp", json=LIST, headers=MCP_HEADERS)
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{PRM_URL}"' in challenge

    async def test_the_prm_names_the_mcp_url_and_the_issuer(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        async with running(app_for(make_config(tmp_path), data_dir), token=None) as client:
            response = await client.get("/.well-known/oauth-protected-resource/mcp")
        assert response.status_code == 200
        assert response.json()["resource"] == "http://localhost:8000/mcp"
        assert response.json()["authorization_servers"] == ["http://localhost:8000"]


class TestVerifier:
    async def test_the_dev_token_resolves_to_the_dev_profile(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, profiles=["alice", "bob"], dev_profile="bob")
        token = await TokenVerifier(config).verify_token(DEV_TOKEN)
        assert token is not None
        assert token.subject == "bob"
        assert token.resource == "http://localhost:8000/mcp"
        assert token.claims is not None and token.claims["iss"] == "http://localhost:8000"
        assert token.expires_at is None

    @pytest.mark.parametrize("presented", ["", "wrong", DEV_TOKEN.upper(), DEV_TOKEN[:-1]])
    async def test_any_other_token_is_refused(self, tmp_path: Path, presented: str) -> None:
        assert await TokenVerifier(make_config(tmp_path)).verify_token(presented) is None

    @pytest.mark.parametrize("mode", ["oauth", None])
    async def test_outside_dev_mode_the_dev_token_is_refused(
        self, tmp_path: Path, mode: str | None
    ) -> None:
        # The OAuth branch only looks up ssat_ tokens in the store, and a config without
        # server.auth_mode accepts nothing (#73).
        config = make_config(tmp_path)
        config = replace(config, server=replace(config.server, auth_mode=mode))
        assert await TokenVerifier(config).verify_token(DEV_TOKEN) is None


class TestStartup:
    async def test_every_dev_mode_start_logs_a_warning(
        self, tmp_path: Path, data_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        for _ in range(2):
            async with running(app_for(make_config(tmp_path), data_dir)):
                pass
        warnings = [r for r in caplog.records if getattr(r, "event", None) == "dev_mode"]
        assert len(warnings) == 2
        assert all(r.levelno == logging.WARNING for r in warnings)
        assert all(r.__dict__["profile"] == "alice" for r in warnings)
        assert DEV_TOKEN not in " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
