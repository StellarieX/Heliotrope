"""Carbon signal contracts."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .jobs import _require_aware


class CarbonPoint(BaseModel):
    time: datetime
    gco2_per_kwh: float = Field(ge=0)
    is_proxy: bool = False
    source: str = Field(min_length=1, default="unknown")

    @field_validator("time")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "time")


class CarbonSignal(BaseModel):
    start: datetime
    end: datetime
    resolution_minutes: int = Field(gt=0)
    points: List[CarbonPoint] = []
    is_proxy: bool = False
    source: str = Field(min_length=1, default="unknown")

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "datetime")


class CarbonQuery(BaseModel):
    start: datetime
    end: datetime
    resolution_minutes: int = Field(gt=0, le=60, default=15)
    provider: str = "synthetic"

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "datetime")

    @field_validator("provider")
    @classmethod
    def _known(cls, v: str) -> str:
        allowed = {"synthetic", "csv", "external"}
        if v not in allowed:
            raise ValueError(f"provider must be one of {sorted(allowed)}")
        return v
