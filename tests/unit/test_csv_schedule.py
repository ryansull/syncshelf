"""When the Goodreads CSV export is due (SPEC §3.4b.5): five days on, in the next night window
in the profile's own timezone.

These are pure functions of stored times on purpose — that is what makes `next_due_at` survive a
restart without the five days starting again.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from syncshelf.config import CadenceConfig
from syncshelf.sync.csv_schedule import (
    due_after_failure,
    due_after_success,
    in_window,
    next_window_start,
)

WINDOW = (time(2, 0), time(5, 0))
NEW_YORK = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")


def at(year: int, month: int, day: int, hour: int, minute: int = 0, tz: object = UTC) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=tz)  # type: ignore[arg-type]


def stamp(moment: datetime) -> int:
    return int(moment.timestamp())


class TestTheWindowItself:
    @pytest.mark.parametrize(
        ("hour", "minute", "inside"),
        [(1, 59, False), (2, 0, True), (3, 30, True), (4, 59, True), (5, 0, False), (6, 0, False)],
    )
    def test_the_window_is_half_open(self, hour: int, minute: int, inside: bool) -> None:
        # 02:00 is in, 05:00 is out, so a due time at the end waits for the next night.
        assert in_window(at(2026, 10, 16, hour, minute), WINDOW) is inside

    def test_a_moment_before_tonight_waits_for_tonight(self) -> None:
        assert next_window_start(at(2026, 10, 16, 1, 0), WINDOW) == at(2026, 10, 16, 2, 0)

    def test_a_moment_after_tonight_waits_for_tomorrow(self) -> None:
        assert next_window_start(at(2026, 10, 16, 9, 0), WINDOW) == at(2026, 10, 17, 2, 0)

    def test_a_moment_inside_the_window_is_already_due(self) -> None:
        # The window's start is in the past, which callers read as "run now".
        assert next_window_start(at(2026, 10, 16, 3, 0), WINDOW) == at(2026, 10, 16, 2, 0)

    def test_a_moment_exactly_at_the_end_waits_for_tomorrow(self) -> None:
        assert next_window_start(at(2026, 10, 16, 5, 0), WINDOW) == at(2026, 10, 17, 2, 0)


class TestDueAfterSuccess:
    def test_five_days_on_then_the_next_window(self) -> None:
        success = stamp(at(2026, 10, 16, 3, 15))
        due = due_after_success(success, UTC, interval_days=5, window=WINDOW)
        # 21 Oct 03:15 is inside that night's window, so it is due at its start.
        assert datetime.fromtimestamp(due, UTC) == at(2026, 10, 21, 2, 0)

    def test_a_daytime_success_waits_for_the_following_night(self) -> None:
        success = stamp(at(2026, 10, 16, 14, 0))
        due = due_after_success(success, UTC, interval_days=5, window=WINDOW)
        assert datetime.fromtimestamp(due, UTC) == at(2026, 10, 22, 2, 0)

    def test_the_window_is_the_profiles_own_timezone(self) -> None:
        # 02:00 in New York, not 02:00 UTC: the point of the window is local quiet hours.
        success = stamp(at(2026, 10, 16, 14, 0, NEW_YORK))
        due = due_after_success(success, NEW_YORK, interval_days=5, window=WINDOW)
        assert datetime.fromtimestamp(due, NEW_YORK) == at(2026, 10, 22, 2, 0, NEW_YORK)

    def test_two_timezones_give_different_due_times(self) -> None:
        success = stamp(at(2026, 10, 16, 14, 0))
        in_ny = due_after_success(success, NEW_YORK, interval_days=5, window=WINDOW)
        in_tokyo = due_after_success(success, TOKYO, interval_days=5, window=WINDOW)
        assert in_ny != in_tokyo

    def test_the_interval_is_configurable(self) -> None:
        success = stamp(at(2026, 10, 16, 3, 0))
        due = due_after_success(success, UTC, interval_days=1, window=WINDOW)
        assert datetime.fromtimestamp(due, UTC) == at(2026, 10, 17, 2, 0)

    def test_the_due_time_does_not_depend_on_now(self) -> None:
        # The whole reason it is stored: a restart must not start the five days again.
        success = stamp(at(2026, 10, 16, 3, 15))
        first = due_after_success(success, UTC, interval_days=5, window=WINDOW)
        second = due_after_success(success, UTC, interval_days=5, window=WINDOW)
        assert first == second


class TestDueAfterFailure:
    def test_a_failure_waits_for_the_next_night(self) -> None:
        now = stamp(at(2026, 10, 16, 14, 0))
        due = due_after_failure(now, UTC, window=WINDOW)
        assert datetime.fromtimestamp(due, UTC) == at(2026, 10, 17, 2, 0)

    def test_a_failure_inside_the_window_still_waits_for_tomorrow(self) -> None:
        # "not sooner" (§3.4b.5): failing at 03:00 must not retry at 03:01.
        now = stamp(at(2026, 10, 16, 3, 0))
        due = due_after_failure(now, UTC, window=WINDOW)
        assert datetime.fromtimestamp(due, UTC) == at(2026, 10, 17, 2, 0)

    def test_a_failure_just_before_the_window_still_waits(self) -> None:
        now = stamp(at(2026, 10, 16, 1, 59))
        due = due_after_failure(now, UTC, window=WINDOW)
        assert datetime.fromtimestamp(due, UTC) == at(2026, 10, 17, 2, 0)

    def test_a_failure_is_always_in_the_future(self) -> None:
        for hour in range(24):
            now = stamp(at(2026, 10, 16, hour))
            assert due_after_failure(now, UTC, window=WINDOW) > now


class TestTheConfiguredDefaults:
    def test_the_defaults_are_the_spec_values(self) -> None:
        cadence = CadenceConfig()
        assert cadence.csv_interval_days == 5
        assert cadence.csv_window == (time(2, 0), time(5, 0))

    def test_the_defaults_give_a_five_day_night_cadence(self) -> None:
        cadence = CadenceConfig()
        success = stamp(at(2026, 10, 16, 14, 0))
        due = due_after_success(
            success, UTC, interval_days=cadence.csv_interval_days, window=cadence.csv_window
        )
        assert datetime.fromtimestamp(due, UTC) - at(2026, 10, 16, 14, 0) >= timedelta(days=5)
