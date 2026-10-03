"""Forecasters (Phase 5, §3, §4, §5, §6, §7, §10, §29, §30).

    history -> CarbonForecaster.forecast(history, horizon, resolution) -> CarbonForecast

TWO TRANSPARENT BASELINES, NO NEURAL NETWORK (§4, §49). A persistence model
(«tomorrow looks like the most recent relevant observation») and a seasonal
model («this time of day, averaged over recent days»). They are the right first
step for this problem because electricity carbon intensity has a strong, stable
daily shape — the solar midday valley and the evening peak repeat every day — and
a seasonal average captures that with arithmetic a person can check by hand.

WHAT THIS IS NOT. These are not claimed to predict grid dynamics, weather-driven
renewable curtailment, or outages. They have no view of the generation mix, no
weather feed, and no net load. Their measured errors are in
`ForecastEvaluator`, and those numbers are the honest description of their skill.

WHY UNCERTAINTY IS EMPIRICAL AND NOT ASSUMED (§10). The interval comes from the
model's OWN past residuals: run the model over the recent history, measure how
far off it was, and take a quantile of those absolute errors. This needs no
assumption about error distribution, and it degrades honestly — if the model was
bad yesterday, today's interval is wide. The cost is that the interval is only
as good as the residual window, and it says nothing about regime changes the
history never contained. `CoverageReport` exists so that claim can be checked
rather than trusted.

LEAKAGE IS STRUCTURAL, NOT A CONVENTION (§25). `forecast()` accepts history
strictly earlier than the forecast origin and the residual window is built from
the same rule. There is no code path in which a future actual value can reach a
prediction, because the only way in is `history`, and the backtester enforces
the ordering.
"""

from __future__ import annotations

import math
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from ..domain.carbon import CarbonPoint, Quality, SignalType
from ..domain.forecasting import (
    CarbonForecast,
    CarbonForecastPoint,
    ForecastModel,
    ForecastProvenance,
)
from ..utils.time import to_utc


class ForecastError(ValueError):
    """The forecast request cannot be served as asked."""


#: A time-of-day bucket needs at least this many residuals before its own
#: quantile is used. Below it, the pooled quantile is used instead — a 2-sample
#: quantile is arithmetic, not evidence.
MIN_RESIDUALS_PER_BUCKET = 4


# --- history -----------------------------------------------------------------


@dataclass(frozen=True)
class CarbonHistory:
    """Observed carbon intensity, strictly time-ordered.

    `points` MUST be sorted ascending and MUST contain no duplicate timestamps.
    Both invariants are checked in `__post_init__` rather than trusted, because
    a silently unsorted history makes a seasonal forecaster quietly use the
    wrong day's value while still returning a plausible-looking number.
    """

    points: tuple[CarbonPoint, ...]

    def __post_init__(self) -> None:
        previous: Optional[datetime] = None
        for point in self.points:
            if previous is not None:
                if point.time == previous:
                    raise ForecastError(
                        f"history has two observations at {point.time.isoformat()}; "
                        f"a duplicated timestamp makes 'the same time yesterday' ambiguous"
                    )
                if point.time < previous:
                    raise ForecastError(
                        f"history is not time-ordered: {point.time.isoformat()} follows "
                        f"{previous.isoformat()}. Sorting silently would hide a bug in "
                        f"whatever produced the history."
                    )
            previous = point.time

    def __len__(self) -> int:
        return len(self.points)

    @property
    def start(self) -> Optional[datetime]:
        return self.points[0].time if self.points else None

    @property
    def end(self) -> Optional[datetime]:
        return self.points[-1].time if self.points else None

    def before(self, moment: datetime) -> "CarbonHistory":
        """Everything strictly earlier than `moment` (§25, §26).

        This is the ONLY way a forecaster narrows its window, which is what makes
        leakage structurally impossible rather than a rule everyone must
        remember.
        """
        return CarbonHistory(tuple(p for p in self.points if p.time < moment))

    def by_time_of_day(self) -> dict[tuple[int, int], list[float]]:
        """Mean intensity per (hour, minute) of day across the whole history.

        This is the seasonal profile. Averaging over days is exactly the
        assumption a seasonal model makes: the daily shape is roughly stable and
        the noise averages out.
        """
        buckets: dict[tuple[int, int], list[float]] = {}
        for point in self.points:
            key = (point.time.hour, point.time.minute)
            buckets.setdefault(key, []).append(point.gco2_per_kwh)
        return {k: sum(v) / len(v) for k, v in buckets.items()}

    def source_signal(self) -> str:
        return self.points[0].source if self.points else "unknown"

    def source_signal_type(self) -> str:
        if not self.points:
            return SignalType.SYNTHETIC.value
        return getattr(self.points[0].signal_type, "value", str(self.points[0].signal_type))


# --- uncertainty (§9, §10) ---------------------------------------------------


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile of a non-empty sample.

    `q` is a probability in [0, 1]. Uses the standard "sort, then index between
    the two neighbours" definition so that q=0.9 on ten samples is not silently
    the ninth or the tenth.
    """
    if not values:
        raise ForecastError("cannot take a quantile of an empty sample")
    if not 0.0 <= q <= 1.0:
        raise ForecastError(f"quantile probability must be in [0, 1]; got {q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def absolute_errors(predicted: Sequence[float], actual: Sequence[float]) -> list[float]:
    if len(predicted) != len(actual):
        raise ForecastError(
            f"cannot measure error of {len(predicted)} predictions against "
            f"{len(actual)} actuals"
        )
    return [abs(p - a) for p, a in zip(predicted, actual)]


def signed_errors(predicted: Sequence[float], actual: Sequence[float]) -> list[float]:
    """actual - predicted. Positive means the model UNDER-forecasted."""
    if len(predicted) != len(actual):
        raise ForecastError(
            f"cannot measure error of {len(predicted)} predictions against "
            f"{len(actual)} actuals"
        )
    return [a - p for p, a in zip(predicted, actual)]


# --- the interface (§3) ------------------------------------------------------


class CarbonForecaster(ABC):
    """The one forecasting interface.

    Subclasses implement `point_predictions`: a list of `(timestamp, gCO2/kWh)`
    for the requested horizon, computed from history that ends strictly before
    the first predicted timestamp. Interval construction, provenance, resolution
    checks and leakage guards live in the base class so every model inherits the
    same guarantees.
    """

    model: ForecastModel
    description: str = ""

    @abstractmethod
    def point_predictions(
        self,
        history: CarbonHistory,
        start: datetime,
        end: datetime,
        resolution_minutes: int,
    ) -> list[tuple[datetime, float]]:
        """Predicted intensity per slot in `[start, end)`.

        MUST use only observations strictly before `start`.
        """

    # --- shared machinery ---------------------------------------------------

    def forecast(
        self,
        history: CarbonHistory,
        start: datetime,
        end: datetime,
        resolution_minutes: int = 15,
        coverage: float = 0.9,
        lookback_days: int = 30,
    ) -> CarbonForecast:
        start, end = to_utc(start), to_utc(end)
        if end <= start:
            raise ForecastError("forecast end must be after start")
        if resolution_minutes <= 0:
            raise ForecastError("resolution_minutes must be positive")
        if not 0.0 < coverage < 1.0:
            raise ForecastError(
                f"interval coverage must be a probability strictly between 0 and 1; "
                f"got {coverage}"
            )

        # §25: the guard. Everything the model may see ends before the horizon.
        usable = history.before(start)
        if not len(usable):
            raise ForecastError(
                f"no carbon history exists before {start.isoformat()}, so nothing can "
                f"be forecast for that window. A forecast built from future data "
                f"would be leakage, so this is refused rather than fabricated."
            )

        predictions = self.point_predictions(usable, start, end, resolution_minutes)
        expected_slots = int((end - start).total_seconds() // (resolution_minutes * 60))
        if len(predictions) != expected_slots:
            raise ForecastError(
                f"{self.model.value} returned {len(predictions)} predictions for a "
                f"window of {expected_slots} slots at {resolution_minutes} min"
            )

        interval = self.residual_interval(
            usable, start, end, resolution_minutes, coverage, lookback_days
        )
        # Per-slot half-widths. Clamped at zero on the low side because carbon
        # intensity is non-negative and a negative lower bound is not a number
        # the scheduler could use.
        #
        # These VARY BY SLOT, and that is the whole point. An earlier version used
        # one global quantile for every slot, which produced an identical width
        # everywhere. Adding a constant to every slot cannot change which slot is
        # cheapest, so ROBUST mode silently degenerated into EXPECTED — it looked
        # like it was working because the mode was accepted and the numbers moved,
        # but no schedule ever changed because of it. Uncertainty has to be
        # time-of-day dependent to mean anything operationally: a forecast of a
        # volatile evening peak deserves a wider band than a forecast of a flat
        # night.
        points = []
        for (timestamp, value), half_width in zip(predictions, interval["half_widths"]):
            points.append(
                CarbonForecastPoint(
                    timestamp=timestamp,
                    predicted_gco2_per_kwh=value,
                    lower_gco2_per_kwh=max(0.0, value - half_width),
                    upper_gco2_per_kwh=value + half_width,
                )
            )
        return CarbonForecast(
            points=points,
            resolution_minutes=resolution_minutes,
            provenance=ForecastProvenance(
                model=self.model.value,
                model_description=self.description,
                generated_at=datetime.now(tz=start.tzinfo),
                training_window_start=usable.start,
                training_window_end=usable.end,
                training_points=len(usable),
                source_signal=history.source_signal(),
                source_signal_type=history.source_signal_type(),
                horizon_start=start,
                horizon_end=end,
                resolution_minutes=resolution_minutes,
                interval_nominal_coverage=coverage,
                uncertainty_method=interval["method"],
                configuration=self.configuration(),
            ),
        )

    def configuration(self) -> dict:
        """Model settings, recorded in the forecast's provenance."""
        return {}

    def residual_interval(
        self,
        history: CarbonHistory,
        start: datetime,
        end: datetime,
        resolution_minutes: int,
        coverage: float,
        lookback_days: int,
    ) -> dict:
        """Empirical prediction-interval width, from this model's own errors.

        The model is re-run over the recent past — always with a history that
        stops before the window being predicted — and the absolute errors of
        those retroactive predictions form the sample. The interval half-width is
        the `coverage` quantile of that sample.

        Returns per-slot half-widths plus the bookkeeping the API and tests need:
        how many residuals backed them, how many slots had to fall back to the
        pooled quantile, and what the fallback was when there were too few
        residuals at all. Too few residuals is reported, not hidden.
        """
        cutoff = start - timedelta(days=lookback_days)
        recent = history.before(start)
        if recent.start is not None:
            window = CarbonHistory(tuple(p for p in recent.points if p.time >= cutoff))
        else:
            window = recent

        residuals: list[float] = []
        # residuals bucketed by (hour, minute) so the interval can vary across the
        # day instead of adding the same constant to every slot
        by_time_of_day: dict[tuple[int, int], list[float]] = {}
        # Walk backward over the window, re-forecasting each day with only the
        # history available before it. Every one of these calls also passes a
        # window ending before the day being predicted, so the residuals are
        # themselves leakage-free.
        if len(window) >= 2:
            day = timedelta(days=1)
            cursor = start - day
            guard = 0
            while cursor >= window.start and guard < 31:
                guard += 1
                day_start = cursor
                day_end = cursor + day
                usable = history.before(day_start)
                if len(usable) >= 2 and any(p.time < day_start for p in usable.points):
                    try:
                        retro = self.point_predictions(
                            usable, day_start, day_end, resolution_minutes
                        )
                    except ForecastError:
                        retro = []
                    if retro:
                        actual = {p.time: p.gco2_per_kwh for p in history.points}
                        pairs = [
                            (t, value, actual[t])
                            for t, value in retro
                            if t in actual and t < start
                        ]
                        if pairs:
                            errors = absolute_errors(
                                [v for _t, v, _a in pairs], [a for _t, _v, a in pairs]
                            )
                            residuals.extend(errors)
                            for (moment, _v, _a), error in zip(pairs, errors):
                                by_time_of_day.setdefault(
                                    (moment.hour, moment.minute), []
                                ).append(error)
                cursor -= day

        step = timedelta(minutes=resolution_minutes)
        slot_times = [
            moment for moment in _slots(start, end, resolution_minutes)
        ]

        if residuals:
            # Per-slot quantile where that time of day has enough residuals,
            # otherwise the pooled quantile. A 2-residual bucket would give an
            # arbitrary width; the pooled value is at least a defensible one, and
            # the count is reported so the caller knows which was used.
            pooled = quantile(residuals, coverage)
            minimum_bucket = MIN_RESIDUALS_PER_BUCKET
            half_widths: list[float] = []
            reused = 0
            for moment in slot_times:
                bucket = by_time_of_day.get((moment.hour, moment.minute))
                if bucket and len(bucket) >= minimum_bucket:
                    half_widths.append(quantile(bucket, coverage))
                else:
                    half_widths.append(pooled)
                    reused += 1
            return {
                "half_widths": half_widths,
                "quantile": pooled,
                "residual_count": len(residuals),
                "slots_using_pooled_quantile": reused,
                "method": (
                    f"empirical residual quantile by time of day "
                    f"(>= {minimum_bucket} residuals per bucket, else the pooled "
                    f"quantile), coverage {coverage}"
                ),
                "fallback": None,
            }

        # No usable residuals. The honest fallback is the standard deviation of
        # the history itself, which is a statement about the data's spread, not
        # about the model's skill — and `fallback` says so.
        values = [p.gco2_per_kwh for p in window.points]
        spread = 0.0
        if len(values) >= 2:
            mean = sum(values) / len(values)
            spread = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
        return {
            "half_widths": [spread] * len(slot_times),
            "quantile": spread,
            "residual_count": 0,
            "slots_using_pooled_quantile": len(slot_times),
            "method": "standard deviation of the observed history (no residuals available)",
            "fallback": (
                "no model residuals could be computed (history shorter than one day); "
                "the interval width falls back to the standard deviation of the "
                "observed history, which describes the data's spread, not this "
                "model's skill"
            ),
        }


# --- baselines (§5, §6) ------------------------------------------------------


class PersistenceForecaster(CarbonForecaster):
    """§5: the future repeats the most recent observation.

    DEFAULT VARIANT: `last_observation` — every predicted slot equals the most
    recent value in the history. This is the simplest honest baseline, and it is
    the right control for the seasonal model: a seasonal model that cannot beat
    "the last thing I saw" has not learned anything about the daily cycle.

    Other variants exist so the comparison in §27 is against a chosen, stated
    baseline rather than a vague one:

        last_observation  — most recent value, held flat (default)
        last_day_same_time — the value exactly 24 h earlier, when present
        moving_average    — mean of the last N observations

    Persistence is genuinely hard to beat on short horizons and genuinely weak
    across a day boundary, which is exactly the trade-off the seasonal model
    exists to fix.
    """

    model = ForecastModel.PERSISTENCE
    description = (
        "Persistence baseline: holds the most recent observed carbon intensity "
        "across the forecast horizon. A control model, not a prediction of the grid."
    )

    #: §5
    VARIANTS = ("last_observation", "last_day_same_time", "moving_average")

    def __init__(
        self,
        variant: str = "last_observation",
        window: int = 4,
    ) -> None:
        if variant not in self.VARIANTS:
            raise ForecastError(
                f"unknown persistence variant {variant!r}; expected one of {self.VARIANTS}"
            )
        if window < 1:
            raise ForecastError("moving-average window must be >= 1")
        self.variant = variant
        self.window = window

    def configuration(self) -> dict:
        return {"variant": self.variant, "window": self.window}

    def point_predictions(
        self,
        history: CarbonHistory,
        start: datetime,
        end: datetime,
        resolution_minutes: int,
    ) -> list[tuple[datetime, float]]:
        if not len(history):
            raise ForecastError("persistence needs at least one observation")

        if self.variant == "last_observation":
            value = history.points[-1].gco2_per_kwh
            return [(t, value) for t in _slots(start, end, resolution_minutes)]

        if self.variant == "moving_average":
            recent = history.points[-self.window :]
            value = sum(p.gco2_per_kwh for p in recent) / len(recent)
            return [(t, value) for t in _slots(start, end, resolution_minutes)]

        # last_day_same_time
        by_time = {p.time: p.gco2_per_kwh for p in history.points}
        step = timedelta(minutes=resolution_minutes)
        predictions: list[tuple[datetime, float]] = []
        current = start
        while current < end:
            source = by_time.get(current - timedelta(days=1))
            if source is None:
                # No observation exactly 24 h back. Falling back to the most
                # recent observation is stated behaviour, not a silent guess.
                source = history.points[-1].gco2_per_kwh
            predictions.append((current, source))
            current += step
        return predictions


class SeasonalForecaster(CarbonForecaster):
    """§6: today's shape, learned from recent days.

    The predicted intensity at 18:15 tomorrow is the mean observed intensity at
    18:15 over the recent history. That is the whole model, and it works because
    the daily shape of carbon intensity is the strongest and most repeatable
    signal in this data.

    `lookback_days` bounds how far back the profile is averaged. Shorter is
    more reactive to a recent change in the grid mix; longer is smoother and
    less able to notice one. It is configurable precisely because neither is
    right in general, and pretending otherwise would be a claim without
    evidence.

    NOT CLAIMED: this does not capture the grid. It has no generation mix, no
    weather, no net load. Its errors are measured in `ForecastEvaluator`.
    """

    model = ForecastModel.SEASONAL
    description = (
        "Seasonal baseline: for each slot, the mean observed intensity at the same "
        "time of day over the recent history window. Exploits the daily shape of "
        "carbon intensity; has no view of generation mix, weather or net load."
    )

    def __init__(self, lookback_days: int = 14) -> None:
        if lookback_days < 1:
            raise ForecastError("lookback_days must be >= 1")
        self.lookback_days = lookback_days

    def configuration(self) -> dict:
        return {"lookback_days": self.lookback_days}

    def point_predictions(
        self,
        history: CarbonHistory,
        start: datetime,
        end: datetime,
        resolution_minutes: int,
    ) -> list[tuple[datetime, float]]:
        cutoff = start - timedelta(days=self.lookback_days)
        window = CarbonHistory(tuple(p for p in history.points if p.time >= cutoff))
        if not len(window):
            window = history
        profile = window.by_time_of_day()
        if not profile:
            raise ForecastError("seasonal model has no time-of-day profile to use")

        overall = sum(p.gco2_per_kwh for p in window.points) / len(window.points)
        predictions: list[tuple[datetime, float]] = []
        for moment in _slots(start, end, resolution_minutes):
            key = (moment.hour, moment.minute)
            # A time of day never seen in the history (possible when the
            # resolution or the window boundaries do not line up) falls back to
            # the window mean, which is at least the right ballpark.
            predictions.append((moment, profile.get(key, overall)))
        return predictions


# --- future-model boundaries (§29, §30) --------------------------------------


class MLCarbonForecaster(CarbonForecaster):
    """§29: the seam a learned model plugs into, with nothing behind it yet.

    This class is intentionally NOT a working model. It exists so that adding
    XGBoost, LightGBM or a temporal network later is a matter of writing
    `point_predictions` and registering the name — not of restructuring the
    scheduler, the API or the evaluation harness.

    Raising here is the correct behaviour. A stub that returned a plausible
    number would be the single easiest way for this codebase to start making
    accuracy claims nobody measured (§50).
    """

    model = ForecastModel.PERSISTENCE  # unused: this model never runs
    description = "Placeholder boundary for a future learned forecaster. Not implemented."

    def point_predictions(self, history, start, end, resolution_minutes):
        raise NotImplementedError(
            "MLCarbonForecaster is a boundary, not a model. Phase 5 ships the "
            "persistence and seasonal baselines; a learned model must be "
            "implemented and backtested before it is allowed to produce numbers."
        )


class CarbonFeatureBuilder:
    """§30: the seam for feature generation, with nothing behind it yet.

    A future learned model needs lagged carbon, rolling statistics, hour-of-day,
    day-of-week, and eventually weather and net load. This boundary fixes WHERE
    that assembly happens so it cannot leak: features are built from `history`
    alone, and any feature whose value depends on the prediction time is
    computed by shifting the source series, never by looking forward.

    No external weather dependency is added (§30) and none is stubbed in.
    """

    def build(self, history: CarbonHistory, start: datetime, end: datetime) -> dict:
        raise NotImplementedError(
            "CarbonFeatureBuilder is a boundary, not an implementation. Phase 5 "
            "deliberately adds no features and no weather dependency."
        )


# --- synthetic history (§7) --------------------------------------------------


@dataclass(frozen=True)
class SyntheticHistoryConfig:
    """Knobs for the SYNTHETIC test history (§7).

    These are the defaults the Phase 5 tests and backtests run on. The numbers
    are illustrative and are labeled SYNTHETIC everywhere they surface; they are
    not any real region's grid.
    """

    baseline: float = 340.0
    solar_dip: float = 190.0
    evening_peak: float = 220.0
    morning_bump: float = 60.0
    #: standard deviation of the multiplicative daily jitter, as a fraction
    daily_spread: float = 0.06
    #: standard deviation of the per-slot noise, in g/kWh
    noise: float = 9.0
    seed: int = 11
    solar_center_hour: float = 13.0
    evening_center_hour: float = 19.0


class SyntheticCarbonHistory:
    """A deterministic multi-day SYNTHETIC history (§7).

    The curve is the same physics the Phase 2 provider already used — a solar
    midday valley and an evening peak — plus two things a forecasting test
    actually needs and a clean curve lacks:

      * a DAY-TO-DAY multiplicative spread, so "the same time yesterday" is not
        an exact answer and the seasonal model has something to be wrong about;
      * per-slot NOISE, so residuals are non-zero and prediction intervals are
        not degenerate.

    Deterministic given the seed, so every test and every backtest run sees
    exactly the same history. `seed` and every parameter are exposed: a test that
    needs a harder or easier dataset changes the config rather than the code.
    """

    #: never claim otherwise
    SIGNAL_TYPE = SignalType.SYNTHETIC
    QUALITY = Quality.SYNTHETIC
    SOURCE = "synthetic_history"

    def __init__(self, config: Optional[SyntheticHistoryConfig] = None) -> None:
        self.config = config or SyntheticHistoryConfig()

    def _gauss(self, hour: float, center: float, width: float) -> float:
        return math.exp(-((hour - center) ** 2) / width)

    def daily_factor(self, day_index: int) -> float:
        """One multiplier per day. Seeded, reproducible, not per-slot."""
        rng = random.Random(self.config.seed * 1000003 + day_index)
        return 1.0 + rng.gauss(0.0, self.config.daily_spread)

    def base_intensity(self, moment: datetime, day_index: int) -> float:
        hour = moment.hour + moment.minute / 60.0
        c = self.config
        return (
            c.baseline
            + c.evening_peak * self._gauss(hour, c.evening_center_hour, 5.5)
            + c.morning_bump * self._gauss(hour, 8.0, 6.0)
            - c.solar_dip * self._gauss(hour, c.solar_center_hour, 7.5)
        ) * self.daily_factor(day_index)

    def points(
        self, end: datetime, days: int, resolution_minutes: int = 15
    ) -> list[CarbonPoint]:
        """`days` of history ending at `end`, most recent last."""
        if days < 1:
            raise ForecastError("need at least one day of history")
        if resolution_minutes <= 0:
            raise ForecastError("resolution_minutes must be positive")
        end = to_utc(end)
        step = timedelta(minutes=resolution_minutes)
        start = end - timedelta(days=days)
        rng = random.Random(self.config.seed)
        result: list[CarbonPoint] = []
        current = start
        while current < end:
            day_index = (current.date() - start.date()).days
            value = self.base_intensity(current, day_index) + rng.gauss(0.0, self.config.noise)
            result.append(
                CarbonPoint(
                    time=current,
                    gco2_per_kwh=max(0.0, value),
                    signal_type=self.SIGNAL_TYPE,
                    quality=self.QUALITY,
                    source=self.SOURCE,
                )
            )
            current += step
        return result

    def history(self, end: datetime, days: int, resolution_minutes: int = 15) -> CarbonHistory:
        return CarbonHistory(tuple(self.points(end, days, resolution_minutes)))


def _slots(start: datetime, end: datetime, resolution_minutes: int) -> list[datetime]:
    step = timedelta(minutes=resolution_minutes)
    result: list[datetime] = []
    current = start
    while current < end:
        result.append(current)
        current += step
    return result


# --- registry ----------------------------------------------------------------


def build_forecaster(
    model: str, lookback_days: int = 14, variant: str = "last_observation"
) -> CarbonForecaster:
    """Resolve a model name to an instance (§8, §34-style: never substitute).

    An unknown name raises rather than quietly returning the seasonal model. A
    request that said "persistence" and got "seasonal" would report metrics for a
    model the caller did not ask to evaluate, and every comparison downstream
    would be quietly wrong.
    """
    try:
        name = ForecastModel(model.strip().lower())
    except (AttributeError, ValueError):
        valid = ", ".join(m.value for m in ForecastModel)
        raise ForecastError(f"unknown forecast model {model!r}; expected one of: {valid}")
    if name is ForecastModel.PERSISTENCE:
        return PersistenceForecaster(variant=variant)
    return SeasonalForecaster(lookback_days=lookback_days)
