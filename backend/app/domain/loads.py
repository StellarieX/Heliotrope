"""Canonical load intelligence models (Phase 3, §3-§8, §13-§14, §17).

This module is the vocabulary the rest of Phase 3 speaks. It deliberately
contains NO scheduling logic: it says what a load *is* and what a future
optimizer will be handed, never what the optimizer should do.

Two rules the whole phase rests on:

1. ENERGY IS NOT DURATION (§14). `power x duration` is not a universal
   reduction. An atomic load is described by a duration, an interruptible one
   by an energy target, a thermal one by a state trajectory, a fixed one by
   baseline occupancy. `primary_requirement` below makes that structural.

2. NO FAKE PRECISION (§30). Every numeric value the system does not receive
   from the user carries a `ParameterOrigin`, and every inferred value leaves
   a human-readable `Assumption` behind. A missing `energy_required_kwh` is
   `None` (unknown), never silently 0.0 (known-to-be-zero).
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .jobs import JobType, _require_aware

# Phase 1 already established the four-way enum in domain/jobs.py. Reusing it
# (rather than declaring a near-identical one) keeps the Phase 1 `Job` contract
# and the Phase 3 `LoadSpec` on the same taxonomy.
LoadType = JobType

THERMAL_TYPES = frozenset({LoadType.THERMAL})


class LoadCategory(str, Enum):
    """Known categories. `LoadSpec.category` stays a plain `str` so legacy
    Firestore `kind` values (free text) still normalize without loss."""

    EV_CHARGING = "EV charging"
    LAUNDRY = "Laundry"
    DISHWASHING = "Dishwashing"
    WATER_HEATING = "Water heating"
    SPACE_HEATING = "Space heating"
    COOLING = "Cooling"
    PUMPING = "Pumping"
    REFRIGERATION = "Refrigeration"
    ALWAYS_ON = "Always-on"
    BATTERY = "Battery storage"
    UNKNOWN = "Unknown"


class ParameterOrigin(str, Enum):
    """Where a physical number came from. Surfaced in the API so the UI can
    say "estimated" instead of presenting a guess as a measurement."""

    USER_CONFIGURED = "user-configured"
    SYNTHETIC_DEFAULT = "synthetic default"
    ESTIMATED = "estimated"
    DERIVED = "derived"
    LEGACY_INFERRED = "legacy-inferred"


class Assumption(BaseModel):
    """One machine-readable + human-readable note about how a field was set."""

    field: str = Field(min_length=1)
    origin: ParameterOrigin
    detail: str = Field(min_length=1)

    def as_sentence(self) -> str:
        return f"{self.field}: {self.detail}"


class RequiredField(str, Enum):
    """The fields a user must supply for a class to be schedulable.

    Drives progressive disclosure in the UI (§25): only ask for what the
    class actually needs, so an ordinary user never sees thermal coefficients.
    """

    POWER = "power_kw"
    MAX_POWER = "max_power_kw"
    DURATION = "duration_minutes"
    ENERGY = "energy_required_kwh"
    MIN_CHUNK = "min_chunk_minutes"
    RELEASE = "release_at"
    DEADLINE = "deadline_at"
    TEMPERATURE_BAND = "temperature_band"
    THERMAL_COEFFICIENTS = "thermal_coefficients"
    OCCUPANCY = "occupancy_window"


# --- Scheduling semantics (§13) -------------------------------------------


class DecisionVariable(str, Enum):
    """What the future optimizer is allowed to decide for this class."""

    NONE = "none"  # load[t] = baseline
    START = "start"  # binary start[j, t]
    POWER = "power"  # 0 <= power[j, t] <= max_power
    POWER_AND_STATE = "power+state"  # power[j, t] with T[t+1] = aT[t] + bP[t] + c


class PrimaryRequirement(str, Enum):
    """What the class is actually required to deliver (§14)."""

    BASELINE = "baseline_occupancy"
    DURATION = "duration"
    ENERGY = "energy"
    STATE_TRAJECTORY = "state_trajectory"


class LoadSemantics(BaseModel):
    """A class's scheduling semantics, stated once and reusable by the UI,
    the API, and (in Phase 4) the solver's variable builder."""

    job_type: LoadType
    shiftable: bool
    decision_variable: DecisionVariable
    primary_requirement: PrimaryRequirement
    constraints: list[str] = Field(default_factory=list)
    notes: str = ""


def semantics_for(job_type: LoadType) -> LoadSemantics:
    """The single source of truth for §13 semantics."""
    if job_type is LoadType.FIXED:
        return LoadSemantics(
            job_type=job_type,
            shiftable=False,
            decision_variable=DecisionVariable.NONE,
            primary_requirement=PrimaryRequirement.BASELINE,
            constraints=["load[t] = baseline"],
            notes="Cannot be shifted or paused; contributes background load, not an optimization opportunity.",
        )
    if job_type is LoadType.DEFERRABLE_ATOMIC:
        return LoadSemantics(
            job_type=job_type,
            shiftable=True,
            decision_variable=DecisionVariable.START,
            primary_requirement=PrimaryRequirement.DURATION,
            constraints=[
                "start[j, t] in {0, 1}",
                "sum_t start[j, t] = 1",
                "release_at <= start <= deadline_at - duration",
            ],
            notes="Movable start time; runs continuously once started; cannot be paused.",
        )
    if job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
        return LoadSemantics(
            job_type=job_type,
            shiftable=True,
            decision_variable=DecisionVariable.POWER,
            primary_requirement=PrimaryRequirement.ENERGY,
            constraints=[
                "sum_t power[j, t] * slot_hours >= energy_required_kwh",
                "0 <= power[j, t] <= max_power_kw",
                "power[j, t] > 0 only for whole multiples of min_chunk_minutes",
            ],
            notes="Can pause and resume. Energy is the primary requirement, not a fixed duration.",
        )
    return LoadSemantics(
        job_type=job_type,
        shiftable=True,
        decision_variable=DecisionVariable.POWER_AND_STATE,
        primary_requirement=PrimaryRequirement.STATE_TRAJECTORY,
        constraints=[
            "T[t+1] = a*T[t] + b*P[t] + c",
            "temperature_min_c <= T[t] <= temperature_max_c",
            "T[service_time] >= temperature_target_c",
        ],
        notes="Stores useful energy as thermal state; the scheduler buys state, not a start time.",
    )


# --- Recurrence (§16) ------------------------------------------------------


class Frequency(str, Enum):
    DAILY = "daily"
    WEEKLY = "weekly"


class Recurrence(BaseModel):
    """Optional. Present so recurring loads are representable without making
    the Phase 3 (single-window) engine pretend to support them."""

    frequency: Frequency
    interval: int = Field(default=1, gt=0)
    active_days: list[int] = Field(default_factory=list)
    timezone: str = Field(min_length=1, default="UTC")

    @field_validator("active_days")
    @classmethod
    def _valid_days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("active_days entries must be 0..6 (Monday=0)")
        return sorted(set(v))


def required_fields_for(job_type: LoadType) -> list[RequiredField]:
    """What a user must supply for a class to be schedulable.

    Lives in the domain because two different normalizers need it and they must
    agree: a user who overrides the class by hand gets the same question list
    whether the override came from an API request or a stored Firestore field.
    """
    if job_type is LoadType.FIXED:
        return [RequiredField.POWER, RequiredField.OCCUPANCY]
    if job_type is LoadType.DEFERRABLE_ATOMIC:
        return [RequiredField.POWER, RequiredField.DURATION, RequiredField.DEADLINE]
    if job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
        return [RequiredField.ENERGY, RequiredField.MAX_POWER, RequiredField.DEADLINE]
    return [RequiredField.DEADLINE, RequiredField.TEMPERATURE_BAND]


# --- Thermal parameters (§4, §5) -------------------------------------------


class ThermalSpec(BaseModel):
    """Abstract first-order thermal dynamics: T[t+1] = a*T[t] + b*P[t] + c.

    The coefficients are intentionally abstract. `a` is decay, `b` is the
    signed sensitivity of state to electrical input (positive for a heater
    storing heat, negative for a cooler removing it), `c` is ambient drift.
    No claim is made that these describe any real appliance; the defaults are
    labeled `synthetic default` wherever they are used.
    """

    a: float = Field(ge=0.0, le=1.0)
    b: float
    c: float = 0.0
    max_power_kw: float = Field(ge=0.0)
    resolution_minutes: int = Field(default=15, gt=0)
    temperature_initial_c: Optional[float] = None
    temperature_min_c: float
    temperature_max_c: float
    temperature_target_c: Optional[float] = None

    @field_validator("a", "b", "c", "max_power_kw", "temperature_initial_c", "temperature_min_c", "temperature_max_c", "temperature_target_c")
    @classmethod
    def _finite(cls, v: Optional[float], info) -> Optional[float]:
        if v is not None and not math.isfinite(v):
            raise ValueError(f"{info.field_name} must be a finite number")
        return v

    @field_validator("b")
    @classmethod
    def _non_degenerate(cls, v: float) -> float:
        if v == 0.0:
            raise ValueError(
                "thermal_b must be non-zero: b = 0 means electrical input has no "
                "effect on the thermal state, so the load is not thermal"
            )
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "ThermalSpec":
        if self.temperature_min_c > self.temperature_max_c:
            raise ValueError("temperature_min_c must be <= temperature_max_c")
        if self.temperature_target_c is not None:
            if not (self.temperature_min_c <= self.temperature_target_c <= self.temperature_max_c):
                raise ValueError(
                    "temperature_target_c must lie inside [temperature_min_c, temperature_max_c]"
                )
        if self.temperature_initial_c is not None and self.a == 0.0 and self.b == 0.0:
            raise ValueError("a = 0 and b = 0 leaves the thermal state undefined")
        return self

    def band_text(self) -> str:
        return f"{self.temperature_min_c:g}°C–{self.temperature_max_c:g}°C"


# --- The canonical load spec (§8) ------------------------------------------


class LoadSpec(BaseModel):
    """The rich intermediate model between a user sentence and a job.

    Every optional field is Optional because "unknown" and "zero" are different
    claims, and Heliotrope must not confuse them (§19, §21).
    """

    id: str = Field(default="", max_length=120)
    user_input: str = Field(default="", max_length=400)
    normalized_name: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    job_type: LoadType
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)
    ambiguous: bool = False

    # Physical requirements — None means UNKNOWN, not zero.
    power_kw: Optional[float] = Field(default=None, ge=0.0)
    max_power_kw: Optional[float] = Field(default=None, ge=0.0)
    duration_minutes: Optional[int] = Field(default=None, gt=0)
    energy_required_kwh: Optional[float] = Field(default=None, ge=0.0)
    min_chunk_minutes: Optional[int] = Field(default=None, gt=0)
    release_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    timezone: str = Field(default="UTC", min_length=1)
    recurrence: Optional[Recurrence] = None
    thermal: Optional[ThermalSpec] = None

    # Provenance and explainability (§20, §30).
    explanation: str = Field(default="", max_length=600)
    assumptions: list[Assumption] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    required_fields: list[RequiredField] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)

    # Derived; set in _derive(). Kept as fields so they serialize for the UI.
    shiftable: bool = True
    interruptible: bool = False

    @field_validator("release_at", "deadline_at")
    @classmethod
    def _aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        return None if v is None else _require_aware(v, "datetime")

    @field_validator("duration_minutes", "min_chunk_minutes")
    @classmethod
    def _finite_int(cls, v: Optional[int]) -> Optional[int]:
        return v

    @model_validator(mode="after")
    def _coherent(self) -> "LoadSpec":
        if self.release_at is not None and self.deadline_at is not None:
            if self.release_at > self.deadline_at:
                raise ValueError("release_at must be <= deadline_at")
        if self.max_power_kw is not None and self.power_kw is not None:
            if self.power_kw > self.max_power_kw:
                raise ValueError("power_kw must be <= max_power_kw")
        # A THERMAL load with no dynamics yet is a legitimate INCOMPLETE spec,
        # not an invalid one: the user has said what the appliance is and has
        # not supplied a comfort band. core.feasibility reports the missing
        # field as a structured, quotable error, which beats a hard parse
        # failure here. (The stricter Phase 1 `Job` rule is untouched.)
        if self.job_type is not LoadType.THERMAL and self.thermal is not None:
            raise ValueError("thermal parameters are only meaningful for THERMAL loads")
        return self

    def model_post_init(self, __context) -> None:
        self._derive()

    def _derive(self) -> None:
        """Apply the taxonomy invariants that are not user choices."""
        sem = semantics_for(self.job_type)
        self.shiftable = sem.shiftable
        self.interruptible = self.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE

    # --- semantics helpers -------------------------------------------------

    @property
    def semantics(self) -> LoadSemantics:
        return semantics_for(self.job_type)

    @property
    def primary_requirement(self) -> PrimaryRequirement:
        return self.semantics.primary_requirement

    def window_minutes(self) -> Optional[float]:
        if self.release_at is None or self.deadline_at is None:
            return None
        return (self.deadline_at - self.release_at).total_seconds() / 60.0

    def effective_max_power_kw(self) -> Optional[float]:
        """The power ceiling the future optimizer would be bounded by."""
        candidates = [p for p in (self.max_power_kw, self.power_kw) if p is not None]
        if not candidates:
            return self.thermal.max_power_kw if self.thermal else None
        if self.thermal is not None:
            candidates.append(self.thermal.max_power_kw)
        return max(candidates)

    def min_runtime_minutes(self) -> Optional[float]:
        """Minimum contiguous time the load must draw power (§13 semantics)."""
        if self.min_chunk_minutes:
            return float(self.min_chunk_minutes)
        return 1.0 if self.job_type is not LoadType.FIXED else 0.0

    def derived_energy_kwh(self) -> Optional[float]:
        """Energy implied by power x duration.

        Only meaningful for ATOMIC. Interruptible loads are the reverse (energy
        is given, duration follows); THERMAL loads have no meaningful total.
        """
        if self.job_type is not LoadType.DEFERRABLE_ATOMIC:
            return None
        if self.power_kw is None or not self.duration_minutes:
            return None
        return self.power_kw * self.duration_minutes / 60.0

    def peak_contribution_kw(self) -> Optional[float]:
        """Load's maximum instantaneous draw — an input to future peak
        metrics, not a computed metric (§28)."""
        return self.effective_max_power_kw()

    def minimum_runtime_hours_for_energy(self) -> Optional[float]:
        """How long the load must be ON to hit its energy target."""
        power = self.effective_max_power_kw()
        if power is None or power <= 0 or self.energy_required_kwh is None:
            return None
        return self.energy_required_kwh / power

    def assumptions_for(self, field: str) -> list[Assumption]:
        """Every note recorded about one field."""
        return [a for a in self.assumptions if a.field == field]

    def assumption_origin(self, field: str) -> Optional[ParameterOrigin]:
        """Where a field's value came from, or None if nothing was assumed.

        Lets a caller ask "is this number real?" without parsing prose.
        """
        matches = self.assumptions_for(field)
        return matches[0].origin if matches else None

    def metric_inputs(self) -> dict:
        """The load-level metadata future phases need to compute energy, carbon,
        cost, peak contribution and delay (§28). Values only — no metric is
        computed here, because no schedule exists yet."""
        return {
            "load_id": self.id or self.normalized_name,
            "job_type": self.job_type.value,
            "peak_contribution_kw": self.peak_contribution_kw(),
            "energy_required_kwh": self.energy_required_kwh,
            "derived_energy_kwh": self.derived_energy_kwh(),
            "window_minutes": self.window_minutes(),
            "release_at": self.release_at.isoformat() if self.release_at else None,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
        }

    def missing_required_fields(self) -> list[RequiredField]:
        """Which user inputs this class still needs before a schedule exists."""
        missing: list[RequiredField] = []
        if self.release_at is None:
            missing.append(RequiredField.RELEASE)
        if self.deadline_at is None:
            missing.append(RequiredField.DEADLINE)
        if self.job_type is LoadType.DEFERRABLE_ATOMIC:
            if self.power_kw is None:
                missing.append(RequiredField.POWER)
            if self.duration_minutes is None:
                missing.append(RequiredField.DURATION)
        elif self.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
            if self.energy_required_kwh is None:
                missing.append(RequiredField.ENERGY)
            if self.effective_max_power_kw() is None:
                missing.append(RequiredField.MAX_POWER)
        elif self.job_type is LoadType.THERMAL:
            if self.thermal is None:
                missing.append(RequiredField.THERMAL_COEFFICIENTS)
            elif self.thermal.temperature_initial_c is None:
                missing.append(RequiredField.TEMPERATURE_BAND)
        elif self.job_type is LoadType.FIXED:
            if self.power_kw is None:
                missing.append(RequiredField.POWER)
            missing.append(RequiredField.OCCUPANCY)
        return missing

    def is_schedulable(self) -> bool:
        return not self.missing_required_fields()

    def assumed_window(self, default_hours: int = 12) -> tuple[Optional[datetime], Optional[datetime]]:
        """Fallback window used only for display; never a feasibility input."""
        if self.release_at is not None and self.deadline_at is not None:
            return self.release_at, self.deadline_at
        return None, None

    def expiry_check(self, now: datetime) -> Optional[str]:
        if self.deadline_at is None:
            return None
        if self.deadline_at < now:
            return f"deadline {self.deadline_at.isoformat()} is already in the past"
        return None
