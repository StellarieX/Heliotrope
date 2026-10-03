"""The canonical 15-minute time model (§3).

Every scheduler reads the same grid. These tests pin the grid itself, because a
subtle disagreement here would be invisible in any single schedule and would
silently corrupt comparisons between engines.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.horizon import SLOT_HOURS, SLOT_MINUTES, HorizonError, SchedulingHorizon

START = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)


def day() -> SchedulingHorizon:
    return SchedulingHorizon.create(START, START + timedelta(hours=24))


def test_slot_is_fifteen_minutes():
    """§3: Heliotrope's slot is 15 minutes, so 0.25 h."""
    assert SLOT_MINUTES == 15
    assert SLOT_HOURS == 0.25
    assert day().slot_hours == 0.25


def test_a_day_is_ninety_six_slots():
    """§3: 24 h / 0.25 h = 96 slots."""
    assert day().slot_count == 96
    assert len(day()) == 96


def test_a_different_slot_length_keeps_the_geometry():
    """The horizon is not hard-wired to 15 min; the default just is."""
    hour_grid = SchedulingHorizon.create(START, START + timedelta(hours=24), 60)
    assert hour_grid.slot_count == 24
    assert hour_grid.slot_hours == 1.0


def test_slot_windows_tile_without_gaps_or_overlap():
    """Consecutive slots must share a boundary exactly, or energy double-counts."""
    windows = list(SchedulingHorizon.create(START, START + timedelta(hours=6)).iter_slot_windows())
    assert len(windows) == 24
    for (_i, _s, end), (_j, start, _e) in zip(windows, windows[1:]):
        assert end == start, "a gap or overlap in the slot grid"
    for _index, start, end in windows:
        assert end - start == timedelta(minutes=SLOT_MINUTES)


def test_the_end_boundary_is_exclusive():
    """A 24 h span holds 00:00..23:45. The 24:00 slot does not exist."""
    index, start, end = list(day().iter_slot_windows())[-1]
    assert index == 95
    assert start == START + timedelta(hours=23, minutes=45)
    assert end == START + timedelta(hours=24)


def test_slot_start_and_end_round_trip_through_index_of():
    horizon = day()
    for index in (0, 1, 28, 72, 95):
        assert horizon.index_of(horizon.slot_start(index)) == index
        assert horizon.index_of(horizon.slot_end(index)) == index + 1


def test_index_of_returns_sentinels_outside_the_horizon():
    """One comparison detects "outside", per the documented contract."""
    horizon = day()
    assert horizon.index_of(START - timedelta(minutes=15)) == -1
    assert horizon.index_of(START + timedelta(hours=24)) == 96


def test_index_of_snaps_a_moment_inside_a_slot_to_that_slot():
    """§3: a moment belongs to the slot it falls inside, not to a new slot."""
    horizon = day()
    assert horizon.index_of(START + timedelta(minutes=7)) == 0
    assert horizon.index_of(START + timedelta(minutes=15)) == 1


def test_slot_start_rejects_an_index_outside_the_horizon():
    horizon = day()
    with pytest.raises(HorizonError):
        horizon.slot_start(96)
    with pytest.raises(HorizonError):
        horizon.slot_end(-1)


def test_first_slot_at_or_after_rounds_up_not_down():
    """A load cannot start before it is released, so this must round UP."""
    horizon = day()
    # 18:07 is inside slot 72 (18:00-18:15) but that slot OPENS before the
    # release, so the first legal start is slot 73 (18:15) -- never 72.
    assert horizon.index_of(START + timedelta(hours=18, minutes=7)) == 72
    assert horizon.first_slot_at_or_after(START + timedelta(hours=18, minutes=7)) == 73
    # Already on the grid: unchanged, no drift.
    assert horizon.first_slot_at_or_after(START + timedelta(hours=18)) == 72
    assert horizon.first_slot_at_or_after(START - timedelta(hours=5)) == 0
    assert horizon.first_slot_at_or_after(START + timedelta(hours=48)) == 96


def test_last_slot_before_is_the_last_slot_fully_ended():
    """A deadline is exclusive: the load must be FINISHED by then."""
    horizon = day()
    assert horizon.last_slot_before(START + timedelta(hours=7)) == 27
    assert horizon.index_of(START + timedelta(hours=7)) == 28


def test_slot_range_for_is_half_open_and_never_inverted():
    horizon = day()
    slots = horizon.slot_range_for(START, START + timedelta(hours=1))
    assert list(slots) == [0, 1, 2, 3]
    # An inverted or sub-slot window yields nothing rather than wrapping.
    assert list(horizon.slot_range_for(START + timedelta(hours=2), START)) == []


def test_minutes_to_slots_rounds_up_so_energy_is_never_truncated():
    """§9: truncating here would silently drop part of a job's requirement."""
    horizon = day()
    assert horizon.minutes_to_slots(15) == 1
    assert horizon.minutes_to_slots(16) == 2
    assert horizon.minutes_to_slots(30) == 2
    assert horizon.minutes_to_slots(240) == 16
    assert horizon.minutes_to_slots(0) == 0
    assert horizon.minutes_to_slots(-5) == 0


def test_slots_to_minutes_is_the_inverse():
    horizon = day()
    for minutes in (15, 45, 240):
        assert horizon.slots_to_minutes(horizon.minutes_to_slots(minutes)) >= minutes


def test_spanning_aligns_the_grid_down_so_every_moment_is_covered():
    """§42: all engines must see byte-identical slot boundaries."""
    moments = [
        START + timedelta(hours=18, minutes=7),
        START + timedelta(hours=31, minutes=3),
    ]
    horizon = SchedulingHorizon.spanning(moments)
    for moment in moments:
        slot = horizon.index_of(moment)
        assert 0 <= slot < horizon.slot_count, "a release fell outside the horizon"
        assert horizon.slot_start(slot) <= moment


def test_spanning_of_no_moments_is_an_error():
    with pytest.raises(HorizonError):
        SchedulingHorizon.spanning([])


def test_spanning_pads_the_horizon_when_asked():
    moments = [START + timedelta(hours=1)]
    bare = SchedulingHorizon.spanning(moments)
    padded = SchedulingHorizon.spanning(moments, pad_slots=2)
    assert padded.slot_count == bare.slot_count + 2


def test_for_hours_aligns_the_start_down_to_the_grid():
    off_grid = START + timedelta(hours=18, minutes=7)
    horizon = SchedulingHorizon.for_hours(24, start=off_grid)
    assert horizon.slot_minutes == 15
    assert horizon.start == START + timedelta(hours=18)
    assert horizon.slot_count == 96


def test_create_anchors_the_grid_at_start():
    """The grid is relative to `start`, so an off-grid anchor is legal and exact."""
    off_grid = START + timedelta(minutes=7)
    horizon = SchedulingHorizon.create(off_grid, off_grid + timedelta(hours=1))
    assert horizon.start == off_grid
    assert horizon.slot_count == 4
    assert horizon.iter_slot_windows().__next__() == (0, off_grid, off_grid + timedelta(minutes=15))


def test_create_truncates_a_partial_end_slot():
    """A trailing partial slot would misreport energy, so it is dropped.

    Rounding UP would instead invent a slot the carbon signal may not cover.
    """
    horizon = SchedulingHorizon.create(START, START + timedelta(hours=24, minutes=7))
    assert horizon.slot_count == 96
    assert horizon.end == START + timedelta(hours=24)


def test_create_rejects_a_window_shorter_than_one_slot():
    with pytest.raises(HorizonError):
        SchedulingHorizon.create(START, START + timedelta(minutes=5))


def test_create_rejects_a_non_positive_slot_length():
    with pytest.raises(HorizonError):
        SchedulingHorizon.create(START, START + timedelta(hours=1), slot_minutes=0)


def test_create_rejects_an_end_at_or_before_the_start():
    with pytest.raises(HorizonError):
        SchedulingHorizon.create(START, START)


def test_naive_datetimes_are_rejected_or_assumed_utc_consistently():
    """A naive timestamp must never silently become a local-time slot."""
    naive = datetime(2026, 10, 5, 0, 0)
    horizon = SchedulingHorizon.create(naive, START + timedelta(hours=24))
    # Whatever the policy, the result must be timezone-aware and land on the
    # same instant as the aware equivalent, so no engine sees a local grid.
    assert horizon.start.tzinfo is not None
    assert horizon.start == START


def test_contains_window_is_inclusive_at_the_boundary():
    horizon = day()
    assert horizon.contains_window(START, START + timedelta(hours=24))
    assert not horizon.contains_window(START - timedelta(minutes=15), START + timedelta(hours=1))