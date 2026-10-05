"""Canonical job model.

A Job is the unit the scheduler reasons about. The frontend collects
free-form loads (see lib/jobs/normalize.ts); this model is the normalized,
validated boundary the backend (and later the solver) depends on.

Architectural invariant: forecast values are NEVER hard constraints.
Feasibility (release/deadline/energy/capacity) is enforced here, from
user-declared values only.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

import math


class JobType(str, Enum):
    FIXED = "FIXED"
    DEFERRABLE_ATOMIC = "DEFERRABLE_ATOMIC"
    DEFERRABLE_INTERRUPTIBLE = "DEFERRABLE_INTERRUPTIBLE"
    THERMAL = "THERMAL"


def _require_aware(v: datetime, field: str) -> datetime:
    if v.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware (got naive datetime)")
    return v


def _require_finite_optional(v: Optional[float], field: str) -> Optional[float]:
    if v is not None and not math.isfinite(v):
        raise ValueError(f"{field} must be a finite number")
    return v


class Job(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=120)
    type: JobType = JobType.DEFERRABLE_INTERRUPTIBLE
    power_kw: float = Field(ge=0)
    release_time: datetime
    deadline: datetime
    duration_minutes: int = Field(gt=0)
    energy_kwh: float = Field(ge=0, default=0)
    flexibility_hours: float = Field(ge=0, default=0)
    interruptible: bool = False
    min_chunk_minutes: int = Field(gt=0, default=15)

    # THERMAL boundary (dynamics NOT modeled until a later phase).
    temperature_initial_c: Optional[float] = None
    temperature_min_c: Optional[float] = None
    temperature_max_c: Optional[float] = None
    temperature_target_c: Optional[float] = None
    thermal_a: Optional[float] = None
    thermal_b: Optional[float] = None
    thermal_c: Optional[float] = None
    max_power_kw: Optional[float] = None

    @field_validator("release_time", "deadline")
    @classmethod
    def _aware(cls, v: datetime, info) -> datetime:
        return _require_aware(v, info.field_name)

    @field_validator("power_kw", "energy_kwh", "flexibility_hours", "max_power_kw")
    @classmethod
    def _finite(cls, v: Optional[float], info) -> Optional[float]:
        return _require_finite_optional(v, info.field_name)

    @model_validator(mode="after")
    def _coherent(self) -> "Job":
        if self.release_time > self.deadline:
            raise ValueError("release_time must be <= deadline")
        if self.max_power_kw is not None and self.max_power_kw < 0:
            raise ValueError("max_power_kw must be >= 0")
        if self.type == JobType.THERMAL:
            band = [self.temperature_min_c, self.temperature_max_c]
            if any(v is None for v in band):
                raise ValueError("THERMAL jobs require temperature_min_c and temperature_max_c")
            assert self.temperature_min_c is not None and self.temperature_max_c is not None
            if self.temperature_min_c > self.temperature_max_c:
                raise ValueError("temperature_min_c must be <= temperature_max_c")
        return self

    def window_minutes(self) -> float:
        return (self.deadline - self.release_time).total_seconds() / 60


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
