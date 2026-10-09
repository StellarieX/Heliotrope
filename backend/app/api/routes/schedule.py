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
from pydantic import BaseModel, Field, field_validator

import math

from ...domain.forecasting import ForecastConfig, ForecastMode
from ...domain.horizon import HorizonError, SchedulingHorizon
from ...domain.scaling import ScalingError
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


def _err(detail: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "message": detail},
    )


def _parse_aware_iso(value: str, field: str) -> datetime:
    """Parse an ISO-8601 timestamp, rejecting naive values (no silent UTC shift)."""
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(v)
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive timestamp would be interpreted "
            "against the server's local zone, which silently shifts a schedule"
        )
    return parsed


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

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        if v.upper() not in ("FORECAST", "ACTUAL"):
            raise ValueError(
                f"unknown carbon mode {v!r}; expected 'FORECAST' or 'ACTUAL'"
            )
        return v


class ScheduleRequest(BaseModel):
    jobs: list[LoadSpec] = Field(min_length=1)
    capacity_kw: float = Field(gt=0)
    #: optional per-slot connection capacity in kW. Overrides the scalar
    #: `capacity_kw` slot by slot when given; the length must equal the
    #: horizon slot count or normalization refuses it with a 422 (§5).
    capacity_profile_kw: Optional[list[float]] = None
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
    #: anti-herding: steer away from slots other users' active plans already crowd
    share_pool: bool = False
    #: a live schedule to leave out of the pool (e.g. the caller's own plan)
    pool_exclude_schedule_id: Optional[str] = None
    #: §38. Optional and backward compatible: absent means observed carbon.
    carbon: Optional[CarbonForecastOptions] = None
    #: skip the per-job counterfactual explanations when they are not wanted
    explain: bool = True

    @field_validator("capacity_kw")
    @classmethod
    def _finite_capacity(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("capacity_kw must be a finite number")
        return v

    @field_validator("capacity_profile_kw")
    @classmethod
    def _profile_entries(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is None:
            return v
        if not v:
            raise ValueError("capacity_profile_kw must not be empty when provided")
        for entry in v:
            if not math.isfinite(entry):
                raise ValueError("capacity_profile_kw entries must be finite numbers")
            if entry < 0:
                raise ValueError("capacity_profile_kw entries must be >= 0")
        return v

    @field_validator("carbon_resolution_minutes")
    @classmethod
    def _allowed_resolution(cls, v: int) -> int:
        if v not in (5, 15, 30, 60):
            raise ValueError("carbon_resolution_minutes must be one of 5, 15, 30, 60")
        return v

    @field_validator("scheduler")
    @classmethod
    def _non_empty_scheduler(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("scheduler must be a non-empty name")
        return v


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
    if (request.carbon_start is None) != (request.carbon_end is None):
        raise NormalizationError("supply both carbon_start and carbon_end, or neither")
    if request.carbon_start and request.carbon_end:
        start = _parse_aware_iso(request.carbon_start, "carbon_start")
        end = _parse_aware_iso(request.carbon_end, "carbon_end")
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
        detail = str(exc.args[0])
        return None, _err(detail, "invalid_scheduler", 400)

    forecast = None
    uncertainty_upper = None
    grid_horizon = None
    if request.carbon is not None and request.carbon.mode.upper() == "FORECAST":
        try:
            # The schedule is built on the canonical horizon grid, which may be
            # finer (or offset) relative to the forecast grid. Uncertainty and
            # the forecast signal are resampled onto that exact grid, so a
            # resolution or alignment difference is interpolated within
            # tolerance instead of rejected as a missing point.
            grid_horizon = _schedule_horizon(request)
            forecast = _forecast_for(request, end=grid_horizon.end)
            uncertainty_upper = forecast_service.uncertainty_over_horizon(
                forecast,
                grid_horizon.start,
                resolution_minutes=grid_horizon.slot_minutes,
                horizon_end=grid_horizon.end,
            )
            signal = forecast_service.signal_over_horizon(
                forecast,
                grid_horizon.start,
                grid_horizon.end,
                resolution_minutes=grid_horizon.slot_minutes,
            )
        except (ForecastServiceError, ForecastError, ValueError) as exc:
            detail = str(exc)
            return None, _err(detail, "invalid_request", 422)
    else:
        try:
            signal = _load_signal(request)
        except CarbonBadRequest as exc:
            detail = str(exc)
            return None, _err(detail, "invalid_request", 422)
        except CarbonUnavailable as exc:
            detail = str(exc)
            return None, _err(detail, "provider_unavailable", 503)
        except ValueError as exc:
            detail = str(exc)
            return None, _err(detail, "invalid_request", 422)

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
        detail = str(exc)
        return None, _err(detail, "invalid_request", 422)

    try:
        scheduler_input, warnings = service.build_input(
            request.jobs,
            signal,
            request.capacity_kw,
            objective=request.objective,
            horizon=grid_horizon if grid_horizon is not None else request.horizon,
            tariff=request.tariff,
            forecast_config=forecast_config,
            uncertainty_upper=uncertainty_upper,
            forecast_provenance=(
                forecast.provenance.model_dump(mode="json") if forecast is not None else None
            ),
            capacity_profile_kw=request.capacity_profile_kw,
        )
    except (NormalizationError, ScalingError, HorizonError) as exc:
        detail = str(exc)
        return None, _err(detail, "invalid_request", 422)

    return (name, scheduler_input, warnings, forecast_config, forecast), None


def _horizon_start(request: ScheduleRequest) -> datetime:
    if request.horizon is not None:
        return request.horizon.start
    moments = [m for job in request.jobs for m in (job.release_at, job.deadline_at) if m]
    if not moments:
        raise ValueError("no job has a release or deadline to forecast over")
    return min(moments)


def _schedule_horizon(request: ScheduleRequest):
    """The exact horizon `build_input` will normalize against.

    When the caller pins one it is used verbatim; otherwise this replicates the
    normalizer's derived horizon (`spanning` with the same arguments), so the
    grid the forecast is resampled onto is byte-identical to the grid the
    scheduler sees. Kept in sync with `SchedulerNormalizer._horizon_from`.
    """
    if request.horizon is not None:
        return request.horizon
    from ...domain.horizon import SchedulingHorizon

    moments = [m for job in request.jobs for m in (job.release_at, job.deadline_at) if m]
    if not moments:
        raise NormalizationError("no job has a release or deadline to derive a signal range")
    return SchedulingHorizon.spanning(moments, slot_minutes=15, pad_slots=1)


def _horizon_end(request: ScheduleRequest) -> datetime:
    from datetime import timedelta

    if request.horizon is not None:
        return request.horizon.end
    moments = [m for job in request.jobs for m in (job.release_at, job.deadline_at) if m]
    if not moments:
        raise ValueError("no job has a release or deadline to forecast over")
    return max(moments) + timedelta(minutes=request.carbon_resolution_minutes)


def _forecast_for(request: ScheduleRequest, end=None):
    from ...utils.time import to_utc

    options = request.carbon
    window_end = end if end is not None else _horizon_end(request)
    # The forecast must cover the whole schedule horizon; the resampler refuses
    # to invent values past the forecast, so the window is extended (never
    # shrunk) to the horizon end up front.
    window_end = max(to_utc(window_end), to_utc(_horizon_end(request)))
    return forecast_service.forecast(
        _horizon_start(request),
        window_end,
        resolution_minutes=request.carbon_resolution_minutes,
        model=options.forecast_model,
        lookback_days=options.lookback_days,
        coverage=options.coverage,
        history_days=options.history_days,
    )


def _with_pool(request: ScheduleRequest, scheduler_input):
    """Attach the anti-herding pool when the caller opts in (aggregate load only)."""
    from ...services.load_pool import attach_pool, pool_summary
    from .execution import store

    if not request.share_pool:
        return scheduler_input, pool_summary(None)
    exclude = [request.pool_exclude_schedule_id] if request.pool_exclude_schedule_id else []
    return attach_pool(store, scheduler_input, exclude)


@router.post("/schedule")
def create_schedule(request: ScheduleRequest) -> JSONResponse:
    prepared, error = _prepare(request)
    if error is not None:
        return error
    name, scheduler_input, warnings, forecast_config, forecast = prepared
    scheduler_input, pool = _with_pool(request, scheduler_input)

    result = service.run(
        scheduler_input, name, config=request.solver_config, explain=request.explain
    )
    payload = result.model_dump(mode="json")
    payload["warnings"] = warnings
    payload["pool"] = pool
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
    try:
        start = _parse_aware_iso(str(payload["start"]), "actual_signal.start")
        end = _parse_aware_iso(str(payload["end"]), "actual_signal.end")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"actual_signal window is malformed: {exc}") from exc
    parsed_points = []
    for p in points:
        try:
            ts = _parse_aware_iso(str(p["timestamp"]), "actual_signal.points.timestamp")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"actual_signal point is malformed: {exc}") from exc
        try:
            gco2 = float(p.get("gco2_per_kwh", p.get("carbon_intensity_gco2_per_kwh", 0)))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"actual_signal point carbon value is malformed: {exc}") from exc
        if gco2 < 0:
            raise ValueError("actual_signal carbon values must be >= 0")
        parsed_points.append(
            CarbonPoint(
                time=ts,
                gco2_per_kwh=gco2,
                source=payload.get("source", "actual"),
            )
        )
    try:
        return CarbonSignal(
            start=start,
            end=end,
            resolution_minutes=int(payload.get("resolution_minutes", 15)),
            source=payload.get("source", "actual"),
            points=parsed_points,
        )
    except Exception as exc:
        raise ValueError(f"actual_signal is malformed: {exc}") from exc


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
            "co2_basis": "OBSERVED",
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
        # The solver minimizes the objective carbon (EXPECTED: the point
        # forecast; ROBUST: forecast + risk_weight * (upper - forecast)). The
        # reporting ledger behind metrics.total_co2_kg and co2_saved_* is the
        # point forecast itself, so those figures are estimates, not readings.
        "co2_basis": "FORECAST",
        "co2_note": "total_co2_kg and co2_saved_* are computed from the forecast, not "
        "from measured grid data; supply carbon.actual_signal to get realized CO2",
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
            detail = str(exc.args[0])
            return _err(detail, "invalid_scheduler", 400)

    scheduler_input, pool = _with_pool(request, scheduler_input)
    comparison = service.compare(
        scheduler_input, schedulers=names, config=request.solver_config
    )
    payload = comparison.model_dump(mode="json")
    payload["warnings"] = warnings
    payload["pool"] = pool
    return JSONResponse(status_code=200, content=payload)
