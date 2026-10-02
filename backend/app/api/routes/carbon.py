"""GET /api/v1/carbon — contract only in Phase 1.

Validates the query honestly (422 on bad input), then returns 501:
no provider exists yet and no signal is fabricated.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ...domain.carbon import CarbonQuery

router = APIRouter()


@router.get("/carbon")
def get_carbon(
    start: str,
    end: str,
    resolution_minutes: int = 15,
    provider: str = "synthetic",
) -> JSONResponse:
    from datetime import datetime

    try:
        query = CarbonQuery(
            start=datetime.fromisoformat(start),
            end=datetime.fromisoformat(end),
            resolution_minutes=resolution_minutes,
            provider=provider,
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    if query.start >= query.end:
        return JSONResponse(status_code=422, content={"detail": "start must be < end"})
    return JSONResponse(
        status_code=501,
        content={
            "detail": f"Carbon provider '{query.provider}' is not implemented yet (Phase 2). No signal was produced.",
            "provider": query.provider,
        },
    )
