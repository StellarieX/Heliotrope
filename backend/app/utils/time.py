"""Deterministic time utilities: 15-min slots, bucketing, gaps, interpolation.

Phase 3 adds wall-clock resolution (Phase 3, §15). The frontend speaks "ready by
07:00"; the backend speaks timezone-aware instants. These helpers are the ONLY
place that translation happens, so a deadline can never silently land before its
release time.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo

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


# --- Wall-clock resolution (Phase 3, §15) ---------------------------------
#
# A user saying "charged by 07:00" after plugging in at 21:00 means 07:00 the
# NEXT day. Comparing naive wall times would call that window negative. These
# helpers make the day-rollover an explicit, testable decision.


class WallClockError(ValueError):
    """A wall-clock string or window is unusable."""


def parse_wall_clock(value: str) -> time:
    """Parse a strict "HH:MM" (or "HH:MM:SS") wall time. 24:00 is rejected."""
    if not isinstance(value, str) or ":" not in value:
        raise WallClockError(f"expected a HH:MM wall time, got {value!r}")
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        raise WallClockError(f"expected a HH:MM wall time, got {value!r}")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        raise WallClockError(f"expected a HH:MM wall time, got {value!r}") from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise WallClockError(f"wall time out of range: {value!r}")
    return time(hour=hour, minute=minute, second=second)


def wall_instant(wall: time, day: date, tz: tzinfo) -> datetime:
    """Anchor a wall time to a calendar day in `tz`.

    Ambiguous DST local times resolve to the first (fold=0) occurrence. That is
    deterministic and documented rather than silently implementation-defined.
    """
    return datetime.combine(day, wall).replace(tzinfo=tz, fold=0)


def next_occurrence(wall: time, now: datetime, tz: tzinfo | None = None) -> datetime:
    """First instant at or after `now` whose local time-of-day is `wall`."""
    if now.tzinfo is None:
        raise WallClockError("`now` must be timezone-aware")
    zone = tz or now.tzinfo
    local_now = now.astimezone(zone)
    candidate = wall_instant(wall, local_now.date(), zone)
    if candidate < local_now:
        candidate = wall_instant(wall, local_now.date() + timedelta(days=1), zone)
    return candidate


def days_after(wall: time, day: date, tz: tzinfo, days: int) -> datetime:
    """Anchor `wall` to `day + days` in `tz`."""
    return wall_instant(wall, day + timedelta(days=days), tz)


@dataclass(frozen=True)
class WallWindow:
    """A resolved release/deadline pair plus how the rollover was decided."""

    release: datetime
    deadline: datetime
    crosses_midnight: bool
    rollover_source: str  # "inferred" | "declared"

    def minutes(self) -> float:
        return (self.deadline - self.release).total_seconds() / 60.0


def resolve_wall_window(
    release_wall: time,
    deadline_wall: time,
    now: datetime,
    tz: tzinfo | None = None,
    crosses_midnight: bool | None = None,
) -> WallWindow:
    """Resolve a same-day pair of wall times into a valid time window.

    `crosses_midnight` may assert the rollover explicitly:
      * None      -> inferred from the wall times (deadline < release => next day)
      * True      -> deadline is forced onto the next day
      * False     -> same day only; a deadline before its release is an error
    """
    if now.tzinfo is None:
        raise WallClockError("`now` must be timezone-aware")
    zone = tz or now.tzinfo
    inferred = deadline_wall < release_wall

    if crosses_midnight is False and inferred:
        raise WallClockError(
            f"deadline {deadline_wall.isoformat()} is before release {release_wall.isoformat()} "
            "on the same day, but same-day was declared — use crosses_midnight=True for next-day completion"
        )

    if crosses_midnight is not True and deadline_wall == release_wall:
        # Equal wall times describe a zero-length window, which is what the
        # widening fix-up below used to silently turn into a full 24 hours of
        # scheduling freedom — a night the user never asked for, reported back
        # to them as "deadline rolled over to the next day" for a window that
        # does not cross midnight at all. An explicit crosses_midnight=True is
        # the honest way to ask for the all-day window; anything else is an
        # error, because a load needs time to actually run.
        raise WallClockError(
            f"release and deadline are both {release_wall.isoformat()}, which is a "
            "zero-length window. Give the load a later deadline, or pass "
            "crosses_midnight=True to ask for the full day."
        )

    crosses = inferred if crosses_midnight is None else crosses_midnight
    source = "inferred" if crosses_midnight is None else "declared"

    release = next_occurrence(release_wall, now, zone)
    if crosses:
        # Anchor to the RELEASE day, not to "now": a window that crosses
        # midnight belongs to the evening it starts.
        deadline = days_after(deadline_wall, release.astimezone(zone).date(), zone, 1)
        if deadline <= release:
            deadline = deadline + timedelta(days=1)
    else:
        deadline = next_occurrence(deadline_wall, now, zone)

    if deadline <= release:
        deadline = deadline + timedelta(days=1)
        crosses = True
        source = "inferred"
    return WallWindow(release=release, deadline=deadline, crosses_midnight=crosses, rollover_source=source)


def elapsed_minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0
