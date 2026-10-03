"""Time semantics: wall-clock parsing and midnight rollover (§15, §23).

The bug this file exists to prevent: a window from 21:00 to 07:00 being read as
a NEGATIVE ten-hour window because the two wall times were compared without
regard to the day they belong to.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.utils.time import (
    WallClockError,
    elapsed_minutes,
    next_occurrence,
    parse_wall_clock,
    resolve_wall_window,
    wall_instant,
)

IST = ZoneInfo("Asia/Kolkata")


# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("07:00", time(7, 0)),
        ("21:30", time(21, 30)),
        ("00:00", time(0, 0)),
        ("23:59", time(23, 59)),
        ("07:00:30", time(7, 0, 30)),
    ],
)
def test_valid_wall_times_parse(value, expected):
    assert parse_wall_clock(value) == expected


@pytest.mark.parametrize("value", ["24:00", "7:60", "-1:00", "abc", "", "7", "07:00:99"])
def test_invalid_wall_times_rejected(value):
    with pytest.raises(WallClockError):
        parse_wall_clock(value)


# --- next occurrence -------------------------------------------------------


def test_next_occurrence_is_today_when_still_ahead():
    now = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
    assert next_occurrence(time(21, 0), now) == datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)


def test_next_occurrence_rolls_to_tomorrow_when_passed():
    now = datetime(2026, 10, 5, 22, 0, tzinfo=timezone.utc)
    assert next_occurrence(time(7, 0), now) == datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)


def test_next_occurrence_accepts_the_current_minute():
    now = datetime(2026, 10, 5, 7, 0, tzinfo=timezone.utc)
    assert next_occurrence(time(7, 0), now) == now


def test_next_occurrence_requires_an_aware_reference():
    with pytest.raises(WallClockError, match="timezone-aware"):
        next_occurrence(time(7, 0), datetime(2026, 10, 5, 7, 0))


# --- midnight rollover (the §15 headline case) ----------------------------


def test_ev_window_across_midnight_is_ten_hours_not_negative():
    now = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
    window = resolve_wall_window(time(21, 0), time(7, 0), now)
    assert window.crosses_midnight is True
    assert window.release == datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)
    assert window.deadline == datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
    assert window.minutes() == pytest.approx(600.0)
    assert window.deadline > window.release


def test_same_day_window_does_not_roll():
    now = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
    window = resolve_wall_window(time(9, 0), time(17, 0), now)
    assert window.crosses_midnight is False
    assert window.minutes() == pytest.approx(480.0)


def test_declared_midnight_is_honored():
    now = datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc)
    window = resolve_wall_window(time(9, 0), time(17, 0), now, crosses_midnight=True)
    assert window.crosses_midnight is True
    assert window.rollover_source == "declared"
    assert window.deadline.day == 6


def test_declaring_same_day_for_an_earlier_deadline_is_rejected():
    """An explicit "same day" against a 21:00 -> 07:00 intent is a user error,
    not something to silently reinterpret."""
    now = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
    with pytest.raises(WallClockError, match="crosses_midnight=True"):
        resolve_wall_window(time(21, 0), time(7, 0), now, crosses_midnight=False)


def test_rollover_is_reported_as_inferred_by_default():
    now = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)
    assert resolve_wall_window(time(21, 0), time(7, 0), now).rollover_source == "inferred"


# --- timezone handling -----------------------------------------------------


def test_window_is_anchored_in_the_supplied_timezone():
    now = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)  # 20:30 IST
    window = resolve_wall_window(time(21, 0), time(7, 0), now, tz=IST)
    assert window.release.utcoffset() == timedelta(hours=5, minutes=30)
    assert window.release.astimezone(timezone.utc).hour == 15  # 21:00 IST == 15:30 UTC
    assert window.deadline.astimezone(timezone.utc) > window.release.astimezone(timezone.utc)


def test_wall_instant_is_anchored_to_a_calendar_day():
    day = datetime(2026, 10, 5, tzinfo=timezone.utc).date()
    assert wall_instant(time(21, 0), day, timezone.utc).day == 5


def test_elapsed_minutes_matches_delta():
    a = datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert elapsed_minutes(a, a + timedelta(hours=2, minutes=30)) == pytest.approx(150.0)
