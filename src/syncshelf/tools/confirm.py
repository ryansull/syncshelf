"""Confirm tokens: a deletion previews first, and acts only on a token from that preview
(SPEC §3.3.9, "Deletions and the confirm token"; reused by ``chaptarr_request`` in phase 6).

A token lives :data:`TTL` seconds, in memory only, and is bound to the profile, the tool, what
it acts on (a book, a read) and a snapshot of what the preview showed. Redeeming one for its
own action spends it, whatever the outcome, so a stale or replayed token never acts: the caller
re-previews and hands back a fresh one. A restart forgets every token, which only costs a fresh
preview.
"""

import hashlib
import json
import secrets
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = ["TTL", "ConfirmTokens", "Redeemed", "snapshot_of"]

TTL = 600.0
"""How long a confirm token stays good: 10 minutes (§3.3.9)."""


class Redeemed(Enum):
    """What redeeming a token found."""

    OK = "ok"
    """Bound to this very action, in time, over an unchanged snapshot: go ahead."""
    UNKNOWN = "unknown"
    """Never issued, already spent, expired, or issued for another action."""
    CHANGED = "changed"
    """Issued for this action, but what it previewed has changed since."""


@dataclass(frozen=True)
class _Issued:
    binding: tuple[Hashable, ...]
    snapshot: str
    expires: float


def snapshot_of(value: Any) -> str:
    """A digest of what a preview showed, to tell on confirm whether it changed."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class ConfirmTokens:
    """The tokens issued and not yet spent, for every profile."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._issued: dict[str, _Issued] = {}

    def issue(self, profile: str, tool: str, subject: Hashable, snapshot: str) -> str:
        """A new token for ``tool`` acting on ``subject`` (a book id, or a (book, read) pair)
        for ``profile``, over what the preview showed (:func:`snapshot_of`)."""
        self._sweep()
        token = secrets.token_urlsafe(16)
        binding = (profile, tool, subject)
        self._issued[token] = _Issued(binding, snapshot, self._clock() + TTL)
        return token

    def redeem(
        self, token: str, profile: str, tool: str, subject: Hashable, snapshot: str
    ) -> Redeemed:
        """Spend ``token`` and say whether it confirms this action over this snapshot."""
        self._sweep()
        issued = self._issued.get(token)
        if issued is None or issued.binding != (profile, tool, subject):
            # Another action's token stays good for it: a mix-up mustn't cost it.
            return Redeemed.UNKNOWN
        del self._issued[token]
        if issued.snapshot != snapshot:
            return Redeemed.CHANGED
        return Redeemed.OK

    def _sweep(self) -> None:
        now = self._clock()
        for token in [t for t, i in self._issued.items() if i.expires <= now]:
            del self._issued[token]
