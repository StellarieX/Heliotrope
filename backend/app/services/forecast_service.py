"""ForecastService — the facade the API talks to (Phase 5, §8, §13, §40).

One place that turns "a window plus a model name" into a `CarbonForecast`,
supplies the history those forecasts are built from, and runs the three
evaluation entry points. Routes stay thin and this stays testable without HTTP.

WHERE HISTORY COMES FROM, in descending order of honesty:

    history_points     — the caller supplies the observed series. Used by the
                         backtest and evaluation endpoints, where the data is
                         the point of the request.
    CarbonService      — the configured provider is queried for the days BEFORE
                         the forecast window. The observed signal that already
                         exists in the system, used by the forecast endpoint.
    SyntheticCarbonHistory — a deterministic SYNTHETIC series, used only when no
                         real history can be obtained. Always labeled SYNTHETIC
                         in the provenance, so a forecast built this way can
                         never be mistaken for one built from grid data (§7).

The synthetic fallback is the reason `/carbon/forecast` works at all in a fresh
checkout. It is disclosed rather than hidden: `provenance.source_signal` says
`synthetic_history` and `source_signal_type` says `SYNTHETIC`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from ..domain.carbon import CarbonSignal
from ..domain.forecasting import CarbonForecast
from ..utils.time import to_utc
from .carbon_service import CarbonService, CarbonUnavailable
from .forecast_backtest import (
    BacktestConfig,
    BacktestError,
    ForecastComparisonService,
    ForecastComparisonResult,
)
from .forecast_evaluator import ForecastEvaluation, ForecastEvaluationError, build_evaluation
from .forecasting import (
    CarbonHistory,
    ForecastError,
    SyntheticCarbonHistory,
    build_forecaster,
)


class ForecastServiceError(ValueError):
    """The forecast request cannot be served as asked."""


class ForecastHistoryTooShort(ForecastServiceError):
    """There is not enough history before the window to forecast it."""


class ForecastService:
    """Forecasting, evaluation and backtesting behind one interface."""

    #: default window of observed history pulled from the provider, in days
    DEFAULT_HISTORY_DAYS = 14
    #: hard cap on a caller-supplied history, in days (§40)
    MAX_HISTORY_DAYS = 60
    #: hard cap on a single forecast request, in days
    MAX_FORECAST_DAYS = 7

    def __init__(
        self,
        carbon_service: Optional[CarbonService] = None,
        synthetic: Optional[SyntheticCarbonHistory] = None,
    ) -> None:
        self._carbon = carbon_service
        self._synthetic = synthetic or SyntheticCarbonHistory()
        self._backtester = ForecastComparisonService()

    # --- history ------------------------------------------------------------

    def history_from_points(
        self,
        points: Iterable[tuple[datetime | str, float]],
        source: str = "caller",
    ) -> CarbonHistory:
        """Build a history from caller-supplied `(timestamp, gCO2/kWh)` pairs.

        Timestamps may be `datetime` objects or ISO-8601 strings, because the API
        receives JSON. A naive string is refused: a bare timestamp would be read
        against the server's local zone, which silently shifts a whole forecast.

        Ordering and duplicates are checked by `CarbonHistory` itself. Points
        arriving out of order are an error, not something to silently sort: an
        unsorted series means whatever produced it is buggy, and sorting would
        hide that behind a plausible-looking forecast.
        """
        from ..domain.carbon import CarbonPoint, Quality, SignalType

        parsed = []
        for moment, value in points:
            if isinstance(moment, str):
                try:
                    moment = datetime.fromisoformat(moment)
                except ValueError as exc:
                    raise ForecastServiceError(
                        f"history timestamp is not ISO-8601: {moment!r}"
                    ) from exc
            if getattr(moment, "tzinfo", None) is None:
                raise ForecastServiceError(
                    f"history timestamp {moment!r} is timezone-aware-required; a naive "
                    f"timestamp would be read against the server's local zone and "
                    f"silently shift the forecast"
                )
            parsed.append((moment, value))

        return CarbonHistory(
            tuple(
                CarbonPoint(
                    time=to_utc(moment),
                    gco2_per_kwh=float(value),
                    signal_type=SignalType.SYNTHETIC,
                    quality=Quality.SYNTHETIC,
                    source=source,
                )
                for moment, value in parsed
            )
        )

    def history_from_provider(
        self, end: datetime, days: int = DEFAULT_HISTORY_DAYS, resolution_minutes: int = 15
    ) -> CarbonHistory:
        """Observed history from the configured carbon provider."""
        if days < 1 or days > self.MAX_HISTORY_DAYS:
            raise ForecastServiceError(
                f"history window must be between 1 and {self.MAX_HISTORY_DAYS} days; "
                f"got {days}"
            )
        end = to_utc(end)
        start = end - timedelta(days=days)
        service = self._carbon or CarbonService.default()
        response = service.get_signal(start, end, resolution_minutes)
        return CarbonHistory(
            tuple(
                type(p)(
                    time=p.timestamp,
                    gco2_per_kwh=p.carbon_intensity_gco2_per_kwh,
                    source=response.source,
                )
                for p in response.points
            )
        )

    def resolve_history(
        self,
        end: datetime,
        days: int = DEFAULT_HISTORY_DAYS,
        resolution_minutes: int = 15,
    ) -> CarbonHistory:
        """Provider history if obtainable, otherwise the SYNTHETIC fallback."""
        try:
            history = self.history_from_provider(end, days, resolution_minutes)
            if len(history) >= 2:
                return history
        except (CarbonUnavailable, ForecastServiceError, ValueError):
            pass
        return self._synthetic.history(end, days, resolution_minutes)

    # --- forecast (§8) -------------------------------------------------------

    def forecast(
        self,
        start: datetime,
        end: datetime,
        resolution_minutes: int = 15,
        model: str = "seasonal",
        lookback_days: int = 14,
        coverage: float = 0.9,
        history: Optional[CarbonHistory] = None,
        history_days: int = DEFAULT_HISTORY_DAYS,
    ) -> CarbonForecast:
        start, end = to_utc(start), to_utc(end)
        span_days = (end - start).total_seconds() / 86400.0
        if span_days <= 0:
            raise ForecastServiceError("forecast end must be after start")
        if span_days > self.MAX_FORECAST_DAYS:
            raise ForecastServiceError(
                f"a single forecast may cover at most {self.MAX_FORECAST_DAYS} days; "
                f"asked for {span_days:.2f}"
            )
        if resolution_minutes <= 0 or resolution_minutes > 60:
            raise ForecastServiceError("resolution_minutes must be between 1 and 60")

        usable = history if history is not None else self.resolve_history(
            start, history_days, resolution_minutes
        )
        try:
            forecaster = build_forecaster(model, lookback_days=lookback_days)
            return forecaster.forecast(
                usable,
                start,
                end,
                resolution_minutes=resolution_minutes,
                coverage=coverage,
            )
        except ForecastError as exc:
            raise ForecastServiceError(str(exc)) from exc

    # --- evaluate (§13) ------------------------------------------------------

    def evaluate(
        self,
        forecast: CarbonForecast,
        actual: Iterable[tuple[datetime, float]],
    ) -> ForecastEvaluation:
        try:
            return build_evaluation(forecast, actual)
        except ForecastEvaluationError as exc:
            raise ForecastServiceError(str(exc)) from exc

    # --- backtest (§24, §40) -------------------------------------------------

    def backtest(
        self,
        history: CarbonHistory,
        config: Optional[BacktestConfig] = None,
    ):
        try:
            return self._backtester.backtester.run(history, config)
        except BacktestError as exc:
            raise ForecastServiceError(str(exc)) from exc

    def compare_models(
        self,
        history: CarbonHistory,
        models: Optional[Iterable[str]] = None,
        config: Optional[BacktestConfig] = None,
    ) -> ForecastComparisonResult:
        try:
            return self._backtester.compare(history, models=models, config=config)
        except BacktestError as exc:
            raise ForecastServiceError(str(exc)) from exc

    # --- forecast-relative scheduling input (§38) ----------------------------

    def uncertainty_over_horizon(
        self, forecast: CarbonForecast, horizon_start: datetime
    ) -> list[int]:
        """Per-slot upper prediction bounds aligned to `horizon_start`.

        The forecast's own resolution grid is matched against the horizon grid.
        A horizon slot with no forecast point raises rather than borrowing a
        neighbouring value — the normalizer refuses to invent carbon, and this
        is the same rule one layer earlier.
        """
        from ..domain.scaling import to_carbon_int

        by_time = {p.timestamp: p for p in forecast.points}
        step = timedelta(minutes=forecast.resolution_minutes)
        upper: list[int] = []
        current = to_utc(horizon_start)
        while current < forecast.provenance.horizon_end:
            point = by_time.get(current)
            if point is None:
                raise ForecastServiceError(
                    f"the forecast has no point at {current.isoformat()}, so there is no "
                    f"uncertainty for that slot. Refusing to borrow a neighbouring value."
                )
            upper.append(to_carbon_int(point.upper_gco2_per_kwh))
            current += step
        return upper

    def predicted_over_horizon(
        self, forecast: CarbonForecast, horizon_start: datetime
    ) -> list[int]:
        """Per-slot point forecasts aligned to `horizon_start` (§38, EXPECTED)."""
        from ..domain.scaling import to_carbon_int

        by_time = {p.timestamp: p for p in forecast.points}
        step = timedelta(minutes=forecast.resolution_minutes)
        values: list[int] = []
        current = to_utc(horizon_start)
        while current < forecast.provenance.horizon_end:
            point = by_time.get(current)
            if point is None:
                raise ForecastServiceError(
                    f"the forecast has no point at {current.isoformat()}."
                )
            values.append(to_carbon_int(point.predicted_gco2_per_kwh))
            current += step
        return values

    def as_carbon_signal(
        self, forecast: CarbonForecast, values: Optional[list[float]] = None
    ) -> CarbonSignal:
        """Turn a forecast into a `CarbonSignal` the scheduler can consume.

        Used with the point forecasts for EXPECTED mode. `is_forecast` is set and
        `signal_type` is FORECAST, so a schedule built from this carries its own
        provenance and can never be reported as if it had seen real grid data.
        """
        from ..domain.carbon import CarbonPoint, Quality, SignalType

        series = values if values is not None else [
            p.predicted_gco2_per_kwh for p in forecast.points
        ]
        return CarbonSignal(
            start=forecast.provenance.horizon_start,
            end=forecast.provenance.horizon_end,
            resolution_minutes=forecast.resolution_minutes,
            source=f"forecast:{forecast.provenance.model}",
            points=[
                CarbonPoint(
                    time=p.timestamp,
                    gco2_per_kwh=value,
                    signal_type=SignalType.SYNTHETIC,
                    is_forecast=True,
                    quality=Quality.ESTIMATED,
                    source=f"forecast:{forecast.provenance.model}",
                )
                for p, value in zip(forecast.points, series)
            ],
        )