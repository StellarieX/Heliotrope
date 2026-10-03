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


def _signal_for(request: CoordinationRequest):
    from datetime import datetime, timedelta

    if request.carbon_start and request.carbon_end:
        start = datetime.fromisoformat(request.carbon_start)
        end = datetime.fromisoformat(request.carbon_end)
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
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except CarbonBadRequest as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except CarbonUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": "provider_unavailable"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})

    try:
        result = coordinator.coordinate(request, signal)
        payload = result.model_dump(mode="json")
    except CoordinationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except NormalizationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    return JSONResponse(status_code=200, content=payload)


@router.post("/coordination/compare")
def compare(request: CoordinationRequest) -> JSONResponse:
    """Independent vs coordinated on the same input, both from real runs."""
    try:
        signal = _signal_for(request)
    except CoordinationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except CarbonBadRequest as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except CarbonUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": "provider_unavailable"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    try:
        comparison = coordinator.compare(request, signal)
    except CoordinationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except NormalizationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    return JSONResponse(status_code=200, content=comparison.model_dump(mode="json"))
