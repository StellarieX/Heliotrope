"""Multi-user coordination domain (Phase 6).

One connection, many participants. Individually optimal schedules can pile
every flexible load into the same clean window, so the coordinator optimizes
all participants jointly against a SHARED capacity constraint that lives
inside the model — never as a post-hoc repair.

Units: power in watts, energy in watt-minutes, delay in milli-slots (see
services/coordinated_cpsat.py for why thousandths).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .loads import LoadSpec
from .scheduling import SchedulerConfig, TimeOfUseTariff


class FairnessMode(str, Enum):
    """How participant inconvenience is aggregated. Configurable; neither is
    claimed universally correct."""

    AVG = "AVG"  # minimize total inconvenience
    MAX = "MAX"  # minimize the worst-off participant (minimax)


class CoordinationMode(str, Enum):
    INDEPENDENT = "INDEPENDENT"  # each participant optimized alone (herding baseline)
    COORDINATED = "COORDINATED"  # joint optimization under shared capacity


class Participant(BaseModel):
    """A scheduling party. No personal data beyond a display label."""

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(default="", max_length=120)
    priority_weight: float = Field(default=1.0, gt=0.0)
    #: optional hard cap on this participant's mean delay, in slots. None = uncapped.
    max_inconvenience_slots: Optional[float] = Field(default=None, ge=0.0)


class SharedResource(BaseModel):
    """The common connection: hostel transformer, building feeder, campus link."""

    id: str = Field(min_length=1, max_length=120, default="building")
    name: str = Field(min_length=1, max_length=120, default="shared connection")
    capacity_kw: float = Field(gt=0.0)
    #: optional per-slot capacity in kW (length must equal slot count when given)
    capacity_profile_kw: Optional[list[float]] = None
    timestep_minutes: int = Field(default=15, gt=0)

    @field_validator("capacity_profile_kw")
    @classmethod
    def _non_negative(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is not None and any(c < 0 for c in v):
            raise ValueError("capacity profile entries must be >= 0")
        return v


class CoordinationWeights(BaseModel):
    """Soft-objective weights. Carbon stays primary; these shape the rest.

    congestion: cost of aggregate load above the utilization target, in
        mean-carbon-equivalent units per watt-slot (see coordinated_cpsat).
    inconvenience: cost of normalized participant delay, same units.
    """

    congestion: float = Field(default=1.0, ge=0.0)
    inconvenience: float = Field(default=1.0, ge=0.0)
    target_utilization: float = Field(default=0.8, gt=0.0, le=1.0)


class CoordinationRequest(BaseModel):
    participants: list[Participant] = Field(min_length=1)
    shared_resource: SharedResource
    jobs: list[LoadSpec] = Field(min_length=1)
    baseline_kw: list[float] = Field(default_factory=list)
    carbon_provider: Optional[str] = None
    carbon_start: Optional[str] = None
    carbon_end: Optional[str] = None
    carbon_resolution_minutes: int = Field(default=15, ge=5, le=60)
    coordination_mode: CoordinationMode = CoordinationMode.COORDINATED
    fairness_mode: FairnessMode = FairnessMode.AVG
    weights: CoordinationWeights = Field(default_factory=CoordinationWeights)
    solver_config: Optional[SchedulerConfig] = None
    tariff: Optional[TimeOfUseTariff] = None

    @field_validator("baseline_kw")
    @classmethod
    def _non_negative(cls, v: list[float]) -> list[float]:
        if any(b < 0 for b in v):
            raise ValueError("baseline entries must be >= 0")
        return v


class CongestionPoint(BaseModel):
    """One slot of the shared connection's state. An optimization signal
    (labeled as such), NOT a monetary tariff."""

    timestamp: datetime
    aggregate_kw: float = 0.0
    capacity_kw: float = 0.0
    utilization: float = 0.0
    congestion_score: float = 0.0


class AggregatePoint(BaseModel):
    timestamp: datetime
    baseline_kw: float = 0.0
    flexible_kw: float = 0.0
    total_kw: float = 0.0
    capacity_kw: float = 0.0
    utilization: float = 0.0
    carbon_intensity: float = 0.0
    congestion_score: float = 0.0


class ParticipantJobResult(BaseModel):
    participant_id: str
    job_id: str
    name: str
    scheduled_start: datetime
    scheduled_end: datetime
    energy_kwh: float = 0.0
    power_kw: float = 0.0
    delay_minutes: float = 0.0
    carbon_kg: float = 0.0
    reason: str = ""


class ParticipantMetrics(BaseModel):
    participant_id: str
    inconvenience_score: float = 0.0  # normalized milli-slot delay, mean per job
    delay_minutes: float = 0.0
    jobs_shifted: int = 0
    job_count: int = 0
    co2_kg: float = 0.0


class CoordinationMetrics(BaseModel):
    total_energy_kwh: Optional[float] = None
    total_co2_kg: Optional[float] = None
    peak_kw: Optional[float] = None
    capacity_violations: int = 0
    total_delay_minutes: float = 0.0
    worst_inconvenience: float = 0.0
    participant_count: int = 0
    job_count: int = 0
    solve_time_ms: Optional[int] = None


class CoordinationComparison(BaseModel):
    independent: "CoordinationResult"
    coordinated: "CoordinationResult"


class CoordinationResult(BaseModel):
    status: str
    coordination_mode: CoordinationMode
    participants: list[ParticipantMetrics] = Field(default_factory=list)
    jobs: list[ParticipantJobResult] = Field(default_factory=list)
    aggregate_profile: list[AggregatePoint] = Field(default_factory=list)
    congestion_profile: list[CongestionPoint] = Field(default_factory=list)
    metrics: CoordinationMetrics = CoordinationMetrics()
    fairness_mode: FairnessMode = FairnessMode.AVG
    solver_status: str = "UNKNOWN"
    solve_time_ms: Optional[int] = None
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)
    signal_provenance: dict = Field(default_factory=dict)


CoordinationComparison.model_rebuild()
