"""Deterministic time utilities: 15-min slots, bucketing, gaps, interpolation."""

from datetime import datetime, timedelta, timezone

ALLOWED_RESOLUTIONS = (5, 15, 30, 60)


def to_utc(v: datetime) -> datetime:
    """Normalize to timezone-aware UTC. Naive values are assumed UTC (explicit)."""
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


def floor_to_resolution(v: datetime, resolution_minutes: int) -> datetime:
    v = to_utc(v)
    minute = (v.minute // resolution_minutes) * resolution_minutes
    return v.replace(minute=minute, second=0, microsecond=0)


def generate_slots(start: datetime, end: datetime, resolution_minutes: int) -> list[datetime]:
    """Half-open [start, end) slot starts at exactly the requested resolution."""
    if resolution_minutes not in ALLOWED_RESOLUTIONS:
        raise ValueError(f"resolution must be one of {ALLOWED_RESOLUTIONS}")
    s = floor_to_resolution(start, resolution_minutes)
    e = to_utc(end)
    if e <= s:
        raise ValueError("start must be < end")
    step = timedelta(minutes=resolution_minutes)
    slots: list[datetime] = []
    t = s
    while t < e:
        slots.append(t)
        t += step
    return slots


def find_missing_slots(slots: list[datetime], present: set[datetime]) -> list[datetime]:
    return [s for s in slots if s not in present]


def group_gaps(missing: list[datetime], resolution_minutes: int) -> list[list[datetime]]:
    """Group consecutive missing slots into gaps."""
    if not missing:
        return []
    step = timedelta(minutes=resolution_minutes)
    gaps: list[list[datetime]] = [[missing[0]]]
    for prev, cur in zip(missing, missing[1:]):
        if cur - prev == step:
            gaps[-1].append(cur)
        else:
            gaps.append([cur])
    return gaps


def linear_interpolate(
    before: tuple[datetime, float],
    after: tuple[datetime, float],
    targets: list[datetime],
) -> list[float]:
    """Linear interpolation between two known points. Straight-line only."""
    (t0, v0), (t1, v1) = before, after
    span = (t1 - t0).total_seconds()
    if span <= 0:
        raise ValueError("interpolation needs increasing timestamps")
    out = []
    for t in targets:
        frac = (t - t0).total_seconds() / span
        out.append(v0 + frac * (v1 - v0))
    return out


def validate_range(start: datetime, end: datetime, max_days: int) -> None:
    s, e = to_utc(start), to_utc(end)
    if e <= s:
        raise ValueError("start must be < end")
    if (e - s).days > max_days or (e - s).total_seconds() > max_days * 86400:
        raise ValueError(f"range exceeds maximum of {max_days} days")
