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
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NotRequired, Protocol, TypedDict, cast
from urllib.parse import urlsplit

__all__ = [
    "AMAZON_COOKIES",
    "APPROVAL_WAIT",
    "GOODREADS_SUFFIX",
    "INTERRUPTED",
    "PENDING_STATES",
    "SIGN_IN_URL",
    "AUTO_SIGN_IN_EVERY",
    "STICKY_STATES",
    "GOODREADS_DIR",
    "JAR_MODE",
    "Cookie",
    "AutoSignIn",
    "Browser",
    "NoSidecar",
    "NotWaiting",
    "SignIns",
    "PageFacts",
    "SidecarBrowser",
    "CookieJar",
    "SessionReconcile",
    "SessionState",
    "SignIn",
    "SignInOutcome",
    "SignInResult",
    "attempt_sign_in",
    "auto_sign_in_state",
    "may_sign_in_again",
    "classify",
    "harvest",
    "jar_path",
    "load_jar",
    "reconcile_configured",
    "record",
    "reset_pending",
    "stamp_auto_sign_in",
    "save_jar",
]

log = logging.getLogger(__name__)


class Writer(Protocol):
    """The database access this module needs: :class:`syncshelf.db.Database`'s two
    methods."""

    async def write(self, unit: Any, /, *args: Any) -> Any: ...

    async def read(self, unit: Any, /, *args: Any) -> Any: ...


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


# --- signing in (§3.4b.2) -------------------------------------------------------------------

SIGN_IN_URL = "https://www.goodreads.com/ap/signin"
"""Amazon's authentication portal, hosted on Goodreads' own domain (§3.4b.1)."""

GOODREADS_SUFFIX = "goodreads.com"
"""Cookies are harvested for this domain only — never ``accounts.google.com``,
``appleid.apple.com`` or ``amazon.com`` (§3.4b.2)."""

APPROVAL_WAIT = 300.0
"""§3.4b.2: the browser waits up to 5 minutes while the person taps Approve."""

INTERRUPTED = "the sign-in was interrupted by a restart"
"""The ``detail`` left on a session that :func:`reset_pending` moved (#230)."""

PENDING_STATES: tuple[SessionState, ...] = ("awaiting_approval", "awaiting_code")
"""The two states that exist only while a browser is waiting.

Nothing but a live process can be behind them, so startup resets them (:func:`reset_pending`),
exactly as it marks a run left ``running`` as ``aborted``. Decided on #230.
"""

SignInOutcome = Literal["ok", "awaiting_approval", "awaiting_code", "challenged", "bad_credentials"]
"""What one sign-in attempt ended as (§3.4b.2's outcome table)."""


@dataclass(frozen=True)
class SignInResult:
    """One attempt's outcome and, when it signed in, the cookies it harvested."""

    outcome: SignInOutcome
    jar: CookieJar = field(default_factory=CookieJar)
    detail: str | None = None
    """A scrubbed reason, safe for ``goodreads_sessions.detail`` and for a tool result."""

    @property
    def state(self) -> SessionState:
        """The session state this outcome becomes. The names line up by design."""
        return self.outcome


class SignIn(Protocol):
    """One attended sign-in in progress, in a browser context seeded from the jar.

    A protocol so the state machine can be driven by a fake: §3.4b.7 says sign-in is **never**
    automated against Goodreads, so no test may touch a real browser.
    """

    async def submit_credentials(self, email: str, password: str) -> SignInOutcome:
        """Fill ``ap_email`` / ``ap_password``, tick ``rememberMe``, submit, and say what
        appeared."""
        ...

    async def submit_code(self, code: str) -> SignInOutcome:
        """Submit a one-time code to a sign-in waiting for one."""
        ...

    async def cookies(self) -> list[Cookie]:
        """Every cookie the context holds, for :func:`harvest` to filter."""
        ...

    async def close(self) -> None:
        """Release the browser context. The sidecar keeps no state of its own."""
        ...


class Browser(Protocol):
    """Somewhere to open a sign-in: the Playwright sidecar in the service, a fake in tests."""

    async def open_sign_in(self, jar: CookieJar) -> SignIn:
        """Seed a fresh context from ``jar`` and open Amazon's sign-in page.

        Seeding matters even when the jar holds no session: ``ubid-main`` and ``session-id``
        tell Amazon this is a known device, which makes a challenge less likely (§3.4b.2).
        """
        ...


def harvest(cookies: Iterable[Cookie]) -> CookieJar:
    """The Goodreads cookies from ``cookies``, as a jar.

    Everything else is dropped rather than stored: the sign-in passes through Amazon, so a
    context can end up holding cookies for hosts SyncShelf has no business keeping (§3.4b.2).
    ``jwt_token`` is removed by :func:`save_jar` on the way to disk.
    """
    kept = tuple(cookie for cookie in cookies if _is_goodreads(cookie["domain"]))
    return CookieJar(tuple(sorted(kept, key=lambda c: (c["name"], c["domain"]))))


def _is_goodreads(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return host == GOODREADS_SUFFIX or host.endswith(f".{GOODREADS_SUFFIX}")


async def attempt_sign_in(
    browser: Browser, jar: CookieJar, email: str, password: str
) -> SignInResult:
    """Sign in once, attended, and report where it got to (§3.4b.2).

    Returns as soon as the outcome is known. ``ok`` carries the harvested jar; the two waiting
    outcomes carry none, because nothing has signed in yet. ``challenged`` and
    ``bad_credentials`` are sticky states for the caller to record: a challenge means stop, and
    a rejected password is **never** retried, so SyncShelf cannot lock the account.

    SyncShelf drives a plain browser and never solves, evades or works around a CAPTCHA, a bot
    check or a WAF challenge (§3.4b, Boundary).
    """
    session = await browser.open_sign_in(jar)
    try:
        return await _settle(session, await session.submit_credentials(email, password))
    finally:
        await session.close()


async def _settle(session: SignIn, outcome: SignInOutcome) -> SignInResult:
    """One outcome as a result, harvesting only when it actually signed in."""
    if outcome == "ok":
        return SignInResult("ok", harvest(await session.cookies()))
    details: dict[SignInOutcome, str] = {
        "challenged": "a bot check or CAPTCHA appeared",
        "bad_credentials": "Goodreads rejected the password",
        "awaiting_code": "waiting for a one-time code",
        "awaiting_approval": "waiting for approval on your device",
    }
    return SignInResult(outcome, detail=details[outcome])


def record(
    conn: sqlite3.Connection,
    profile: str,
    state: SessionState,
    now: int,
    detail: str | None = None,
) -> None:
    """Write ``profile``'s session state, stamping when it changed.

    ``detail`` is a scrubbed reason only: no password, cookie or CSRF value ever reaches it
    (§3.4b.2).
    """
    conn.execute(
        "INSERT INTO goodreads_sessions (profile, state, state_since, detail) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (profile) DO UPDATE SET state = ?, state_since = ?, detail = ?",
        (profile, state, now, detail, state, now, detail),
    )


def reset_pending(conn: sqlite3.Connection, now: int) -> list[str]:
    """Move every waiting session to ``needs_sign_in`` at ``now``; returns which profiles moved.

    Startup calls this. A browser waiting for an approval or a code lives in the process that
    opened it, and the sidecar keeps no state, so only a dead process can leave one of these
    behind — the same reasoning that marks a run left ``running`` as ``aborted`` (§3.4a.7).
    Leaving the row alone would make ``sync_status`` and ``attention`` describe a wait that
    nothing can finish. Decided on #230.
    """
    placeholders = ", ".join("?" for _ in PENDING_STATES)
    moved = [
        name
        for (name,) in conn.execute(
            f"SELECT profile FROM goodreads_sessions WHERE state IN ({placeholders})",
            PENDING_STATES,
        )
    ]
    if moved:
        conn.execute(
            "UPDATE goodreads_sessions SET state = 'needs_sign_in', state_since = ?, detail = ? "
            f"WHERE state IN ({placeholders})",
            (now, INTERRUPTED, *PENDING_STATES),
        )
    return sorted(moved)


# --- reading the sign-in page, and the sidecar (§3.4b.1, §3.4b.2) ---------------------------

EMAIL_FIELD = "input[name='ap_email']"
PASSWORD_FIELD = "input[name='ap_password']"
REMEMBER_BOX = "input[name='rememberMe']"
OTP_FIELD = "input[name='otpCode'], #auth-mfa-otpcode"
APPROVAL_PROMPT = "#resend-approval-alert, #channelDetails"
CAPTCHA = "#auth-captcha-image, form[action*='captcha']"
AUTH_ERROR = "#auth-error-message-box"
"""The markup §3.4b.1 names, plus Amazon's usual ids for the prompts it can raise.

**Not confirmed against a signed-in page.** §3.4b's opening step (#219) is what records what
really appears; until it runs these are read from the findings comment on #24 and from Amazon's
public sign-in markup, and :func:`classify` is written so an unrecognised page is safe.
"""


@dataclass(frozen=True)
class PageFacts:
    """What the sign-in page showed, reduced to the few things that decide the outcome.

    Keeping this separate from the browser is what makes §3.4b.2's outcome table testable: the
    rules live in :func:`classify`, and only the gathering needs a real browser.
    """

    url: str
    has_otp_field: bool = False
    has_approval_prompt: bool = False
    has_captcha: bool = False
    has_auth_error: bool = False

    @property
    def on_goodreads(self) -> bool:
        """Whether the browser has landed back on Goodreads, away from the sign-in portal."""
        host = urlsplit(self.url).hostname or ""
        return _is_goodreads(host) and not urlsplit(self.url).path.startswith("/ap/")


def classify(facts: PageFacts) -> SignInOutcome:
    """§3.4b.2's outcome table, as one decision.

    The order matters. A CAPTCHA is checked before anything else, because a page can show one
    *and* look otherwise ordinary, and a challenge means stop. Landing back on Goodreads is
    what "signed in" means. Anything unrecognised is ``challenged``, not an error: §3.4b.2 puts
    "any page SyncShelf doesn't recognise" in that row, and stopping is always safe.
    """
    if facts.has_captcha:
        return "challenged"
    if facts.has_auth_error:
        return "bad_credentials"
    if facts.has_otp_field:
        return "awaiting_code"
    if facts.has_approval_prompt:
        return "awaiting_approval"
    if facts.on_goodreads:
        return "ok"
    return "challenged"


class SidecarBrowser:
    """Sign-ins driven through the Playwright sidecar (§3.4b.2).

    ``playwright`` is imported inside :meth:`open_sign_in`, not at module scope, so a stack with
    no ``services.browser_ws`` never loads it and the offline test suite never touches it
    (SPEC §2.1). SyncShelf drives a **plain** Chromium: no stealth patches and no fingerprint
    changes (§3.4b, Boundary).

    Not covered by the offline suite: §3.4b.7 says sign-in is never automated against Goodreads,
    so what exercises this is the attended opening test (#219).
    """

    def __init__(self, ws_endpoint: str, *, approval_wait: float = APPROVAL_WAIT) -> None:
        self._ws_endpoint = ws_endpoint
        self._approval_wait = approval_wait

    async def open_sign_in(self, jar: CookieJar) -> SignIn:
        from playwright.async_api import async_playwright  # noqa: PLC0415 — lazy by design

        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect(self._ws_endpoint)
        context = await browser.new_context()
        if jar.cookies:
            # Seeded so Amazon sees a known device (ubid-main, session-id) and is less likely
            # to challenge (§3.4b.2).
            await context.add_cookies(list(jar.cookies))  # type: ignore[arg-type]
        page = await context.new_page()
        await page.goto(SIGN_IN_URL)
        return _SidecarSignIn(playwright, context, page, self._approval_wait)


class _SidecarSignIn:
    """One sign-in in a sidecar context. Gathers :class:`PageFacts`; the rules are in
    :func:`classify`."""

    def __init__(
        self, playwright: object, context: object, page: object, approval_wait: float
    ) -> None:
        self._playwright = playwright
        self._context = context
        self._page = page
        self._approval_wait = approval_wait

    async def submit_credentials(self, email: str, password: str) -> SignInOutcome:
        page = self._page
        await page.fill(EMAIL_FIELD, email)  # type: ignore[attr-defined]
        await page.fill(PASSWORD_FIELD, password)  # type: ignore[attr-defined]
        if await page.locator(REMEMBER_BOX).count():  # type: ignore[attr-defined]
            await page.check(REMEMBER_BOX)  # type: ignore[attr-defined]
        await page.press(PASSWORD_FIELD, "Enter")  # type: ignore[attr-defined]
        return classify(await self._facts())

    async def submit_code(self, code: str) -> SignInOutcome:
        await self._page.fill(OTP_FIELD, code)  # type: ignore[attr-defined]
        await self._page.press(OTP_FIELD, "Enter")  # type: ignore[attr-defined]
        return classify(await self._facts())

    async def cookies(self) -> list[Cookie]:
        return cast(list[Cookie], await self._context.cookies())  # type: ignore[attr-defined]

    async def close(self) -> None:
        await self._context.close()  # type: ignore[attr-defined]
        await self._playwright.stop()  # type: ignore[attr-defined]

    async def _facts(self) -> PageFacts:
        page = self._page
        return PageFacts(
            url=page.url,  # type: ignore[attr-defined]
            has_otp_field=bool(await page.locator(OTP_FIELD).count()),  # type: ignore[attr-defined]
            has_approval_prompt=bool(
                await page.locator(APPROVAL_PROMPT).count()  # type: ignore[attr-defined]
            ),
            has_captcha=bool(await page.locator(CAPTCHA).count()),  # type: ignore[attr-defined]
            has_auth_error=bool(await page.locator(AUTH_ERROR).count()),  # type: ignore[attr-defined]
        )


# --- attended sign-ins in progress (§3.4b.2, §3.4b.6) ---------------------------------------


class NoSidecar(Exception):
    """A sign-in was asked for with no ``services.browser_ws`` configured.

    Config validation already refuses credentials without a sidecar (#232), so reaching this
    means the profile has none either and phase 4b is simply off for this stack.
    """


class NotWaiting(Exception):
    """A code was submitted with no sign-in waiting for one.

    Either none was started, or the process that held it has gone — which is why startup moves
    those rows to ``needs_sign_in`` (#230). Either way the answer is to sign in again.
    """


class SignIns:
    """The attended sign-ins this process is holding (§3.4b.2).

    ``goodreads_sign_in`` returns as soon as the outcome is known while the browser keeps
    waiting, so a sign-in that stopped at an approval or a code has to outlive the call that
    started it. It lives **here, in memory**: the jar is the only thing on disk, and the
    sidecar is stateless, so nothing here survives a restart. That is the whole reason startup
    resets the two waiting states (#230).

    Every outcome is written to ``goodreads_sessions`` as it happens, so ``sync_status`` and
    ``attention`` see it without this object being consulted.
    """

    def __init__(
        self,
        browser: Browser | None,
        *,
        data_dir: Path,
        db: Writer,
        clock: Callable[[], float],
    ) -> None:
        self._browser = browser
        self._data_dir = data_dir
        self._db = db
        self._clock = clock
        self._pending: dict[str, SignIn] = {}

    @property
    def available(self) -> bool:
        """Whether a sign-in can be started at all, i.e. whether a sidecar is configured."""
        return self._browser is not None

    def waiting(self, profile: str) -> bool:
        """Whether this process is holding a sign-in for ``profile``."""
        return profile in self._pending

    async def start(self, profile: str, email: str, password: str) -> SignInResult:
        """Begin an attended sign-in, replacing any this process was already holding.

        Trying afresh is also how a sticky ``challenged`` or ``bad_credentials`` is cleared
        (§3.4b.6): nothing checks the old state first, because the person asking for a sign-in
        is the thing that was being waited for.
        """
        if self._browser is None:
            raise NoSidecar("there is no browser sidecar configured")
        await self._forget(profile)
        session = await self._browser.open_sign_in(load_jar(jar_path(self._data_dir, profile)))
        try:
            outcome = await session.submit_credentials(email, password)
        except BaseException:
            await session.close()
            raise
        return await self._settled(profile, session, outcome)

    async def verify(self, profile: str, code: str) -> SignInResult:
        """Submit a one-time code to the sign-in this process is holding for ``profile``."""
        session = self._pending.get(profile)
        if session is None:
            raise NotWaiting("no sign-in is waiting for a code")
        try:
            outcome = await session.submit_code(code)
        except BaseException:
            await self._forget(profile)
            raise
        return await self._settled(profile, session, outcome)

    async def close(self) -> None:
        """Release every sign-in still being held. Shutdown calls this."""
        for profile in list(self._pending):
            await self._forget(profile)

    async def _settled(self, profile: str, session: SignIn, outcome: SignInOutcome) -> SignInResult:
        """Record ``outcome``, keep the browser only while it is still needed."""
        result = await _settle(session, outcome)
        if result.outcome == "ok":
            save_jar(result.jar, jar_path(self._data_dir, profile))
        if result.outcome in PENDING_STATES:
            self._pending[profile] = session
        else:
            # Signed in, challenged or rejected: nothing is waiting, so let the context go.
            self._pending.pop(profile, None)
            await session.close()
        await self._db.write(record, profile, result.state, int(self._clock()), result.detail)
        return result

    async def attempt_unattended(
        self, profile: str, email: str, password: str, now: int
    ) -> AutoSignIn:
        """Sign in again without a person, when a scheduled export found the session expired.

        Refused outright past a sticky state, and at most once per 24 h per profile, both
        checked before a browser is touched (:func:`may_sign_in_again`).

        **Any prompt abandons the attempt.** An approval or a code needs a person, and nobody
        is watching, so the browser is released and the session becomes ``needs_sign_in`` —
        rather than left waiting for an approval that will never come (§3.4b.2). The attempt is
        stamped either way, so a profile that keeps stopping at a prompt is not retried nightly.
        """
        if self._browser is None:
            raise NoSidecar("there is no browser sidecar configured")
        state, last = await self._db.read(auto_sign_in_state, profile)
        if (refusal := may_sign_in_again(state, last, now)) is not None:
            log.info(
                "not signing in to Goodreads unattended",
                extra={
                    "event": "goodreads_auto_sign_in_refused",
                    "profile": profile,
                    "reason": refusal,
                },
            )
            return AutoSignIn(state or "needs_sign_in", attempted=False, reason=refusal)

        await self._db.write(stamp_auto_sign_in, profile, now)
        result = await self.start(profile, email, password)
        if result.state in PENDING_STATES:
            # Nobody is watching, so the wait cannot be answered. Let it go and say so.
            await self._forget(profile)
            await self._db.write(
                record,
                profile,
                "needs_sign_in",
                now,
                "an unattended sign-in needed a person, so it stopped",
            )
            return AutoSignIn("needs_sign_in", attempted=True, reason="the sign-in needed a person")
        return AutoSignIn(result.state, attempted=True, reason=result.detail)

    async def _forget(self, profile: str) -> None:
        if (session := self._pending.pop(profile, None)) is not None:
            await session.close()


# --- signing in again, unattended (§3.4b.2) -------------------------------------------------

AUTO_SIGN_IN_EVERY = 24 * 3600
"""At most one unattended sign-in per profile in this many seconds (§3.4b.2)."""

STICKY_STATES: tuple[SessionState, ...] = ("challenged", "bad_credentials", "disabled")
"""States no unattended sign-in may pass.

``challenged`` and ``bad_credentials`` are sticky until a person runs ``goodreads_sign_in``
(§3.4b.2): a challenge must not be hammered, and a rejected password must never be retried,
or SyncShelf could lock the account. ``disabled`` has no credentials to try.
"""


@dataclass(frozen=True)
class AutoSignIn:
    """What one unattended attempt did."""

    state: SessionState
    attempted: bool
    """``False`` when it was refused before touching a browser."""
    reason: str | None = None
    """Why it was refused, or the outcome's scrubbed detail."""


def may_sign_in_again(state: SessionState | None, last_attempt: int | None, now: int) -> str | None:
    """Why an unattended sign-in is not allowed now, or ``None`` when it is (§3.4b.2).

    A pure function so the two rules that protect the account — never past a sticky state, and
    never more than once a day — can be read and tested on their own.
    """
    if state in STICKY_STATES:
        return f"the session is {state}, which only a person can clear"
    if last_attempt is not None and now - last_attempt < AUTO_SIGN_IN_EVERY:
        hours = AUTO_SIGN_IN_EVERY // 3600
        return f"an unattended sign-in was already tried within {hours} hours"
    return None


def auto_sign_in_state(
    conn: sqlite3.Connection, profile: str
) -> tuple[SessionState | None, int | None]:
    """``profile``'s session state and when an unattended sign-in was last tried."""
    row = conn.execute(
        "SELECT state, last_auto_sign_in_at FROM goodreads_sessions WHERE profile = ?",
        (profile,),
    ).fetchone()
    return (None, None) if row is None else (row[0], row[1])


def stamp_auto_sign_in(conn: sqlite3.Connection, profile: str, now: int) -> None:
    """Record that an unattended sign-in was tried, whatever came of it.

    The cap counts **attempts**, not successes: a profile whose sign-in keeps stopping at a
    prompt must not be retried every night window either.
    """
    conn.execute(
        "INSERT INTO goodreads_sessions (profile, state, state_since, last_auto_sign_in_at) "
        "VALUES (?, 'needs_sign_in', ?, ?) "
        "ON CONFLICT (profile) DO UPDATE SET last_auto_sign_in_at = ?",
        (profile, now, now, now),
    )
