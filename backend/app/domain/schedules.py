"""Schedule + metrics contracts.

Metrics fields stay Optional: Phase 1 returns no solver output, and later
phases must never fabricate values for metrics they did not compute.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

import math

from .jobs import _require_aware, utcnow


class PlacedStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    UNSCHEDULABLE = "UNSCHEDULABLE"


class PlacedJob(BaseModel):
    job_id: str
    start_time: datetime
    end_time: datetime
    power_kw: float = Field(ge=0)
    energy_kwh: float = Field(ge=0)
    status: PlacedStatus = PlacedStatus.SCHEDULED
    reason: str = ""

    @field_validator("start_time", "end_time")
    @classmethod
    def _aware(cls, v: datetime, info) -> datetime:
        return _require_aware(v, info.field_name)

    @field_validator("power_kw", "energy_kwh")
    @classmethod
    def _finite(cls, v: float, info) -> float:
        if not math.isfinite(v):
            raise ValueError(f"{info.field_name} must be a finite number")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> "PlacedJob":
        if self.end_time < self.start_time:
            raise ValueError("end_time must be >= start_time")
        return self


class Metrics(BaseModel):
    total_energy_kwh: Optional[float] = None
    total_co2_kg: Optional[float] = None
    co2_saved_kg: Optional[float] = None
    co2_saved_percent: Optional[float] = None
    peak_kw: Optional[float] = None
    energy_cost: Optional[float] = None
    deadline_misses: Optional[int] = None
    solve_time_ms: Optional[int] = None


class ScheduleResponse(BaseModel):
    schedule: List[PlacedJob] = []
    metrics: Metrics = Metrics()
    warnings: List[str] = []
    solver: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("created_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "created_at")
