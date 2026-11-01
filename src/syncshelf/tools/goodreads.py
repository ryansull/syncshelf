"""``goodreads_sign_in`` and ``goodreads_verify``: starting an attended Goodreads sign-in
(SPEC §3.4b.6, Appendix B).

Both act on the **caller's own** profile only. Neither returns a cookie, a code or a password,
and the ``detail`` they pass on was scrubbed where it was made (§3.4b.2).

A sign-in returns as soon as the outcome is known while the browser keeps waiting, so these
two are a pair: ``goodreads_sign_in`` may answer ``awaiting_code``, and ``goodreads_verify``
finishes that attempt. Nothing survives a restart, which is why a code submitted to a sign-in
this process no longer holds says to start again rather than failing obscurely (#230).
"""

from typing import TypedDict

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from syncshelf.sync.goodreads_session import ADVICE, NoSidecar, NotWaiting, SessionState
from syncshelf.tools import REGISTRY, current_profile, services

__all__ = ["SignInState", "goodreads_sign_in", "goodreads_verify"]


class SignInState(TypedDict):
    """Where the caller's sign-in stands, and what to do next."""

    state: SessionState
    detail: str | None
    """A scrubbed reason: never a password, a cookie or the code."""
    next: str
    """What the person does now, in words Claude can pass on."""


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False))
async def goodreads_sign_in() -> SignInState:
    """Sign in to Goodreads as yourself, so SyncShelf can run your library export.

    Returns at once with where the sign-in got to; the browser keeps going in the background.
    If it needs a one-time code, ask the person for it and call goodreads_verify. If it says a
    bot check appeared, SyncShelf stops: it never works around one.

    Use it again to retry after a bot check or a rejected password, which otherwise stay stuck
    until a person asks. Only your own profile can be signed in.
    """
    caller = current_profile()
    bound = services()
    goodreads = bound.profiles[caller].goodreads
    if goodreads is None or goodreads.password is None or goodreads.email is None:
        raise ToolError(
            "Your profile has no Goodreads email and password configured, so there is nothing "
            "to sign in to. Goodreads still syncs through its RSS feed."
        )
    try:
        result = await bound.sign_ins.start(caller, goodreads.email, goodreads.password)
    except NoSidecar as error:
        raise ToolError(
            "This SyncShelf has no browser sidecar configured (services.browser_ws), so it "
            "cannot sign in to Goodreads. Your RSS sync is unaffected."
        ) from error
    return SignInState(state=result.state, detail=result.detail, next=ADVICE[result.state])


@REGISTRY.tool(annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False))
async def goodreads_verify(code: str) -> SignInState:
    """Give Goodreads the one-time code it asked for during your sign-in.

    - code: the code Amazon sent, exactly as received.

    Only works while a sign-in of yours is waiting for one. If SyncShelf restarted in the
    meantime, that sign-in is gone and you start again with goodreads_sign_in.
    """
    caller = current_profile()
    try:
        result = await services().sign_ins.verify(caller, code)
    except NotWaiting as error:
        raise ToolError(
            "No sign-in of yours is waiting for a code. Run goodreads_sign_in to start one."
        ) from error
    return SignInState(state=result.state, detail=result.detail, next=ADVICE[result.state])
