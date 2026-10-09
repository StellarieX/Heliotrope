"""GET /api/v1/pool/stats: the community's planned load, aggregated per slot.

Aggregates only. The payload never carries a schedule id, job id, name or any
per-user row, and nothing here is cached per user.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...core import config
from ...services.load_pool import compute_pool_stats
from ...utils.time import ALLOWED_RESOLUTIONS, floor_to_resolution, to_utc
from . import execution

router = APIRouter()

DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=7)


class PoolSlotOut(BaseModel):
    timestamp: datetime
    kw: float


class PoolConfigOut(BaseModel):
    enabled: bool
    beta: float
    ref_kw: float
    scale_kw: float


class PoolStatsResponse(BaseModel):
    generated_at: datetime
    start: datetime
    end: datetime
    resolution_minutes: int
    active_schedules: int
    active_loads: int
    total_planned_kwh: float
    peak_kw: float
    peak_at: Optional[datetime]
    average_kw: float
    peak_to_average: Optional[float]
    slots: list[PoolSlotOut]
    pool: PoolConfigOut


def _err(detail: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "message": detail},
    )


@router.get("/pool/stats")
def pool_stats(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    resolution_minutes: int = 15,
) -> JSONResponse:
    """Planned kW of every live schedule per slot, with no way to tell whose it is.

    Defaults to now (floored to 15 minutes) through the next 24 hours.
    """
    if resolution_minutes not in ALLOWED_RESOLUTIONS:
        return _err(
            f"resolution_minutes must be one of {list(ALLOWED_RESOLUTIONS)}",
            "invalid_request",
            422,
        )
    for name, value in (("start", start), ("end", end)):
        if value is not None and value.tzinfo is None:
            return _err(f"{name} must be timezone-aware", "invalid_request", 422)

    now = datetime.now(timezone.utc)
    s = to_utc(start) if start is not None else floor_to_resolution(now, 15)
    e = to_utc(end) if end is not None else s + DEFAULT_WINDOW
    if e <= s:
        return _err("end must be after start", "invalid_request", 422)
    if e - s > MAX_WINDOW:
        return _err(f"window must be at most {MAX_WINDOW.days} days", "invalid_request", 422)

    stats = compute_pool_stats(execution.store, s, e, resolution_minutes)
    starts = stats.slot_starts
    response = PoolStatsResponse(
        generated_at=now,
        start=starts[0],
        end=starts[-1] + timedelta(minutes=resolution_minutes),
        resolution_minutes=resolution_minutes,
        active_schedules=stats.active_schedules,
        active_loads=stats.active_loads,
        total_planned_kwh=stats.total_planned_kwh,
        peak_kw=stats.peak_kw,
        peak_at=stats.peak_at,
        average_kw=stats.average_kw,
        peak_to_average=stats.peak_to_average,
        slots=[PoolSlotOut(timestamp=t, kw=k) for t, k in zip(starts, stats.kw)],
        pool=PoolConfigOut(
            enabled=config.POOL_ENABLED,
            beta=config.POOL_BETA,
            ref_kw=config.POOL_REF_KW,
            scale_kw=config.POOL_SCALE_KW,
        ),
    )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
