"""GET /api/v1/carbon — live in Phase 2.

Returns a validated canonical signal with quality metadata. Errors are
structured: 422 invalid request, 503 provider unavailable/misconfigured.
Synthetic output is always labeled SYNTHETIC — never grid data.
"""

from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...domain.carbon import CarbonQuery
from ...services.carbon_service import CarbonBadRequest, CarbonService, CarbonUnavailable

router = APIRouter()


@router.get("/carbon")
def get_carbon(
    start: str,
    end: str,
    resolution_minutes: int = 15,
    provider: str | None = None,
) -> JSONResponse:
    # `provider` passes straight through to the service, which owns the
    # provider allowlist and the configured default: an omitted provider uses
    # CARBON_PROVIDER via `CarbonService.default()`, and an unknown name is a
    # 422 from the service. The query object below validates only the window;
    # it never decides which provider serves, so there is exactly one place
    # where provider names are accepted or rejected.
    try:
        query = CarbonQuery(
            start=datetime.fromisoformat(start),
            end=datetime.fromisoformat(end),
            resolution_minutes=resolution_minutes,
            provider="synthetic",
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    try:
        service = CarbonService.default() if provider is None else CarbonService(provider_name=provider)
        response = service.get_signal(query.start, query.end, query.resolution_minutes)
    except CarbonBadRequest as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except CarbonUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": "provider_unavailable"})
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
