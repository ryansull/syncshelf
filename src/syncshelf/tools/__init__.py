"""The tool registry, the per-identity tool filter, and profile resolution (SPEC §2.8, §3.2).

Every tool module registers its tools on :data:`REGISTRY` with :meth:`ToolRegistry.tool`,
naming the permission a tool needs, if any. :class:`SyncShelfServer` serves them: each caller's
``tools/list`` holds only the tools its profile may use, and a direct call to any other is
refused as a tool error (``isError: true``, HTTP 200), never a 403, which Claude would treat
as terminal.

How SDK v2 exposes per-request listing: ``MCPServer``'s low-level ``tools/list`` handler
returns ``await self.list_tools()``, and its ``tools/call`` handler calls ``self.call_tool()``
and turns a ``ToolError`` into an ``isError`` result. Both run inside the request, where
``get_access_token()`` gives the caller, so overriding those two public methods filters per
identity without touching private SDK state. (The SDK's server middleware could do the same,
but its signature is marked provisional.)

A tool reaches the database and the clock through :func:`services`. The app binds them to its
server once the lifespan has opened the database, and :meth:`SyncShelfServer.call_tool` puts
them in a context variable for the call, the same way the SDK carries the caller.
"""

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from inspect import iscoroutinefunction
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, ToolAnnotations
from mcp.types import Tool as MCPTool

from syncshelf.config import PERMISSIONS, ProfileConfig
from syncshelf.db import Database
from syncshelf.sync.limiter import interactive
from syncshelf.sync.mirror import Mirror

__all__ = [
    "HOUSEHOLD",
    "ME",
    "REGISTRY",
    "RegisteredTool",
    "Services",
    "SyncShelfServer",
    "ToolRegistry",
    "current_profile",
    "resolve_profiles",
    "services",
]

ME = "me"
"""The ``profile`` argument for the caller's own profile, and its default (§2.8)."""

HOUSEHOLD = "household"
"""The ``profile`` argument for the caller plus every profile that opted in (§2.8)."""

ToolFn = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class RegisteredTool:
    """One tool as registered: its function, and the permission it needs, if any."""

    fn: ToolFn
    permission: str | None = None
    annotations: ToolAnnotations | None = None

    @property
    def name(self) -> str:
        """The tool's name, which is its function's name."""
        return self.fn.__name__


class ToolRegistry:
    """The tools SyncShelf serves, in registration order."""

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def tool(
        self, *, permission: str | None = None, annotations: ToolAnnotations | None = None
    ) -> Callable[[ToolFn], ToolFn]:
        """Register the decorated ``async def`` as a tool.

        Its docstring is the description Claude reads (§2.10). ``permission`` is one of
        :data:`syncshelf.config.PERMISSIONS`: a profile without it never sees the tool.
        """
        if permission is not None and permission not in PERMISSIONS:
            raise ValueError(f"unknown permission {permission!r}")

        def register(fn: ToolFn) -> ToolFn:
            # A plain def runs in a worker thread in SDK v2, where asyncio locks can't be used.
            if not iscoroutinefunction(fn):
                raise TypeError(f"tool {fn.__name__} must be an async def")
            if fn.__name__ in self._tools:
                raise ValueError(f"tool {fn.__name__} is already registered")
            self._tools[fn.__name__] = RegisteredTool(fn, permission, annotations)
            return fn

        return register

    def __iter__(self) -> Iterator[RegisteredTool]:
        return iter(self._tools.values())


REGISTRY = ToolRegistry()
"""The production registry, which every tool module registers on."""


@dataclass(frozen=True)
class Services:
    """What a tool reaches beyond its arguments and its caller."""

    db: Database
    clock: Callable[[], float]
    """Wall-clock epoch seconds: ``create_app``'s one clock (§2.9)."""
    mirror: Mirror
    """Each profile's copy of its Hardcover library (§3.3.4)."""


_services: ContextVar[Services | None] = ContextVar("syncshelf_services", default=None)


def services() -> Services:
    """The running app's database and clock, for the tool being called."""
    bound = _services.get()
    if bound is None:
        # Tools run only inside a call, after the lifespan has bound the server.
        raise RuntimeError("no SyncShelf services are bound to this call")
    return bound


class SyncShelfServer(MCPServer[Any]):
    """An ``MCPServer`` serving a registry's tools, each only to the profiles allowed it."""

    def __init__(
        self,
        registry: ToolRegistry,
        profiles: Mapping[str, ProfileConfig],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._profiles = profiles
        self._permissions = {tool.name: tool.permission for tool in registry}
        self._services: Services | None = None
        for tool in registry:
            self.add_tool(tool.fn, annotations=tool.annotations)

    def bind(self, bound: Services | None) -> None:
        """Give tools ``bound`` from now on, or nothing again when ``None``."""
        self._services = bound

    async def list_tools(self) -> list[MCPTool]:
        """The tools the caller's profile may use."""
        return [tool for tool in await super().list_tools() if self._allowed(tool.name)]

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        """Run a tool, refusing one the caller's profile may not use."""
        if not self._allowed(name):
            if name not in self._permissions:
                # Hidden from tools/list, so it must not run either; read as unknown.
                raise ToolError(f"Unknown tool: {name}")
            raise ToolError(
                f"The {name} tool needs the {self._permissions[name]} permission, "
                "which this SyncShelf profile doesn't have."
            )
        reset = _services.set(self._services)
        try:
            # A tool call is someone waiting: its Hardcover calls jump the sync queue (§3.3.3).
            with interactive():
                return await super().call_tool(name, arguments, context)
        finally:
            _services.reset(reset)

    def _allowed(self, name: str) -> bool:
        # Only registry tools are served: anything added to the server another way is hidden.
        if name not in self._permissions:
            return False
        needed = self._permissions[name]
        if needed is None:
            return True
        token = get_access_token()
        profile = self._profiles.get(token.subject or "") if token is not None else None
        return profile is not None and needed in profile.permissions


def current_profile() -> str:
    """The calling profile's name, from the request's access token (§2.2)."""
    token = get_access_token()
    if token is None or not token.subject:
        # The SDK's bearer check refuses a request without a token before any tool runs.
        raise ToolError("This call isn't signed in to a SyncShelf profile.")
    return token.subject


def resolve_profiles(
    requested: str, caller: str, profiles: Mapping[str, ProfileConfig]
) -> tuple[str, ...]:
    """The profiles a read tool's ``profile`` argument names, for ``caller`` (§2.8).

    ``"me"`` is the caller. ``"household"`` is the caller, then every other profile with
    ``household_visible: true`` in name order. Any other value is a profile name, which must
    be the caller's own or one that opted in. Anything else is refused as a tool error rather
    than answered with nothing. The keywords are checked before names (#74).
    """
    if requested == ME:
        return (caller,)
    if requested == HOUSEHOLD:
        others = sorted(n for n, p in profiles.items() if n != caller and p.household_visible)
        return (caller, *others)
    if requested == caller:
        return (caller,)
    profile = profiles.get(requested)
    if profile is None:
        raise ToolError(
            f"There is no profile named {requested!r}. Use 'me', 'household' or a profile name."
        )
    if not profile.household_visible:
        raise ToolError(
            f"The {requested} profile hasn't opted in to sharing (household_visible), "
            "so its books can't be shown."
        )
    return (requested,)
