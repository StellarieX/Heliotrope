"""Time utilities are deterministic and exact."""

from datetime import datetime, timedelta, timezone

import pytest

from app.utils.time import (
    floor_to_resolution,
    generate_slots,
    group_gaps,
    linear_interpolate,
    to_utc,
    validate_range,
)


def test_to_utc_assumes_utc_for_naive():
    assert to_utc(datetime(2026, 1, 1, 5, 0)).utcoffset() == timedelta(0)


def test_generate_96_slots():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    slots = generate_slots(start, start + timedelta(hours=24), 15)
    assert len(slots) == 96
    assert slots[0] == start


def test_floor_snaps_down():
    v = datetime(2026, 1, 1, 5, 37, tzinfo=timezone.utc)
    assert floor_to_resolution(v, 15) == datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc)


def test_bad_resolution_rejected():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="resolution"):
        generate_slots(start, start + timedelta(hours=1), 7)


def test_gap_grouping():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    missing = [base, base + timedelta(minutes=15), base + timedelta(hours=2)]
    gaps = group_gaps(missing, 15)
    assert len(gaps) == 2
    assert len(gaps[0]) == 2 and len(gaps[1]) == 1


def test_interpolate_midpoint():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)
    mid = t0 + timedelta(minutes=30)
    assert linear_interpolate((t0, 100.0), (t1, 200.0), [mid]) == [150.0]


def test_range_guard():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    validate_range(start, start + timedelta(days=7), 7)
    with pytest.raises(ValueError, match="exceeds"):
        validate_range(start, start + timedelta(days=8), 7)
