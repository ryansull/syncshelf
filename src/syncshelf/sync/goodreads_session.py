"""The Goodreads session state (SPEC §3.4b.1, §3.4b.2).

``goodreads_sessions`` holds one row per profile saying where that person's sign-in stands.
The session itself is the cookie jar on disk (§3.4b.3), never here, so a copy of the database
carries no live session.

This module holds the startup reconciliation and the cookie jar; the sign-in state machine and
the sidecar connection arrive with
`#224 <https://github.com/ryansull/syncshelf/issues/224>`_.

The database units here are plain functions on the writer connection, run inside its
transaction. The jar functions touch only the filesystem.
"""

import json
import logging
import os
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

__all__ = [
    "AMAZON_COOKIES",
    "GOODREADS_DIR",
    "JAR_MODE",
    "Cookie",
    "CookieJar",
    "SessionReconcile",
    "SessionState",
    "jar_path",
    "load_jar",
    "reconcile_configured",
    "save_jar",
]

log = logging.getLogger(__name__)

SessionState = Literal[
    "ok",
    "needs_sign_in",
    "awaiting_approval",
    "awaiting_code",
    "challenged",
    "bad_credentials",
    "disabled",
]
"""Every state ``goodreads_sessions.state`` accepts (Appendix A, §3.4b.2).

``disabled`` means this profile has no Goodreads credentials, so there is nothing to sign in
to. An absent ``services.browser_ws`` is **not** this state: it switches 4b off stack-wide as
configuration, and never per profile (`#232
<https://github.com/ryansull/syncshelf/issues/232>`_).
"""


@dataclass(frozen=True)
class SessionReconcile:
    """What one startup reconciliation did, for the log."""

    disabled: list[str]
    """Profiles newly moved to ``disabled``, having no credentials configured."""
    enabled: list[str]
    """Profiles moved off ``disabled`` to ``needs_sign_in``, now that they have credentials."""


def reconcile_configured(
    conn: sqlite3.Connection,
    with_credentials: Collection[str],
    without_credentials: Collection[str],
    now: int,
) -> SessionReconcile:
    """Make each profile's session state agree with whether it has credentials, at ``now``.

    A unit of work for :meth:`syncshelf.db.Database.write`. Startup passes the configured
    profiles split by whether ``goodreads.password`` is set, so the state never claims a
    sign-in that configuration has made impossible:

    - a profile **without** credentials becomes ``disabled``, whatever state it held. Nothing
      about the old state is actionable once there is no password to sign in with, and the
      stickiness of ``challenged`` and ``bad_credentials`` (§3.4b.2) only governs *unattended*
      reuse, which cannot happen either;
    - a profile **with** credentials that sits at ``disabled`` becomes ``needs_sign_in``, so it
      is offered a sign-in rather than staying switched off. Any other state is left alone,
      because only the sign-in machine moves a session out of a state it genuinely reached.

    Running it again changes nothing. Returns which profiles moved, each list sorted.
    """
    disabled = _move(conn, without_credentials, "disabled", now, "state <> 'disabled'")
    enabled = _move(conn, with_credentials, "needs_sign_in", now, "state = 'disabled'")
    return SessionReconcile(disabled, enabled)


def _move(
    conn: sqlite3.Connection,
    profiles: Collection[str],
    state: SessionState,
    now: int,
    when: str,
) -> list[str]:
    """Set ``state`` on each of ``profiles`` whose row matches ``when``, inserting a missing
    row. Returns the profiles that changed, sorted."""
    moved: list[str] = []
    for name in sorted(profiles):
        changed = conn.execute(
            "INSERT INTO goodreads_sessions (profile, state, state_since) VALUES (?, ?, ?) "
            f"ON CONFLICT (profile) DO UPDATE SET state = ?, state_since = ? WHERE {when}",
            (name, state, now, state, now),
        ).rowcount
        if changed:
            moved.append(name)
    return moved


# --- the cookie jar (§3.4b.3) ---------------------------------------------------------------

GOODREADS_DIR = "goodreads"
"""The data subdirectory holding each profile's jar. Checked writable from phase 1."""

JAR_MODE = 0o600
"""The jar's file mode. The session is a credential, so only the service user may read it."""

SESSION_COOKIE = "_session_id2"
"""Rails' own session cookie, regenerated from the Amazon set through a redirect (§3.4b.1)."""

AMAZON_COOKIES = frozenset(
    {
        "at-main",
        "sess-at-main",
        "session-token",
        "x-main",
        "ubid-main",
        "session-id",
        "session-id-time",
        "sst-main",
    }
)
"""The Amazon SSO set scoped to ``.goodreads.com``. §3.4b.1 calls this **the long-lived
credential**, so a jar missing any of it cannot carry a session forward."""

OPTIONAL_COOKIES = frozenset({"ccsid", "aws-waf-token"})
"""Kept when present, never required (§3.4b.1)."""

STRIPPED_COOKIES = frozenset({"jwt_token"})
"""Dropped on every write: it expires within minutes, and sending a stale one breaks the
request outright (§3.4b.1)."""

_REQUIRED_FIELDS = ("name", "value", "domain")


class Cookie(TypedDict):
    """One stored cookie.

    The field names are Playwright's, so a jar round-trips through a browser context with no
    translation: :func:`load_jar` feeds ``add_cookies`` and ``cookies()`` feeds
    :func:`save_jar`.
    """

    name: str
    value: str
    domain: str
    path: str
    expires: NotRequired[float]
    httpOnly: NotRequired[bool]
    secure: NotRequired[bool]
    sameSite: NotRequired[str]


@dataclass(frozen=True)
class CookieJar:
    """A profile's stored cookies.

    ``cookies`` is kept out of ``repr`` so that logging a jar, or an object holding one, can
    never print a cookie value (§3.4b.3). :meth:`names` is the only form a log line may carry.
    """

    cookies: tuple[Cookie, ...] = field(default=(), repr=False)

    def __bool__(self) -> bool:
        return bool(self.cookies)

    def names(self) -> list[str]:
        """Each cookie as ``name@domain``, sorted. Safe to log: no values."""
        return sorted(f"{cookie['name']}@{cookie['domain']}" for cookie in self.cookies)

    @property
    def has_session(self) -> bool:
        """Whether the whole Amazon set is present, which is what makes a jar reusable.

        ``_session_id2`` is deliberately **not** required: §3.4b.1 says it is regenerated from
        the Amazon set through a redirect, so a jar without it still signs in.
        """
        return {cookie["name"] for cookie in self.cookies} >= AMAZON_COOKIES


def jar_path(data_dir: Path, profile: str) -> Path:
    """Where ``profile``'s jar lives: ``<data_dir>/goodreads/<profile>.cookies.json``."""
    return data_dir / GOODREADS_DIR / f"{profile}.cookies.json"


def load_jar(path: Path) -> CookieJar:
    """Read the jar at ``path``, or an empty jar when there is no usable session.

    A missing file is the ordinary first-run case. A malformed or unreadable one is treated the
    same way rather than raising: the cost of an empty jar is one sign-in, while refusing to
    start over a corrupt cache would take the whole service down. Nothing here is logged with a
    cookie value in it.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CookieJar()
    except OSError, ValueError:
        log.warning(
            "the Goodreads cookie jar could not be read, so this profile must sign in again",
            extra={"event": "goodreads_jar_unreadable", "jar": path.name},
        )
        return CookieJar()
    cookies = _cookies(raw)
    if cookies is None:
        log.warning(
            "the Goodreads cookie jar is not in the expected shape, so it is being ignored",
            extra={"event": "goodreads_jar_malformed", "jar": path.name},
        )
        return CookieJar()
    jar = CookieJar(tuple(cookies))
    log.debug(
        "read the Goodreads cookie jar",
        extra={"event": "goodreads_jar_read", "jar": path.name, "cookies": jar.names()},
    )
    return jar


def save_jar(jar: CookieJar, path: Path) -> CookieJar:
    """Write ``jar`` to ``path`` at mode 0600, atomically, and return what was stored.

    ``jwt_token`` is dropped here rather than at the call sites, so no path can store one. The
    file is opened 0600 by hand so it is never briefly readable by anyone else, written under a
    temporary name, and then moved into place in one step: a crash part-way through leaves the
    previous jar intact rather than a half-written one.
    """
    stored = tuple(cookie for cookie in jar.cookies if cookie["name"] not in STRIPPED_COOKIES)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, JAR_MODE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(list(stored), handle)
        # A umask can clear bits O_CREAT asked for, and an existing temp file keeps its own.
        os.chmod(partial, JAR_MODE)
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    saved = CookieJar(stored)
    log.debug(
        "wrote the Goodreads cookie jar",
        extra={"event": "goodreads_jar_written", "jar": path.name, "cookies": saved.names()},
    )
    return saved


def _cookies(raw: object) -> list[Cookie] | None:
    """``raw`` as cookies, or ``None`` when it is not a list of usable cookie objects.

    One unusable entry rejects the whole jar: a partial session is not worth guessing at, and
    signing in again is cheap.
    """
    if not isinstance(raw, list):
        return None
    found: list[Cookie] = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        if not all(isinstance(item.get(name), str) for name in _REQUIRED_FIELDS):
            return None
        path = item.get("path")
        found.append(cast(Cookie, {**item, "path": path if isinstance(path, str) else "/"}))
    return found
