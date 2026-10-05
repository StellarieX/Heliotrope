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

import bisect
import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

from ..domain.carbon import CarbonPoint, CarbonSignal, Quality, SignalType
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


log = logging.getLogger("heliotrope.forecast")


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
        points: Iterable,
        source: str = "caller",
        signal_type: Optional[str | SignalType] = None,
        quality: Optional[str | Quality] = None,
    ) -> CarbonHistory:
        """Build a history from caller-supplied observations.

        Timestamps may be `datetime` objects or ISO-8601 strings, because the API
        receives JSON. A naive string is refused: a bare timestamp would be read
        against the server's local zone, which silently shifts a whole forecast.

        Ordering and duplicates are checked by `CarbonHistory` itself. Points
        arriving out of order are an error, not something to silently sort: an
        unsorted series means whatever produced it is buggy, and sorting would
        hide that behind a plausible-looking forecast.

        Each item may be a `(timestamp, value)` pair, a `CarbonPoint`, or a
        mapping with `timestamp`/`time` and `gco2_per_kwh`/`value` keys plus
        optional `source`, `signal_type` and `quality` entries. Labels the
        caller supplies travel with the point: caller-measured data is never
        relabelled SYNTHETIC. Bare pairs carry no provenance of their own, so
        they take the call-level `source`/`signal_type`/`quality`, which default
        to the historical SYNTHETIC labelling for backward compatibility —
        pass `quality="MEASURED"` (and the observed `signal_type`) when the
        pairs are real grid observations. Every point is constructed as an
        explicit `CarbonPoint`, never by re-invoking the input's own type.
        """
        default_type = (
            SignalType(signal_type) if signal_type is not None else SignalType.SYNTHETIC
        )
        default_quality = (
            Quality(quality) if quality is not None else Quality.SYNTHETIC
        )

        built: list[CarbonPoint] = []
        for item in points:
            if isinstance(item, CarbonPoint):
                moment, value = item.time, item.gco2_per_kwh
                point_type, point_quality, point_source = (
                    item.signal_type,
                    item.quality,
                    item.source,
                )
            elif isinstance(item, dict):
                raw_moment = item.get("timestamp", item.get("time"))
                if raw_moment is None:
                    raise ForecastServiceError(
                        "history mapping has neither 'timestamp' nor 'time'"
                    )
                if "gco2_per_kwh" in item:
                    value = item["gco2_per_kwh"]
                elif "value" in item:
                    value = item["value"]
                else:
                    raise ForecastServiceError(
                        "history mapping has neither 'gco2_per_kwh' nor 'value'"
                    )
                moment = self._parse_moment(raw_moment)
                raw_type = item.get("signal_type", signal_type)
                raw_quality = item.get("quality", quality)
                point_type = (
                    SignalType(raw_type)
                    if raw_type is not None
                    else default_type
                )
                point_quality = (
                    Quality(raw_quality)
                    if raw_quality is not None
                    else default_quality
                )
                point_source = item.get("source", source)
                built.append(
                    CarbonPoint(
                        time=to_utc(moment),
                        gco2_per_kwh=float(value),
                        signal_type=point_type,
                        quality=point_quality,
                        source=point_source,
                    )
                )
                continue
            else:
                moment, value = item
                point_type, point_quality, point_source = (
                    default_type,
                    default_quality,
                    source,
                )
            moment = self._parse_moment(moment)
            built.append(
                CarbonPoint(
                    time=to_utc(moment),
                    gco2_per_kwh=float(value),
                    signal_type=point_type,
                    quality=point_quality,
                    source=point_source,
                )
            )

        return CarbonHistory(tuple(built))

    @staticmethod
    def _parse_moment(moment: datetime | str) -> datetime:
        """An ISO-8601 string or datetime, always timezone-aware."""
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
        return moment

    def history_from_provider(
        self, end: datetime, days: int = DEFAULT_HISTORY_DAYS, resolution_minutes: int = 15
    ) -> CarbonHistory:
        """Observed history from the configured carbon provider.

        Every point is constructed as an explicit `CarbonPoint`: the provider
        response shape (`CarbonPointOut` with `timestamp` /
        `carbon_intensity_gco2_per_kwh`) is never the history shape (`time` /
        `gco2_per_kwh`), and rebuilding one via the other's constructor is what
        made this crash. The response's own `signal_type` and `source` travel
        with each point; quality is MEASURED for observed signals and SYNTHETIC
        only when the provider itself says the series is synthetic.
        """
        if days < 1 or days > self.MAX_HISTORY_DAYS:
            raise ForecastServiceError(
                f"history window must be between 1 and {self.MAX_HISTORY_DAYS} days; "
                f"got {days}"
            )
        end = to_utc(end)
        start = end - timedelta(days=days)
        service = self._carbon or CarbonService.default()
        # The carbon service caps a single query (CARBON_MAX_RANGE_DAYS). A longer
        # history is read as consecutive windows, otherwise every default request
        # (14 days) would exceed the cap and silently train on SYNTHETIC history.
        cap_days = getattr(service, "max_range_days", None)
        if isinstance(cap_days, int) and cap_days >= 1 and days > cap_days:
            responses = []
            cursor = start
            while cursor < end:
                nxt = min(cursor + timedelta(days=cap_days), end)
                responses.append(service.get_signal(cursor, nxt, resolution_minutes))
                cursor = nxt
        else:
            responses = [service.get_signal(start, end, resolution_minutes)]
        response = responses[0]
        signal_type = response.signal_type
        kind = getattr(signal_type, "value", signal_type)
        quality = (
            Quality.SYNTHETIC
            if kind == SignalType.SYNTHETIC.value
            else Quality.ESTIMATED
            if kind == SignalType.PROXY.value
            else Quality.MEASURED
        )
        seen: set = set()
        history_points = []
        for resp in responses:
            for p in resp.points:
                t = to_utc(p.timestamp)
                if t in seen:  # window boundaries can share one slot
                    continue
                seen.add(t)
                history_points.append(
                    CarbonPoint(
                        time=t,
                        gco2_per_kwh=float(p.carbon_intensity_gco2_per_kwh),
                        signal_type=signal_type,
                        quality=quality,
                        source=response.source,
                        is_forecast=bool(resp.quality.is_forecast),
                    )
                )
        return CarbonHistory(tuple(history_points))

    def resolve_history(
        self,
        end: datetime,
        days: int = DEFAULT_HISTORY_DAYS,
        resolution_minutes: int = 15,
    ) -> CarbonHistory:
        """Provider history if obtainable, otherwise the SYNTHETIC fallback.

        The fallback is kept — it is what makes forecasting work in a fresh
        checkout — but it is never silent: a warning names the cause, and the
        returned history is the labelled `synthetic_history` series, so the
        forecast provenance built from it says SYNTHETIC and can never be
        mistaken for grid data.
        """
        try:
            history = self.history_from_provider(end, days, resolution_minutes)
            if len(history) >= 2:
                return history
            log.warning(
                "carbon provider returned only %d point(s) for %d day(s) at %d min; "
                "falling back to SYNTHETIC history (source_signal='synthetic_history')",
                len(history),
                days,
                resolution_minutes,
            )
        except (CarbonUnavailable, ForecastServiceError, ValueError) as exc:
            log.warning(
                "carbon history unavailable (%s); falling back to SYNTHETIC history "
                "(source_signal='synthetic_history')",
                exc,
            )
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
        self,
        forecast: CarbonForecast,
        horizon_start: datetime,
        resolution_minutes: Optional[int] = None,
        horizon_end: Optional[datetime] = None,
    ) -> list[int]:
        """Per-slot upper prediction bounds aligned to the horizon grid.

        The forecast series is resampled onto `[horizon_start, horizon_end)` at
        `resolution_minutes` (defaults: the forecast's own grid). A horizon slot
        takes its exact forecast point when one exists; otherwise the nearest
        point within half a forecast step, linearly interpolated between its two
        bracketing points when straddled. The normalizer refuses to invent
        carbon, and so does this: a slot with no forecast point within
        tolerance — a horizon reaching past what was forecast — raises rather
        than borrowing a distant value.
        """
        from ..domain.scaling import to_carbon_int

        values = self._values_over_horizon(
            forecast,
            horizon_start,
            horizon_end,
            resolution_minutes,
            lambda p: p.upper_gco2_per_kwh,
        )
        return [to_carbon_int(v) for v in values]

    def predicted_over_horizon(
        self,
        forecast: CarbonForecast,
        horizon_start: datetime,
        resolution_minutes: Optional[int] = None,
        horizon_end: Optional[datetime] = None,
    ) -> list[int]:
        """Per-slot point forecasts aligned to the horizon grid (§38, EXPECTED).

        Same resampling contract as `uncertainty_over_horizon`: nearest within
        half a forecast step (linearly interpolated when straddled), strict
        error when the horizon reaches past the forecast.
        """
        from ..domain.scaling import to_carbon_int

        values = self._values_over_horizon(
            forecast,
            horizon_start,
            horizon_end,
            resolution_minutes,
            lambda p: p.predicted_gco2_per_kwh,
        )
        return [to_carbon_int(v) for v in values]

    def _values_over_horizon(
        self,
        forecast: CarbonForecast,
        horizon_start: datetime,
        horizon_end: Optional[datetime],
        resolution_minutes: Optional[int],
        select,
    ) -> list[float]:
        """Resample one forecast field onto an arbitrary horizon grid."""
        if not forecast.points:
            raise ForecastServiceError("the forecast has no points to align")
        step_minutes = resolution_minutes or forecast.resolution_minutes
        if step_minutes <= 0:
            raise ForecastServiceError("resolution_minutes must be positive")
        end = to_utc(horizon_end) if horizon_end is not None else forecast.provenance.horizon_end
        start = to_utc(horizon_start)
        if end <= start:
            raise ForecastServiceError("horizon end must be after horizon start")

        ordered = sorted(forecast.points, key=lambda p: p.timestamp)
        stamps = [p.timestamp for p in ordered]
        forecast_step = timedelta(minutes=forecast.resolution_minutes)
        tolerance = forecast_step / 2
        cover_end = forecast.provenance.horizon_end

        values: list[float] = []
        current = start
        hstep = timedelta(minutes=step_minutes)
        while current < end:
            value = self._sample_at(ordered, stamps, current, select, tolerance, cover_end)
            if value is None:
                raise ForecastServiceError(
                    f"the forecast covers "
                    f"{forecast.provenance.horizon_start.isoformat()} to "
                    f"{forecast.provenance.horizon_end.isoformat()} but the horizon "
                    f"needs {current.isoformat()}, which is beyond resampling "
                    f"tolerance. Refusing to borrow a distant value."
                )
            values.append(value)
            current += hstep
        return values

    @staticmethod
    def _sample_at(ordered, stamps, moment, select, tolerance, cover_end) -> Optional[float]:
        """Exact hit, else linear interpolation between bracketing points.

        Forecast points are interval values: the last point covers up to the
        forecast horizon end, so a finer-grid slot inside that final interval
        takes the last point's value. Returns None when `moment` lies beyond
        the forecast extent plus half a forecast step — genuinely out of
        range, not merely off-grid.
        """
        idx = bisect.bisect_left(stamps, moment)
        if idx < len(stamps) and stamps[idx] == moment:
            return float(select(ordered[idx]))
        before = ordered[idx - 1] if idx > 0 else None
        after = ordered[idx] if idx < len(ordered) else None
        if before is None:
            if after is not None and (stamps[idx] - moment) <= tolerance:
                return float(select(after))
            return None
        if after is None:
            # Inside the final interval, or a grace of half a step past it.
            if moment < cover_end or (moment - stamps[-1]) <= tolerance:
                return float(select(before))
            return None
        # Straddled: linear interpolation. Off-grid horizon slots between two
        # forecast points get the straight-line value, not a stair-step.
        span = (after.timestamp - before.timestamp).total_seconds()
        if span <= 0:
            return float(select(before))
        frac = (moment - before.timestamp).total_seconds() / span
        return float(select(before)) * (1.0 - frac) + float(select(after)) * frac

    def as_carbon_signal(
        self, forecast: CarbonForecast, values: Optional[list[float]] = None
    ) -> CarbonSignal:
        """Turn a forecast into a `CarbonSignal` the scheduler can consume.

        Used with the point forecasts for EXPECTED mode. `is_forecast` is set and
        `signal_type` is FORECAST, so a schedule built from this carries its own
        provenance and can never be reported as if it had seen real grid data.
        """
        series = values if values is not None else [
            p.predicted_gco2_per_kwh for p in forecast.points
        ]
        stamps = [p.timestamp for p in forecast.points]
        return self._signal_from_series(
            forecast,
            stamps,
            list(series),
            forecast.resolution_minutes,
            forecast.provenance.horizon_start,
            forecast.provenance.horizon_end,
        )

    def signal_over_horizon(
        self,
        forecast: CarbonForecast,
        start: datetime,
        end: datetime,
        resolution_minutes: Optional[int] = None,
    ) -> CarbonSignal:
        """A forecast `CarbonSignal` resampled onto an arbitrary horizon grid.

        The point forecasts are interpolated onto
        `[start, end)` at `resolution_minutes` (same tolerance contract as
        `uncertainty_over_horizon`), so a forecast built at one resolution can
        still be scheduled on the canonical horizon grid without the normalizer
        having to reject it for a missing slot.
        """
        step = resolution_minutes or forecast.resolution_minutes
        values = self._values_over_horizon(
            forecast, start, end, step, lambda p: p.predicted_gco2_per_kwh
        )
        stamps: list[datetime] = []
        current = to_utc(start)
        hstep = timedelta(minutes=step)
        horizon_end = to_utc(end)
        while current < horizon_end:
            stamps.append(current)
            current += hstep
        return self._signal_from_series(
            forecast, stamps, values, step, to_utc(start), to_utc(end)
        )

    @staticmethod
    def _signal_from_series(
        forecast: CarbonForecast,
        stamps: list[datetime],
        series: list[float],
        resolution_minutes: int,
        start: datetime,
        end: datetime,
    ) -> CarbonSignal:
        """Explicit `CarbonSignal` construction from one value per timestamp."""
        from ..domain.carbon import CarbonPoint, Quality, SignalType

        if len(stamps) != len(series):
            raise ForecastServiceError(
                f"cannot build a carbon signal from {len(series)} values for "
                f"{len(stamps)} timestamps"
            )
        return CarbonSignal(
            start=start,
            end=end,
            resolution_minutes=resolution_minutes,
            source=f"forecast:{forecast.provenance.model}",
            points=[
                CarbonPoint(
                    time=stamp,
                    gco2_per_kwh=value,
                    signal_type=SignalType.SYNTHETIC,
                    is_forecast=True,
                    quality=Quality.ESTIMATED,
                    source=f"forecast:{forecast.provenance.model}",
                )
                for stamp, value in zip(stamps, series)
            ],
        )