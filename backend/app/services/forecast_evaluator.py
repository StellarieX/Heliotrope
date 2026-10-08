"""ForecastEvaluator (Phase 5, §11, §12, §13).

    forecast[t] vs actual[t] -> MAE, RMSE, sMAPE, bias, coverage, width

WHY THIS EXISTS. A forecast model with no measured error is a belief. This is the
layer that turns belief into numbers, and every accuracy statement Heliotrope
makes has to come from here, computed on a stated dataset over a stated period.

MAPE IS NOT REPORTED, AND sMAPE IS USED INSTEAD (§11). Carbon intensity gets
close to zero on a very sunny midday, and MAPE divides by the actual value, so a
single near-zero actual turns a percentage error into an arbitrarily large
number. That would make the metric a function of the weather rather than of the
model. sMAPE is symmetric and bounded at 200%, so it stays meaningful at zero.
MAPE is deliberately absent from the result rather than computed-and-hidden.

COVERAGE IS EMPIRICAL, NOT NOMINAL (§12). `interval_coverage` is the measured
fraction of actuals that fell inside the predicted interval. It is reported next
to the nominal quantile, and the gap between them is the useful information: an
interval asking for 90% that contains 61% of outcomes is a wide, honest number
that is still too narrow. Nothing here is called a confidence interval.

MISSING ACTUALS ARE DROPPED, AND COUNTED. Comparing a forecast against a slot
that has no observation would require inventing one. The dropped count is
reported so a coverage figure computed over 40 of 96 slots cannot be mistaken for
one computed over all 96.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from ..domain.forecasting import CarbonForecast, CarbonForecastPoint


@dataclass
class ForecastMetrics:
    """Measured forecast skill over one evaluated window (§11, §12).

    A field is `None` when it could not be computed — never 0.0, which would
    read as "measured, and it was zero".
    """

    #: points where a forecast and an actual were both available
    evaluated_points: int = 0
    #: forecast points that had no matching actual observation
    missing_actuals: int = 0
    #: actual observations with no matching forecast point
    missing_forecasts: int = 0

    mae: Optional[float] = None
    rmse: Optional[float] = None
    #: symmetric MAPE, as a PERCENTAGE (0-200). None when nothing evaluated.
    smape_percent: Optional[float] = None
    #: mean signed error, actual - predicted. POSITIVE means the model
    #: systematically UNDER-forecast. Units are gCO2/kWh, same as the input.
    bias: Optional[float] = None

    #: measured fraction of actuals inside [lower, upper], as a percentage.
    interval_coverage_percent: Optional[float] = None
    #: mean (upper - lower), in gCO2/kWh
    interval_width: Optional[float] = None
    #: the quantile the interval asked for, copied from provenance so the
    #: measured coverage can be read next to its target
    nominal_coverage_percent: Optional[float] = None

    #: mean absolute error restricted to points inside the interval
    mae_inside_interval: Optional[float] = None

    def as_dict(self) -> dict:
        return {
            "evaluated_points": self.evaluated_points,
            "missing_actuals": self.missing_actuals,
            "missing_forecasts": self.missing_forecasts,
            "mae_gco2_per_kwh": self.mae,
            "rmse_gco2_per_kwh": self.rmse,
            "smape_percent": self.smape_percent,
            "bias_gco2_per_kwh": self.bias,
            "interval_coverage_percent": self.interval_coverage_percent,
            "interval_width_gco2_per_kwh": self.interval_width,
            "nominal_coverage_percent": self.nominal_coverage_percent,
            "mae_inside_interval_gco2_per_kwh": self.mae_inside_interval,
        }


class ForecastEvaluationError(ValueError):
    """The forecast and the actuals cannot be compared as asked."""


def symmetric_mape_percent(predicted: float, actual: float) -> float:
    """sMAPE for one point, as a percentage, defined at zero without dividing by it.

    With both values zero the forecast is exact and the error is 0%. With one of
    them zero and the other not, the disagreement is total and the metric is its
    maximum, 200%. That is the standard symmetric definition and it stays finite
    at zero, which is the entire reason it is used here.
    """
    denominator = abs(predicted) + abs(actual)
    if denominator == 0.0:
        return 0.0
    return 100.0 * 2.0 * abs(predicted - actual) / denominator


class ForecastEvaluator:
    """Compares forecasts against observed carbon intensity."""

    def evaluate(
        self,
        forecast: CarbonForecast,
        actual: Iterable[tuple[datetime, float]],
        nominal_coverage: Optional[float] = None,
    ) -> ForecastMetrics:
        actual_map = {moment: value for moment, value in actual}
        forecast_map = {p.timestamp: p for p in forecast.points}

        if not forecast_map:
            raise ForecastEvaluationError("the forecast has no points to evaluate")

        evaluated: list[CarbonForecastPoint] = []
        missing_actuals = 0
        for point in forecast.points:
            if point.timestamp in actual_map:
                evaluated.append(point)
            else:
                missing_actuals += 1
        missing_forecasts = len([t for t in actual_map if t not in forecast_map])

        metrics = ForecastMetrics(
            evaluated_points=len(evaluated),
            missing_actuals=missing_actuals,
            missing_forecasts=missing_forecasts,
        )
        nominal = (
            nominal_coverage
            if nominal_coverage is not None
            else forecast.provenance.interval_nominal_coverage
        )
        metrics.nominal_coverage_percent = round(nominal * 100.0, 6)

        if not evaluated:
            # No overlap at all. Every error metric stays None rather than 0.0.
            return metrics

        errors = [actual_map[p.timestamp] - p.predicted_gco2_per_kwh for p in evaluated]
        absolute = [abs(e) for e in errors]

        n = len(evaluated)
        metrics.mae = sum(absolute) / n
        metrics.rmse = math.sqrt(sum(e * e for e in errors) / n)
        metrics.bias = sum(errors) / n
        metrics.smape_percent = (
            sum(
                symmetric_mape_percent(p.predicted_gco2_per_kwh, actual_map[p.timestamp])
                for p in evaluated
            )
            / n
        )

        inside = [
            p
            for p in evaluated
            if p.lower_gco2_per_kwh <= actual_map[p.timestamp] <= p.upper_gco2_per_kwh
        ]
        metrics.interval_coverage_percent = 100.0 * len(inside) / n
        metrics.interval_width = (
            sum(p.upper_gco2_per_kwh - p.lower_gco2_per_kwh for p in evaluated) / n
        )
        if inside:
            metrics.mae_inside_interval = (
                sum(abs(actual_map[p.timestamp] - p.predicted_gco2_per_kwh) for p in inside)
                / len(inside)
            )
        return metrics


@dataclass
class ForecastEvaluation:
    """A metrics bundle with the forecast and the actuals it was computed over."""

    model: str
    metrics: ForecastMetrics
    #: the evaluated window, so a number can always be traced to a period
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    resolution_minutes: int = 15
    horizon_hours: Optional[float] = None
    forecast: Optional[CarbonForecast] = None
    #: aligned (timestamp, predicted, actual, lower, upper) rows for inspection
    rows: list[dict] = field(default_factory=list)
    provenance: Optional[dict] = None

    def as_dict(self) -> dict:
        payload = {
            "model": self.model,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "resolution_minutes": self.resolution_minutes,
            "horizon_hours": self.horizon_hours,
            "metrics": self.metrics.as_dict(),
            "provenance": self.provenance,
            "rows": self.rows,
        }
        return payload


def build_evaluation(
    forecast: CarbonForecast,
    actual: Iterable[tuple[datetime, float]],
) -> ForecastEvaluation:
    """Evaluate and keep the aligned rows, so a metric can be audited by hand."""
    actual = list(actual)  # consumed twice below
    actual_map = {moment: value for moment, value in actual}
    rows = [
        {
            "timestamp": p.timestamp.isoformat(),
            "predicted_gco2_per_kwh": round(p.predicted_gco2_per_kwh, 6),
            "lower_gco2_per_kwh": round(p.lower_gco2_per_kwh, 6),
            "upper_gco2_per_kwh": round(p.upper_gco2_per_kwh, 6),
            "actual_gco2_per_kwh": (
                round(actual_map[p.timestamp], 6) if p.timestamp in actual_map else None
            ),
        }
        for p in forecast.points
    ]
    evaluated_rows = [r for r in rows if r["actual_gco2_per_kwh"] is not None]
    window_start = evaluated_rows[0]["timestamp"] if evaluated_rows else None
    window_end = evaluated_rows[-1]["timestamp"] if evaluated_rows else None
    metrics = ForecastEvaluator().evaluate(forecast, actual)
    horizon_hours = (
        (forecast.provenance.horizon_end - forecast.provenance.horizon_start).total_seconds()
        / 3600.0
    )
    return ForecastEvaluation(
        model=forecast.provenance.model,
        metrics=metrics,
        window_start=window_start and datetime.fromisoformat(window_start),
        window_end=window_end and datetime.fromisoformat(window_end),
        resolution_minutes=forecast.resolution_minutes,
        horizon_hours=horizon_hours,
        forecast=forecast,
        rows=rows,
        provenance=forecast.provenance.model_dump(mode="json"),
    )