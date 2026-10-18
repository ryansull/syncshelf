"""The test harness itself: blocked sockets, anyio, the injectable clock and live gating."""

import socket
from pathlib import Path

import anyio
import pytest
from pytest_socket import SocketConnectBlockedError

from clock import FakeClock

TESTS = Path(__file__).parents[1]

pytest_plugins = ["pytester"]


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_outbound_sockets_are_blocked() -> None:
    with (
        pytest.raises(SocketConnectBlockedError),
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
    ):
        sock.connect(("192.0.2.1", 443))


@pytest.mark.anyio
async def test_async_tests_run_on_anyio_with_sockets_blocked() -> None:
    await anyio.lowlevel.checkpoint()


class TestFakeClock:
    def test_starts_at_a_fixed_epoch_and_advances_only_when_told(self, clock: FakeClock) -> None:
        start = clock.time()
        assert start == FakeClock.START
        assert clock.time() == start
        clock.advance(90)
        assert clock.time() == start + 90
        assert clock.monotonic() == 90

    def test_now_is_timezone_aware_utc(self, clock: FakeClock) -> None:
        assert clock.now().timestamp() == clock.time()
        assert clock.now().utcoffset() is not None

    def test_rejects_going_backwards(self, clock: FakeClock) -> None:
        with pytest.raises(ValueError):
            clock.advance(-1)

    @pytest.mark.anyio
    async def test_sleep_advances_time_without_waiting(self, clock: FakeClock) -> None:
        with anyio.fail_after(1):
            await clock.sleep(3600)
        assert clock.monotonic() == 3600


class TestLiveGating:
    """The ``live`` marker is collected only with ``--live`` and ``SYNCSHELF_LIVE=1``."""

    SUITE = """
        import pytest

        @pytest.mark.live
        def test_live():
            pass

        def test_offline():
            pass
    """

    @pytest.fixture
    def suite(self, pytester: pytest.Pytester) -> pytest.Pytester:
        # The conftest imports its sibling helper modules, so copy those too.
        for helper in TESTS.glob("*.py"):
            pytester.path.joinpath(helper.name).write_text(
                helper.read_text(encoding="utf-8"), encoding="utf-8"
            )
        pytester.makepyfile(self.SUITE)
        pytester.makeini("[pytest]\nmarkers =\n    live: live\n")
        return pytester

    @pytest.mark.parametrize(
        ("args", "env"),
        [((), None), (("--live",), None), ((), "1"), (("--live",), "0")],
        ids=["neither", "flag-only", "env-only", "flag-env-not-1"],
    )
    def test_not_collected_without_both(
        self,
        suite: pytest.Pytester,
        monkeypatch: pytest.MonkeyPatch,
        args: tuple[str, ...],
        env: str | None,
    ) -> None:
        if env is None:
            monkeypatch.delenv("SYNCSHELF_LIVE", raising=False)
        else:
            monkeypatch.setenv("SYNCSHELF_LIVE", env)
        result = suite.runpytest_subprocess("-p", "no:cacheprovider", "-v", *args)
        result.assert_outcomes(passed=1, deselected=1)
        result.stdout.no_fnmatch_line("*test_live*PASSED*")

    def test_collected_with_both(
        self, suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNCSHELF_LIVE", "1")
        result = suite.runpytest_subprocess("-p", "no:cacheprovider", "-v", "--live")
        result.assert_outcomes(passed=2)
