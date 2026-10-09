"""Forecast history honesty and horizon alignment (service level).

  * caller-supplied labels travel with the point — measured data is never
    relabelled SYNTHETIC when the caller says otherwise
  * provider history is built as explicit `CarbonPoint`s, preserving the
    response's signal type and source
  * the synthetic fallback is loud: a warning names the cause, and the
    provenance still says SYNTHETIC
  * horizon alignment resamples within half-step tolerance (exact, off-grid
    and cross-resolution) and stays strict past the forecast extent
"""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.carbon import CarbonPoint, Quality, SignalType
from app.services.forecast_service import ForecastService, ForecastServiceError
from app.services.forecasting import SeasonalForecaster, SyntheticCarbonHistory

ORIGIN = datetime(2026, 10, 5, tzinfo=timezone.utc)


def _forecast(resolution_minutes=60, hours=6):
    history = SyntheticCarbonHistory().history(
        ORIGIN, days=14, resolution_minutes=resolution_minutes
    )
    return SeasonalForecaster().forecast(
        history, ORIGIN, ORIGIN + timedelta(hours=hours),
        resolution_minutes=resolution_minutes,
    )


# --- caller labels are preserved -------------------------------------------


def test_carbonpoint_labels_survive_history_from_points():
    service = ForecastService()
    points = [
        CarbonPoint(
            time=ORIGIN + timedelta(minutes=15 * i),
            gco2_per_kwh=300.0,
            signal_type=SignalType.AVERAGE,
            quality=Quality.MEASURED,
            source="grid",
        )
        for i in range(4)
    ]
    history = service.history_from_points(points)
    assert all(p.signal_type is SignalType.AVERAGE for p in history.points)
    assert all(p.quality is Quality.MEASURED for p in history.points)
    assert all(p.source == "grid" for p in history.points)


def test_pair_labels_can_declare_measured_data():
    service = ForecastService()
    pairs = [((ORIGIN + timedelta(minutes=15 * i)).isoformat(), 300.0) for i in range(4)]
    history = service.history_from_points(
        pairs, signal_type="AVERAGE", quality="MEASURED", source="grid"
    )
    assert history.source_signal() == "grid"
    assert history.source_signal_type() == "AVERAGE"
    assert all(p.quality is Quality.MEASURED for p in history.points)


def test_mapping_labels_are_preserved_per_item():
    service = ForecastService()
    history = service.history_from_points(
        [
            {
                "timestamp": ORIGIN.isoformat(),
                "gco2_per_kwh": 300.0,
                "signal_type": "MARGINAL",
                "quality": "MEASURED",
                "source": "grid",
            },
            {"timestamp": (ORIGIN + timedelta(minutes=15)).isoformat(), "gco2_per_kwh": 310.0},
        ]
    )
    assert history.points[0].signal_type is SignalType.MARGINAL
    assert history.points[0].quality is Quality.MEASURED
    # A bare second item keeps the call-level defaults, not the first item's.
    assert history.points[1].source == "caller"


# --- provider history is explicit -------------------------------------------


def test_history_from_provider_preserves_type_and_source():
    service = ForecastService()
    history = service.history_from_provider(ORIGIN, days=1, resolution_minutes=60)
    assert len(history) == 24
    assert history.source_signal() == "synthetic_duck_curve"
    assert history.source_signal_type() == "SYNTHETIC"
    assert all(isinstance(p, CarbonPoint) for p in history.points)


# --- the fallback is loud -----------------------------------------------------


class _ProviderDown:
    """A carbon source that is unreachable: the one real reason to fall back."""

    max_range_days = 7

    def get_signal(self, *args, **kwargs):
        from app.services.carbon_service import CarbonUnavailable

        raise CarbonUnavailable("upstream down")


def test_resolve_history_fallback_logs_a_warning(caplog):
    service = ForecastService(carbon_service=_ProviderDown())
    with caplog.at_level(logging.WARNING, logger="heliotrope.forecast"):
        history = service.resolve_history(ORIGIN, days=60, resolution_minutes=15)
    assert len(history) > 0
    assert history.source_signal() == "synthetic_history"
    assert history.source_signal_type() == "SYNTHETIC"
    assert any(
        "SYNTHETIC" in record.message or "synthetic" in record.message
        for record in caplog.records
    ), "the synthetic fallback must be logged, never silent"


# --- resampling -----------------------------------------------------------------


def test_exact_grid_is_unchanged():
    service = ForecastService()
    forecast = _forecast()
    upper = service.uncertainty_over_horizon(forecast, ORIGIN)
    assert len(upper) == 6


def test_off_grid_start_resamples_instead_of_422():
    service = ForecastService()
    forecast = _forecast()
    upper = service.uncertainty_over_horizon(forecast, ORIGIN + timedelta(minutes=7))
    assert len(upper) == 6


def test_coarse_forecast_resamples_onto_a_fine_grid():
    service = ForecastService()
    forecast = _forecast(resolution_minutes=60, hours=6)
    upper = service.uncertainty_over_horizon(
        forecast, ORIGIN, resolution_minutes=15,
        horizon_end=ORIGIN + timedelta(hours=6),
    )
    predicted = service.predicted_over_horizon(
        forecast, ORIGIN, resolution_minutes=15,
        horizon_end=ORIGIN + timedelta(hours=6),
    )
    assert len(upper) == 24
    assert len(predicted) == 24
    # The coarse slots agree exactly with the resampled fine slots above them.
    exact = service.uncertainty_over_horizon(forecast, ORIGIN)
    assert upper[::4] == exact


def test_truly_out_of_range_stays_strict():
    service = ForecastService()
    forecast = _forecast()
    with pytest.raises(ForecastServiceError):
        service.uncertainty_over_horizon(forecast, ORIGIN + timedelta(hours=7))
    with pytest.raises(ForecastServiceError):
        service.predicted_over_horizon(forecast, ORIGIN + timedelta(hours=7))


# --- forecast signals are labelled FORECAST, not SYNTHETIC ------------------


def test_forecast_signal_built_from_measured_history_is_labelled_forecast():
    service = ForecastService()
    pairs = [
        ((ORIGIN - timedelta(days=14) + timedelta(minutes=60 * i)).isoformat(), 300.0 + (i % 24))
        for i in range(14 * 24)
    ]
    history = service.history_from_points(
        pairs, signal_type="AVERAGE", quality="MEASURED", source="grid"
    )
    forecast = SeasonalForecaster().forecast(
        history, ORIGIN, ORIGIN + timedelta(hours=6), resolution_minutes=60
    )
    signal = service.as_carbon_signal(forecast)
    assert signal.points
    assert all(p.signal_type is SignalType.FORECAST for p in signal.points)
    assert all(p.is_forecast for p in signal.points)


def test_forecast_signal_built_from_synthetic_history_stays_synthetic():
    signal = ForecastService().as_carbon_signal(_forecast())
    assert signal.points
    assert all(p.signal_type is SignalType.SYNTHETIC for p in signal.points)
    assert all(p.is_forecast for p in signal.points)


def test_forecast_starting_off_the_slot_grid_still_follows_the_daily_shape():
    """A start like 16:23:08 must snap to the 15-minute grid. Unsnapped, no point
    matches a time-of-day bucket and the whole forecast collapses to one value."""
    service = ForecastService()
    start = datetime(2026, 10, 9, 16, 23, 8, tzinfo=timezone.utc)
    forecast = service.forecast(start, start + timedelta(hours=24))
    first = forecast.points[0].timestamp
    assert first.minute % 15 == 0 and first.second == 0
    predicted = {round(p.predicted_gco2_per_kwh, 3) for p in forecast.points}
    assert len(predicted) > 10, "forecast is flat: points did not match the history grid"
