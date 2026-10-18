"""``my_connections`` and ``revoke_connection``: a profile's own Claude connections (SPEC §3.2.7).

A connection is a grant (:mod:`syncshelf.auth.tokens`). Both tools need no permission, so every
profile has them, and both touch only the caller's grants: another profile's grant id reads as
unknown, so it isn't even confirmed to exist. The current call's grant comes from its access
token's ``grant_id`` claim. A dev-mode token has none, so in dev mode no grant is current.
"""

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Literal, TypedDict

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Strict

from syncshelf.auth.tokens import Grant, open_grants, revoke_grant
from syncshelf.tools import REGISTRY, current_profile, services

__all__ = ["CLIENT_NAMES", "Connection", "my_connections", "revoke_connection"]

CLIENT_NAMES = {"claude.ai": "Claude app", "Claude Code": "Claude Code"}
"""The name a grant's ``client_label`` is shown under."""

REVOKE_REASON = "user"

GrantId = Annotated[int, Strict()]
"""A grant id, strictly: lax validation would read ``true`` or ``"1"`` as grant 1, and a
revocation can't be undone."""

log = logging.getLogger(__name__)


class Connection(TypedDict):
    """One connection as the tools return it."""

    grant_id: int
    client: str
    created_at: str
    last_used_at: str
    current: bool


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _current_grant() -> int | None:
    token = get_access_token()
    claims = token.claims if token is not None else None
    return claims.get("grant_id") if claims else None


def _connection(grant: Grant, current: int | None) -> Connection:
    return Connection(
        grant_id=grant.id,
        client=CLIENT_NAMES.get(grant.client_label, grant.client_label),
        created_at=_iso(grant.created_at),
        last_used_at=_iso(grant.last_used_at),
        current=grant.id == current,
    )


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=True))
async def my_connections() -> list[Connection]:
    """List the Claude connections signed in to the caller's SyncShelf profile.

    Use this when the user asks what is connected to their SyncShelf, or before revoking a
    connection, to find its grant_id. Each Claude app (web, desktop, mobile) or Claude Code
    sign-in is one connection. Only the caller's own open connections are listed, oldest
    first; revoked ones are left out.

    Each row has grant_id (pass it to revoke_connection), client ("Claude app" or "Claude
    Code"), created_at and last_used_at (ISO 8601, UTC; last_used_at moves at most once a
    minute), and current (true for the connection making this call). A local dev-mode
    session isn't a connection, so it isn't listed and no row is current; an empty list
    there is normal.
    """
    current = _current_grant()
    grants = await services().db.read(open_grants, current_profile())
    return [_connection(grant, current) for grant in grants]


@REGISTRY.tool()
async def revoke_connection(target: GrantId | Literal["all_others", "all"]) -> list[Connection]:
    """Revoke Claude connections to the caller's SyncShelf profile, signing them out.

    Use this when the user wants to disconnect a device or client, or to sign out
    everywhere. It can only revoke the caller's own connections. target is one of:

    - a grant_id from my_connections: revoke that one connection;
    - "all_others": revoke every connection except the one making this call;
    - "all": revoke every connection, including this one. This ends the current session:
      the next call fails and Claude has to reconnect to SyncShelf. Say so before using it.
      (A local dev-mode session is the exception: it isn't a connection, so it carries on.)

    Revoking can't be undone; the client signs in again to reconnect. Returns the
    connections revoked, in my_connections' row shape (empty when there was nothing else to
    revoke).
    """
    profile = current_profile()
    current = _current_grant()
    bound = services()
    revoked = await bound.db.write(_revoke, profile, target, current, int(bound.clock()))
    if revoked is None:
        raise ToolError(
            f"There is no open connection {target} on this SyncShelf profile. "
            "Call my_connections to see the connections that can be revoked."
        )
    # Their held refresh pairs go too (§3.2.7). /token checks the grant before serving one,
    # so this only ends the raw tokens' time in memory.
    bound.grace.drop_grants({grant.id for grant in revoked})
    for grant in revoked:
        log.info(
            "revoked grant %d at the user's request",
            grant.id,
            extra={
                "event": "grant_revoked",
                "grant_id": grant.id,
                "client_label": grant.client_label,
                "reason": REVOKE_REASON,
            },
        )
    return [_connection(grant, current) for grant in revoked]


def _revoke(
    conn: sqlite3.Connection,
    profile: str,
    target: int | str,
    current: int | None,
    now: int,
) -> list[Grant] | None:
    """Revoke ``profile``'s grants that ``target`` names and return them, or ``None`` when
    ``target`` is an id that isn't one of its open grants."""
    grants = open_grants(conn, profile)
    if target == "all":
        chosen = grants
    elif target == "all_others":
        chosen = [grant for grant in grants if grant.id != current]
    else:
        chosen = [grant for grant in grants if grant.id == target]
        if not chosen:
            return None
    for grant in chosen:
        revoke_grant(conn, grant.id, REVOKE_REASON, now)
    return chosen
