"""OAuth discovery (SPEC §3.2.1, §3.2.2): the authorization-server metadata, the
protected-resource metadata with its scopes, no dynamic client registration, and no AS routes
outside ``oauth`` mode."""

import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.auth.oauth import PRM_PATH
from syncshelf.config import Config
from syncshelf.tools import ToolRegistry

from serving import MCP_HEADERS, make_config, running

pytestmark = pytest.mark.anyio

AS_METADATA = "/.well-known/oauth-authorization-server"
PRM = "/.well-known/oauth-protected-resource/mcp"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

# §3.2.2, with the placeholder host swapped for the test public_url.
EXPECTED = {
    "issuer": "http://localhost:8000",
    "authorization_endpoint": "http://localhost:8000/authorize",
    "token_endpoint": "http://localhost:8000/token",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "token_endpoint_auth_methods_supported": ["none"],
    "code_challenge_methods_supported": ["S256"],
    "client_id_metadata_document_supported": True,
    "authorization_response_iss_parameter_supported": True,
    "scopes_supported": ["syncshelf", "offline_access"],
}


def app_for(config: Config, data_dir: Path) -> Starlette:
    return create_app(config, data_dir=data_dir, registry=ToolRegistry())


def oauth_config(tmp_path: Path) -> Config:
    return make_config(tmp_path, auth_mode="oauth")


class TestAuthorizationServerMetadata:
    async def test_it_is_exactly_the_spec_document(self, tmp_path: Path, data_dir: Path) -> None:
        async with running(app_for(oauth_config(tmp_path), data_dir), token=None) as client:
            response = await client.get(AS_METADATA)
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json() == EXPECTED

    async def test_it_is_built_from_public_url(self, tmp_path: Path, data_dir: Path) -> None:
        config = make_config(tmp_path, "http://127.0.0.1:9000", auth_mode="oauth")
        async with running(app_for(config, data_dir), token=None) as client:
            body = (await client.get(AS_METADATA)).json()
        assert body["issuer"] == "http://127.0.0.1:9000"
        assert body["authorization_endpoint"] == "http://127.0.0.1:9000/authorize"
        assert body["token_endpoint"] == "http://127.0.0.1:9000/token"

    async def test_the_issuer_keeps_public_urls_exact_form(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        # Pydantic's AnyHttpUrl would drop the default port; issuer comparison is exact.
        config = make_config(tmp_path, "http://localhost:80", auth_mode="oauth")
        async with running(app_for(config, data_dir), token=None) as client:
            assert (await client.get(AS_METADATA)).json()["issuer"] == "http://localhost:80"
            assert (await client.get(PRM)).json()["resource"] == "http://localhost:80/mcp"

    async def test_there_is_no_registration(self, tmp_path: Path, data_dir: Path) -> None:
        async with running(app_for(oauth_config(tmp_path), data_dir), token=None) as client:
            assert "registration_endpoint" not in (await client.get(AS_METADATA)).json()
            assert (await client.get("/register")).status_code == 404
            assert (await client.post("/register", json={})).status_code == 404


class TestProtectedResourceMetadata:
    @pytest.mark.parametrize("mode", ["oauth", "dev"])
    async def test_it_names_the_mcp_url_the_issuer_and_the_scopes(
        self, tmp_path: Path, data_dir: Path, mode: str
    ) -> None:
        config = make_config(tmp_path, auth_mode=mode)
        async with running(app_for(config, data_dir), token=None) as client:
            response = await client.get(PRM)
        assert response.status_code == 200
        body = response.json()
        assert body["resource"] == "http://localhost:8000/mcp"
        assert body["authorization_servers"][0] == "http://localhost:8000"
        assert body["scopes_supported"] == ["syncshelf", "offline_access"]

    async def test_the_401_challenge_leads_to_this_prm(
        self, tmp_path: Path, data_dir: Path
    ) -> None:
        # The outer route only helps if the SDK's challenge names its path, not the SDK's own.
        async with running(app_for(oauth_config(tmp_path), data_dir), token=None) as client:
            challenge = (await client.post("/mcp", json=LIST, headers=MCP_HEADERS)).headers[
                "www-authenticate"
            ]
            match = re.search(r'resource_metadata="([^"]+)"', challenge)
            assert match is not None
            assert urlsplit(match[1]).path == PRM_PATH
            body = (await client.get(urlsplit(match[1]).path)).json()
        assert body["scopes_supported"] == ["syncshelf", "offline_access"]


class TestOutsideOAuthMode:
    @pytest.mark.parametrize("mode", ["dev", None])
    async def test_there_are_no_as_routes(
        self, tmp_path: Path, data_dir: Path, mode: str | None
    ) -> None:
        config = make_config(tmp_path, auth_mode="dev")
        if mode is None:
            # With server.auth_mode absent nothing can sign in (#73), so there is nothing to
            # discover. make_config always writes the key, so drop it here.
            config = replace(config, server=replace(config.server, auth_mode=None, dev=None))
        async with running(app_for(config, data_dir), token=None) as client:
            for path in (AS_METADATA, "/authorize", "/token", "/register"):
                assert (await client.get(path)).status_code == 404, path
            assert (await client.post("/token", data={})).status_code == 404
