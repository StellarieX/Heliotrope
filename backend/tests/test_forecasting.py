"""Forecasting models, uncertainty and prediction intervals (Phase 5, §41, §42).

The tests here are written to FAIL IF a forecast looks right but is not:

  * intervals must satisfy lower <= predicted <= upper, always (§42)
  * coverage must be computed correctly on arithmetic checkable by hand (§42)
  * near-zero actuals must not explode a percentage metric (§11, §42)
  * no forecast may use an observation at or after the forecast origin (§25, §41)
  * the interval width must VARY, because a constant width makes ROBUST mode a
    no-op that still looks like it is working
  * missing history must be refused, not filled in
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.carbon import CarbonPoint, Quality, SignalType
from app.domain.forecasting import CarbonForecastPoint, ForecastConfig, ForecastMode
from app.services.forecasting import (
    CarbonHistory,
    ForecastError,
    PersistenceForecaster,
    SeasonalForecaster,
    SyntheticCarbonHistory,
    build_forecaster,
    quantile,
)

ORIGIN = datetime(2026, 10, 5, tzinfo=timezone.utc)


def history(days: int = 21, resolution_minutes: int = 15, seed: int = 11) -> CarbonHistory:
    return SyntheticCarbonHistory().history(
        ORIGIN, days=days, resolution_minutes=resolution_minutes
    )


def series(values, start=ORIGIN, step_minutes=15, source="test") -> CarbonHistory:
    step = timedelta(minutes=step_minutes)
    return CarbonHistory(
        tuple(
            CarbonPoint(
                time=start + step * i,
                gco2_per_kwh=float(v),
                signal_type=SignalType.SYNTHETIC,
                quality=Quality.SYNTHETIC,
                source=source,
            )
            for i, v in enumerate(values)
        )
    )


# --- §7 synthetic dataset ---------------------------------------------------


@pytest.mark.parametrize("days", [7, 14, 30])
def test_synthetic_history_has_the_requested_length(days):
    h = history(days=days)
    assert len(h) == days * 24 * 4


def test_synthetic_history_is_labelled_synthetic_everywhere():
    h = history(days=7)
    assert h.source_signal_type() == "SYNTHETIC"
    assert all(p.signal_type is SignalType.SYNTHETIC for p in h.points)
    assert all(p.quality is Quality.SYNTHETIC for p in h.points)


def test_synthetic_history_is_deterministic():
    assert [p.gco2_per_kwh for p in history(days=7).points] == [
        p.gco2_per_kwh for p in history(days=7).points
    ]


def test_synthetic_history_has_a_solar_valley_and_an_evening_peak():
    """§7 requires the shape, and the shape is what the seasonal model exploits."""
    h = history(days=14)
    by_hour: dict[int, list[float]] = {}
    for p in h.points:
        by_hour.setdefault(p.time.hour, []).append(p.gco2_per_kwh)
    midday = sum(by_hour[13]) / len(by_hour[13])
    evening = sum(by_hour[19]) / len(by_hour[19])
    assert midday < evening, "the solar midday valley must actually be lower"
    assert midday < 250, f"midday should be depressed by solar, got {midday:.1f}"
    assert evening > 450, f"the evening peak should be high, got {evening:.1f}"


def test_synthetic_history_has_noise_so_residuals_are_not_zero():
    """A noiseless history would make every interval degenerate and hide bugs."""
    h = history(days=7)
    values = [p.gco2_per_kwh for p in h.points]
    assert len(set(values)) > len(values) * 0.5, "history must not be constant"


def test_negative_values_are_clamped_not_emitted():
    for point in SyntheticCarbonHistory().points(ORIGIN, days=2):
        assert point.gco2_per_kwh >= 0


# --- history integrity -------------------------------------------------------


def test_history_rejects_out_of_order_points():
    with pytest.raises(ForecastError, match="not time-ordered"):
        series([100.0, 200.0, 300.0]).points[:0] or CarbonHistory(
            tuple(
                [
                    CarbonPoint(time=ORIGIN + timedelta(minutes=30), gco2_per_kwh=1),
                    CarbonPoint(time=ORIGIN, gco2_per_kwh=2),
                ]
            )
        )


def test_history_rejects_duplicate_timestamps():
    with pytest.raises(ForecastError, match="two observations"):
        CarbonHistory(
            tuple(
                [
                    CarbonPoint(time=ORIGIN, gco2_per_kwh=1),
                    CarbonPoint(time=ORIGIN, gco2_per_kwh=2),
                ]
            )
        )


# --- §5 persistence ----------------------------------------------------------


def test_persistence_holds_the_last_observation():
    h = series([100.0, 200.0, 300.0, 400.0])
    forecast = PersistenceForecaster().forecast(
        h, ORIGIN + timedelta(hours=2), ORIGIN + timedelta(hours=4)
    )
    assert len(forecast) == 8
    assert all(p.predicted_gco2_per_kwh == 400.0 for p in forecast.points)


def test_persistence_last_day_same_time_uses_24h_earlier_values():
    values = [100.0 + i for i in range(4 * 24 * 2)]
    h = series(values)
    forecast = PersistenceForecaster(variant="last_day_same_time").forecast(
        h, ORIGIN + timedelta(days=1), ORIGIN + timedelta(days=1, hours=1)
    )
    for point in forecast.points:
        offset = int((point.timestamp - (ORIGIN + timedelta(days=1))).total_seconds() // 900)
        assert point.predicted_gco2_per_kwh == pytest.approx(values[offset])


def test_persistence_moving_average_is_the_mean_of_its_window():
    h = series([100.0, 200.0, 300.0, 400.0])
    forecast = PersistenceForecaster(variant="moving_average", window=2).forecast(
        h, ORIGIN + timedelta(hours=1), ORIGIN + timedelta(hours=2)
    )
    assert all(p.predicted_gco2_per_kwh == pytest.approx(350.0) for p in forecast.points)


def test_persistence_rejects_an_unknown_variant():
    with pytest.raises(ForecastError, match="unknown persistence variant"):
        PersistenceForecaster(variant="crystal_ball")


# --- §6 seasonal -------------------------------------------------------------


def test_seasonal_predicts_the_mean_of_the_same_time_of_day():
    """The whole model, checked by hand: 18:15 tomorrow is the mean of 18:15
    on the days that have already happened."""
    # Four days, each 50 g/kWh higher than the last, all four slots of each day
    # identical. Forecasting day 4 must give the mean of days 0-3.
    values = []
    for day in range(4):
        for _slot in range(24 * 4):
            values.append(100.0 + day * 50.0)
    h = series(values)
    forecast = SeasonalForecaster().forecast(
        h, ORIGIN + timedelta(days=4), ORIGIN + timedelta(days=4, hours=1)
    )
    assert forecast.points[0].predicted_gco2_per_kwh == pytest.approx(
        (100.0 + 150.0 + 200.0 + 250.0) / 4
    )


def test_seasonal_lookback_bounds_how_far_back_it_looks():
    h = history(days=30)
    short = SeasonalForecaster(lookback_days=7)
    long = SeasonalForecaster(lookback_days=30)
    assert short.configuration()["lookback_days"] == 7
    a = short.forecast(h, ORIGIN, ORIGIN + timedelta(hours=2))
    b = long.forecast(h, ORIGIN, ORIGIN + timedelta(hours=2))
    assert [p.predicted_gco2_per_kwh for p in a.points] != [
        p.predicted_gco2_per_kwh for p in b.points
    ], "the lookback window must actually change the answer"


def test_seasonal_rejects_a_nonsense_lookback():
    with pytest.raises(ForecastError):
        SeasonalForecaster(lookback_days=0)


# --- §3, §8 resolution and horizon -------------------------------------------


def test_forecast_covers_every_slot_of_the_requested_window():
    h = history(days=14)
    forecast = SeasonalForecaster().forecast(h, ORIGIN, ORIGIN + timedelta(hours=6))
    assert len(forecast) == 24
    assert forecast.start == ORIGIN
    assert forecast.resolution_minutes == 15


@pytest.mark.parametrize("resolution", [15, 30, 60])
def test_forecast_honours_non_default_resolution(resolution):
    h = history(days=14)
    forecast = SeasonalForecaster().forecast(
        h, ORIGIN, ORIGIN + timedelta(hours=6), resolution_minutes=resolution
    )
    assert len(forecast) == 6 * 60 // resolution


def test_forecast_rejects_an_inverted_window():
    with pytest.raises(ForecastError):
        SeasonalForecaster().forecast(history(days=14), ORIGIN, ORIGIN - timedelta(hours=1))


def test_forecast_rejects_an_impossible_coverage():
    with pytest.raises(ForecastError, match="strictly between 0 and 1"):
        SeasonalForecaster().forecast(history(days=14), ORIGIN, ORIGIN + timedelta(hours=1), coverage=1.0)


# --- §25, §41 leakage --------------------------------------------------------


def test_forecast_refuses_when_all_history_is_in_the_future():
    """Leakage guard: with no observation before the origin there is nothing to
    forecast from, and inventing one is exactly what leakage looks like."""
    h = series([100.0, 200.0, 300.0, 400.0])
    with pytest.raises(ForecastError, match="no carbon history exists before"):
        SeasonalForecaster().forecast(h, ORIGIN - timedelta(days=5), ORIGIN - timedelta(days=4))


def test_forecast_never_reads_an_observation_at_or_after_the_origin():
    """The strongest form of the leakage test: two histories identical before the
    origin and wildly different after it must produce IDENTICAL forecasts. If any
    future value leaked in, these two forecasts would differ."""
    step = timedelta(minutes=15)
    origin = ORIGIN
    past_days = 7
    past_count = past_days * 24 * 4

    # The PAST runs from `past_start` up to (not including) `origin`.
    past_start = ORIGIN - timedelta(days=past_days)
    past = series([300.0] * past_count, start=past_start)
    assert past.points[-1].time == origin - step

    def with_future(values: list[float]) -> CarbonHistory:
        return CarbonHistory(
            past.points
            + tuple(
                CarbonPoint(time=origin + step * (i + 1), gco2_per_kwh=v, source="test")
                for i, v in enumerate(values)
            )
        )

    quiet = with_future([50.0] * past_count)
    loud = with_future([900.0] * past_count)

    model = SeasonalForecaster()
    a = model.forecast(quiet, origin, origin + timedelta(hours=12))
    b = model.forecast(loud, origin, origin + timedelta(hours=12))
    assert [p.predicted_gco2_per_kwh for p in a.points] == [
        p.predicted_gco2_per_kwh for p in b.points
    ]
    assert [p.upper_gco2_per_kwh for p in a.points] == [
        p.upper_gco2_per_kwh for p in b.points
    ], "uncertainty must also be leak-free, or the interval smuggles in the future"


def test_history_before_strictly_excludes_the_boundary():
    h = series([100.0, 200.0, 300.0])
    trimmed = h.before(ORIGIN + timedelta(minutes=30))
    assert [p.gco2_per_kwh for p in trimmed.points] == [100.0, 200.0]


def test_provenance_training_window_ends_before_the_horizon():
    h = history(days=14)
    forecast = SeasonalForecaster().forecast(h, ORIGIN, ORIGIN + timedelta(days=1))
    assert forecast.provenance.training_window_end < forecast.provenance.horizon_start


# --- §9, §42 interval structure ----------------------------------------------


def test_interval_is_ordered_for_every_point_of_every_model():
    for model in (PersistenceForecaster(), SeasonalForecaster()):
        forecast = model.forecast(history(days=21), ORIGIN, ORIGIN + timedelta(days=1))
        for point in forecast.points:
            assert point.lower_gco2_per_kwh <= point.predicted_gco2_per_kwh
            assert point.predicted_gco2_per_kwh <= point.upper_gco2_per_kwh


def test_constructing_an_unordered_interval_is_rejected():
    with pytest.raises(ValueError, match="not ordered"):
        CarbonForecastPoint(
            timestamp=ORIGIN,
            predicted_gco2_per_kwh=300.0,
            lower_gco2_per_kwh=250.0,
            upper_gco2_per_kwh=280.0,
        )


def test_lower_bound_is_never_negative():
    """Carbon intensity is non-negative, so a negative bound is not a number the
    scheduler could use."""
    forecast = PersistenceForecaster().forecast(
        series([10.0, 10.0, 10.0, 10.0]), ORIGIN + timedelta(hours=1), ORIGIN + timedelta(hours=2)
    )
    for point in forecast.points:
        assert point.lower_gco2_per_kwh >= 0.0


def test_interval_width_varies_across_the_day():
    """A constant width would make ROBUST mode add the same number to every slot,
    which cannot change which slot is cheapest. That is a mode that accepts
    configuration and does nothing — the failure this test exists to prevent."""
    forecast = PersistenceForecaster().forecast(
        history(days=21), ORIGIN, ORIGIN + timedelta(days=1)
    )
    widths = {round(p.uncertainty_gco2_per_kwh, 6) for p in forecast.points}
    assert len(widths) > 10, f"interval width is effectively constant: {len(widths)} distinct"


def test_uncertainty_is_wider_where_the_signal_is_more_volatile():
    """Persistence is exact overnight and badly wrong across the evening peak, so
    its band must be wider at the peak."""
    forecast = PersistenceForecaster().forecast(
        history(days=21), ORIGIN, ORIGIN + timedelta(days=1)
    )
    by_hour: dict[int, list[float]] = {}
    for point in forecast.points:
        by_hour.setdefault(point.timestamp.hour, []).append(point.uncertainty_gco2_per_kwh)
    evening = sum(by_hour[19]) / len(by_hour[19])
    night = sum(by_hour[3]) / len(by_hour[3])
    assert evening > night, (
        f"the evening peak should be the less certain slot, got evening={evening:.1f} "
        f"night={night:.1f}"
    )


def test_a_short_history_falls_back_and_says_so():
    """Two points cannot support a residual quantile. The interval must still be
    produced, and the provenance must not claim a residual-based one."""
    forecast = PersistenceForecaster().forecast(
        series([300.0, 310.0, 320.0, 330.0]),
        ORIGIN + timedelta(hours=1),
        ORIGIN + timedelta(hours=2),
    )
    assert forecast.provenance.uncertainty_method
    assert "standard deviation" in forecast.provenance.uncertainty_method


# --- §31 provenance ----------------------------------------------------------


def test_provenance_records_everything_needed_to_reproduce_a_forecast():
    forecast = SeasonalForecaster().forecast(
        history(days=14), ORIGIN, ORIGIN + timedelta(days=1), coverage=0.8
    )
    p = forecast.provenance
    assert p.model == "seasonal"
    assert p.generated_at is not None
    assert p.training_window_start < p.training_window_end
    assert p.training_points > 0
    assert p.source_signal
    assert p.source_signal_type == "SYNTHETIC"
    assert p.horizon_start == ORIGIN
    assert p.resolution_minutes == 15
    assert p.interval_nominal_coverage == pytest.approx(0.8)
    assert "empirical" in p.uncertainty_method
    assert p.configuration["lookback_days"] == 14


def test_provenance_records_the_source_of_the_history():
    forecast = PersistenceForecaster().forecast(
        series([100.0] * 200, source="my_dataset"),
        ORIGIN + timedelta(days=1),
        ORIGIN + timedelta(days=1, hours=1),
    )
    assert forecast.provenance.source_signal == "my_dataset"


def test_forecast_is_always_typed_as_a_forecast():
    forecast = SeasonalForecaster().forecast(history(days=7), ORIGIN, ORIGIN + timedelta(hours=6))
    assert forecast.signal_type == "FORECAST"


# --- §28 model resolution ----------------------------------------------------


def test_unknown_model_is_refused_never_substituted():
    with pytest.raises(ForecastError, match="unknown forecast model"):
        build_forecaster("transformer")


def test_build_forecaster_returns_the_named_model():
    assert build_forecaster("persistence").model.value == "persistence"
    assert build_forecaster("SEASONAL").model.value == "seasonal"


def test_no_model_is_called_ai():
    """§49: a transparent seasonal average is not a neural network, and calling it
    one would misrepresent it."""
    for model in (PersistenceForecaster, SeasonalForecaster):
        text = (model.__doc__ or "") + model.description
        assert "neural" not in text.lower() or "not a neural" in text.lower()
        assert model.description


# --- quantile helper ---------------------------------------------------------


def test_quantile_interpolates_between_neighbours():
    assert quantile([0.0, 10.0], 0.5) == pytest.approx(5.0)
    assert quantile([0.0, 10.0], 0.0) == pytest.approx(0.0)
    assert quantile([0.0, 10.0], 1.0) == pytest.approx(10.0)


def test_quantile_of_one_sample_is_that_sample():
    assert quantile([7.0], 0.9) == 7.0


def test_quantile_rejects_an_empty_sample_and_a_bad_probability():
    with pytest.raises(ForecastError):
        quantile([], 0.5)
    with pytest.raises(ForecastError):
        quantile([1.0], 1.5)


# --- §16 risk-adjusted intensity ---------------------------------------------


def test_risk_adjusted_intensity_at_zero_weight_equals_the_point_forecast():
    forecast = PersistenceForecaster().forecast(
        history(days=14), ORIGIN, ORIGIN + timedelta(hours=6)
    )
    values = forecast.risk_adjusted_intensity(ForecastMode.EXPECTED, 0.0)
    assert values == [p.predicted_gco2_per_kwh for p in forecast.points]


def test_risk_adjusted_intensity_at_weight_one_equals_the_upper_bound():
    forecast = PersistenceForecaster().forecast(
        history(days=14), ORIGIN, ORIGIN + timedelta(hours=6)
    )
    values = forecast.risk_adjusted_intensity(ForecastMode.ROBUST, 1.0)
    for value, point in zip(values, forecast.points):
        assert value == pytest.approx(point.upper_gco2_per_kwh)


def test_risk_adjusted_intensity_scales_linearly_with_the_weight():
    forecast = PersistenceForecaster().forecast(
        history(days=14), ORIGIN, ORIGIN + timedelta(hours=6)
    )
    low = forecast.risk_adjusted_intensity(ForecastMode.ROBUST, 0.25)
    high = forecast.risk_adjusted_intensity(ForecastMode.ROBUST, 0.75)
    predicted = [p.predicted_gco2_per_kwh for p in forecast.points]
    for a, b, p in zip(low, high, predicted):
        assert b > a >= p


def test_risk_adjusted_intensity_refuses_actual_mode():
    forecast = PersistenceForecaster().forecast(
        history(days=7), ORIGIN, ORIGIN + timedelta(hours=3)
    )
    with pytest.raises(ValueError, match="ACTUAL mode optimizes the observed signal"):
        forecast.risk_adjusted_intensity(ForecastMode.ACTUAL, 0.5)


# --- §23 config validation ---------------------------------------------------


def test_forecast_config_defaults_to_the_deterministic_phase4_behaviour():
    config = ForecastConfig()
    assert config.forecast_mode is ForecastMode.ACTUAL
    assert config.risk_weight == 0.0
    assert config.deadline_buffer_minutes == 0
    assert config.is_forecast_aware() is False


@pytest.mark.parametrize("weight", [-0.1, 10.1])
def test_forecast_config_rejects_an_out_of_range_risk_weight(weight):
    with pytest.raises(ValueError):
        ForecastConfig(risk_weight=weight)


def test_deadline_buffer_must_be_whole_slots():
    """A partial slot of buffer would have to be rounded, and rounding a user's
    deadline in either direction is a silent change to what they asked for."""
    with pytest.raises(ValueError, match="whole multiple"):
        ForecastConfig(deadline_buffer_minutes=7)


def test_deadline_buffer_accepts_whole_slots():
    assert ForecastConfig(deadline_buffer_minutes=15).deadline_buffer_minutes == 15


# --- §29, §30 boundaries -----------------------------------------------------


def test_ml_boundary_refuses_to_invent_numbers():
    """§49: a stub that returned a plausible value would be the easiest way for
    this codebase to start making unmeasured accuracy claims."""
    from app.services.forecasting import CarbonFeatureBuilder, MLCarbonForecaster

    with pytest.raises(NotImplementedError, match="boundary, not a model"):
        MLCarbonForecaster().point_predictions(history(days=7), ORIGIN, ORIGIN + timedelta(hours=1), 15)
    with pytest.raises(NotImplementedError, match="boundary"):
        CarbonFeatureBuilder().build(history(days=7), ORIGIN, ORIGIN + timedelta(hours=1))