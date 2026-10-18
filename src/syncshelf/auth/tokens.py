"""Grants, tokens, and the bearer-token check behind ``/mcp`` (SPEC §3.2.4, §3.2.5).

**The store.** A grant is one connection: a profile that allowed one client (§3.2.7 lists and
revokes them). Its access and refresh tokens are opaque, ``ssat_``/``ssrt_`` +
``token_urlsafe(32)``, and only their sha256 hex is stored, so the database never holds a
token that would work. The store functions are units of work for
:meth:`syncshelf.db.Database.write` and :meth:`~syncshelf.db.Database.read`: plain sync
functions taking the connection first and the time as ``now`` (epoch seconds from the one
clock). The code exchange and refresh rotation compose them inside one transaction.

**The verifier.** The MCP SDK, as the resource server, hands every bearer token to
:class:`TokenVerifier`. A token it accepts becomes the request's identity:
``get_access_token().subject`` is the profile name, and ``.claims`` carries ``iss``, ``email``
and ``grant_id`` (§2.2). A token it refuses, or no token at all, gets the SDK's 401 naming the
protected-resource metadata, which is what starts Claude's sign-in (§3.2.1). There is one
branch per ``server.auth_mode``. With ``server.auth_mode`` absent, no branch runs and every
token is refused (#73).

Raw tokens are never logged (§2.7): log lines carry the ``grant_id`` only.
"""

import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields

from mcp.server.auth.provider import AccessToken

from syncshelf.config import Config, DevConfig
from syncshelf.db import Database

__all__ = [
    "ACCESS_PREFIX",
    "ACCESS_TOKEN_TTL",
    "DEV_CLIENT_ID",
    "LAST_USED_INTERVAL",
    "REFRESH_PREFIX",
    "REFRESH_WINDOW",
    "SCOPE",
    "Grant",
    "PresentedToken",
    "TokenVerifier",
    "create_grant",
    "find_access_token",
    "hash_token",
    "insert_access_token",
    "insert_refresh_token",
    "open_grants",
    "revoke_grant",
    "touch_grant",
]

SCOPE = "syncshelf"
"""The scope every accepted token carries (§3.2.2). There is no read/write split."""

DEV_CLIENT_ID = "dev"
"""The ``client_id`` of the dev token, which belongs to no registered client."""

ACCESS_PREFIX = "ssat_"
"""Every access token starts with this, so a refresh token is never looked up as one."""

REFRESH_PREFIX = "ssrt_"
"""Every refresh token starts with this."""

ACCESS_TOKEN_TTL = 3600
"""An access token's lifetime in seconds (§3.2.4)."""

REFRESH_WINDOW = 90 * 86400
"""The sliding refresh window: a grant's ``refresh_expires_at`` is this far past its creation
or its last refresh (§3.2.4). There is no absolute cap."""

LAST_USED_INTERVAL = 60
"""Verification moves a grant's ``last_used_at`` at most this often, in seconds, so a busy
connection doesn't turn every request into a write (§3.2.4, assembly choice)."""

log = logging.getLogger(__name__)


def hash_token(raw: str) -> str:
    """The sha256 hex of a raw token or code: the only form the database holds."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Grant:
    """One ``oauth_grant`` row: a profile's connection to one client."""

    id: int
    profile: str
    email: str
    client_id: str
    client_label: str
    resource: str
    scopes: tuple[str, ...]
    created_at: int
    last_used_at: int
    refresh_expires_at: int
    revoked_at: int | None
    revoked_reason: str | None


_GRANT_FIELDS = tuple(f.name for f in fields(Grant))
# Each field is the oauth_grant column of the same name.
_GRANT_COLUMNS = ", ".join(f"g.{name}" for name in _GRANT_FIELDS)


def _grant(row: tuple) -> Grant:
    values = dict(zip(_GRANT_FIELDS, row, strict=True))
    values["scopes"] = tuple(values["scopes"].split())  # stored space-separated
    return Grant(**values)


def create_grant(
    conn: sqlite3.Connection,
    *,
    profile: str,
    email: str,
    client_id: str,
    client_label: str,
    resource: str,
    scopes: Iterable[str],
    now: int,
) -> int:
    """Record a new grant at ``now`` and return its id.

    ``last_used_at`` starts at ``now`` and ``refresh_expires_at`` at ``now`` +
    :data:`REFRESH_WINDOW`. ``email`` is stored as given, so pass it lowercased.
    """
    cursor = conn.execute(
        "INSERT INTO oauth_grant (profile, email, client_id, client_label, resource, scopes, "
        "created_at, last_used_at, refresh_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            profile,
            email,
            client_id,
            client_label,
            resource,
            " ".join(scopes),
            now,
            now,
            now + REFRESH_WINDOW,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def insert_access_token(conn: sqlite3.Connection, grant_id: int, now: int) -> str:
    """Issue an access token on ``grant_id``, valid for :data:`ACCESS_TOKEN_TTL` from ``now``.

    Returns the raw token, which exists only in the caller's hands from here: hand it to the
    client and drop it.
    """
    raw = _new_token(ACCESS_PREFIX)
    conn.execute(
        "INSERT INTO oauth_access_token (token_hash, grant_id, created_at, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (hash_token(raw), grant_id, now, now + ACCESS_TOKEN_TTL),
    )
    return raw


def insert_refresh_token(conn: sqlite3.Connection, grant_id: int, now: int) -> str:
    """Issue a refresh token on ``grant_id`` and return it raw, as
    :func:`insert_access_token` does. Its expiry is the grant's ``refresh_expires_at``."""
    raw = _new_token(REFRESH_PREFIX)
    conn.execute(
        "INSERT INTO oauth_refresh_token (token_hash, grant_id, created_at) VALUES (?, ?, ?)",
        (hash_token(raw), grant_id, now),
    )
    return raw


def open_grants(conn: sqlite3.Connection, profile: str) -> list[Grant]:
    """``profile``'s grants that aren't revoked, oldest first."""
    rows = conn.execute(
        f"SELECT {_GRANT_COLUMNS} FROM oauth_grant g "
        "WHERE g.profile = ? AND g.revoked_at IS NULL ORDER BY g.created_at, g.id",
        (profile,),
    )
    return [_grant(row) for row in rows]


def revoke_grant(conn: sqlite3.Connection, grant_id: int, reason: str, now: int) -> bool:
    """Revoke ``grant_id`` with ``reason`` (one of the schema's ``revoked_reason`` values),
    which kills every token on it. Returns False if it was already revoked, in which case
    its first revocation stands."""
    cursor = conn.execute(
        "UPDATE oauth_grant SET revoked_at = ?, revoked_reason = ? "
        "WHERE id = ? AND revoked_at IS NULL",
        (now, reason, grant_id),
    )
    return cursor.rowcount == 1


def touch_grant(conn: sqlite3.Connection, grant_id: int, now: int) -> bool:
    """Set ``last_used_at`` to ``now`` unless it moved less than :data:`LAST_USED_INTERVAL`
    ago. Returns whether it moved. The guard is in the ``UPDATE``, so two requests racing
    still write once."""
    cursor = conn.execute(
        "UPDATE oauth_grant SET last_used_at = ? WHERE id = ? AND last_used_at <= ?",
        (now, grant_id, now - LAST_USED_INTERVAL),
    )
    return cursor.rowcount == 1


@dataclass(frozen=True)
class PresentedToken:
    """An access token's row, its grant, and whether that grant's profile is removed."""

    expires_at: int
    revoked_at: int | None
    grant: Grant
    profile_removed: bool
    """True when the profile is marked removed, or has no row at all."""

    def refusal(self, now: int) -> str | None:
        """Why this token is refused at ``now``, or ``None`` if it is accepted (§3.2.4)."""
        if now >= self.expires_at:
            return "expired"
        if self.revoked_at is not None:
            return "token_revoked"
        if self.grant.revoked_at is not None:
            return "grant_revoked"
        if self.profile_removed:
            return "profile_removed"
        return None


def find_access_token(conn: sqlite3.Connection, token_hash: str) -> PresentedToken | None:
    """The access token stored under ``token_hash``, whatever its state, or ``None``."""
    row = conn.execute(
        f"SELECT t.expires_at, t.revoked_at, p.name IS NULL OR p.removed_at IS NOT NULL, "
        f"{_GRANT_COLUMNS} FROM oauth_access_token t "
        "JOIN oauth_grant g ON g.id = t.grant_id "
        "LEFT JOIN profiles p ON p.name = g.profile "
        "WHERE t.token_hash = ?",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    expires_at, revoked_at, removed, *grant = row
    return PresentedToken(expires_at, revoked_at, _grant(tuple(grant)), bool(removed))


class TokenVerifier:
    """Implements the SDK's ``TokenVerifier`` protocol for ``config``'s auth mode.

    The OAuth branch reads the token store, so it refuses everything until :meth:`bind`
    gives it the database. The app builds the verifier before its lifespan opens the
    database, and binds it there. ``clock`` gives epoch seconds, as ``create_app``'s does.
    """

    def __init__(self, config: Config, *, clock: Callable[[], float] = time.time) -> None:
        self._config = config
        self._clock = clock
        self._db: Database | None = None

    def bind(self, db: Database | None) -> None:
        """Use ``db`` for the token store from now on, or nothing again when ``None``."""
        self._db = db

    async def verify_token(self, token: str) -> AccessToken | None:
        """The identity ``token`` carries, or ``None`` to refuse it with a 401."""
        server = self._config.server
        if (dev := server.dev_mode) is not None:
            return self._verify_dev(token, dev)
        if server.auth_mode == "oauth":
            return await self._verify_oauth(token)
        return None

    def _verify_dev(self, token: str, dev: DevConfig) -> AccessToken | None:
        server = self._config.server
        if not hmac.compare_digest(token.encode(), dev.token.encode()):
            return None
        return AccessToken(
            token=token,
            client_id=DEV_CLIENT_ID,
            scopes=[SCOPE],
            expires_at=None,
            resource=server.mcp_url,
            subject=dev.profile,
            # A dev token has no grant behind it, so there is no grant id.
            claims={
                "iss": server.public_url,
                "email": self._config.profiles[dev.profile].email,
                "grant_id": None,
            },
        )

    async def _verify_oauth(self, token: str) -> AccessToken | None:
        db = self._db
        if db is None or not token.startswith(ACCESS_PREFIX):
            return None
        found = await db.read(find_access_token, hash_token(token))
        if found is None:
            return None
        now = int(self._clock())
        grant = found.grant
        reason = found.refusal(now)
        if reason is None and grant.profile not in self._config.profiles:
            # The startup profile sync keeps the table and the config in step; this keeps a
            # profile the config doesn't know from reaching the tools even if they drift.
            reason = "profile_removed"
        if reason is not None:
            log.debug(
                "refused an access token on grant %d: %s",
                grant.id,
                reason,
                extra={"event": "token_refused", "grant_id": grant.id, "reason": reason},
            )
            return None
        # Checked here to skip the write transaction on most requests; touch_grant's own
        # guard is the one that holds when two requests race.
        if now - grant.last_used_at >= LAST_USED_INTERVAL:
            try:
                await db.write(touch_grant, grant.id, now)
            except sqlite3.Error, RuntimeError:
                # Bookkeeping only: a busy or closing database must not fail a valid token.
                log.warning(
                    "could not update last_used_at on grant %d",
                    grant.id,
                    exc_info=True,
                    extra={"event": "grant_touch_failed", "grant_id": grant.id},
                )
        return AccessToken(
            token=token,
            client_id=grant.client_id,
            scopes=list(grant.scopes),
            expires_at=found.expires_at,
            # The SDK refuses the token unless this is the MCP URL (validate_token_resource).
            resource=grant.resource,
            subject=grant.profile,
            claims={
                "iss": self._config.server.public_url,
                "email": grant.email,
                "grant_id": grant.id,
            },
        )
