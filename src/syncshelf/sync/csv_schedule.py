"""When a profile's Goodreads CSV export is due (SPEC §3.4b.5).

The rule is two parts. A CSV becomes **due** five days after the last CSV success, and a due
export **runs in the next 02:00–05:00 window in the profile's own timezone** — so the export,
which is the slowest Goodreads call SyncShelf makes, happens while nobody is reading.

The due time is stored in ``schedules.next_due_at``, so a restart picks up where it left off
rather than starting the five days again. Everything here is a pure function of the stored
times, which is what makes that safe: the answer does not depend on how long the process has
been up.

What counts as a success is wider than "we exported": a **dated** ``/ingest`` upload is a CSV
success too and pushes the due date out (§3.4a.8, §3.4b.5), because the household now has a
fresh export whoever fetched it. An undated upload does not, since it only fills.
"""

from datetime import datetime, time, timedelta, tzinfo

__all__ = ["due_after_failure", "due_after_success", "in_window", "next_window_start"]


def next_window_start(moment: datetime, window: tuple[time, time]) -> datetime:
    """The start of the first ``window`` at or after ``moment``, in ``moment``'s timezone.

    ``moment`` inside a window returns that window's start, which is in the past — callers
    treat that as "due now". :func:`in_window` is the readable way to ask.
    """
    start, _ = window
    today = moment.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0, fold=0)
    if in_window(moment, window):
        return today
    if moment < today:
        return today
    return today + timedelta(days=1)


def in_window(moment: datetime, window: tuple[time, time]) -> bool:
    """Whether ``moment``'s local time is inside ``window``.

    Half-open: the start is inside, the end is not, so a due time exactly at 05:00 waits for
    the next night rather than squeezing in.
    """
    start, end = window
    return start <= moment.timetz().replace(tzinfo=None) < end


def due_after_success(
    last_success: int, tz: tzinfo, *, interval_days: int, window: tuple[time, time]
) -> int:
    """When the next CSV is due, given a success at ``last_success`` (epoch seconds).

    Five days on, then the next night window in ``tz``. Returned as epoch seconds for
    ``schedules.next_due_at``.
    """
    ready = datetime.fromtimestamp(last_success, tz) + timedelta(days=interval_days)
    return int(next_window_start(ready, window).timestamp())


def due_after_failure(now: int, tz: tzinfo, *, window: tuple[time, time]) -> int:
    """When a failed export tries again: the **next** night window, not sooner (§3.4b.5).

    Deliberately not a backoff curve. The window is already a once-a-night pace, so a failure
    waits for the next one, and a WAF challenge stops the source entirely until a person
    clears it (§3.4b.2) rather than being retried on any schedule.
    """
    moment = datetime.fromtimestamp(now, tz)
    # Even a failure inside tonight's window waits for tomorrow's: retrying straight away is
    # what the "not sooner" in §3.4b.5 rules out.
    tomorrow = moment.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return int(next_window_start(tomorrow, window).timestamp())
