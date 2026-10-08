"""POST /api/v1/carbon/forecast{,/evaluate,/backtest} (Phase 5, §39, §40).

    GET  /api/v1/carbon                   — observed signal (Phase 2, unchanged)
    POST /api/v1/carbon/forecast          — point forecast + prediction interval
    POST /api/v1/carbon/forecast/evaluate — forecast vs actual metrics (§13)
    POST /api/v1/carbon/forecast/backtest  — rolling evaluation (§24, §40)

ERROR VOCABULARY
    400 unknown forecast model — never silently substituted for another (§28)
    422 invalid request: bad window, resolution, coverage, or missing history
    503 the configured carbon provider is unavailable

WHAT THESE ENDPOINTS DO NOT DO. They do not return a "confidence" figure, because
no confidence level has been established (§12). They return an empirical
prediction interval plus its measured coverage, and the coverage comes back from
the evaluate and backtest endpoints — not from an assumption baked into the
forecast response.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...services.forecast_backtest import BacktestConfig
from ...services.forecast_service import ForecastService, ForecastServiceError
from ...services.forecasting import ForecastError

router = APIRouter()
service = ForecastService()


def _error(exc: Exception, status: int, code: str) -> JSONResponse:
    detail = str(exc)
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "message": detail},
    )


def _parsed(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive timestamp would be interpreted "
            f"against the server's local zone, which silently shifts a forecast"
        )
    return parsed


class ForecastRequest(BaseModel):
    """§8. Note that nothing here is optional-but-defaulted into a claim: an
    omitted `model` means the seasonal baseline and says so in the response."""

    start: str
    end: str
    resolution_minutes: int = Field(default=15, ge=5, le=60)
    #: §28 — an unknown model is a 400, not a substitution
    model: str = "seasonal"
    lookback_days: int = Field(default=14, ge=1, le=60)
    #: the quantile the prediction interval should target
    coverage: float = Field(default=0.9, gt=0.0, lt=1.0)
    #: how much observed history to pull for the forecast window
    history_days: int = Field(default=14, ge=1, le=60)


class ObservedPointIn(BaseModel):
    timestamp: str
    gco2_per_kwh: float = Field(ge=0)


class EvaluateRequest(BaseModel):
    """§13: a forecast plus the actuals it should be judged against.

    `forecast` may be supplied inline (as a previously returned forecast, points
    and provenance included) or by asking the service to rebuild it for a window.
    The inline form is preferred because it guarantees the metrics describe the
    exact forecast the caller holds, not a re-run that might differ.
    """

    forecast: Optional[dict] = None
    #: rebuild path, used when `forecast` is absent
    start: Optional[str] = None
    end: Optional[str] = None
    resolution_minutes: int = Field(default=15, ge=5, le=60)
    model: str = "seasonal"
    lookback_days: int = Field(default=14, ge=1, le=60)
    coverage: float = Field(default=0.9, gt=0.0, lt=1.0)
    #: the observed values to score against
    actual: list[ObservedPointIn] = Field(default_factory=list)


class BacktestRequest(BaseModel):
    """§40: bounded, because an unbounded backtest is a compute DoS vector.

    `history` is optional; without it the service uses its deterministic SYNTHETIC
    history, which is what makes this endpoint usable in development without a
    dataset.
    """

    history: Optional[list[ObservedPointIn]] = None
    model: str = "seasonal"
    horizon_hours: float = Field(default=24.0, gt=0.0, le=168.0)
    step_hours: float = Field(default=24.0, gt=0.0, le=168.0)
    resolution_minutes: int = Field(default=15, ge=5, le=60)
    lookback_days: int = Field(default=14, ge=1, le=60)
    coverage: float = Field(default=0.9, gt=0.0, lt=1.0)
    max_steps: int = Field(default=8, ge=1, le=32)
    max_history_days: int = Field(default=60, ge=1, le=60)
    variant: str = "last_observation"
    #: §27, §28: run both baselines over the same evaluation period
    compare_models: bool = False


def _forecast_from_dict(payload: dict):
    from ...domain.forecasting import (
        CarbonForecast,
        CarbonForecastPoint,
        ForecastProvenance,
    )

    points = [CarbonForecastPoint(**p) for p in payload.get("points", [])]
    provenance = ForecastProvenance(**payload["provenance"])
    return CarbonForecast(
        points=points,
        resolution_minutes=int(payload.get("resolution_minutes", 15)),
        provenance=provenance,
        signal_type=payload.get("signal_type", "FORECAST"),
    )


@router.post("/carbon/forecast")
def create_forecast(request: ForecastRequest) -> JSONResponse:
    try:
        start = _parsed(request.start, "start")
        end = _parsed(request.end, "end")
    except ValueError as exc:
        return _error(exc, 422, "invalid_request")
    if request.model not in ("persistence", "seasonal"):
        return _error(
            ValueError(
                f"unknown forecast model {request.model!r}; expected persistence or "
                f"seasonal. The requested model is never silently replaced with another."
            ),
            400,
            "unknown_model",
        )
    try:
        forecast = service.forecast(
            start,
            end,
            resolution_minutes=request.resolution_minutes,
            model=request.model,
            lookback_days=request.lookback_days,
            coverage=request.coverage,
            history_days=request.history_days,
        )
    except ForecastServiceError as exc:
        return _error(exc, 422, "invalid_request")
    except ForecastError as exc:
        return _error(exc, 422, "invalid_request")
    except ValueError as exc:
        return _error(exc, 422, "invalid_request")
    return JSONResponse(status_code=200, content=forecast.model_dump(mode="json"))


@router.post("/carbon/forecast/evaluate")
def evaluate_forecast(request: EvaluateRequest) -> JSONResponse:
    try:
        if request.forecast is not None:
            forecast = _forecast_from_dict(request.forecast)
        else:
            if not request.start or not request.end:
                raise ValueError(
                    "supply either an inline `forecast` or a `start`/`end` window to "
                    "rebuild one from"
                )
            start = _parsed(request.start, "start")
            end = _parsed(request.end, "end")
            forecast = service.forecast(
                start,
                end,
                resolution_minutes=request.resolution_minutes,
                model=request.model,
                lookback_days=request.lookback_days,
                coverage=request.coverage,
            )
        actual = []
        for point in request.actual:
            actual.append((_parsed(point.timestamp, "actual.timestamp"), point.gco2_per_kwh))
        evaluation = service.evaluate(forecast, actual)
    except (ValueError, KeyError, TypeError) as exc:
        return _error(exc, 422, "invalid_request")
    except ForecastServiceError as exc:
        return _error(exc, 422, "invalid_request")
    except ForecastError as exc:
        return _error(exc, 422, "invalid_request")
    return JSONResponse(status_code=200, content=evaluation.as_dict())


@router.post("/carbon/forecast/backtest")
def backtest_forecast(request: BacktestRequest) -> JSONResponse:
    """§40. Development and evaluation only.

    Deliberately capped in every direction that costs compute: number of steps,
    history length, resolution, horizon and step size. The response carries the
    measured runtime so a caller can see what they spent.
    """
    if request.model not in ("persistence", "seasonal"):
        return _error(
            ValueError(f"unknown forecast model {request.model!r}"),
            400,
            "unknown_model",
        )
    config = BacktestConfig(
        model=request.model,
        horizon_hours=request.horizon_hours,
        step_hours=request.step_hours,
        resolution_minutes=request.resolution_minutes,
        lookback_days=request.lookback_days,
        coverage=request.coverage,
        max_steps=request.max_steps,
        max_history_days=request.max_history_days,
        variant=request.variant,
    )
    try:
        if request.history:
            history = service.history_from_points(
                [(p.timestamp, p.gco2_per_kwh) for p in request.history]
            )
        else:
            # No supplied dataset: use the deterministic SYNTHETIC history that
            # ends now. This is what makes the endpoint usable in development,
            # and it is labeled SYNTHETIC in the response's dataset block.
            from datetime import timezone

            now = datetime.now(tz=timezone.utc)
            history = service.resolve_history(
                now,
                max(request.lookback_days * 2, request.max_history_days),
                request.resolution_minutes,
            )
        if request.compare_models:
            result = service.compare_models(
                history, models=["persistence", "seasonal"], config=config
            )
            return JSONResponse(status_code=200, content=result.as_dict())
        result = service.backtest(history, config)
    except (ForecastServiceError, ForecastError, ValueError) as exc:
        return _error(exc, 422, "invalid_request")
    return JSONResponse(status_code=200, content=result.as_dict())