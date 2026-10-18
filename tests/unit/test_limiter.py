"""The per-profile Hardcover limiter (SPEC §3.3.3), driven by the injectable clock."""

import logging

import anyio
import pytest

from syncshelf.clients.hardcover import (
    ME,
    HardcoverCapacityError,
    HardcoverClient,
    HardcoverError,
    HardcoverRateLimited,
    RateLimit,
    Result,
)
from syncshelf.sync.limiter import (
    BURST,
    DEFAULT_PAUSE,
    Deferred,
    HardcoverLimiter,
    LimiterState,
    ServeStale,
    interactive,
)

from clock import FakeClock
from fakes.hardcover import URL, FakeHardcover

TOKEN = "fake-hardcover-token"
DAILY_RESET = 4_102_531_200  # what FakeHardcover sends: a day after FakeClock.START

pytestmark = pytest.mark.anyio


@pytest.fixture
def fake() -> FakeHardcover:
    fake = FakeHardcover()
    fake.add_user(TOKEN, user_id=42)
    return fake


@pytest.fixture
def limiter(clock: FakeClock) -> HardcoverLimiter:
    return HardcoverLimiter("alice", reserve=1000, clock=clock)


def client_for(fake: FakeHardcover, limiter: HardcoverLimiter) -> HardcoverClient:
    return HardcoverClient(URL, TOKEN, gate=limiter, transport=fake.transport)


# --- the bucket -------------------------------------------------------------------------


async def test_a_burst_of_8_goes_out_at_once_then_1_per_second(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    client = client_for(fake, limiter)
    for _ in range(BURST):
        await client.execute(ME)
    assert clock.monotonic() == 0
    await client.execute(ME)
    assert clock.monotonic() == pytest.approx(1)
    await client.execute(ME)
    assert clock.monotonic() == pytest.approx(2)


async def test_the_bucket_refills_but_never_past_the_burst(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    client = client_for(fake, limiter)
    for _ in range(BURST):
        await client.execute(ME)
    clock.advance(60)
    for _ in range(BURST):
        await client.execute(ME)
    assert clock.monotonic() == pytest.approx(60)
    await client.execute(ME)
    assert clock.monotonic() == pytest.approx(61)


async def test_a_call_costs_one_token_per_top_level_field(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    client = client_for(fake, limiter)
    await client.library()  # Me (1) + Library (2)
    for _ in range(BURST - 3):
        await client.execute(ME)
    assert clock.monotonic() == 0
    await client.execute(ME)
    assert clock.monotonic() == pytest.approx(1)


async def test_a_call_that_could_never_fit_is_refused_as_a_bug(
    limiter: HardcoverLimiter, caplog: pytest.LogCaptureFixture
) -> None:
    async def send() -> Result:
        raise AssertionError("never sent")

    with caplog.at_level(logging.ERROR), pytest.raises(HardcoverError):
        await limiter.run(BURST + 1, send)
    assert [r.__dict__["event"] for r in caplog.records] == ["hardcover_capacity_bug"]


async def test_queued_interactive_calls_serve_stale_once_their_turn_is_over_5s_away(
    limiter: HardcoverLimiter,
) -> None:
    async def send() -> Result:
        return Result(data={}, cost=1, rate=RateLimit())

    for _ in range(BURST):
        await limiter.run(1, send)
    outcomes: list[str] = []

    async def call() -> None:
        with interactive():
            try:
                await limiter.run(1, send)
                outcomes.append("served")
            except ServeStale:
                outcomes.append("stale")

    async with anyio.create_task_group() as tg:
        for _ in range(7):
            tg.start_soon(call)
    # Each waits for its own token and those of the calls ahead. The first one's 1 s sleep
    # moves the fake clock before the rest arrive, so the k-th sees k - 1 s to wait: only the
    # 7th's 6 s is over the limit.
    assert sorted(outcomes) == ["served"] * 6 + ["stale"]


async def test_a_retried_call_keeps_its_place_in_the_queue(limiter: HardcoverLimiter) -> None:
    order: list[str] = []
    first_try = True

    async def flaky() -> Result:
        nonlocal first_try
        if first_try:
            first_try = False
            raise HardcoverRateLimited(RateLimit(retry_after=2.0))
        order.append("retried")
        return Result(data={}, cost=1, rate=RateLimit())

    async def later() -> Result:
        order.append("later")
        return Result(data={}, cost=1, rate=RateLimit())

    async with anyio.create_task_group() as tg:
        tg.start_soon(limiter.run, 1, flaky)
        tg.start_soon(limiter.run, 1, later)
    assert order == ["retried", "later"]


async def test_interactive_calls_are_served_before_queued_sync_calls(
    fake: FakeHardcover, limiter: HardcoverLimiter
) -> None:
    client = client_for(fake, limiter)
    for _ in range(BURST):
        await client.execute(ME)
    served: list[str] = []

    async def call(name: str, priority: str) -> None:
        if priority == "interactive":
            with interactive():
                await client.execute(ME)
        else:
            await client.execute(ME)
        served.append(name)

    async with anyio.create_task_group() as tg:
        tg.start_soon(call, "sync-1", "sync")
        tg.start_soon(call, "sync-2", "sync")
        tg.start_soon(call, "tool", "interactive")
    assert served[0] == "tool"


# --- 429 --------------------------------------------------------------------------------


async def test_a_429_pauses_the_profile_until_retry_after_then_resends(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    fake.fail_next("Me", "rate_limited", retry_after=7)
    result = await client_for(fake, limiter).execute(ME)
    assert result.data["me"] == [{"id": 42}]
    assert clock.monotonic() == pytest.approx(7)
    assert [op for op, _ in fake.requests] == ["Me", "Me"]


async def test_a_429_pauses_every_caller_of_the_profile(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    client = client_for(fake, limiter)
    fake.fail_next("Me", "rate_limited", retry_after=30)
    times: list[float] = []

    async def call() -> None:
        await client.execute(ME)
        times.append(clock.monotonic())

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        tg.start_soon(call)
    assert min(times) >= 30


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [({"retry_after": 7.0}, 7.0), ({"ratelimit_reset": 4.0}, 4.0), ({}, DEFAULT_PAUSE)],
)
async def test_the_pause_falls_back_from_retry_after_to_ratelimit_reset_to_5s(
    limiter: HardcoverLimiter, clock: FakeClock, headers: dict[str, float], seconds: float
) -> None:
    calls = 0

    async def send() -> Result:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HardcoverRateLimited(RateLimit(**headers))
        return Result(data={}, cost=1, rate=RateLimit())

    await limiter.run(1, send)
    assert clock.monotonic() == pytest.approx(seconds)


async def test_an_interactive_call_paused_beyond_5s_is_told_to_serve_stale(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    fake.fail_next("Me", "rate_limited", retry_after=30)
    with interactive(), pytest.raises(ServeStale) as excinfo:
        await client_for(fake, limiter).execute(ME)
    assert excinfo.value.reason == "rate_limited"
    assert excinfo.value.until == pytest.approx(clock.time() + 30)
    state = limiter.state()
    assert state.limiter == "paused"
    assert state.paused_until == pytest.approx(clock.time() + 30)


async def test_an_interactive_call_waits_out_a_short_pause(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    fake.fail_next("Me", "rate_limited", retry_after=3)
    with interactive():
        await client_for(fake, limiter).execute(ME)
    assert clock.monotonic() == pytest.approx(3)


async def test_a_429_is_logged_as_a_pause(
    fake: FakeHardcover, limiter: HardcoverLimiter, caplog: pytest.LogCaptureFixture
) -> None:
    fake.fail_next("Me", "rate_limited", retry_after=7)
    with caplog.at_level(logging.WARNING, logger="syncshelf.sync.limiter"):
        await client_for(fake, limiter).execute(ME)
    assert [r.__dict__["event"] for r in caplog.records] == ["hardcover_paused"]


# --- the daily budget -------------------------------------------------------------------


async def test_below_the_reserve_syncs_are_deferred_and_interactive_calls_continue(
    fake: FakeHardcover, limiter: HardcoverLimiter, caplog: pytest.LogCaptureFixture
) -> None:
    client = client_for(fake, limiter)
    fake.daily_remaining = 1000
    await client.execute(ME)  # the response says 999 remain
    with (
        caplog.at_level(logging.WARNING, logger="syncshelf.sync.limiter"),
        pytest.raises(Deferred) as excinfo,
    ):
        await client.execute(ME)
    assert excinfo.value.until == DAILY_RESET
    assert [r.__dict__["event"] for r in caplog.records] == ["hardcover_reserve"]
    assert limiter.state() == LimiterState("reserve", None, 999)
    with interactive():
        await client.execute(ME)


async def test_at_zero_interactive_calls_are_told_to_serve_stale(
    fake: FakeHardcover, limiter: HardcoverLimiter
) -> None:
    client = client_for(fake, limiter)
    fake.daily_remaining = 1
    with interactive():
        await client.execute(ME)  # the response says 0 remain
        with pytest.raises(ServeStale) as excinfo:
            await client.execute(ME)
    assert excinfo.value.reason == "daily_budget"


async def test_the_daily_reset_lifts_the_reserve(
    fake: FakeHardcover, limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    client = client_for(fake, limiter)
    fake.daily_remaining = 1
    await client.execute(ME)
    with pytest.raises(Deferred):
        await client.execute(ME)
    clock.advance(DAILY_RESET - clock.time())
    fake.daily_remaining = 5000
    await client.execute(ME)
    assert limiter.state().limiter == "ok"


# --- bugs -------------------------------------------------------------------------------


async def test_request_exceeds_capacity_is_logged_as_a_bug_and_never_retried(
    fake: FakeHardcover, limiter: HardcoverLimiter, caplog: pytest.LogCaptureFixture
) -> None:
    fake.fail_next("Me", "capacity")
    with caplog.at_level(logging.ERROR), pytest.raises(HardcoverCapacityError):
        await client_for(fake, limiter).execute(ME)
    assert [op for op, _ in fake.requests] == ["Me"]
    assert [r.__dict__["event"] for r in caplog.records] == ["hardcover_capacity_bug"]


def result_with(**rate: float) -> Result:
    return Result(data={}, cost=1, rate=RateLimit(**rate))  # type: ignore[arg-type]


async def test_without_a_reset_header_the_reserve_lasts_until_midnight_utc(
    limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    async def send() -> Result:
        return result_with(daily_remaining=10)

    await limiter.run(1, send)
    with pytest.raises(Deferred) as excinfo:
        await limiter.run(1, send)
    midnight = (clock.time() // 86400 + 1) * 86400
    assert excinfo.value.until == midnight
    clock.advance(midnight - clock.time())
    assert limiter.state().limiter == "ok"


async def test_a_429_that_spends_the_daily_budget_is_not_retried(
    limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    sent = 0

    async def send() -> Result:
        nonlocal sent
        sent += 1
        raise HardcoverRateLimited(RateLimit(retry_after=3600.0, daily_remaining=0))

    with pytest.raises(Deferred):
        await limiter.run(1, send)
    with interactive(), pytest.raises(ServeStale) as excinfo:
        await limiter.run(1, send)
    assert excinfo.value.reason == "daily_budget"
    assert sent == 1


async def test_retry_after_0_means_resend_at_once(
    limiter: HardcoverLimiter, clock: FakeClock
) -> None:
    calls = 0

    async def send() -> Result:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HardcoverRateLimited(RateLimit(retry_after=0.0, ratelimit_reset=30.0))
        return result_with()

    await limiter.run(1, send)
    assert clock.monotonic() == 0


async def test_the_reserve_warning_comes_back_after_the_count_recovers(
    limiter: HardcoverLimiter, caplog: pytest.LogCaptureFixture
) -> None:
    remaining = iter([999, 2000, 999])

    async def send() -> Result:
        return result_with(daily_remaining=next(remaining), daily_reset=float(DAILY_RESET))

    with caplog.at_level(logging.WARNING, logger="syncshelf.sync.limiter"):
        await limiter.run(1, send)
        with pytest.raises(Deferred):
            await limiter.run(1, send)
        with interactive():
            await limiter.run(1, send)  # 2000: out of the reserve
            await limiter.run(1, send)  # 999 again
        with pytest.raises(Deferred):
            await limiter.run(1, send)
    assert [r.__dict__["event"] for r in caplog.records] == ["hardcover_reserve"] * 2


async def test_a_fresh_limiter_is_ok(limiter: HardcoverLimiter) -> None:
    assert limiter.state() == LimiterState("ok", None, None)
