"""The per-profile Hardcover limiter (SPEC §3.3.3).

Hardcover's Free plan gives each user a bucket of 10 refilling at 1/s, plus 5,000 a day, and
the limit is shared by all of that user's tokens and apps. SyncShelf keeps to a smaller bucket
(**burst 8, refill 1/s**) so the user's own Hardcover use always has headroom, and counts a
call's cost as its number of top-level fields, as Hardcover does.

Every Hardcover call for a profile goes through that profile's :class:`HardcoverLimiter`:

- **Interactive calls jump the queue.** Code serving an MCP tool runs inside
  :func:`interactive`, and is served before any queued sync call.
- **A 429 pauses the whole profile** until ``Retry-After`` (else ``Ratelimit-Reset``, else 5 s),
  then the call is sent again. A sync call simply waits. An interactive call that would wait
  more than ~5 s gets :class:`ServeStale` instead, so its tool answers from the mirror.
- **The daily budget.** Below ``hardcover_daily_reserve`` remaining, sync calls get
  :class:`Deferred` until the daily reset (midnight UTC), so their run is ``deferred`` and their
  baselines don't advance. Interactive calls carry on until 0, then get :class:`ServeStale`.
- **``403 request_exceeds_capacity``** is SyncShelf's bug: logged at ERROR and re-raised,
  never retried.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import IntEnum
from itertools import count
from typing import Literal, Protocol

from syncshelf.clients.hardcover import (
    HardcoverCapacityError,
    HardcoverError,
    HardcoverRateLimited,
    RateLimit,
    Result,
)

__all__ = [
    "BURST",
    "DEFAULT_PAUSE",
    "INTERACTIVE_WAIT",
    "REFILL_PER_SECOND",
    "Clock",
    "Deferred",
    "HardcoverLimiter",
    "LimiterState",
    "Priority",
    "ServeStale",
    "SystemClock",
    "interactive",
]

log = logging.getLogger(__name__)

BURST = 8
"""Tokens the bucket holds: Hardcover's burst of 10, less 2 for the user's own use."""

REFILL_PER_SECOND = 1.0

DEFAULT_PAUSE = 5.0
"""How long a 429 pauses the profile when it carries neither ``Retry-After`` nor
``Ratelimit-Reset``."""

INTERACTIVE_WAIT = 5.0
"""The longest an interactive call waits before it is told to serve stale instead."""

MAX_RATE_LIMITED = 5
"""429s in a row on one call before it gives up with the last one."""


class Priority(IntEnum):
    """Lower is served first."""

    INTERACTIVE = 0
    SYNC = 1


_PRIORITY: ContextVar[Priority] = ContextVar("hardcover_priority", default=Priority.SYNC)


@contextmanager
def interactive() -> Iterator[None]:
    """Run the enclosed Hardcover calls at interactive priority: what an MCP tool call does."""
    token = _PRIORITY.set(Priority.INTERACTIVE)
    try:
        yield
    finally:
        _PRIORITY.reset(token)


class Clock(Protocol):
    """Wall time, monotonic time and sleep: what the limiter needs, injectable for tests."""

    def time(self) -> float: ...

    def monotonic(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock."""

    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ServeStale(HardcoverError):
    """An interactive call can't be served soon: the profile is paused for longer than
    :data:`INTERACTIVE_WAIT`, or the daily budget is spent. A read tool answers from the
    mirror, flagged stale; a write tool reports Hardcover as busy."""

    def __init__(self, reason: Literal["rate_limited", "daily_budget"], until: float) -> None:
        what = "rate limited" if reason == "rate_limited" else "out of daily requests"
        super().__init__(f"Hardcover is {what} for this profile until {_iso(until)}")
        self.reason = reason
        self.until = until
        """Wall-clock epoch seconds when calls can go out again."""


class Deferred(HardcoverError):
    """A sync call refused because the day's remaining budget is below the reserve. The run
    is ``deferred`` and tries again after the daily reset."""

    def __init__(self, until: float) -> None:
        super().__init__(f"Hardcover's daily budget is down to the reserve until {_iso(until)}")
        self.until = until


@dataclass(frozen=True)
class LimiterState:
    """What ``sync_status`` shows for a profile's limiter (4a)."""

    limiter: Literal["ok", "paused", "reserve"]
    paused_until: float | None
    """Wall-clock epoch seconds, while paused."""
    daily_remaining: int | None
    """From the last response's ``X-Ratelimit-Daily-Remaining``; ``None`` until one arrives
    or once the daily reset has passed."""


@dataclass(eq=False)
class _Waiter:
    priority: Priority
    seq: int
    cost: int
    wake: asyncio.Future[None] | None = None

    @property
    def key(self) -> tuple[int, int]:
        return (self.priority, self.seq)


class HardcoverLimiter:
    """One profile's token bucket, pause and daily budget. ``run`` gates one call."""

    def __init__(
        self,
        profile: str,
        *,
        reserve: int,
        clock: Clock | None = None,
        burst: int = BURST,
        refill: float = REFILL_PER_SECOND,
    ) -> None:
        self.profile = profile
        self._reserve = reserve
        self._clock: Clock = clock or SystemClock()
        self._burst = burst
        self._refill = refill
        self._tokens = float(burst)
        self._refilled_at = self._clock.monotonic()
        self._paused_until = 0.0  # monotonic
        self._daily_remaining: int | None = None
        self._daily_reset: float | None = None  # wall epoch
        self._in_reserve = False
        self._queue: list[_Waiter] = []
        self._seq = count()

    # --- the call ---------------------------------------------------------------------------

    async def run(self, cost: int, send: Callable[[], Awaitable[Result]]) -> Result:
        """Wait for ``cost`` tokens at the current priority, then ``send`` the call.

        A 429 pauses the profile and the call is sent again (up to :data:`MAX_RATE_LIMITED`
        times), keeping its place in the queue. Raises :class:`ServeStale` or
        :class:`Deferred` when the call can't go out, and re-raises whatever else ``send``
        raises.
        """
        if cost > self._burst:
            log.error(
                "a Hardcover query costs more than the limiter's bucket holds; this is a bug",
                extra={"event": "hardcover_capacity_bug", "profile": self.profile, "cost": cost},
            )
            raise HardcoverError(
                f"a Hardcover call costing {cost} can never fit a bucket of {self._burst}"
            )
        waiter = _Waiter(_PRIORITY.get(), next(self._seq), cost)
        for attempt in range(1, MAX_RATE_LIMITED + 1):
            await self._acquire(waiter)
            try:
                result = await send()
            except HardcoverRateLimited as exc:
                # A 429 also carries the daily count: when the day's budget is the cause, the
                # next _acquire turns this into Deferred or ServeStale.
                self.observe(exc.rate)
                self._pause(exc.rate)
                if attempt == MAX_RATE_LIMITED:
                    raise
                continue
            except HardcoverCapacityError:
                log.error(
                    "a Hardcover query asked for more than Hardcover allows; this is a bug",
                    extra={"event": "hardcover_capacity_bug", "profile": self.profile},
                )
                raise
            self.observe(result.rate)
            return result
        raise AssertionError("unreachable")

    def observe(self, rate: RateLimit) -> None:
        """Take the daily budget from a response's headers.

        Without a readable reset time, the count is taken to hold until the next midnight UTC,
        when Hardcover's daily window resets (§3.3.1).
        """
        if rate.daily_remaining is not None:
            self._daily_remaining = rate.daily_remaining
            self._daily_reset = rate.daily_reset or _next_midnight_utc(self._clock.time())
            if not self._below_reserve():
                self._in_reserve = False
        self._expire_daily()

    def state(self) -> LimiterState:
        """The limiter as ``sync_status`` reports it."""
        self._expire_daily()
        wait = self._paused_until - self._clock.monotonic()
        if wait > 0:
            return LimiterState("paused", self._clock.time() + wait, self._daily_remaining)
        if self._below_reserve():
            return LimiterState("reserve", None, self._daily_remaining)
        return LimiterState("ok", None, self._daily_remaining)

    # --- internals --------------------------------------------------------------------------

    async def _acquire(self, waiter: _Waiter) -> None:
        self._queue.append(waiter)
        self._queue.sort(key=lambda w: w.key)
        try:
            while True:
                self._check_budget(waiter.priority)
                wait = self._wait_for(waiter)
                if waiter.priority is Priority.INTERACTIVE and wait > INTERACTIVE_WAIT:
                    raise ServeStale("rate_limited", self._clock.time() + wait)
                if self._queue[0] is not waiter:
                    # Not this call's turn: wait until the head leaves the queue.
                    waiter.wake = asyncio.get_running_loop().create_future()
                    await waiter.wake
                    continue
                if wait <= 0:
                    self._tokens -= waiter.cost
                    return
                await self._clock.sleep(wait)
        finally:
            self._queue.remove(waiter)
            if self._queue and (head := self._queue[0]).wake and not head.wake.done():
                head.wake.set_result(None)

    def _wait_for(self, waiter: _Waiter) -> float:
        """Seconds until ``waiter`` could go out: once no pause is in force and the bucket has
        refilled for it and for every call queued ahead of it. 0 means now."""
        now = self._clock.monotonic()
        self._tokens = min(self._burst, self._tokens + (now - self._refilled_at) * self._refill)
        self._refilled_at = now
        needed = 0
        for queued in self._queue:
            needed += queued.cost
            if queued is waiter:
                break
        paused = self._paused_until - now
        return max(paused, (needed - self._tokens) / self._refill, 0.0)

    def _pause(self, rate: RateLimit) -> None:
        given = (s for s in (rate.retry_after, rate.ratelimit_reset) if s is not None)
        seconds = next(given, DEFAULT_PAUSE)
        until = self._clock.monotonic() + seconds
        if until > self._paused_until:
            self._paused_until = until
            log.warning(
                "Hardcover rate-limited this profile; pausing its calls for %.0f s",
                seconds,
                extra={"event": "hardcover_paused", "profile": self.profile, "seconds": seconds},
            )

    def _check_budget(self, priority: Priority) -> None:
        self._expire_daily()
        until = self._daily_reset or _next_midnight_utc(self._clock.time())
        if priority is Priority.SYNC and self._below_reserve():
            if not self._in_reserve:
                self._in_reserve = True
                log.warning(
                    "Hardcover's daily budget is down to the reserve; syncs wait for the reset",
                    extra={
                        "event": "hardcover_reserve",
                        "profile": self.profile,
                        "daily_remaining": self._daily_remaining,
                    },
                )
            raise Deferred(until)
        if priority is Priority.INTERACTIVE and self._daily_remaining == 0:
            raise ServeStale("daily_budget", until)

    def _below_reserve(self) -> bool:
        return self._daily_remaining is not None and self._daily_remaining < self._reserve

    def _expire_daily(self) -> None:
        # Past the reset, the last count no longer holds; the next response brings a new one.
        if self._daily_reset is not None and self._clock.time() >= self._daily_reset:
            self._daily_remaining = None
            self._daily_reset = None
            self._in_reserve = False


def _next_midnight_utc(now: float) -> float:
    return (now // 86400 + 1) * 86400


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
