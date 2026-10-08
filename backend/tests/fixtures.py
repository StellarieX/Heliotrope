"""Deterministic fixtures for scheduling (§38).

Five canonical scenarios, all synthetic and all fixed:

  1. EV            7.2 kW, 18 kWh, released 18:00, due 07:00, interruptible
  2. Washing machine 2.1 kW, 1 h, released 18:00, due 23:00, atomic
  3. Geyser        3.0 kW, thermal, hot-water target, morning deadline
  4. Fixed fan + refrigerator — baseline load, never scheduled
  5. Capacity bottleneck — several loads that cannot all run at once

The carbon signal has an OBVIOUS structure so tests can assert that a scheduler
moves load toward cleanliness without asserting an exact slot:

    09:00-16:00   100 gCO2/kWh   (solar dip)
    otherwise    500 gCO2/kWh   (evening ramp)
    02:00-05:00   150 gCO2/kWh   (overnight trough)

`horizon_hours` is generous on purpose: the normalizer refuses to schedule
against a signal that does not cover the horizon, which is correct but would
make every fixture brittle. 56 hours covers the 48-hour fixtures plus the
normalizer's one-slot padding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.carbon import CarbonPoint, CarbonSignal, SignalType
from app.domain.loads import LoadSpec, LoadType
from app.domain.thermal_examples import GEYSER_SYNTHETIC

def _day_start() -> datetime:
    """Midnight UTC on the day the suite runs.

    Anchoring to a literal date was a time bomb. Routes that fall back to
    `utcnow()` internally — `/tick`, `/override`, and the version bookkeeping
    behind them — compare against the real clock, so a horizon built on a fixed
    past date silently stops working the moment the calendar passes it: ticks
    look "not due" because the elapsed time is negative, and overrides are
    rejected for having no slots left before a deadline that is already behind
    the server. Deriving the anchor from "today" keeps those routes
    deterministic and the suite green on any run date.
    """
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


DAY_START = _day_start()


def day_str(offset_days: int = 0) -> str:
    """`DAY_START` shifted by `offset_days`, as the YYYY-MM-DD prefix that
    every wall-clock literal in the suite is built from."""
    return (DAY_START + timedelta(days=offset_days)).date().isoformat()


def at(hours: float) -> datetime:
    """Hours after DAY_START."""
    return DAY_START + timedelta(hours=hours)


def carbon_at_hour(hour: float) -> float:
    """The obvious duck-curve used by every scheduling test."""
    hour = hour % 24
    if 9 <= hour < 16:
        return 100.0
    if 2 <= hour < 5:
        return 150.0
    return 500.0


def make_signal(
    start: datetime = DAY_START,
    hours: int = 56,
    resolution_minutes: int = 15,
    source: str = "test_duck_curve",
) -> CarbonSignal:
    """A complete, gapless signal so the normalizer never has to reject it."""
    slots = int(hours * 60 / resolution_minutes)
    points = [
        CarbonPoint(
            time=start + timedelta(minutes=resolution_minutes * i),
            gco2_per_kwh=carbon_at_hour(
                start.hour + (start.minute + resolution_minutes * i) / 60
            ),
            signal_type=SignalType.SYNTHETIC,
            source=source,
        )
        for i in range(slots)
    ]
    return CarbonSignal(
        start=start,
        end=start + timedelta(hours=hours),
        resolution_minutes=resolution_minutes,
        points=points,
    )


# --- 1. EV -----------------------------------------------------------------


def ev_job(**overrides) -> LoadSpec:
    base = dict(
        id="ev-1",
        normalized_name="Hostel EV",
        category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=7.2,
        max_power_kw=7.2,
        energy_required_kwh=18.0,
        min_chunk_minutes=15,
        release_at=at(18),
        deadline_at=at(31),
    )
    base.update(overrides)
    return LoadSpec(**base)


# --- 2. washing machine ----------------------------------------------------


def washing_machine_job(**overrides) -> LoadSpec:
    base = dict(
        id="wm-1",
        normalized_name="Washing machine",
        category="Laundry",
        job_type=LoadType.DEFERRABLE_ATOMIC,
        power_kw=2.1,
        duration_minutes=60,
        release_at=at(18),
        deadline_at=at(23),
    )
    base.update(overrides)
    return LoadSpec(**base)


def oven_job(**overrides) -> LoadSpec:
    base = dict(
        id="oven-1",
        normalized_name="Oven",
        category="Space heating",
        job_type=LoadType.DEFERRABLE_ATOMIC,
        power_kw=2.3,
        duration_minutes=90,
        release_at=at(18),
        deadline_at=at(23),
    )
    base.update(overrides)
    return LoadSpec(**base)


# --- 3. geyser -------------------------------------------------------------


def geyser_job(**overrides) -> LoadSpec:
    """3.0 kW, hot-water target, morning deadline.

    The synthetic geyser dynamics cap out at 2 kW, so a 3 kW rating would exceed
    the model's own ceiling. The rating is therefore 2 kW and the PLACEHOLDER
    coefficient set is labelled synthetic — inventing a 3 kW coefficient to
    match a spec example would be exactly the fake precision Phase 3 forbids.
    """
    base = dict(
        id="geyser-1",
        normalized_name="Geyser",
        category="Water heating",
        job_type=LoadType.THERMAL,
        power_kw=2.0,
        max_power_kw=2.0,
        thermal=GEYSER_SYNTHETIC.model_copy(deep=True),
        release_at=at(18),
        deadline_at=at(31),
    )
    base.update(overrides)
    return LoadSpec(**base)


# --- 4. fixed baseline loads ----------------------------------------------


def fan_job(**overrides) -> LoadSpec:
    base = dict(
        id="fan-1",
        normalized_name="Ceiling fan",
        category="Always-on",
        job_type=LoadType.FIXED,
        power_kw=0.1,
        release_at=at(0),
        deadline_at=at(48),
    )
    base.update(overrides)
    return LoadSpec(**base)


def fridge_job(**overrides) -> LoadSpec:
    base = dict(
        id="fridge-1",
        normalized_name="Refrigerator",
        category="Refrigeration",
        job_type=LoadType.FIXED,
        power_kw=0.2,
        release_at=at(0),
        deadline_at=at(48),
    )
    base.update(overrides)
    return LoadSpec(**base)


def baseline_loads() -> list[LoadSpec]:
    """Example 4: 0.1 kW fan + 0.2 kW refrigerator = 0.3 kW of baseline."""
    return [fan_job(), fridge_job()]


# --- 5. capacity bottleneck ------------------------------------------------


def bottleneck_jobs() -> list[LoadSpec]:
    """Three loads that individually fit but cannot all run at once.

    7.2 + 5.0 + 4.0 = 16.2 kW of demand against a 10 kW connection, so any
    scheduler that ignores capacity would breach it immediately.
    """
    return [
        ev_job(id="ev-a", energy_required_kwh=18.0),
        washing_machine_job(id="wm-a", power_kw=5.0, duration_minutes=90),
        oven_job(id="oven-a", power_kw=4.0, duration_minutes=60, deadline_at=at(26)),
    ]


BOTTLENECK_CAPACITY_KW = 10.0


# --- combined scenarios ----------------------------------------------------


def ev_and_washing() -> list[LoadSpec]:
    return [ev_job(), washing_machine_job()]


def mixed_scenario() -> list[LoadSpec]:
    """Baseline + all three flexible classes in one horizon."""
    return baseline_loads() + [ev_job(), washing_machine_job(), geyser_job()]


def tariff_profile(
    slot_count: int, cheap_hours: tuple[float, float] = (9, 16), cheap: float = 0.10, dear: float = 0.30
) -> list[int]:
    """Time-of-use prices in micro-currency per kWh (§27).

    Deliberately NOT correlated with carbon in the fixtures that use it: the
    cheapest hours are the solar hours, but a test can invert the two to prove
    cost and carbon are accounted independently.
    """
    prices = []
    for i in range(slot_count):
        hour = (DAY_START.hour + i * 0.25) % 24
        prices.append(int(cheap * 1_000_000) if cheap_hours[0] <= hour < cheap_hours[1] else int(dear * 1_000_000))
    return prices
