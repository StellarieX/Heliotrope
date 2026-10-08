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
    # Provider names are validated in one place (CARBON_PROVIDER_NAMES in
    # domain.carbon); an omitted provider uses CARBON_PROVIDER via
    # `CarbonService.default()`.
    def _err(detail: str, code: str, status: int) -> JSONResponse:
        return JSONResponse(
            status_code=status,
            content={"detail": detail, "code": code, "message": detail},
        )

    def _coerce(value: str) -> str:
        # Accept a trailing "Z" (UTC) which fromisoformat does not parse.
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return v

    try:
        query = CarbonQuery(
            start=datetime.fromisoformat(_coerce(start)),
            end=datetime.fromisoformat(_coerce(end)),
            resolution_minutes=resolution_minutes,
            provider=(provider or "synthetic").strip().lower(),
        )
    except ValueError as exc:
        detail = str(exc)
        return _err(detail, "invalid_request", 422)
    try:
        service = CarbonService.default() if provider is None else CarbonService(provider_name=provider)
        response = service.get_signal(query.start, query.end, query.resolution_minutes)
    except CarbonBadRequest as exc:
        detail = str(exc)
        return _err(detail, "invalid_request", 422)
    except CarbonUnavailable as exc:
        detail = str(exc)
        return _err(detail, "provider_unavailable", 503)
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
