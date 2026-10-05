"""Execution & rescheduling domain (Phase 7).

A schedule is a plan, not a guarantee. These models track what was PLANNED
versus what ACTUALLY happened, so replanning optimizes only the remaining
future and never rewrites history.

Three vocabularies that must not be confused:

  planned   — what the optimizer decided (SchedulerResult versions)
  forecast  — what carbon was expected (Phase 5, input to planning)
  actual    — what devices really did (execution states, simulator or meters)
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from .scheduling import SchedulerInput, SchedulerResult


class ScheduleLifecycle(str, Enum):
    """Where a schedule is in its life. Transitions only move forward,
    except FAILED -> RESCHEDULED (a new version) and any -> CANCELLED."""

    DRAFT = "DRAFT"                      # built, not yet activated
    SCHEDULED = "SCHEDULED"              # accepted, awaiting start
    ACTIVE = "ACTIVE"                    # current time inside its horizon
    COMPLETED = "COMPLETED"              # horizon passed, work done
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"  # horizon passed, some jobs missed/failed
    CANCELLED = "CANCELLED"              # explicitly withdrawn
    FAILED = "FAILED"                    # infeasible/unschedulable turn
    STALE = "STALE"                      # superseded by a newer version


class JobStatus(str, Enum):
    PENDING = "PENDING"      # scheduled, not yet started
    READY = "READY"          # window open, cleared to start
    RUNNING = "RUNNING"      # physically drawing power
    PAUSED = "PAUSED"        # interruptible, intentionally held
    COMPLETED = "COMPLETED"  # requirement met
    MISSED = "MISSED"        # scheduled start passed without starting
    FAILED = "FAILED"        # could not run (device fault)
    CANCELLED = "CANCELLED"  # withdrawn by user/coordinator


class RescheduleReason(str, Enum):
    CARBON_FORECAST_CHANGED = "CARBON_FORECAST_CHANGED"
    LOAD_ADDED = "LOAD_ADDED"
    LOAD_REMOVED = "LOAD_REMOVED"
    JOB_DELAYED = "JOB_DELAYED"
    JOB_FAILED = "JOB_FAILED"
    CAPACITY_CHANGE = "CAPACITY_CHANGE"
    USER_OVERRIDE = "USER_OVERRIDE"
    MISSED_START = "MISSED_START"
    SYSTEM_RECOVERY = "SYSTEM_RECOVERY"
    PERIODIC = "PERIODIC"
    MANUAL = "MANUAL"


class ReschedulePolicy(str, Enum):
    MANUAL = "MANUAL"            # replan only when asked
    PERIODIC = "PERIODIC"        # replan every interval
    EVENT_DRIVEN = "EVENT_DRIVEN"  # replan on meaningful events
    HYBRID = "HYBRID"            # periodic + event-driven (default)


class ScheduleEventType(str, Enum):
    CARBON_FORECAST_UPDATED = "CARBON_FORECAST_UPDATED"
    JOB_ADDED = "JOB_ADDED"
    JOB_REMOVED = "JOB_REMOVED"
    JOB_STARTED = "JOB_STARTED"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_MISSED = "JOB_MISSED"
    JOB_FAILED = "JOB_FAILED"
    JOB_PAUSED = "JOB_PAUSED"
    JOB_RESUMED = "JOB_RESUMED"
    CAPACITY_CHANGED = "CAPACITY_CHANGED"
    USER_OVERRIDE = "USER_OVERRIDE"
    CLOCK_ADVANCED = "CLOCK_ADVANCED"


class OverrideCommand(str, Enum):
    START_NOW = "START_NOW"
    PAUSE = "PAUSE"
    CANCEL = "CANCEL"
    MOVE = "MOVE"
    RUN_ASAP = "RUN_ASAP"


class JobExecutionState(BaseModel):
    """Physical truth about one job. Updated by events/simulator, never by
    the optimizer directly."""

    job_id: str
    participant_id: str = ""
    status: JobStatus = JobStatus.PENDING
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None
    actual_start: Optional[datetime] = None
    actual_end: Optional[datetime] = None
    energy_delivered_kwh: float = 0.0
    expected_energy_kwh: float = 0.0
    delivered_slots: dict[int, int] = Field(default_factory=dict)  # slot -> power_w actually drawn
    last_updated: Optional[datetime] = None
    note: str = ""
    #: Provenance of the energy numbers: "SIMULATED" (deterministic replay in
    #: services/simulator.py) or "MEASURED" (real meter via telemetry push).
    #: Defaults to SIMULATED so stored records without the field still read.
    telemetry_source: str = "SIMULATED"


class ScheduleEvent(BaseModel):
    event_id: str = ""
    event_type: ScheduleEventType
    timestamp: datetime
    participant_id: str = ""
    job_id: str = ""
    payload: dict = Field(default_factory=dict)


class ScheduleChange(BaseModel):
    job_id: str
    previous_start: Optional[datetime] = None
    new_start: Optional[datetime] = None
    previous_end: Optional[datetime] = None
    new_end: Optional[datetime] = None
    change_minutes: float = 0.0
    reason: str = ""


class ScheduleVersion(BaseModel):
    version: int = 1
    created_at: datetime
    reason: RescheduleReason = RescheduleReason.MANUAL
    result: SchedulerResult
    changed_jobs: list[ScheduleChange] = Field(default_factory=list)
    carbon_estimate_kg: Optional[float] = None
    peak_kw: Optional[float] = None
    solver_status: str = "UNKNOWN"


class ExecutionConfig(BaseModel):
    """Knobs for the rolling-horizon controller. All optional with safe defaults."""

    policy: ReschedulePolicy = ReschedulePolicy.HYBRID
    horizon_minutes: int = Field(default=24 * 60, gt=0, le=7 * 24 * 60)
    reoptimization_interval_minutes: int = Field(default=15, gt=0)
    commitment_window_minutes: int = Field(default=30, ge=0)
    min_shift_minutes: int = Field(default=5, ge=0)
    change_penalty_weight: float = Field(default=0.0, ge=0.0)
    improvement_threshold_percent: float = Field(default=1.0, ge=0.0)


class ScheduleRecord(BaseModel):
    """The full auditable life of one schedule: immutable versions + live truth."""

    schedule_id: str
    lifecycle: ScheduleLifecycle = ScheduleLifecycle.DRAFT
    versions: list[ScheduleVersion] = Field(default_factory=list)
    execution: dict[str, JobExecutionState] = Field(default_factory=dict)  # job_id -> state
    scheduler_input: Optional[SchedulerInput] = None  # latest version's input
    forecast_carbon: dict[str, float] = Field(default_factory=dict)  # iso ts -> forecast used
    actual_carbon: dict[str, float] = Field(default_factory=dict)    # iso ts -> observed
    events: list[ScheduleEvent] = Field(default_factory=list)
    #: opaque engine context for replanning (scheduler name, coordination
    #: request snapshot, fairness/weights). Never exposed raw; the API
    #: surface only returns derived results.
    context: dict = Field(default_factory=dict)

    def current_version(self) -> ScheduleVersion:
        return self.versions[-1]

    def current_version_number(self) -> int:
        return self.versions[-1].version if self.versions else 0
