"""The one injectable test clock (SPEC §2.9).

Everything time-driven (the limiter, schedules, TTLs, quarantine, the Chaptarr watch, OAuth
expiry) takes a clock, so tests move time forward instead of sleeping.
"""

from datetime import UTC, datetime

import anyio


class FakeClock:
    """A clock that stands still until a test advances it.

    ``time()`` is wall-clock epoch seconds and ``monotonic()`` is seconds since the clock was
    made; both move together and only forward. ``sleep()`` advances time and returns at once.
    """

    START = 1_767_225_600
    """2026-01-01T00:00:00Z: a fixed, recognisable start so failures read the same every run."""

    def __init__(self, start: float = START) -> None:
        self._wall = float(start)
        self._mono = 0.0

    def time(self) -> float:
        """Wall-clock time in epoch seconds (UTC)."""
        return self._wall

    def monotonic(self) -> float:
        """Seconds elapsed since the clock was created."""
        return self._mono

    def now(self) -> datetime:
        """The current wall-clock time as an aware UTC datetime."""
        return datetime.fromtimestamp(self._wall, UTC)

    def advance(self, seconds: float) -> None:
        """Move time forward by ``seconds``. Time never goes backwards."""
        if seconds < 0:
            raise ValueError("FakeClock cannot go backwards")
        self._wall += seconds
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        """Advance by ``seconds`` and yield once, so other tasks get a turn, without waiting."""
        self.advance(seconds)
        await anyio.lowlevel.checkpoint()
