"""POST /api/v1/coordination/schedule — multi-user coordination (Phase 6).

Thin route: validation errors are 422, unconfigured carbon is 503, and an
INFEASIBLE building is a 200 with status INFEASIBLE (no schedule exists is a
valid answer, not a malformed request). Nothing is fabricated: every metric
comes from the coordinator's accounting over real optimizer output.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...domain.coordination import CoordinationRequest
from ...services.carbon_service import CarbonBadRequest, CarbonService, CarbonUnavailable
from ...services.coordinator import CoordinationError, MultiUserCoordinator
from ...services.scheduler_normalizer import NormalizationError

router = APIRouter()
coordinator = MultiUserCoordinator()


def _err(detail: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "message": detail},
    )


def _parse_aware_iso(value: str, field: str):
    from datetime import datetime

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


def _signal_for(request: CoordinationRequest):
    from datetime import timedelta

    if (request.carbon_start is None) != (request.carbon_end is None):
        from ...services.coordinator import CoordinationError as _CE

        raise _CE("supply both carbon_start and carbon_end, or neither")
    if request.carbon_start and request.carbon_end:
        start = _parse_aware_iso(request.carbon_start, "carbon_start")
        end = _parse_aware_iso(request.carbon_end, "carbon_end")
    else:
        moments = [
            m
            for job in request.jobs
            for m in (job.release_at, job.deadline_at)
            if m
        ]
        if not moments:
            raise CoordinationError("no job has a release or deadline to derive a signal range")
        start = min(moments)
        end = max(moments) + timedelta(minutes=request.carbon_resolution_minutes)
    carbon = (
        CarbonService(provider_name=request.carbon_provider)
        if request.carbon_provider
        else CarbonService.default()
    )
    return carbon.get_signal(start, end, request.carbon_resolution_minutes)


@router.post("/coordination/schedule")
def coordinate(request: CoordinationRequest) -> JSONResponse:
    try:
        signal = _signal_for(request)
    except CoordinationError as exc:
        return _err(str(exc), "invalid_request", 422)
    except CarbonBadRequest as exc:
        return _err(str(exc), "invalid_request", 422)
    except CarbonUnavailable as exc:
        return _err(str(exc), "provider_unavailable", 503)
    except ValueError as exc:
        return _err(str(exc), "invalid_request", 422)

    try:
        result = coordinator.coordinate(request, signal)
        payload = result.model_dump(mode="json")
    except CoordinationError as exc:
        return _err(str(exc), "invalid_request", 422)
    except NormalizationError as exc:
        return _err(str(exc), "invalid_request", 422)
    return JSONResponse(status_code=200, content=payload)


@router.post("/coordination/compare")
def compare(request: CoordinationRequest) -> JSONResponse:
    """Independent vs coordinated on the same input, both from real runs."""
    try:
        signal = _signal_for(request)
    except CoordinationError as exc:
        return _err(str(exc), "invalid_request", 422)
    except CarbonBadRequest as exc:
        return _err(str(exc), "invalid_request", 422)
    except CarbonUnavailable as exc:
        return _err(str(exc), "provider_unavailable", 503)
    except ValueError as exc:
        return _err(str(exc), "invalid_request", 422)
    try:
        comparison = coordinator.compare(request, signal)
    except CoordinationError as exc:
        return _err(str(exc), "invalid_request", 422)
    except NormalizationError as exc:
        return _err(str(exc), "invalid_request", 422)
    return JSONResponse(status_code=200, content=comparison.model_dump(mode="json"))
