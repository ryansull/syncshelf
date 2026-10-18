"""The tool registry, the per-identity ``tools/list`` filter and its server-side refusal, and
profile resolution (SPEC §2.8, §3.2).

No real tool exists yet, so these register test-only tools: one anyone may use, one that
needs the ``chaptarr`` permission, and one that reads the caller and resolves ``profile``.
"""

from pathlib import Path
from typing import Any

import pytest
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.applications import Starlette

from syncshelf.app import create_app
from syncshelf.config import Config, ProfileConfig
from syncshelf.tools import SyncShelfServer, ToolRegistry, current_profile, resolve_profiles

from serving import make_config, mcp_client, running

pytestmark = pytest.mark.anyio

CHAPTARR = "{permissions: [chaptarr]}"


def build_registry(config: Config) -> ToolRegistry:
    registry = ToolRegistry()

    @registry.tool()
    async def whoami() -> dict[str, Any]:
        """Who is calling."""
        token = get_access_token()
        assert token is not None
        return {"subject": token.subject, "claims": token.claims}

    @registry.tool(permission="chaptarr", annotations=ToolAnnotations(destructive_hint=True))
    async def request_book() -> str:
        """Needs chaptarr."""
        return "requested"

    @registry.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def profiles_for(profile: str = "me") -> list[str]:
        """Resolve ``profile`` for the caller."""
        return list(resolve_profiles(profile, current_profile(), config.profiles))

    return registry


def app_for(tmp_path: Path, config: Config) -> Starlette:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return create_app(config, data_dir=data, registry=build_registry(config))


class TestRegistry:
    def test_a_tool_must_be_async(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(TypeError, match="async"):

            @registry.tool()  # pyright: ignore[reportArgumentType]
            def blocking() -> None:  # pyright: ignore[reportUnusedFunction]
                """Plain def tools run in a thread, where asyncio locks don't work."""

    def test_an_unknown_permission_is_refused(self) -> None:
        registry = ToolRegistry()
        with pytest.raises(ValueError, match="permission"):

            @registry.tool(permission="admin")
            async def anything() -> None:  # pyright: ignore[reportUnusedFunction]
                """Nothing."""

    def test_a_name_is_registered_once(self) -> None:
        registry = ToolRegistry()

        async def twice() -> None:
            """Nothing."""

        registry.tool()(twice)
        with pytest.raises(ValueError, match="twice"):
            registry.tool()(twice)

    def test_the_registry_lists_its_tools_and_their_permissions(self) -> None:
        registry = ToolRegistry()

        @registry.tool(permission="chaptarr")
        async def gated() -> None:
            """Gated."""

        assert [(t.name, t.permission, t.fn) for t in registry] == [("gated", "chaptarr", gated)]


async def test_a_tool_added_outside_the_registry_is_hidden_and_refused() -> None:
    server = SyncShelfServer(ToolRegistry(), {}, "test")

    async def stray() -> str:
        """Added with add_tool, so it has no permission on record."""
        return "ran"

    server.add_tool(stray)
    assert await server.list_tools() == []
    with pytest.raises(ToolError, match="Unknown tool"):
        await server.call_tool("stray", {})


class TestPerIdentityListing:
    async def test_a_profile_with_the_permission_sees_every_tool(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, profile_yaml={"alice": CHAPTARR})
        async with running(app_for(tmp_path, config)) as http, mcp_client(http) as mcp:
            names = {t.name for t in (await mcp.list_tools()).tools}
        assert names == {"whoami", "request_book", "profiles_for"}

    async def test_a_profile_without_it_sees_only_the_ungated_tools(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path, profiles=["alice", "bob"], dev_profile="bob", profile_yaml={"alice": CHAPTARR}
        )
        async with running(app_for(tmp_path, config)) as http, mcp_client(http) as mcp:
            names = {t.name for t in (await mcp.list_tools()).tools}
        assert names == {"whoami", "profiles_for"}

    async def test_annotations_reach_the_listing(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, profile_yaml={"alice": CHAPTARR})
        async with running(app_for(tmp_path, config)) as http, mcp_client(http) as mcp:
            tools = {t.name: t for t in (await mcp.list_tools()).tools}
        assert tools["request_book"].annotations == ToolAnnotations(destructive_hint=True)
        assert tools["profiles_for"].annotations == ToolAnnotations(read_only_hint=True)

    async def test_calling_a_filtered_out_tool_is_a_tool_error(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        async with running(app_for(tmp_path, config)) as http, mcp_client(http) as mcp:
            result = await mcp.call_tool("request_book", {})
        assert result.is_error
        assert "chaptarr" in result.content[0].text  # pyright: ignore[reportAttributeAccessIssue]

    async def test_calling_an_allowed_gated_tool_works(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, profile_yaml={"alice": CHAPTARR})
        async with running(app_for(tmp_path, config)) as http, mcp_client(http) as mcp:
            result = await mcp.call_tool("request_book", {})
        assert not result.is_error

    async def test_a_tool_sees_the_dev_profile_as_the_caller(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            profiles=["alice", "bob"],
            dev_profile="bob",
            profile_yaml={"bob": "{email: bob@example.com}"},
        )
        async with running(app_for(tmp_path, config)) as http, mcp_client(http) as mcp:
            result = await mcp.call_tool("whoami", {})
        assert result.structured_content is not None
        assert result.structured_content["subject"] == "bob"
        assert result.structured_content["claims"]["email"] == "bob@example.com"


def profiles(**visible: bool) -> dict[str, ProfileConfig]:
    return {name: ProfileConfig(name=name, household_visible=v) for name, v in visible.items()}


class TestResolveProfiles:
    def test_me_is_the_caller(self) -> None:
        assert resolve_profiles("me", "alice", profiles(alice=False, bob=False)) == ("alice",)

    def test_the_callers_own_name_is_the_caller(self) -> None:
        assert resolve_profiles("alice", "alice", profiles(alice=False, bob=False)) == ("alice",)

    def test_a_visible_profile_is_resolved_by_name(self) -> None:
        assert resolve_profiles("bob", "alice", profiles(alice=False, bob=True)) == ("bob",)

    def test_a_profile_that_has_not_opted_in_is_refused(self) -> None:
        with pytest.raises(ToolError, match="household_visible"):
            resolve_profiles("bob", "alice", profiles(alice=True, bob=False))

    def test_an_unknown_profile_is_refused(self) -> None:
        with pytest.raises(ToolError, match="no profile"):
            resolve_profiles("carol", "alice", profiles(alice=True, bob=True))

    def test_household_is_the_caller_then_every_visible_profile(self) -> None:
        everyone = profiles(dave=True, alice=False, bob=True, carol=False)
        assert resolve_profiles("household", "alice", everyone) == ("alice", "bob", "dave")

    def test_household_includes_the_caller_even_when_not_visible(self) -> None:
        assert resolve_profiles("household", "alice", profiles(alice=False, bob=False)) == (
            "alice",
        )

    def test_the_keywords_win_over_profile_names(self) -> None:
        # #74: config allows profiles called "me" and "household"; the keywords still win.
        names = profiles(alice=False, me=True, household=True)
        assert resolve_profiles("me", "alice", names) == ("alice",)
        assert resolve_profiles("household", "alice", names) == ("alice", "household", "me")

    async def test_resolution_through_a_tool(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            profiles=["alice", "bob", "carol"],
            profile_yaml={"bob": "{household_visible: true}"},
        )
        async with running(app_for(tmp_path, config)) as http, mcp_client(http) as mcp:
            household = await mcp.call_tool("profiles_for", {"profile": "household"})
            refused = await mcp.call_tool("profiles_for", {"profile": "carol"})
        assert household.structured_content == {"result": ["alice", "bob"]}
        assert refused.is_error


def test_current_profile_outside_a_request_is_a_tool_error() -> None:
    with pytest.raises(ToolError):
        current_profile()
