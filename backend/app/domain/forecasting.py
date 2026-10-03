"""Forecast contracts (Phase 5, §3, §9, §10, §31, §32).

    Historical carbon -> CarbonForecaster -> CarbonForecast -> robust objective

OBSERVED AND PREDICTED ARE DIFFERENT TYPES, ON PURPOSE (§32). `CarbonPoint` is a
measurement; `CarbonForecastPoint` is a claim about a future that has not
happened yet. Overloading one model with a nullable `is_forecast` flag would let
a prediction be silently summed into a measurement, and every downstream CO2
number would then be a blend of what happened and what we hoped would happen.

A FORECAST IS NOT A BOUND. `lower` and `upper` are EMPIRICAL PREDICTION
INTERVALS: they come from a quantile of past forecast errors, and the
proportion of observations that actually fell inside them is a measured number
reported alongside the forecast. It is not a guarantee, and it is deliberately
NOT called a confidence interval. Saying "90% confidence" would assert a
statistical property that has to be established and re-established whenever the
model, the horizon or the regime changes. The honest phrase is "the interval
that contained 90% of past errors at this horizon", and that is what
`interval_nominal_coverage` records — the intended quantile, not a claim.

EVERY FORECAST CARRIES PROVENANCE (§31). Model name, generation time, the
training window it was built from, the signal it was built from, the horizon and
the resolution travel with the numbers. A forecast without those is a number
from nowhere.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .jobs import _require_aware


class ForecastModel(str, Enum):
    """Registered forecaster names.

    These are TRANSPARENT BASELINES, not machine-learned models (§4, §49). They
    are named for what they actually do. Nothing here is called "AI" and no
    neural network is implied: a seasonal baseline that is honestly backtested
    is worth more than an opaque model with no measured error.
    """

    PERSISTENCE = "persistence"
    SEASONAL = "seasonal"


class ForecastMode(str, Enum):
    """Which carbon numbers the scheduler optimizes against (§16, §23).

        ACTUAL   — observed signal only. The deterministic Phase 4 behaviour,
                   unchanged. This is the default and the backward-compatible
                   path.
        EXPECTED — the point forecast. Optimize against what we think will
                   happen.
        ROBUST   — risk-adjusted: forecast + risk_weight * (upper - forecast).
                   Penalizes slots whose carbon we are LESS sure about.

    Forecast mode changes the OBJECTIVE and nothing else (§19). Release, deadline,
    energy, capacity, thermal comfort and atomicity stay deterministic hard
    constraints in all three modes. There is no mode in which a job is allowed
    to miss its deadline "because the forecast looked good".
    """

    ACTUAL = "ACTUAL"
    EXPECTED = "EXPECTED"
    ROBUST = "ROBUST"


class CarbonForecastPoint(BaseModel):
    """One predicted slot.

    Fields are deliberately explicit rather than a single value plus an error
    bar: `predicted`, `lower` and `upper` are what an operator reads, and a
    caller can check `lower <= predicted <= upper` without knowing anything about
    how the interval was derived.
    """

    timestamp: datetime
    predicted_gco2_per_kwh: float = Field(ge=0)
    lower_gco2_per_kwh: float = Field(ge=0)
    upper_gco2_per_kwh: float = Field(ge=0)

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "timestamp")

    @model_validator(mode="after")
    def _ordered(self) -> "CarbonForecastPoint":
        if not self.lower_gco2_per_kwh <= self.predicted_gco2_per_kwh <= self.upper_gco2_per_kwh:
            raise ValueError(
                f"prediction interval is not ordered: lower={self.lower_gco2_per_kwh}, "
                f"predicted={self.predicted_gco2_per_kwh}, upper={self.upper_gco2_per_kwh}"
            )
        return self

    @property
    def uncertainty_gco2_per_kwh(self) -> float:
        """One-sided width above the prediction (the ROBUST mode penalty base)."""
        return self.upper_gco2_per_kwh - self.predicted_gco2_per_kwh


class ForecastProvenance(BaseModel):
    """§31: who produced this forecast, from what, and when."""

    model: str
    model_description: str = ""
    generated_at: datetime
    #: first and last observation the model was allowed to see
    training_window_start: datetime
    training_window_end: datetime
    training_points: int = Field(ge=0)
    #: the observed signal the history came from
    source_signal: str
    source_signal_type: str
    #: the interval this was asked for
    horizon_start: datetime
    horizon_end: datetime
    resolution_minutes: int = Field(gt=0)
    #: the intended empirical quantile of the interval, e.g. 0.9. This is the
    #: QUANTILE WE ASKED FOR, not a measured coverage — see `coverage` on the
    #: evaluation result for the measured value (§12).
    interval_nominal_coverage: float = Field(default=0.9, gt=0.0, lt=1.0)
    #: how the interval was derived, in words. "empirical residual quantile".
    uncertainty_method: str = "empirical residual quantile"
    #: model hyperparameters, so a result can be reproduced
    configuration: dict = Field(default_factory=dict)

    @field_validator(
        "generated_at",
        "training_window_start",
        "training_window_end",
        "horizon_start",
        "horizon_end",
    )
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "forecast provenance timestamp")


class CarbonForecast(BaseModel):
    """A forecast for a window, with its uncertainty and its provenance."""

    points: list[CarbonForecastPoint] = Field(default_factory=list)
    resolution_minutes: int = Field(gt=0)
    provenance: ForecastProvenance
    #: always FORECAST. Kept as a string so the API response can state the
    #: signal kind explicitly rather than leaving a client to guess.
    signal_type: str = "FORECAST"

    def __len__(self) -> int:
        return len(self.points)

    @property
    def start(self) -> Optional[datetime]:
        return self.points[0].timestamp if self.points else None

    @property
    def end(self) -> Optional[datetime]:
        return self.points[-1].timestamp if self.points else None

    def at(self, moment: datetime) -> Optional[CarbonForecastPoint]:
        for point in self.points:
            if point.timestamp == moment:
                return point
        return None

    def risk_adjusted_intensity(
        self, mode: ForecastMode, risk_weight: float
    ) -> list[float]:
        """The per-slot number a scheduler should minimize against (§15, §16).

            EXPECTED -> predicted
            ROBUST   -> predicted + risk_weight * (upper - predicted)
            ACTUAL   -> not meaningful here; raises.

        `risk_weight` (lambda) is the caller's risk preference and is never
        defaulted into the data: 0.0 recovers EXPECTED exactly, and larger values
        shade every slot toward its own upper bound. At lambda = 1.0 this is the
        pure upper-bound mode of §16, which is a useful, interpretable reference
        point rather than a recommendation — optimizing the upper bound is
        conservative, not universally better, and the evaluation service exists
        precisely so that claim gets measured instead of assumed.
        """
        if mode is ForecastMode.ACTUAL:
            raise ValueError(
                "ACTUAL mode optimizes the observed signal, not a forecast; it has "
                "no risk-adjusted intensity. Use the carbon profile directly."
            )
        values: list[float] = []
        for point in self.points:
            predicted = point.predicted_gco2_per_kwh
            if mode is ForecastMode.ROBUST:
                values.append(predicted + risk_weight * point.uncertainty_gco2_per_kwh)
            else:
                values.append(predicted)
        return values

    def as_signal_points(self) -> list[tuple[datetime, float]]:
        return [(p.timestamp, p.predicted_gco2_per_kwh) for p in self.points]


class ForecastConfig(BaseModel):
    """§23: the configurable part of forecast-aware scheduling.

    Defaults reproduce Phase 4 exactly. `forecast_mode` defaults to ACTUAL, so a
    caller who says nothing about forecasting gets the deterministic scheduler
    they had before — backward compatibility is the default, not an opt-in.
    """

    forecast_mode: ForecastMode = ForecastMode.ACTUAL
    #: lambda. 0.0 means "use the point forecast"; larger means more cautious.
    risk_weight: float = Field(default=0.0, ge=0.0, le=10.0)
    #: §20: an explicit safety margin on deadlines. 15 means "finish by 06:45
    #: for a 07:00 deadline". This TIGHTENS a deadline and can never loosen one.
    deadline_buffer_minutes: int = Field(default=0, ge=0, le=24 * 60)

    @model_validator(mode="after")
    def _buffer_alignment(self) -> "ForecastConfig":
        from .horizon import SLOT_MINUTES

        if self.deadline_buffer_minutes % SLOT_MINUTES != 0:
            raise ValueError(
                f"deadline_buffer_minutes must be a whole multiple of the "
                f"{SLOT_MINUTES}-minute slot; got {self.deadline_buffer_minutes}. A "
                f"partial slot of buffer would either be silently rounded (changing a "
                f"user's stated deadline) or ignored."
            )
        return self

    def is_forecast_aware(self) -> bool:
        return self.forecast_mode is not ForecastMode.ACTUAL
