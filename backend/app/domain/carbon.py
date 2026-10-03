"""Canonical carbon signal contracts.

Signal kinds are NEVER silently equivalent: every point and every response
carries its SignalType (MARGINAL / AVERAGE / PROXY / SYNTHETIC) so the future
scheduler — and the UI — always know what they are consuming.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .jobs import _require_aware


class SignalType(str, Enum):
    MARGINAL = "MARGINAL"
    AVERAGE = "AVERAGE"
    PROXY = "PROXY"
    SYNTHETIC = "SYNTHETIC"


class Quality(str, Enum):
    MEASURED = "MEASURED"
    ESTIMATED = "ESTIMATED"
    INTERPOLATED = "INTERPOLATED"
    SYNTHETIC = "SYNTHETIC"


class CarbonPoint(BaseModel):
    time: datetime
    gco2_per_kwh: float = Field(ge=0)
    signal_type: SignalType = SignalType.SYNTHETIC
    is_proxy: bool = False
    is_forecast: bool = False
    quality: Quality = Quality.SYNTHETIC
    source: str = Field(min_length=1, default="unknown")

    @field_validator("time")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "time")


class SignalQuality(BaseModel):
    complete: bool = True
    missing_points: int = 0
    interpolated_points: int = 0
    source: str = "unknown"
    signal_type: SignalType = SignalType.SYNTHETIC
    is_forecast: bool = False


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


class CarbonSignalResponse(BaseModel):
    """Public API shape for GET /api/v1/carbon."""

    start: datetime
    end: datetime
    resolution_minutes: int = Field(gt=0)
    signal_type: SignalType
    source: str
    points: List["CarbonPointOut"] = []
    quality: SignalQuality = SignalQuality()


class CarbonPointOut(BaseModel):
    timestamp: datetime
    carbon_intensity_gco2_per_kwh: float = Field(ge=0)

    @field_validator("timestamp")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return _require_aware(v, "timestamp")


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


# Resolve forward reference (CarbonPointOut defined after CarbonSignalResponse).
CarbonSignalResponse.model_rebuild()
