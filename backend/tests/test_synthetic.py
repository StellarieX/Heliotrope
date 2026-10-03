"""Synthetic provider: shape, physics-plausibility, determinism."""

from datetime import timedelta

from app.domain.carbon import Quality, SignalType
from app.services.providers.synthetic import SyntheticConfig, SyntheticDuckCurveProvider


def test_24h_15min_gives_96_spaced_points(window):
    start, end = window
    points = SyntheticDuckCurveProvider().get_signal(start, end, 15)
    assert len(points) == 96
    assert points[0].time == start
    for a, b in zip(points, points[1:]):
        assert b.time - a.time == timedelta(minutes=15)
    for p in points:
        assert p.time.tzinfo is not None


def test_non_negative_and_labeled(window):
    start, end = window
    for p in SyntheticDuckCurveProvider().get_signal(start, end, 15):
        assert p.gco2_per_kwh >= 0
        assert p.signal_type == SignalType.SYNTHETIC
        assert p.quality == Quality.SYNTHETIC
        assert p.source == "synthetic_duck_curve"


def test_midday_valley_and_evening_peak(window):
    start, _ = window
    provider = SyntheticDuckCurveProvider()
    midday = [provider.intensity_at(h) for h in (11, 12, 13, 14, 15)]
    evening = [provider.intensity_at(h) for h in (18, 19, 20, 21)]
    assert sum(midday) / len(midday) < sum(evening) / len(evening)


def test_seed_deterministic_and_params_matter(window):
    start, end = window
    a = SyntheticDuckCurveProvider(SyntheticConfig(seed=3)).get_signal(start, end, 15)
    b = SyntheticDuckCurveProvider(SyntheticConfig(seed=3)).get_signal(start, end, 15)
    c = SyntheticDuckCurveProvider(SyntheticConfig(seed=4)).get_signal(start, end, 15)
    assert [p.gco2_per_kwh for p in a] == [p.gco2_per_kwh for p in b]
    d = SyntheticDuckCurveProvider(SyntheticConfig(seed=3, evening_peak=400)).get_signal(start, end, 15)
    assert [p.gco2_per_kwh for p in a] != [p.gco2_per_kwh for p in d]
    assert [p.gco2_per_kwh for p in a] != [p.gco2_per_kwh for p in c]


def test_smooth_no_wild_jumps(window):
    start, end = window
    values = [p.gco2_per_kwh for p in SyntheticDuckCurveProvider().get_signal(start, end, 15)]
    jumps = [abs(b - a) for a, b in zip(values, values[1:])]
    assert max(jumps) < 0.25 * (max(values) - min(values))


def test_seeded_property_sweep():
    """Generative invariant check: across seeds/days, non-negative + ordered."""
    import random
    from datetime import datetime, timezone

    rng = random.Random(1234)
    for _ in range(20):
        day = rng.randint(1, 28)
        seed = rng.randint(0, 10_000)
        start = datetime(2026, 1, day, tzinfo=timezone.utc)
        end = start + timedelta(hours=24)
        points = SyntheticDuckCurveProvider(SyntheticConfig(seed=seed)).get_signal(start, end, 15)
        assert len(points) == 96
        assert all(p.gco2_per_kwh >= 0 for p in points)
        assert all(b.time > a.time for a, b in zip(points, points[1:]))
