"""POST /api/v1/schedule — contract only in Phase 1.

Validates the request honestly (422 on bad jobs), then returns 501:
the engine does not exist yet and no schedule is fabricated.
"""

from typing import List

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...core import validation
from ...domain.jobs import Job

router = APIRouter()


class ScheduleRequest(BaseModel):
    jobs: List[Job]
    capacity_kw: float = Field(gt=0)
    scheduler: str = "CPSAT"


@router.post("/schedule")
def create_schedule(body: ScheduleRequest) -> JSONResponse:
    for job in body.jobs:
        try:
            validation.validate_job(job)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
    try:
        validation.validate_capacity(body.capacity_kw)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    return JSONResponse(
        status_code=501,
        content={
            "detail": f"Scheduler '{body.scheduler}' is not implemented yet (Phase 4). Request was valid; no schedule was produced and no metrics were computed.",
            "solver": body.scheduler,
            "jobs_received": len(body.jobs),
        },
    )
