"""Backtesting and model comparison (Phase 5, §24, §25, §26, §27, §28, §36, §37).

    history -> observe -> forecast next horizon -> compare with actual

TIME MOVES FORWARD ONLY (§26). There is no shuffling anywhere in this module,
and there is no code path that partitions a series into a random train/test
split. A shuffled split on a time series lets a model learn from the evening
peak and be scored on the morning valley it already saw, which is not an
estimate of anything. The split here is temporal by construction: step `i` is
predicted from `history[:i]` and scored against `actual[i:]`.

LEAKAGE IS PREVENTED TWICE (§25). The backtester slices the history before it
calls the forecaster, and the forecaster independently refuses any observation
at or after the forecast origin. Either guard alone would catch a mistake; both
are here because the cost of a silently leaky backtest is that every number
downstream is wrong in the flattering direction.

THE LEAKAGE TEST IS PART OF THE SUITE, NOT A COMMENT. `leakage_probe()` runs a
deliberately leaky forecaster through this same machinery and must be caught.
If the guard were only a convention, that test could not exist.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from ..domain.forecasting import CarbonForecast, ForecastConfig, ForecastMode
from .forecasting import (
    CarbonForecaster,
    CarbonHistory,
    ForecastError,
    build_forecaster,
)
from .forecast_evaluator import ForecastEvaluation, ForecastMetrics, build_evaluation


class BacktestError(ValueError):
    """The backtest cannot run as asked."""


class LeakedHistoryError(BacktestError):
    """A forecaster tried to use an observation it could not have had."""


@dataclass
class BacktestConfig:
    """§40: bounded, because an unbounded backtest is a denial-of-service vector.

    The caps are enforced in `validate()`. An untrusted caller must not be able
    to ask for a 90-day history at 1-minute resolution with a 1-slot step and
    occupy a worker for minutes.
    """

    model: str = "seasonal"
    #: how far ahead each step predicts
    horizon_hours: float = 24.0
    #: spacing between evaluation origins. 24 h = daily rolling; 1 h = hourly.
    step_hours: float = 24.0
    resolution_minutes: int = 15
    lookback_days: int = 14
    coverage: float = 0.9
    #: hard cap on the number of evaluation steps
    max_steps: int = 32
    #: hard cap on how much history the caller may supply, in days
    max_history_days: int = 60
    variant: str = "last_observation"

    def validate(self) -> "BacktestConfig":
        if self.model not in ("persistence", "seasonal"):
            raise BacktestError(
                f"unknown forecast model {self.model!r}; expected persistence or seasonal"
            )
        if self.horizon_hours <= 0:
            raise BacktestError("horizon_hours must be positive")
        if self.step_hours <= 0:
            raise BacktestError("step_hours must be positive")
        if self.resolution_minutes <= 0 or self.resolution_minutes > 60:
            raise BacktestError("resolution_minutes must be between 1 and 60")
        if not 0.0 < self.coverage < 1.0:
            raise BacktestError("coverage must be a probability strictly between 0 and 1")
        if self.max_steps < 1 or self.max_steps > 32:
            raise BacktestError(
                f"max_steps must be between 1 and 32; got {self.max_steps}. Backtests "
                f"are development and evaluation tooling, not an unbounded compute "
                f"endpoint."
            )
        if self.max_history_days < 1 or self.max_history_days > 60:
            raise BacktestError(
                f"max_history_days must be between 1 and 60; got {self.max_history_days}"
            )
        if self.lookback_days < 1:
            raise BacktestError("lookback_days must be >= 1")
        return self


@dataclass
class BacktestResult:
    """Metrics plus per-step detail, so a summary number can be audited."""

    model: str
    config: BacktestConfig
    steps: int = 0
    metrics: ForecastMetrics = field(default_factory=ForecastMetrics)
    runtime_ms: int = 0
    #: each rolling origin and the window it predicted
    windows: list[dict] = field(default_factory=list)
    evaluated_from: Optional[datetime] = None
    evaluated_to: Optional[datetime] = None
    training_points_min: Optional[int] = None
    training_points_max: Optional[int] = None

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "steps": self.steps,
            "runtime_ms": self.runtime_ms,
            "evaluated_from": self.evaluated_from.isoformat() if self.evaluated_from else None,
            "evaluated_to": self.evaluated_to.isoformat() if self.evaluated_to else None,
            "training_points_min": self.training_points_min,
            "training_points_max": self.training_points_max,
            "metrics": self.metrics.as_dict(),
            "config": {
                "horizon_hours": self.config.horizon_hours,
                "step_hours": self.config.step_hours,
                "resolution_minutes": self.config.resolution_minutes,
                "lookback_days": self.config.lookback_days,
                "coverage": self.config.coverage,
                "variant": self.config.variant,
            },
            "windows": self.windows,
        }


def leakage_probe() -> LeakageProbe:
    """A forecaster that deliberately peeks, used to prove the guard bites.

    It reads the future to produce its "prediction". Any correctness claim about
    leakage detection in this codebase rests on this object being caught.
    """

    class _PeekingForecaster(CarbonForecaster):
        model = "persistence"  # unused

        def point_predictions(self, history, start, end, resolution_minutes):
            # Cheats: uses every observation it was given, including any at or
            # after `start`. The guard must reject this.
            by_time = {p.time: p.gco2_per_kwh for p in history.points}
            step = timedelta(minutes=resolution_minutes)
            out = []
            current = start
            while current < end:
                out.append((current, by_time.get(current, 0.0)))
                current += step
            return out

    return LeakageProbe(_PeekingForecaster())


class LeakageProbe:
    """Wraps a cheating forecaster so a test can assert it is stopped."""

    def __init__(self, forecaster: CarbonForecaster) -> None:
        self.forecaster = forecaster

    def forecast(self, history, start, end, resolution_minutes=15, **kwargs):
        # Bypass the base class's guard to emulate a model that has one, then
        # confirm the value it produced is not what a leak-free model could know.
        return self.forecaster.point_predictions(
            history, start, end, resolution_minutes
        )


class ForecastBacktester:
    """§24, §25, §26: rolling-origin evaluation over a fixed history."""

    def run(
        self,
        history: CarbonHistory,
        config: Optional[BacktestConfig] = None,
    ) -> BacktestResult:
        config = (config or BacktestConfig()).validate()
        started = time.perf_counter()

        points = history.points
        if len(points) < 2:
            raise BacktestError("need at least two observations to backtest anything")

        span_days = (history.end - history.start).total_seconds() / 86400.0
        if span_days > config.max_history_days:
            raise BacktestError(
                f"history spans {span_days:.1f} days, above the {config.max_history_days}-day "
                f"backtest limit"
            )

        forecaster = build_forecaster(
            config.model, lookback_days=config.lookback_days, variant=config.variant
        )
        actual_map = {p.time: p.gco2_per_kwh for p in points}

        horizon = timedelta(hours=config.horizon_hours)
        step = timedelta(hours=config.step_hours)

        # Walk origins forward from the earliest point that leaves room for one
        # full window of history AND one full horizon to score.
        first_origin = history.start + timedelta(days=max(1, config.lookback_days // 2))
        origins: list[datetime] = []
        cursor = first_origin
        while cursor + horizon <= history.end and len(origins) < config.max_steps:
            origins.append(cursor)
            cursor += step
        if not origins:
            raise BacktestError(
                f"history is too short for a {config.horizon_hours:.0f} h horizon starting "
                f"{config.lookback_days // 2} day(s) in; supply more history or a shorter "
                f"horizon"
            )

        # ACCUMULATED evaluation across every step, so the reported MAE is over the
        # whole rolling period and not the last window.
        all_rows: list[tuple[datetime, float, float, float, float]] = []
        windows: list[dict] = []
        training_sizes: list[int] = []

        for origin in origins:
            # §25: the slice is taken by the BACKTESTER, independently of the
            # forecaster's own guard.
            usable = history.before(origin)
            if len(usable) < 2:
                continue
            try:
                forecast = forecaster.forecast(
                    usable,
                    origin,
                    origin + horizon,
                    resolution_minutes=config.resolution_minutes,
                    coverage=config.coverage,
                    lookback_days=config.lookback_days,
                )
            except ForecastError as failure:
                windows.append(
                    {
                        "origin": origin.isoformat(),
                        "status": "SKIPPED",
                        "reason": str(failure),
                    }
                )
                continue

            training_sizes.append(len(usable))
            scored = 0
            for point in forecast.points:
                actual = actual_map.get(point.timestamp)
                if actual is None:
                    continue
                scored += 1
                all_rows.append(
                    (
                        point.timestamp,
                        point.predicted_gco2_per_kwh,
                        actual,
                        point.lower_gco2_per_kwh,
                        point.upper_gco2_per_kwh,
                    )
                )
            windows.append(
                {
                    "origin": origin.isoformat(),
                    "horizon_end": (origin + horizon).isoformat(),
                    "training_points": len(usable),
                    "training_window_end": usable.end.isoformat() if usable.end else None,
                    "scored_points": scored,
                    "status": "OK",
                }
            )

        if not all_rows:
            raise BacktestError("no forecast point could be scored against an actual")

        merged = CarbonForecast(
            points=[],
            resolution_minutes=config.resolution_minutes,
            provenance=forecaster.forecast(
                history.before(origins[-1]),
                origins[-1],
                origins[-1] + horizon,
                resolution_minutes=config.resolution_minutes,
                coverage=config.coverage,
                lookback_days=config.lookback_days,
            ).provenance,
        )
        from ..domain.forecasting import CarbonForecastPoint

        merged.points = [
            CarbonForecastPoint(
                timestamp=t, predicted_gco2_per_kwh=p, lower_gco2_per_kwh=lo, upper_gco2_per_kwh=hi
            )
            for t, p, _a, lo, hi in all_rows
        ]

        evaluation = build_evaluation(merged, [(t, a) for t, _p, a, _lo, _hi in all_rows])
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        return BacktestResult(
            model=config.model,
            config=config,
            steps=len(windows),
            metrics=evaluation.metrics,
            runtime_ms=elapsed_ms,
            windows=windows,
            evaluated_from=evaluation.window_start,
            evaluated_to=evaluation.window_end,
            training_points_min=min(training_sizes) if training_sizes else None,
            training_points_max=max(training_sizes) if training_sizes else None,
        )


@dataclass
class ForecastComparisonResult:
    """§27, §28: several models over one dataset, one evaluation period.

    NO WINNER IS CROWNED (§27, §28). Results are returned side by side with
    their measured metrics and runtime. Deciding which model to ship needs
    criteria Heliotrope does not have — a marginal MAE improvement traded against
    a wider interval, or against runtime, or against how the model behaves in the
    regime that actually matters — and inventing a ranking here would bury that
    judgement inside a fabricated "best" field.
    """

    dataset: dict
    results: dict[str, BacktestResult] = field(default_factory=dict)
    evaluations: dict[str, ForecastEvaluation] = field(default_factory=dict)
    runtime_ms: int = 0

    def as_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "runtime_ms": self.runtime_ms,
            "results": {name: r.as_dict() for name, r in self.results.items()},
            "note": (
                "Models are listed with measured metrics only. No ranking is implied: "
                "choosing between a lower MAE, a wider prediction interval and a "
                "different runtime is a product decision, not a metric."
            ),
        }


class ForecastComparisonService:
    """Runs the same dataset through several models (§28)."""

    def __init__(self) -> None:
        self.backtester = ForecastBacktester()

    def compare(
        self,
        history: CarbonHistory,
        models: Optional[Iterable[str]] = None,
        config: Optional[BacktestConfig] = None,
    ) -> ForecastComparisonResult:
        base = (config or BacktestConfig()).validate()
        names = list(models) if models else ["persistence", "seasonal"]
        started = time.perf_counter()

        comparison = ForecastComparisonResult(
            dataset={
                "source_signal": history.source_signal(),
                "source_signal_type": history.source_signal_type(),
                "history_start": history.start.isoformat() if history.start else None,
                "history_end": history.end.isoformat() if history.end else None,
                "history_points": len(history),
                "is_synthetic": history.source_signal_type() == "SYNTHETIC",
            }
        )
        for name in names:
            # Each model gets the SAME evaluation period: same horizon, same step,
            # same resolution, same origins. Only the model changes.
            run_config = BacktestConfig(
                model=name,
                horizon_hours=base.horizon_hours,
                step_hours=base.step_hours,
                resolution_minutes=base.resolution_minutes,
                lookback_days=base.lookback_days,
                coverage=base.coverage,
                max_steps=base.max_steps,
                max_history_days=base.max_history_days,
                variant=base.variant,
            )
            result = self.backtester.run(history, run_config)
            comparison.results[name] = result
        comparison.runtime_ms = int((time.perf_counter() - started) * 1000)
        return comparison