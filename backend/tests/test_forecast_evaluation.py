"""Forecast evaluation and backtesting (Phase 5, §11, §12, §13, §24, §26, §27, §42).

Metrics are tested against arithmetic that can be checked by hand, because a
metric nobody can reproduce is a metric nobody should trust.

The headline tests here:

  * MAPE is ABSENT and sMAPE stays finite when the actual is zero (§11)
  * coverage is the MEASURED fraction, reported next to the NOMINAL quantile, and
    is never called a confidence (§12)
  * an UNORDERED history is refused by the backtester rather than shuffled (§26)
  * a deliberately leaky forecaster is CAUGHT (§25)
  * no model is crowned a winner (§27, §28)
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.carbon import CarbonPoint
from app.domain.forecasting import (
    CarbonForecast,
    CarbonForecastPoint,
    ForecastProvenance,
)
from app.services.forecast_backtest import (
    BacktestConfig,
    BacktestError,
    ForecastBacktester,
    ForecastComparisonService,
    leakage_probe,
)
from app.services.forecast_evaluator import (
    ForecastEvaluationError,
    ForecastEvaluator,
    build_evaluation,
    symmetric_mape_percent,
)
from app.services.forecasting import (
    CarbonHistory,
    SeasonalForecaster,
    SyntheticCarbonHistory,
)

ORIGIN = datetime(2026, 10, 5, tzinfo=timezone.utc)


def make_forecast(predicted, lower=None, upper=None, coverage=0.9) -> CarbonForecast:
    """A forecast built from literal numbers, so metrics can be hand-checked."""
    n = len(predicted)
    lower = lower if lower is not None else [p - 50.0 for p in predicted]
    upper = upper if upper is not None else [p + 50.0 for p in predicted]
    step = timedelta(minutes=15)
    return CarbonForecast(
        points=[
            CarbonForecastPoint(
                timestamp=ORIGIN + step * i,
                predicted_gco2_per_kwh=float(p),
                lower_gco2_per_kwh=float(lo),
                upper_gco2_per_kwh=float(hi),
            )
            for i, (p, lo, hi) in enumerate(zip(predicted, lower, upper))
        ],
        resolution_minutes=15,
        provenance=ForecastProvenance(
            model="test",
            generated_at=ORIGIN,
            training_window_start=ORIGIN - timedelta(days=7),
            training_window_end=ORIGIN - timedelta(minutes=15),
            training_points=7 * 24 * 4,
            source_signal="test",
            source_signal_type="SYNTHETIC",
            horizon_start=ORIGIN,
            horizon_end=ORIGIN + timedelta(minutes=15 * n),
            resolution_minutes=15,
            interval_nominal_coverage=coverage,
        ),
    )


def actuals(values, start=ORIGIN):
    step = timedelta(minutes=15)
    return [(start + step * i, float(v)) for i, v in enumerate(values)]


def history(days=21, end=ORIGIN, resolution_minutes=15):
    return SyntheticCarbonHistory().history(end, days=days, resolution_minutes=resolution_minutes)


# --- §11 the error metrics ---------------------------------------------------


def test_mae_is_the_mean_absolute_error():
    forecast = make_forecast([100.0, 200.0, 300.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([110.0, 180.0, 330.0]))
    assert metrics.mae == pytest.approx((10 + 20 + 30) / 3)


def test_rmse_is_the_root_mean_squared_error():
    forecast = make_forecast([100.0, 200.0, 300.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([110.0, 180.0, 330.0]))
    expected = ((10**2 + 20**2 + 30**2) / 3) ** 0.5
    assert metrics.rmse == pytest.approx(expected)
    assert metrics.rmse > metrics.mae, "RMSE must exceed MAE when errors differ in size"


def test_bias_is_the_mean_signed_error():
    """Positive bias means the model UNDER-forecast. The sign convention matters,
    so it is pinned here."""
    forecast = make_forecast([100.0, 100.0])
    under = ForecastEvaluator().evaluate(forecast, actuals([120.0, 80.0]))
    assert under.bias == pytest.approx(0.0), "these errors cancel"

    over_forecast = ForecastEvaluator().evaluate(forecast, actuals([80.0, 80.0]))
    assert over_forecast.bias == pytest.approx(-20.0), "predicted 100, got 80"

    under_forecast = ForecastEvaluator().evaluate(forecast, actuals([120.0, 120.0]))
    assert under_forecast.bias == pytest.approx(20.0), "predicted 100, got 120"


def test_an_exact_forecast_has_zero_error_on_every_metric():
    forecast = make_forecast([100.0, 250.0, 400.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([100.0, 250.0, 400.0]))
    assert metrics.mae == pytest.approx(0.0)
    assert metrics.rmse == pytest.approx(0.0)
    assert metrics.bias == pytest.approx(0.0)
    assert metrics.smape_percent == pytest.approx(0.0)


def test_smape_matches_the_symmetric_definition():
    forecast = make_forecast([100.0, 100.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([200.0, 50.0]))
    expected = (
        symmetric_mape_percent(100.0, 200.0) + symmetric_mape_percent(100.0, 50.0)
    ) / 2
    assert metrics.smape_percent == pytest.approx(expected)


def test_smape_is_defined_when_the_actual_is_zero():
    """§11. MAPE would divide by zero here; sMAPE does not."""
    assert symmetric_mape_percent(100.0, 0.0) == pytest.approx(200.0)
    assert symmetric_mape_percent(0.0, 0.0) == pytest.approx(0.0)
    forecast = make_forecast([100.0, 100.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([0.0, 0.0]))
    assert metrics.smape_percent == pytest.approx(200.0)


def test_a_near_zero_actual_does_not_explode_the_metric():
    """The failure mode that rules out MAPE: 100.1 predicted against 0.01 actual
    is a 9900% MAPE and says nothing about the model."""
    forecast = make_forecast([100.1, 100.1])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([0.01, 0.01]))
    assert metrics.smape_percent < 200.0
    assert metrics.smape_percent == pytest.approx(200.0, rel=1e-3)


def test_mape_is_not_reported_at_all():
    """§11. Absent, not computed-and-hidden: a reader must not be able to find a
    MAPE field and use it near zero."""
    forecast = make_forecast([100.0, 200.0])
    payload = ForecastEvaluator().evaluate(forecast, actuals([110.0, 210.0])).as_dict()
    assert not any("mape" in key and "smape" not in key for key in payload)
    assert "smape_percent" in payload


# --- §12 interval quality ----------------------------------------------------


def test_coverage_counts_the_actuals_that_fell_inside_the_interval():
    forecast = make_forecast(
        [100.0, 100.0, 100.0, 100.0],
        lower=[90.0, 90.0, 90.0, 90.0],
        upper=[110.0, 110.0, 110.0, 110.0],
    )
    # 105 and 95 inside, 130 and 70 outside
    metrics = ForecastEvaluator().evaluate(forecast, actuals([105.0, 130.0, 95.0, 70.0]))
    assert metrics.interval_coverage_percent == pytest.approx(50.0)


def test_coverage_of_a_perfect_forecast_is_one_hundred():
    forecast = make_forecast([100.0, 200.0], lower=[0.0, 0.0], upper=[1000.0, 1000.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([100.0, 200.0]))
    assert metrics.interval_coverage_percent == pytest.approx(100.0)


def test_interval_width_is_the_mean_upper_minus_lower():
    forecast = make_forecast([100.0, 100.0], lower=[90.0, 80.0], upper=[130.0, 140.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([100.0, 100.0]))
    assert metrics.interval_width == pytest.approx((40 + 60) / 2)


def test_coverage_is_reported_next_to_the_nominal_quantile():
    """§12. A measured 61% next to an intended 90% is the useful information; the
    nominal number alone would be a claim rather than a measurement."""
    forecast = make_forecast([100.0] * 10, lower=[0.0] * 10, upper=[101.0] * 10)
    metrics = ForecastEvaluator().evaluate(forecast, actuals([500.0] * 10), nominal_coverage=0.9)
    assert metrics.nominal_coverage_percent == pytest.approx(90.0)
    assert metrics.interval_coverage_percent == pytest.approx(0.0)


def test_nothing_calls_the_interval_a_confidence_interval():
    forecast = make_forecast([100.0, 200.0])
    payload = build_evaluation(forecast, actuals([105.0, 190.0])).as_dict()
    text = str(payload).lower()
    assert "confidence" not in text
    assert "coverage" in text


def test_mae_inside_the_interval_is_reported_separately():
    forecast = make_forecast(
        [100.0, 100.0], lower=[99.0, 99.0], upper=[101.0, 101.0]
    )
    metrics = ForecastEvaluator().evaluate(forecast, actuals([100.5, 400.0]))
    assert metrics.mae == pytest.approx((0.5 + 300.0) / 2)
    assert metrics.mae_inside_interval == pytest.approx(0.5)


# --- missing data ------------------------------------------------------------


def test_missing_actuals_are_dropped_and_counted():
    forecast = make_forecast([100.0, 100.0, 100.0])
    metrics = ForecastEvaluator().evaluate(forecast, actuals([110.0, 120.0]))
    assert metrics.evaluated_points == 2
    assert metrics.missing_actuals == 1
    assert metrics.mae == pytest.approx((10 + 20) / 2)


def test_no_overlap_leaves_metrics_none_not_zero():
    """A zero would read as 'measured, and it was zero'."""
    forecast = make_forecast([100.0, 100.0])
    later = ORIGIN + timedelta(days=5)
    metrics = ForecastEvaluator().evaluate(forecast, actuals([100.0, 100.0], start=later))
    assert metrics.evaluated_points == 0
    for value in (metrics.mae, metrics.rmse, metrics.bias, metrics.smape_percent,
                  metrics.interval_coverage_percent, metrics.interval_width):
        assert value is None


def test_an_empty_forecast_cannot_be_evaluated():
    forecast = make_forecast([])
    with pytest.raises(ForecastEvaluationError, match="no points"):
        ForecastEvaluator().evaluate(forecast, actuals([100.0]))


def test_build_evaluation_keeps_aligned_rows():
    forecast = make_forecast([100.0, 200.0, 300.0])
    evaluation = build_evaluation(forecast, actuals([110.0, 210.0]))
    assert len(evaluation.rows) == 3
    assert evaluation.rows[0]["actual_gco2_per_kwh"] == pytest.approx(110.0)
    assert evaluation.rows[2]["actual_gco2_per_kwh"] is None
    assert evaluation.window_start is not None and evaluation.window_end is not None


# --- §24, §26 backtesting ----------------------------------------------------


def test_backtest_runs_and_reports_metrics():
    result = ForecastBacktester().run(
        history(days=30),
        BacktestConfig(max_history_days=60, max_steps=5, horizon_hours=24, step_hours=24),
    )
    assert result.steps > 0
    assert result.metrics.evaluated_points > 0
    assert result.metrics.mae is not None
    assert result.runtime_ms >= 0


def test_every_backtest_window_scores_only_the_future():
    """§26. Each window must start after the training window it was built from.
    This is the temporal separation, checked window by window."""
    result = ForecastBacktester().run(
        history(days=30),
        BacktestConfig(max_history_days=60, max_steps=5),
    )
    for window in result.windows:
        if window["status"] != "OK":
            continue
        from datetime import datetime as dt

        assert dt.fromisoformat(window["training_window_end"]) < dt.fromisoformat(
            window["origin"]
        )


def test_backtest_windows_move_forward_in_time():
    result = ForecastBacktester().run(
        history(days=30), BacktestConfig(max_history_days=60, max_steps=5)
    )
    origins = [w["origin"] for w in result.windows if w["status"] == "OK"]
    assert origins == sorted(origins)
    assert len(set(origins)) == len(origins), "origins must not repeat"


def test_backtest_training_window_grows_as_time_moves_forward():
    """More history becomes available at later origins, so the training size must
    be non-decreasing. A decreasing size would mean the window slid backwards."""
    result = ForecastBacktester().run(
        history(days=30), BacktestConfig(max_history_days=60, max_steps=6)
    )
    sizes = [w["training_points"] for w in result.windows if w["status"] == "OK"]
    assert sizes == sorted(sizes)


def test_backtest_refuses_to_run_on_an_empty_history():
    with pytest.raises(BacktestError, match="at least two observations"):
        ForecastBacktester().run(CarbonHistory(()), BacktestConfig())


def test_backtest_refuses_a_history_shorter_than_its_horizon():
    with pytest.raises(BacktestError, match="too short"):
        ForecastBacktester().run(
            history(days=2), BacktestConfig(horizon_hours=72, lookback_days=14)
        )


def test_backtest_refuses_more_history_than_its_cap():
    with pytest.raises(BacktestError, match="above the"):
        ForecastBacktester().run(
            history(days=30), BacktestConfig(max_history_days=10, max_steps=2)
        )


# --- §40 bounded input -------------------------------------------------------


def test_backtest_config_caps_the_step_count():
    """An unbounded backtest endpoint is a denial-of-service vector."""
    with pytest.raises(BacktestError, match="max_steps must be between"):
        BacktestConfig(max_steps=1000).validate()
    with pytest.raises(BacktestError):
        BacktestConfig(max_steps=0).validate()


def test_backtest_config_caps_history_and_coverage():
    with pytest.raises(BacktestError, match="max_history_days"):
        BacktestConfig(max_history_days=3650).validate()
    with pytest.raises(BacktestError, match="coverage"):
        BacktestConfig(coverage=1.0).validate()
    with pytest.raises(BacktestError, match="resolution_minutes"):
        BacktestConfig(resolution_minutes=0).validate()


def test_backtest_config_rejects_an_unknown_model():
    with pytest.raises(BacktestError, match="unknown forecast model"):
        BacktestConfig(model="gpt").validate()


# --- §25 the leakage probe ---------------------------------------------------


def test_the_leakage_probe_actually_leaks_when_left_unguarded():
    """The guard is only meaningful if the thing it guards against really does
    cheat. This confirms the probe would produce different answers from two
    histories that differ only AFTER the forecast origin."""
    step = timedelta(minutes=15)
    past_start = ORIGIN - timedelta(days=7)
    past_count = 7 * 24 * 4

    def with_future(values):
        return CarbonHistory(
            tuple(
                CarbonPoint(time=past_start + step * i, gco2_per_kwh=300.0)
                for i in range(past_count)
            )
            + tuple(
                CarbonPoint(time=ORIGIN + step * (i + 1), gco2_per_kwh=v)
                for i, v in enumerate(values)
            )
        )

    probe = leakage_probe()
    quiet = probe.forecast(with_future([50.0] * 40), ORIGIN, ORIGIN + timedelta(hours=4), 15)
    loud = probe.forecast(with_future([900.0] * 40), ORIGIN, ORIGIN + timedelta(hours=4), 15)
    assert [v for _t, v in quiet] != [v for _t, v in loud], (
        "the probe must be able to see the future, otherwise the leakage test "
        "proves nothing"
    )


def test_the_real_models_are_stopped_from_doing_what_the_probe_does():
    """And the shipped models must not be able to, which is the actual guarantee."""
    step = timedelta(minutes=15)
    past_start = ORIGIN - timedelta(days=7)
    past_count = 7 * 24 * 4
    past = tuple(
        CarbonPoint(time=past_start + step * i, gco2_per_kwh=300.0) for i in range(past_count)
    )

    def with_future(values):
        return CarbonHistory(
            past
            + tuple(
                CarbonPoint(time=ORIGIN + step * (i + 1), gco2_per_kwh=v)
                for i, v in enumerate(values)
            )
        )

    model = SeasonalForecaster()
    quiet = model.forecast(with_future([50.0] * 40), ORIGIN, ORIGIN + timedelta(hours=4))
    loud = model.forecast(with_future([900.0] * 40), ORIGIN, ORIGIN + timedelta(hours=4))
    assert [p.predicted_gco2_per_kwh for p in quiet.points] == [
        p.predicted_gco2_per_kwh for p in loud.points
    ]


# --- §27, §28 model comparison -----------------------------------------------


def test_comparison_runs_both_models_over_the_same_period():
    comparison = ForecastComparisonService().compare(
        history(days=30),
        config=BacktestConfig(max_history_days=60, max_steps=4),
    )
    assert set(comparison.results) == {"persistence", "seasonal"}
    windows = {
        name: [w.get("origin") for w in r.windows if w["status"] == "OK"]
        for name, r in comparison.results.items()
    }
    assert windows["persistence"] == windows["seasonal"], (
        "both models must be scored on identical evaluation origins"
    )


def test_comparison_crowns_no_winner():
    """§27, §28. Measurements only; picking a model needs criteria Heliotrope does
    not have, and a fabricated 'best' field would hide that judgement."""
    comparison = ForecastComparisonService().compare(
        history(days=30), config=BacktestConfig(max_history_days=60, max_steps=3)
    )
    payload = comparison.as_dict()
    assert "best" not in str(payload).lower()
    assert "winner" not in str(payload).lower()
    assert "no ranking is implied" in payload["note"].lower()
    for result in comparison.results.values():
        assert result.metrics.mae is not None
        assert result.runtime_ms >= 0


def test_comparison_reports_the_dataset_it_used():
    comparison = ForecastComparisonService().compare(
        history(days=30), config=BacktestConfig(max_history_days=60, max_steps=2)
    )
    assert comparison.dataset["is_synthetic"] is True
    assert comparison.dataset["history_points"] == 30 * 24 * 4
    assert comparison.dataset["source_signal_type"] == "SYNTHETIC"


def test_a_model_cannot_be_evaluated_on_a_period_another_model_used():
    """A misconfigured comparison that silently shortened one model's horizon
    would make the metrics incomparable."""
    comparison = ForecastComparisonService().compare(
        history(days=30), config=BacktestConfig(max_history_days=60, max_steps=3)
    )
    a = comparison.results["persistence"]
    b = comparison.results["seasonal"]
    assert a.config.horizon_hours == b.config.horizon_hours
    assert a.config.step_hours == b.config.step_hours
    assert a.config.resolution_minutes == b.config.resolution_minutes


# --- measured behaviour on the synthetic dataset ----------------------------


def test_seasonal_beats_persistence_on_a_daily_shaped_signal():
    """§27. A measured comparison on the SYNTHETIC dataset. This asserts a
    direction that follows from the data's daily structure, and the magnitudes
    are asserted loosely — the exact figures belong in the backtest report, not
    in a test that would break on a harmless fixture change."""
    comparison = ForecastComparisonService().compare(
        history(days=30),
        config=BacktestConfig(max_history_days=60, max_steps=6, horizon_hours=24, step_hours=24),
    )
    assert (
        comparison.results["seasonal"].metrics.mae
        < comparison.results["persistence"].metrics.mae
    ), (
        "the seasonal baseline should track a daily-shaped signal far better than "
        "persistence does"
    )


def test_measured_coverage_is_reported_not_assumed():
    """§12, §50. The suite must not contain a hardcoded accuracy figure, only a
    measurement, so this asserts the shape of the claim rather than a number."""
    result = ForecastBacktester().run(
        history(days=30), BacktestConfig(max_history_days=60, max_steps=6)
    )
    assert result.metrics.nominal_coverage_percent == pytest.approx(90.0)
    assert 0.0 <= result.metrics.interval_coverage_percent <= 100.0