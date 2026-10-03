"""Scheduler-facing contracts (Phase 4, §2, §16, §21, §28, §36, §37, §50, §51).

These are the types that cross the scheduler boundary. A scheduler receives a
`SchedulerInput` — already normalized, already in slot space, already in integer
solver units — and returns a `SchedulerResult`. No scheduler may look at a raw
`LoadSpec`, a frontend `readyBy` string, or a wall-clock timezone; by the time a
scheduler runs, that ambiguity is gone.

Two rules hold everywhere in this module:

1. NO CLAIMED OPTIMALITY WITHOUT PROOF. `SolverStatus.OPTIMAL` is only ever set
   from a real solver status. `FEASIBLE` means a solution was found and
   validated; it says nothing about quality.

2. NO FABRICATED METRICS. Every number in `ScheduleMetrics` comes from
   `CarbonAccountingService` applied to the actual produced schedule. A field
   that was not computed stays `None` rather than defaulting to zero, because a
   zero would read as "measured, and it was zero".
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .forecasting import ForecastMode
from .horizon import SchedulingHorizon
from .loads import LoadType
from .scaling import ThermalScale


# --- solver status (§21) ----------------------------------------------------


class SolverStatus(str, Enum):
    """What the engine actually knows. Never upgraded optimistically."""

    OPTIMAL = "OPTIMAL"          # proven optimal within the objective
    FEASIBLE = "FEASIBLE"        # a valid solution was found and validated
    INFEASIBLE = "INFEASIBLE"    # proven: no solution satisfies the hard constraints
    UNKNOWN = "UNKNOWN"          # ran out of time with nothing to show
    NO_FEASIBLE_SOLUTION = "NO_FEASIBLE_SOLUTION"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ScheduleStatus(str, Enum):
    """Result-level status, slightly coarser than the solver's own."""

    FEASIBLE = "FEASIBLE"
    OPTIMAL = "OPTIMAL"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# --- objective configuration (§16, §18, §46) ------------------------------


class ObjectiveWeights(BaseModel):
    """Relative weights on the soft objective terms.

    DEFAULT WEIGHTS (documented, §16):

        carbon = 1.0, peak = 0.0, delay = 0.0, cost = 0.0

    With the defaults the objective is pure carbon, which is the honest default:
    any non-zero penalty changes which schedule is "best", and Heliotrope has no
    user research telling us what a minute of delay is worth.

    The peak and delay terms are NORMALIZED before they enter the model, so a
    weight of 1.0 is meaningful rather than arbitrary:

      * peak:  one watt of extra peak is charged as if it ran for the whole
                horizon at the horizon's MEAN carbon intensity
      * delay: one slot of delay for a job is charged as the carbon cost of
                running that job for one more slot

    That keeps the weights unit-free and comparable, so turning one on does not
    require re-tuning the others.
    """

    carbon: float = Field(default=1.0, ge=0.0)
    peak: float = Field(default=0.0, ge=0.0)
    delay: float = Field(default=0.0, ge=0.0)
    cost: float = Field(default=0.0, ge=0.0)

    def is_pure_carbon(self) -> bool:
        return self.peak == 0 and self.delay == 0 and self.cost == 0


class SchedulerConfig(BaseModel):
    """Runtime knobs. Defaults favour a responsive API over a long solve."""

    time_limit_seconds: float = Field(default=5.0, gt=0.0, le=600.0)
    num_workers: int = Field(default=1, ge=1, le=64)
    random_seed: int = Field(default=0, ge=0)
    relative_gap_limit: float = Field(default=0.0, ge=0.0, le=1.0)


class TimeOfUseTariff(BaseModel):
    """Optional price signal, kept strictly separate from carbon (§27).

    Cheaper is not cleaner: a cheap coal-heavy night is still coal-heavy, so
    cost is a separate objective term with its own weight and never a proxy.
    """

    #: micro-units of currency per kWh, one entry per slot, length == slot_count
    price_micro_per_kwh: list[int]

    @field_validator("price_micro_per_kwh")
    @classmethod
    def _non_negative(cls, v: list[int]) -> list[int]:
        if any(p < 0 for p in v):
            raise ValueError("tariff prices must be >= 0")
        return v


# --- normalized input (§4) --------------------------------------------------


class NormalizedJob(BaseModel):
    """One job in slot space, in integer solver units.

    Slot indices are zero-based into `SchedulerInput.horizon`. `deadline_slot`
    is EXCLUSIVE: a job may occupy slots `release_slot .. deadline_slot - 1`.
    """

    id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    #: owner in multi-user runs; "" means unassigned/single-user. Added in
    #: Phase 6 with a default so every Phase 4/5 input still validates.
    participant_id: str = Field(default="", max_length=120)
    job_type: LoadType
    power_w: int = Field(ge=0)
    max_power_w: int = Field(ge=0)
    energy_required_wmin: Optional[int] = Field(default=None, ge=0)
    duration_slots: Optional[int] = Field(default=None, gt=0)
    min_chunk_slots: int = Field(default=1, ge=1)
    release_slot: int = Field(ge=0)
    deadline_slot: int = Field(gt=0)
    thermal: Optional[ThermalScale] = None
    #: the grid this job was normalized onto, so per-slot arithmetic never
    #: hardcodes 15 minutes
    slot_minutes: int = 15
    #: kept for explanations and accounting labels
    category: str = ""
    shiftable: bool = True

    def __hash__(self) -> int:  # pragma: no cover - convenience only
        return hash((self.id, self.release_slot, self.deadline_slot))

    def slots(self) -> range:
        return range(self.release_slot, self.deadline_slot)

    def window_slots(self) -> int:
        return max(0, self.deadline_slot - self.release_slot)

    def energy_kwh(self) -> Optional[float]:
        if self.energy_required_wmin is None:
            return None
        return self.energy_required_wmin / WMIN_PER_KWH

    def minimum_slots(self) -> int:
        """Smallest number of slots this job must occupy."""
        if self.job_type is LoadType.DEFERRABLE_ATOMIC:
            return self.duration_slots or 0
        if self.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
            if self.energy_required_wmin is None or self.max_power_w <= 0:
                return 0
            slots = -(-self.energy_required_wmin // (self.max_power_w * self.slot_minutes))
            return max(slots, self.min_chunk_slots)
        if self.job_type is LoadType.THERMAL:
            return 1
        return 0

    def max_energy_wmin(self) -> Optional[int]:
        if self.energy_required_wmin is not None:
            return self.energy_required_wmin
        if self.max_power_w <= 0:
            return None
        return self.max_power_w * self.slot_minutes * self.window_slots()


class BaselineProfile(BaseModel):
    """FIXED load, per slot, in watts.

    §5: this is subtracted from capacity before any flexible job is placed. It
    is not an optimization opportunity and it never gets shifted.
    """

    power_w: list[int]
    source: str = "fixed-loads"

    def __len__(self) -> int:
        return len(self.power_w)

    def at(self, slot: int) -> int:
        return self.power_w[slot] if 0 <= slot < len(self.power_w) else 0

    def total_kwh(self, slot_minutes: int) -> float:
        return sum(self.power_w) * slot_minutes / WMIN_PER_KWH

    def peak_w(self) -> int:
        return max(self.power_w) if self.power_w else 0


class CarbonProfile(BaseModel):
    """Carbon intensity per slot, in integer gCO2/kWh, plus its provenance.

    §37: the provenance travels with the numbers. A schedule produced from a
    SYNTHETIC signal must never be reported without saying so.
    """

    gco2_per_kwh: list[int]
    signal_type: str = "SYNTHETIC"
    source: str = "unknown"
    is_forecast: bool = False

    def __len__(self) -> int:
        return len(self.gco2_per_kwh)

    def at(self, slot: int) -> int:
        return self.gco2_per_kwh[slot] if 0 <= slot < len(self.gco2_per_kwh) else 0

    def mean(self) -> float:
        return sum(self.gco2_per_kwh) / len(self.gco2_per_kwh) if self.gco2_per_kwh else 0.0

    def total_weighted(self) -> float:
        return sum(self.gco2_per_kwh)


class UncertaintyProfile(BaseModel):
    """Per-slot forecast uncertainty, in integer gCO2/kWh (§9, §16).

    `upper_gco2_per_kwh` is the upper end of the empirical prediction interval at
    each slot. Only the upper side is carried, because that is what ROBUST mode
    needs: the risk-adjusted cost is

        forecast + risk_weight * (upper - forecast)

    which is the expected cost plus a penalty proportional to how uncertain that
    slot is. The lower bound is not needed by any current mode, and carrying an
    unused field invites someone to read it as a guarantee it is not (§9).
    """

    upper_gco2_per_kwh: list[int] = Field(default_factory=list)

    def __len__(self) -> int:
        return len(self.upper_gco2_per_kwh)

    def upper_at(self, slot: int) -> Optional[int]:
        if 0 <= slot < len(self.upper_gco2_per_kwh):
            return self.upper_gco2_per_kwh[slot]
        return None


class SchedulerInput(BaseModel):
    """Everything a scheduler is allowed to see. Fully normalized (§4).

    TWO CARBON VIEWS, AND THE DIFFERENCE IS THE WHOLE OF PHASE 5 (§2, §19).

        carbon          — the OBSERVED signal. Every emissions number in
                          `ScheduleMetrics` is computed against this, because
                          realized CO2 has to be measured against what actually
                          happened, not against what we predicted (§35).
        objective_carbon — what a scheduler should MINIMIZE. Identical to
                          `carbon` in ACTUAL mode. In EXPECTED mode it is the
                          point forecast; in ROBUST mode it is
                          `forecast + risk_weight * (upper - forecast)`.

    `objective_carbon()` is the single accessor every scheduler uses for its
    objective coefficients, so all three engines consume forecast uncertainty
    the same way and none of them can accidentally read the observed signal
    while claiming to have optimized against a forecast.

    THE INVARIANT THAT MAKES THIS SAFE (§19): uncertainty reaches the OBJECTIVE
    and nothing else. Release, deadline, energy, capacity, thermal comfort and
    atomicity are all read from `jobs`, `baseline` and `capacity_w`, none of
    which this profile can reach. There is no code path by which a wider
    interval makes a deadline easier.
    """

    horizon: SchedulingHorizon
    jobs: list[NormalizedJob] = Field(default_factory=list)
    baseline: BaselineProfile
    carbon: CarbonProfile
    capacity_w: int = Field(gt=0)
    objective: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    tariff: Optional[TimeOfUseTariff] = None
    #: §16, §23: how the objective carbon numbers were derived
    forecast_mode: "ForecastMode" = ForecastMode.ACTUAL
    risk_weight: float = 0.0
    #: per-slot upper prediction bound; None in ACTUAL mode
    uncertainty: Optional[UncertaintyProfile] = None
    #: the forecast these objective numbers came from, for explanations (§34)
    forecast_provenance: Optional[dict] = None

    def objective_carbon(self) -> "CarbonProfile":
        """The per-slot gCO2/kWh a scheduler minimizes against (§15, §16).

        In ACTUAL mode this is the observed profile itself, byte for byte, so the
        Phase 4 deterministic path is bit-for-bit unchanged.
        """
        if self.forecast_mode is ForecastMode.ACTUAL or self.uncertainty is None:
            return self.carbon
        adjusted: list[int] = []
        for slot, predicted in enumerate(self.carbon.gco2_per_kwh):
            upper = self.uncertainty.upper_at(slot)
            if upper is None:
                adjusted.append(predicted)
            elif self.forecast_mode is ForecastMode.ROBUST:
                # risk_weight is applied in float and rounded once, then stored as
                # an integer so the CP-SAT model keeps its integer coefficients.
                adjusted.append(
                    int(predicted + self.risk_weight * (upper - predicted) + 0.5)
                )
            else:
                adjusted.append(predicted)
        return CarbonProfile(
            gco2_per_kwh=adjusted,
            signal_type=self.carbon.signal_type,
            source=self.carbon.source,
            is_forecast=True,
        )

    def job(self, job_id: str) -> NormalizedJob:
        for j in self.jobs:
            if j.id == job_id:
                return j
        raise KeyError(f"no job {job_id!r} in this input")

    def flexible_jobs(self) -> list[NormalizedJob]:
        return [j for j in self.jobs if j.job_type is not LoadType.FIXED]

    def headroom_w(self, slot: int) -> int:
        """Power available to flexible loads at `slot` (§5)."""
        return self.capacity_w - self.baseline.at(slot)

    def signal_provenance(self) -> dict:
        return {
            "signal_type": self.carbon.signal_type,
            "source": self.carbon.source,
            "is_forecast": self.carbon.is_forecast,
        }


# --- output (§36, §50, §51) ------------------------------------------------


class SlotAllocation(BaseModel):
    """One slot of a flexible job's actual allocation."""

    slot: int = Field(ge=0)
    timestamp: datetime
    power_w: int = Field(ge=0)

    @property
    def power_kw(self) -> float:
        return self.power_w / 1000.0


class TemperatureSample(BaseModel):
    """One step of a thermal trajectory, for the future Gantt view (§50)."""

    slot: int = Field(ge=0)
    timestamp: datetime
    temperature_c: float
    power_w: int


class ScheduledJob(BaseModel):
    """One placed job. The profile is the schedule's source of truth (§26)."""

    job_id: str
    name: str
    job_type: LoadType
    start_time: datetime
    end_time: datetime
    start_slot: int = Field(ge=0)
    end_slot: int = Field(ge=0)
    energy_kwh: float = Field(ge=0)
    peak_power_kw: float = Field(ge=0)
    allocations: list[SlotAllocation] = Field(default_factory=list)
    temperature: list[TemperatureSample] = Field(default_factory=list)
    reason_code: str = "SCHEDULED"
    reason: str = ""


class ReasonCode(str, Enum):
    EARLIEST = "EARLIEST_FEASIBLE"
    LOWEST_CARBON = "LOWER_CARBON_WINDOW"
    LOWEST_CARBON_ALLOCATION = "LOWER_CARBON_ALLOCATION"
    CAPACITY_STAGGERED = "CAPACITY_STAGGERED"
    CONGESTION_SHIFTED = "CONGESTION_SHIFTED"
    THERMAL_PRECONDITIONING = "THERMAL_PRECONDITIONING"
    UNCHANGED = "UNCHANGED"
    SCHEDULED = "SCHEDULED"


class JobExplanation(BaseModel):
    """Per-job explainability (§31, §51).

    Every number is computed by accounting over the real ASAP counterfactual and
    the real optimized schedule. Nothing here is illustrative.
    """

    job_id: str
    name: str
    job_type: LoadType
    original_start: datetime
    original_end: datetime
    scheduled_start: datetime
    scheduled_end: datetime
    deadline_at: datetime
    shifted_slots: int = 0
    energy_kwh: float = 0.0
    co2_before_kg: float = 0.0
    co2_after_kg: float = 0.0
    co2_saved_kg: float = 0.0
    deadline_preserved: bool = True
    reason_code: ReasonCode = ReasonCode.SCHEDULED
    reason: str = ""


class ScheduleMetrics(BaseModel):
    """Every metric, computed once by the accounting service (§25, §28).

    Optional fields are Optional on purpose: Phase 4 computes all of these for a
    real schedule, but a partial or failed run must not invent them.
    """

    total_energy_kwh: Optional[float] = None
    total_co2_kg: Optional[float] = None
    co2_saved_kg: Optional[float] = None
    co2_saved_percent: Optional[float] = None
    peak_kw: Optional[float] = None
    baseline_peak_kw: Optional[float] = None
    peak_reduction_kw: Optional[float] = None
    energy_cost: Optional[float] = None
    co2_cost: Optional[float] = None
    cost_delta: Optional[float] = None
    deadline_misses: Optional[int] = None
    feasibility_violations: Optional[int] = None
    solve_time_ms: Optional[int] = None


class SolverInfo(BaseModel):
    """Solver provenance. Fields appear only when the solver supplied them."""

    name: str
    status: SolverStatus = SolverStatus.UNKNOWN
    solve_time_ms: Optional[int] = None
    objective_value: Optional[float] = None
    best_bound: Optional[float] = None
    optimality_gap: Optional[float] = None
    num_workers: Optional[int] = None
    random_seed: Optional[int] = None
    time_limit_seconds: Optional[float] = None
    #: true only when the solver PROVED optimality
    is_optimal: bool = False


class CarbonProvenance(BaseModel):
    """§37: which signal produced this schedule, carried on the result."""

    signal_type: str
    source: str
    is_forecast: bool = False
    slot_count: int = 0
    mean_gco2_per_kwh: float = 0.0


class SchedulerResult(BaseModel):
    """The full result of one scheduler run."""

    status: ScheduleStatus
    scheduler: str
    schedule: list[ScheduledJob] = Field(default_factory=list)
    metrics: ScheduleMetrics = ScheduleMetrics()
    solver: SolverInfo = SolverInfo(name="unknown")
    explanations: list[JobExplanation] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)
    reason: str = ""
    signal: Optional[CarbonProvenance] = None
    horizon: Optional[SchedulingHorizon] = None
    #: per-slot total load in watts, including baseline — for §50 visualization
    slot_load_w: list[int] = Field(default_factory=list)
    baseline_slot_load_w: list[int] = Field(default_factory=list)


class SchedulerComparison(BaseModel):
    """§24: several schedulers over byte-identical input."""

    reference_scheduler: str = "ASAP"
    results: dict[str, SchedulerResult] = Field(default_factory=dict)
    input_fingerprint: str = ""
    best_co2_scheduler: Optional[str] = None
    co2_saved_vs_reference: dict[str, Optional[float]] = Field(default_factory=dict)
    peak_reduction: dict[str, Optional[float]] = Field(default_factory=dict)
    cost_delta: dict[str, Optional[float]] = Field(default_factory=dict)
