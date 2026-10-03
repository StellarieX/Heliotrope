"""POST /api/v1/schedule and POST /api/v1/schedule/compare (Phase 4, §33-§36).

Phase 1 returned 501 here because no engine existed. Phase 4 makes this live:
requests are normalized, scheduled by the requested engine, independently
validated, and measured by the one accounting service.

ERROR VOCABULARY
    400 invalid scheduler name — never silently substituted (§34)
    422 invalid request: malformed jobs, capacity, horizon or objective
    503 carbon signal unavailable

An INFEASIBLE input is NOT an error: it returns 200 with
`status: "INFEASIBLE"` and a reason, because "no schedule exists" is a valid and
important answer, not a malformed request (§20, §44).
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...domain.forecasting import ForecastConfig, ForecastMode
from ...domain.horizon import SchedulingHorizon
from ...domain.loads import LoadSpec
from ...domain.scheduling import (
    ObjectiveWeights,
    SchedulerConfig,
    SchedulerResult,
    TimeOfUseTariff,
)
from ...services.carbon_service import CarbonBadRequest, CarbonService, CarbonUnavailable
from ...services.forecast_service import ForecastService, ForecastServiceError
from ...services.forecasting import ForecastError
from ...services.schedule_realization import RealizedScheduleEvaluator
from ...services.scheduler_normalizer import NormalizationError
from ...services.scheduler_service import SchedulerService, resolve_scheduler
from ...services.schedulers import SchedulerName

router = APIRouter()
service = SchedulerService()
forecast_service = ForecastService()


class CarbonForecastOptions(BaseModel):
    """§38: the optional forecast block on `POST /schedule`.

    ABSENT MEANS ACTUAL. A request that says nothing about forecasting schedules
    against the observed signal exactly as it did in Phase 4 (§14, §38). Every
    field here is a choice the caller has to make visible; none of them changes
    whether a schedule is FEASIBLE, only which feasible schedule is preferred
    (§19).
    """

    #: "FORECAST" means "build a forecast and optimize against it"; omitting the
    #: block altogether is the same as mode "ACTUAL".
    mode: str = "FORECAST"
    forecast_model: str = "seasonal"
    #: §16
    forecast_mode: ForecastMode = ForecastMode.ROBUST
    #: §16, λ. 0.0 means "optimize the point forecast", 1.0 means "optimize the
    #: upper prediction bound"
    risk_weight: float = Field(default=0.0, ge=0.0, le=10.0)
    #: §20, an explicit deadline safety margin, in whole slots
    deadline_buffer_minutes: int = Field(default=0, ge=0, le=24 * 60)
    lookback_days: int = Field(default=14, ge=1, le=60)
    coverage: float = Field(default=0.9, gt=0.0, lt=1.0)
    history_days: int = Field(default=14, ge=1, le=60)
    #: the observed signal used to SCORE the schedule afterwards (§35). Without
    #: it the response reports the schedule's own carbon figure and says which
    #: signal it came from.
    actual_signal: Optional[dict] = None


class ScheduleRequest(BaseModel):
    jobs: list[LoadSpec] = Field(min_length=1)
    capacity_kw: float = Field(gt=0)
    #: requested engine. Case-insensitive; an unknown name is a 400 (§34)
    scheduler: str = "CPSAT"
    objective: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    horizon: Optional[SchedulingHorizon] = None
    tariff: Optional[TimeOfUseTariff] = None
    solver_config: Optional[SchedulerConfig] = None
    #: carbon signal selection; defaults to the configured provider
    carbon_provider: Optional[str] = None
    carbon_start: Optional[str] = None
    carbon_end: Optional[str] = None
    carbon_resolution_minutes: int = Field(default=15, ge=5, le=60)
    #: §38. Optional and backward compatible: absent means observed carbon.
    carbon: Optional[CarbonForecastOptions] = None
    #: skip the per-job counterfactual explanations when they are not wanted
    explain: bool = True


class CompareRequest(ScheduleRequest):
    """Same input shape; runs several engines over it unchanged (§42)."""

    schedulers: Optional[list[str]] = None


def _load_signal(request: ScheduleRequest):
    """Fetch the carbon signal the schedule will be scored against.

    When the caller pins a range, that range is used verbatim. Otherwise the
    horizon's own span is requested, so the signal always covers exactly the
    slots being scheduled and the normalizer never has to reject it for missing
    a point.
    """
    if request.carbon_start and request.carbon_end:
        from datetime import datetime

        start = datetime.fromisoformat(request.carbon_start)
        end = datetime.fromisoformat(request.carbon_end)
    elif request.horizon is not None:
        start, end = request.horizon.start, request.horizon.end
    else:
        from datetime import timedelta

        moments = [m for job in request.jobs for m in (job.release_at, job.deadline_at) if m]
        if not moments:
            raise NormalizationError("no job has a release or deadline to derive a signal range")
        start = min(moments)
        end = max(moments) + timedelta(minutes=request.carbon_resolution_minutes)

    carbon = (
        CarbonService(provider_name=request.carbon_provider)
        if request.carbon_provider
        else CarbonService.default()
    )
    return carbon.get_signal(start, end, request.carbon_resolution_minutes)


def _prepare(request: ScheduleRequest):
    """Shared preamble: validate, normalize, and turn engine errors into HTTP.

    §38: when the request carries a `carbon` block in FORECAST mode, the signal
    handed to the scheduler is the FORECAST, and the per-slot upper prediction
    bound rides alongside so the normalizer can build the objective. Everything
    else is untouched, so the forecast path and the observed path normalize
    through exactly the same code.
    """
    try:
        name = resolve_scheduler(request.scheduler)
    except KeyError as exc:
        return None, JSONResponse(
            status_code=400,
            content={"detail": str(exc.args[0]), "code": "invalid_scheduler"},
        )

    forecast = None
    uncertainty_upper = None
    if request.carbon is not None and request.carbon.mode.upper() == "FORECAST":
        try:
            forecast = _forecast_for(request)
            uncertainty_upper = forecast_service.uncertainty_over_horizon(
                forecast, _horizon_start(request)
            )
            signal = forecast_service.as_carbon_signal(forecast)
        except (ForecastServiceError, ForecastError, ValueError) as exc:
            return None, JSONResponse(
                status_code=422, content={"detail": str(exc), "code": "invalid_request"}
            )
    else:
        try:
            signal = _load_signal(request)
        except CarbonBadRequest as exc:
            return None, JSONResponse(
                status_code=422, content={"detail": str(exc), "code": "invalid_request"}
            )
        except CarbonUnavailable as exc:
            return None, JSONResponse(
                status_code=503, content={"detail": str(exc), "code": "provider_unavailable"}
            )
        except ValueError as exc:
            return None, JSONResponse(
                status_code=422, content={"detail": str(exc), "code": "invalid_request"}
            )

    try:
        forecast_config = ForecastConfig(
            forecast_mode=(
                request.carbon.forecast_mode
                if request.carbon is not None
                else ForecastMode.ACTUAL
            ),
            risk_weight=request.carbon.risk_weight if request.carbon is not None else 0.0,
            deadline_buffer_minutes=(
                request.carbon.deadline_buffer_minutes if request.carbon is not None else 0
            ),
        )
    except ValueError as exc:
        # A config the model rejects (a partial-slot deadline buffer, an
        # out-of-range risk weight) is a malformed REQUEST, not a server fault.
        return None, JSONResponse(
            status_code=422, content={"detail": str(exc), "code": "invalid_request"}
        )

    try:
        scheduler_input, warnings = service.build_input(
            request.jobs,
            signal,
            request.capacity_kw,
            objective=request.objective,
            horizon=request.horizon,
            tariff=request.tariff,
            forecast_config=forecast_config,
            uncertainty_upper=uncertainty_upper,
            forecast_provenance=(
                forecast.provenance.model_dump(mode="json") if forecast is not None else None
            ),
        )
    except NormalizationError as exc:
        return None, JSONResponse(
            status_code=422, content={"detail": str(exc), "code": "invalid_request"}
        )

    return (name, scheduler_input, warnings, forecast_config, forecast), None


def _horizon_start(request: ScheduleRequest) -> datetime:
    if request.horizon is not None:
        return request.horizon.start
    moments = [m for job in request.jobs for m in (job.release_at, job.deadline_at) if m]
    if not moments:
        raise ValueError("no job has a release or deadline to forecast over")
    return min(moments)


def _horizon_end(request: ScheduleRequest) -> datetime:
    from datetime import timedelta

    if request.horizon is not None:
        return request.horizon.end
    moments = [m for job in request.jobs for m in (job.release_at, job.deadline_at) if m]
    if not moments:
        raise ValueError("no job has a release or deadline to forecast over")
    return max(moments) + timedelta(minutes=request.carbon_resolution_minutes)


def _forecast_for(request: ScheduleRequest):
    options = request.carbon
    return forecast_service.forecast(
        _horizon_start(request),
        _horizon_end(request),
        resolution_minutes=request.carbon_resolution_minutes,
        model=options.forecast_model,
        lookback_days=options.lookback_days,
        coverage=options.coverage,
        history_days=options.history_days,
    )


@router.post("/schedule")
def create_schedule(request: ScheduleRequest) -> JSONResponse:
    prepared, error = _prepare(request)
    if error is not None:
        return error
    name, scheduler_input, warnings, forecast_config, forecast = prepared

    result = service.run(
        scheduler_input, name, config=request.solver_config, explain=request.explain
    )
    payload = result.model_dump(mode="json")
    payload["warnings"] = warnings
    payload["forecast"] = _forecast_summary(request, forecast_config, forecast)

    # §35: when the caller supplies actuals, score the schedule against THEM.
    # A schedule optimized on a forecast must never report only its own opinion.
    if request.carbon is not None and request.carbon.actual_signal:
        payload["realized"] = _realized(request, result, forecast)
    return JSONResponse(status_code=200, content=payload)


def _actual_signal(payload: dict):
    from ...domain.carbon import CarbonPoint, CarbonSignal

    points = payload.get("points") if isinstance(payload, dict) else None
    if not points:
        return None
    return CarbonSignal(
        start=datetime.fromisoformat(payload["start"]),
        end=datetime.fromisoformat(payload["end"]),
        resolution_minutes=int(payload.get("resolution_minutes", 15)),
        source=payload.get("source", "actual"),
        points=[
            CarbonPoint(
                time=datetime.fromisoformat(p["timestamp"]),
                gco2_per_kwh=float(
                    p.get("gco2_per_kwh", p.get("carbon_intensity_gco2_per_kwh", 0))
                ),
                source=payload.get("source", "actual"),
            )
            for p in points
        ],
    )


def _realized(request: ScheduleRequest, result: SchedulerResult, forecast):
    failure = {
        "realized_co2_kg": None,
        "forecast_expected_co2_kg": None,
        "forecast_error_kg": None,
        "covered_slots": None,
        "note": "no realized CO2 computed; the schedule's own carbon figure is "
        "measured against the forecast it was built on, not against reality",
    }
    try:
        actual = _actual_signal(request.carbon.actual_signal or {})
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {**failure, "error": f"actual_signal is malformed: {exc}"}
    if actual is None:
        return {**failure, "error": "actual_signal has no points"}
    config = ForecastConfig(
        forecast_mode=request.carbon.forecast_mode,
        risk_weight=request.carbon.risk_weight,
        deadline_buffer_minutes=request.carbon.deadline_buffer_minutes,
    )
    return RealizedScheduleEvaluator().evaluate(result, actual, forecast, config).as_dict()


def _forecast_summary(
    request: ScheduleRequest, config: ForecastConfig, forecast
) -> dict:
    """What was scheduled against, stated plainly (§2, §31)."""
    if request.carbon is None or forecast is None:
        return {
            "mode": "ACTUAL",
            "note": "no forecast was requested; this schedule was built against the "
            "observed carbon signal, exactly as in Phase 4",
        }
    p = forecast.provenance
    return {
        "mode": request.carbon.mode.upper(),
        "forecast_mode": config.forecast_mode.value,
        "risk_weight": config.risk_weight,
        "deadline_buffer_minutes": config.deadline_buffer_minutes,
        "model": p.model,
        "generated_at": p.generated_at.isoformat(),
        "training_window": [p.training_window_start.isoformat(), p.training_window_end.isoformat()],
        "training_points": p.training_points,
        "source_signal": p.source_signal,
        "source_signal_type": p.source_signal_type,
        "horizon": [p.horizon_start.isoformat(), p.horizon_end.isoformat()],
        "resolution_minutes": p.resolution_minutes,
        "interval_nominal_coverage": p.interval_nominal_coverage,
        "uncertainty_method": p.uncertainty_method,
        "note": "forecast uncertainty changes which schedule is preferred, not whether "
        "one is feasible; deadlines, energy, capacity, thermal comfort and atomicity "
        "are unchanged",
    }


@router.post("/schedule/compare")
def compare_schedules(request: CompareRequest) -> JSONResponse:
    prepared, error = _prepare(request)
    if error is not None:
        return error
    _name, scheduler_input, warnings, _config, _forecast = prepared

    names: list[SchedulerName] = [SchedulerName.ASAP, SchedulerName.GREEDY, SchedulerName.CPSAT]
    if request.schedulers:
        try:
            names = [resolve_scheduler(s) for s in request.schedulers]
        except KeyError as exc:
            return JSONResponse(
                status_code=400,
                content={"detail": str(exc.args[0]), "code": "invalid_scheduler"},
            )

    comparison = service.compare(
        scheduler_input, schedulers=names, config=request.solver_config
    )
    payload = comparison.model_dump(mode="json")
    payload["warnings"] = warnings
    return JSONResponse(status_code=200, content=payload)
