"""Execution & rescheduling API (Phase 7).

  POST /api/v1/schedules/plan                 single-user plan -> v1 record
  POST /api/v1/schedules/plan-coordinated     building plan -> v1 record
  GET  /api/v1/schedules/{id}/state           live execution truth
  GET  /api/v1/schedules/{id}/history         immutable versions + diffs
  POST /api/v1/schedules/{id}/events          apply an event (policy may auto-replan)
  POST /api/v1/schedules/{id}/replan          manual/periodic replan over remainders
  POST /api/v1/schedules/{id}/tick            time-driven clock advance + periodic auto-replan
  POST /api/v1/schedules/{id}/override         guarded user override
  POST /api/v1/simulation/advance             deterministic simulated execution

A version is only appended when the candidate actually changes something
worth keeping; otherwise the current version stands and the response says so.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import math

from ...domain.coordination import CoordinationMode, CoordinationRequest
from ...domain.execution import (
    ExecutionConfig,
    JobExecutionState,
    JobStatus,
    OverrideCommand,
    ReschedulePolicy,
    RescheduleReason,
    ScheduleEvent,
    ScheduleEventType,
    ScheduleLifecycle,
    ScheduleRecord,
)
from ...domain.loads import LoadSpec, LoadType
from ...domain.scheduling import BaselineProfile, SchedulerConfig, SchedulerInput
from ...services.carbon_service import CarbonBadRequest, CarbonUnavailable
from ...services.coordinator import CoordinationError, MultiUserCoordinator
from ...services.execution_events import apply_event, check_override, transition
from ...services.execution_store import ExecutionStore, utcnow
from ...services.load_pool import attach_pool, pool_summary
from ...services.meter_provider import InMemoryMeterProvider
from ...services.receding import RecedingHorizon, RemainingInfeasible, now_slot
from ...services.scheduler_normalizer import NormalizationError
from ...services.scheduler_service import SchedulerService
from ...services.schedulers import SchedulerName
from ...services.simulator import ExecutionSimulator
from .schedule import ScheduleRequest, _prepare

router = APIRouter()
store = ExecutionStore()
service = SchedulerService()
coordinator = MultiUserCoordinator()
simulator = ExecutionSimulator()
meters = InMemoryMeterProvider()


# --- helpers --------------------------------------------------------------


def _err(detail: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "message": detail},
    )


def _require_aware_dt(v: Optional[datetime], field: str) -> Optional[datetime]:
    """Reject naive datetimes at the API boundary (no silent UTC assumption)."""
    if v is None:
        return v
    if v.tzinfo is None:
        raise ValueError(
            f"{field} must be timezone-aware; a naive timestamp would be interpreted "
            "against the server's local zone, which silently shifts execution"
        )
    return v


def _record_or_404(schedule_id: str):
    record = store.get(schedule_id)
    if record is None:
        return None, _err("unknown schedule_id", "not_found", 404)
    return record, None


def _placement_of(record: ScheduleRecord) -> dict[str, dict[int, int]]:
    out: dict[str, dict[int, int]] = {}
    for scheduled in record.current_version().result.schedule:
        out[scheduled.job_id] = {a.slot: a.power_w for a in scheduled.allocations}
    return out


def _states_maps(record: ScheduleRecord):
    states = {jid: st.status for jid, st in record.execution.items()}
    delivered = {jid: dict(st.delivered_slots) for jid, st in record.execution.items()}
    kwh = {jid: st.energy_delivered_kwh for jid, st in record.execution.items()}
    return states, delivered, kwh


def _refresh_lifecycle(record: ScheduleRecord, now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if record.scheduler_input is None or not record.versions:
        return
    horizon = record.scheduler_input.horizon
    if now >= horizon.end:
        done = all(
            s.status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED)
            for s in record.execution.values()
        )
        record.lifecycle = (
            ScheduleLifecycle.COMPLETED if done else ScheduleLifecycle.PARTIALLY_COMPLETED
        )
    elif record.lifecycle in (ScheduleLifecycle.SCHEDULED, ScheduleLifecycle.DRAFT):
        if any(
            s.status in (JobStatus.RUNNING, JobStatus.COMPLETED)
            for s in record.execution.values()
        ):
            record.lifecycle = ScheduleLifecycle.ACTIVE


def _state_payload(record: ScheduleRecord) -> dict:
    v = record.current_version()
    base = record.scheduler_input
    deadlines = (
        {j.id: base.horizon.slot_end(j.deadline_slot - 1).isoformat() for j in base.jobs}
        if base is not None
        else {}
    )
    return {
        "schedule_id": record.schedule_id,
        "lifecycle": record.lifecycle.value,
        "version": v.version,
        "solver_status": v.solver_status,
        "jobs": [
            {
                "job_id": s.job_id,
                "participant_id": s.participant_id,
                "status": s.status.value,
                "scheduled_start": s.scheduled_start.isoformat() if s.scheduled_start else None,
                "scheduled_end": s.scheduled_end.isoformat() if s.scheduled_end else None,
                "deadline_at": deadlines.get(s.job_id),
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "expected_energy_kwh": s.expected_energy_kwh,
                "note": s.note,
                "source": s.telemetry_source,
            }
            for s in record.execution.values()
        ],
    }


# --- planning ---------------------------------------------------------------


class PlanRequest(ScheduleRequest):
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    #: live plans always join the pool, so later users steer around this one
    share_pool: bool = True
    #: the caller's previous live plan: cancelled first so it is not counted as
    #: someone else's congestion
    replaces_schedule_id: Optional[str] = None


class CoordinatedPlanRequest(CoordinationRequest):
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


@router.post("/schedules/plan")
def plan_single(body: PlanRequest) -> JSONResponse:
    prepared, error = _prepare(body)
    if error is not None:
        return error
    name, scheduler_input, warnings, _fc, _forecast = prepared
    pool = pool_summary(None)
    solve_input = scheduler_input
    if body.share_pool:
        # The plan being replaced is left out of its own pool: it is cancelled
        # below on the feasible path, so it must not count as someone else's load.
        solve_input, pool = attach_pool(
            store, scheduler_input,
            [body.replaces_schedule_id] if body.replaces_schedule_id else [],
        )
    result = service.run(solve_input, name, config=body.solver_config, explain=True)
    if result.status not in ("OPTIMAL", "FEASIBLE"):
        # Never store an empty schedule: an INFEASIBLE/UNKNOWN result carries
        # no allocations, so persisting it would render "version 1" with no
        # jobs and leave the pool empty. The replaced schedule stays live.
        solver_status = result.solver.status.value if result.solver else result.status.value
        detail = (
            f"No feasible plan ({solver_status})."
            + (f" {result.reason}" if result.reason else "")
            + " Your loads need more power than the building limit allows at once,"
            " or a deadline is too tight. Raise the limit or give more time."
        )
        if warnings:
            detail += " Warnings: " + "; ".join(warnings)
        return JSONResponse(
            status_code=422,
            content={"detail": detail, "code": "no_feasible_plan", "message": detail},
        )
    if body.replaces_schedule_id:
        old = store.get(body.replaces_schedule_id)
        if old is not None and old.lifecycle not in (
            ScheduleLifecycle.COMPLETED, ScheduleLifecycle.CANCELLED, ScheduleLifecycle.FAILED
        ):
            old.lifecycle = ScheduleLifecycle.CANCELLED
            store._persist(old)
    record = store.create(scheduler_input, result, reason=RescheduleReason.MANUAL)
    record.lifecycle = ScheduleLifecycle.SCHEDULED
    record.context = {
        "kind": "single",
        "scheduler": name.value,
        "execution": body.execution.model_dump(mode="json"),
        "solver_config": body.solver_config.model_dump(mode="json") if body.solver_config else None,
    }
    store._persist(record)  # the context is what makes replans faithful after a restart
    payload = _state_payload(record)
    payload["warnings"] = warnings
    payload["pool"] = pool
    return JSONResponse(status_code=200, content=payload)


@router.post("/schedules/plan-coordinated")
def plan_coordinated(body: CoordinatedPlanRequest) -> JSONResponse:
    from .coordination import _signal_for

    if body.coordination_mode is not CoordinationMode.COORDINATED:
        msg = "plan-coordinated tracks a jointly solved schedule; use coordination_mode COORDINATED"
        return JSONResponse(status_code=422, content={"detail": msg, "code": "invalid_request", "message": msg})
    try:
        signal = _signal_for(body)
    except (CoordinationError, CarbonBadRequest, ValueError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request", "message": str(exc)})
    except CarbonUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": "provider_unavailable", "message": str(exc)})
    try:
        result, solved, _placement = coordinator.coordinate_detailed(body, signal)
    except (CoordinationError, NormalizationError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request", "message": str(exc)})
    if result.status not in ("OPTIMAL", "FEASIBLE") or solved is None:
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))
    merged = coordinator.build_merged(body, signal)
    record = store.create(merged, solved, reason=RescheduleReason.MANUAL)
    record.lifecycle = ScheduleLifecycle.SCHEDULED
    record.context = {
        "kind": "coordinated",
        "coordination_request": body.model_dump(mode="json"),
        "execution": body.execution.model_dump(mode="json"),
    }
    store._persist(record)
    payload = _state_payload(record)
    payload["coordination"] = result.model_dump(mode="json")
    return JSONResponse(status_code=200, content=payload)


# --- state + history ------------------------------------------------------------


@router.get("/schedules/{schedule_id}/state")
def get_state(schedule_id: str) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    return JSONResponse(status_code=200, content=_state_payload(record))


@router.get("/schedules/{schedule_id}/history")
def get_history(schedule_id: str) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    return JSONResponse(
        status_code=200,
        content={
            "schedule_id": schedule_id,
            "lifecycle": record.lifecycle.value,
            "versions": [
                {
                    "version": v.version,
                    "created_at": v.created_at.isoformat(),
                    "reason": v.reason.value,
                    "solver_status": v.solver_status,
                    "carbon_estimate_kg": v.carbon_estimate_kg,
                    "peak_kw": v.peak_kw,
                    "changed_jobs": [c.model_dump(mode="json") for c in v.changed_jobs],
                }
                for v in record.versions
            ],
        },
    )


# --- events -----------------------------------------------------------------------


class EventBody(BaseModel):
    event_type: ScheduleEventType
    timestamp: Optional[datetime] = None
    job_id: str = ""
    participant_id: str = ""
    payload: dict = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _aware_ts(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _require_aware_dt(v, "timestamp")


def _maybe_auto_replan(record: ScheduleRecord, advised: bool, now: datetime) -> dict:
    policy = ReschedulePolicy(record.context.get("execution", {}).get("policy", "HYBRID"))
    if advised and policy in (ReschedulePolicy.EVENT_DRIVEN, ReschedulePolicy.HYBRID):
        return _do_replan(record, now, RescheduleReason.SYSTEM_RECOVERY, auto=True)
    return {"replanned": False}


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _last_replan_at(record: ScheduleRecord) -> datetime:
    raw = record.context.get("last_replan_at")
    if raw:
        try:
            return _ensure_aware(datetime.fromisoformat(str(raw)))
        except ValueError:
            pass
    if record.versions:
        return _ensure_aware(record.current_version().created_at)
    return utcnow()


def _periodic_due(record: ScheduleRecord, now: datetime) -> bool:
    try:
        cfg = ExecutionConfig(**record.context.get("execution", {}))
    except Exception:
        cfg = ExecutionConfig()
    policy = cfg.policy
    if policy not in (ReschedulePolicy.PERIODIC, ReschedulePolicy.HYBRID):
        return False
    last = _last_replan_at(record)
    elapsed_s = (_ensure_aware(now) - last).total_seconds()
    return elapsed_s >= cfg.reoptimization_interval_minutes * 60


class TickBody(BaseModel):
    now: Optional[datetime] = None

    @field_validator("now")
    @classmethod
    def _aware_now(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _require_aware_dt(v, "now")


@router.post("/schedules/{schedule_id}/tick")
def post_tick(schedule_id: str, body: TickBody) -> JSONResponse:
    """Time-driven clock advance (daemon contract: client calls every N min).

    Records CLOCK_ADVANCED, refreshes lifecycle, and runs the same
    _do_replan path with reason PERIODIC when the policy is
    PERIODIC/HYBRID and the reoptimization interval has elapsed since
    last_replan_at (or version created_at on first tick). No in-process
    threads/cron; the caller owns the cadence.
    """
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    now = _ensure_aware(body.now or utcnow())
    event = ScheduleEvent(
        event_type=ScheduleEventType.CLOCK_ADVANCED, timestamp=now,
    )
    store.record_event(record, event)
    try:
        _advised, notes = apply_event(record, event)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition", "message": str(exc)})
    _refresh_lifecycle(record, now)
    due = _periodic_due(record, now)
    out: dict = {
        "ticked": True,
        "now": now.isoformat(),
        "periodic_due": due,
        "replan_advised": due,
        "notes": notes,
    }
    if due:
        auto = _do_replan(record, now, RescheduleReason.PERIODIC, auto=True)
        out.update(auto)
        # Interval is measured between attempts (simulated time), so record
        # it even when the candidate was gated and no version was appended.
        record.context["last_replan_at"] = now.isoformat()
    out["state"] = _state_payload(record)
    store._persist(record)
    return JSONResponse(status_code=200, content=out)


@router.post("/schedules/{schedule_id}/events")
def post_event(schedule_id: str, body: EventBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    ts = body.timestamp or utcnow()
    # Energy actuals ride on events so manual telemetry stays truthful.
    # They go through the same meter validation as /telemetry so negative
    # or absurd values are rejected instead of silently stored.
    energy_val: Optional[float] = None
    slot_updates: dict[int, int] = {}
    state = record.execution.get(body.job_id) if body.job_id else None
    if state is not None and isinstance(body.payload, dict):
        has_energy = "energy_delivered_kwh" in body.payload
        delivered = body.payload.get("delivered_slots")
        has_slots = isinstance(delivered, dict)
        if has_energy or has_slots:
            try:
                if has_energy:
                    energy_val = float(body.payload["energy_delivered_kwh"])
                if has_slots:
                    assert isinstance(delivered, dict)
                    for k, v in delivered.items():
                        si = int(k)
                        pw = int(v)
                        if pw < 0:
                            raise ValueError("delivered_slots power must be >= 0")
                        if pw / 1000.0 > meters.MAX_POWER_KW:
                            raise ValueError(
                                f"delivered_slots power {pw}W exceeds plausible maximum"
                            )
                        slot_updates[si] = pw
            except (ValueError, TypeError) as exc:
                return _err(str(exc), "invalid_request", 422)
    event = ScheduleEvent(
        event_type=body.event_type,
        timestamp=ts,
        participant_id=body.participant_id,
        job_id=body.job_id,
        payload=body.payload,
    )

    def _commit(target: ScheduleRecord, push: bool):
        tstate = target.execution.get(body.job_id) if body.job_id else None
        if tstate is not None:
            if push and (energy_val is not None or slot_updates):
                meters.push_reading(
                    schedule_id, body.job_id, ts,
                    energy_kwh=energy_val,
                    power_kw=(
                        max(slot_updates.values()) / 1000.0
                        if slot_updates and energy_val is None else None
                    ),
                    source="EVENT",
                )
            if energy_val is not None:
                tstate.energy_delivered_kwh = energy_val
            for si, pw in slot_updates.items():
                tstate.delivered_slots[si] = pw
        return apply_event(target, event)

    # Dry-run on a copy so a rejected event leaves no energy, slots or log entry behind.
    try:
        _commit(record.model_copy(deep=True), push=False)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition", "message": str(exc)})
    try:
        advised, notes = _commit(record, push=True)
    except ValueError as exc:
        return _err(str(exc), "invalid_request", 422)
    store.record_event(record, event)
    _refresh_lifecycle(record, event.timestamp)
    out: dict = {"state": _state_payload(record), "replan_advised": advised, "notes": notes}
    auto = _maybe_auto_replan(record, advised, event.timestamp)
    out.update(auto)
    return JSONResponse(status_code=200, content=out)


# --- real telemetry ---------------------------------------------------------------


class TelemetryBody(BaseModel):
    job_id: str
    timestamp: Optional[datetime] = None
    energy_kwh: Optional[float] = Field(default=None, ge=0)
    power_kw: Optional[float] = Field(default=None, ge=0)
    source: str = "MEASURED"

    @field_validator("timestamp")
    @classmethod
    def _aware_ts(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _require_aware_dt(v, "timestamp")

    @field_validator("energy_kwh", "power_kw")
    @classmethod
    def _finite_reading(cls, v: Optional[float], info) -> Optional[float]:
        if v is not None and not math.isfinite(v):
            raise ValueError(f"{info.field_name} must be a finite number")
        return v


@router.post("/schedules/{schedule_id}/telemetry")
def post_telemetry(schedule_id: str, body: TelemetryBody) -> JSONResponse:
    """Ingest one validated meter reading (real hardware, not simulation).

    Validates via InMemoryMeterProvider, folds the reading into execution
    truth, and reuses the existing event path: JOB_STARTED (+JOB_COMPLETED
    when cumulative energy meets the expected target), each carrying
    energy_delivered_kwh with source MEASURED. No new state machine; the
    job's provenance flips to MEASURED and shows up as `source` per job in
    the state payload (SIMULATED otherwise).
    """
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    state = record.execution.get(body.job_id)
    if state is None:
        return _err(f"unknown job {body.job_id}", "not_found", 404)
    if body.energy_kwh is None and body.power_kw is None:
        return _err("at least one of energy_kwh or power_kw is required", "invalid_request", 422)
    ts = body.timestamp or utcnow()
    try:
        meters.push_reading(
            schedule_id, body.job_id, ts,
            energy_kwh=body.energy_kwh, power_kw=body.power_kw, source="MEASURED",
        )
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request", "message": str(exc)})
    if body.energy_kwh is not None:
        state.energy_delivered_kwh = float(body.energy_kwh)
    state.telemetry_source = "MEASURED"
    state.last_updated = ts
    payload: dict = {
        "energy_delivered_kwh": state.energy_delivered_kwh,
        "source": "MEASURED",
        "measured": True,
    }
    if body.power_kw is not None:
        payload["power_kw"] = float(body.power_kw)
    target = state.expected_energy_kwh
    complete = target > 0 and state.energy_delivered_kwh + 1e-9 >= target
    terminal = state.status in (JobStatus.COMPLETED, JobStatus.CANCELLED)
    applied: list[str] = []
    notes: list[str] = []
    advised = False

    def _emit(event_type: ScheduleEventType) -> tuple[bool, list[str]]:
        event = ScheduleEvent(
            event_type=event_type, timestamp=ts, job_id=body.job_id, payload=dict(payload),
        )
        store.record_event(record, event)
        return apply_event(record, event)

    try:
        if terminal:
            notes.append("reading recorded on terminal job; no transition emitted")
        elif complete:
            if state.status is not JobStatus.RUNNING:
                # PENDING has no direct edge to COMPLETED; start first.
                a, n = _emit(ScheduleEventType.JOB_STARTED)
                applied.append(ScheduleEventType.JOB_STARTED.value)
                advised, notes = a, notes + n
            a, n = _emit(ScheduleEventType.JOB_COMPLETED)
            applied.append(ScheduleEventType.JOB_COMPLETED.value)
            advised, notes = advised or a, notes + n
        else:
            # Progress note: bring the job to RUNNING, or record a
            # same-state measured heartbeat when already running.
            a, n = _emit(ScheduleEventType.JOB_STARTED)
            applied.append(ScheduleEventType.JOB_STARTED.value)
            advised, notes = a, notes + n
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition", "message": str(exc)})
    _refresh_lifecycle(record, ts)
    store._persist(record)
    out: dict = {
        "accepted": True,
        "source": "MEASURED",
        "event_types": applied,
        "completed": complete,
        "notes": notes,
        "replan_advised": advised,
        "state": _state_payload(record),
    }
    auto = _maybe_auto_replan(record, advised, ts)
    out.update(auto)
    return JSONResponse(status_code=200, content=out)


# --- replan -------------------------------------------------------------------------
class ReplanBody(BaseModel):
    now: Optional[datetime] = None
    reason: RescheduleReason = RescheduleReason.MANUAL
    capacity_kw: Optional[float] = Field(default=None, gt=0)
    #: optional per-slot capacity in kW; length must equal the horizon slot
    #: count. Overrides the scalar per slot; the scalar remains the default.
    capacity_profile_kw: Optional[list[float]] = None
    added_jobs: list[LoadSpec] = Field(default_factory=list)
    removed_job_ids: list[str] = Field(default_factory=list)

    @field_validator("now")
    @classmethod
    def _aware_now(cls, v: Optional[datetime]) -> Optional[datetime]:
        return _require_aware_dt(v, "now")

    @field_validator("capacity_kw")
    @classmethod
    def _finite_cap(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not math.isfinite(v):
            raise ValueError("capacity_kw must be a finite number")
        return v

    @field_validator("capacity_profile_kw")
    @classmethod
    def _profile_sane(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is None:
            return v
        if not v:
            raise ValueError("capacity_profile_kw must not be empty when provided")
        for entry in v:
            if not math.isfinite(entry):
                raise ValueError("capacity_profile_kw entries must be finite numbers")
            if entry < 0:
                raise ValueError("capacity_profile_kw entries must be >= 0")
        return v


def _signal_from_input(scheduler_input):
    from ...domain.carbon import CarbonPoint, CarbonSignal

    horizon = scheduler_input.horizon
    profile = scheduler_input.carbon
    start = horizon.start
    points = [
        CarbonPoint(
            time=horizon.slot_start(i),
            gco2_per_kwh=float(profile.at(i)),
            source=profile.source,
        )
        for i in range(horizon.slot_count)
    ]
    return CarbonSignal(
        start=start, end=horizon.end,
        resolution_minutes=horizon.slot_minutes, points=points, source=profile.source,
    )


def _do_replan(record: ScheduleRecord, now: datetime, reason: RescheduleReason, auto: bool = False) -> dict:
    assert record.scheduler_input is not None
    base_input = record.scheduler_input
    slot = now_slot(base_input.horizon, now)
    cfg = ExecutionConfig(**record.context.get("execution", {}))
    receding = RecedingHorizon(
        solve_fn=lambda inp: _solve_record_input(record, inp),
        commitment_slots=max(0, cfg.commitment_window_minutes // max(1, base_input.horizon.slot_minutes)),
        min_shift_minutes=cfg.min_shift_minutes,
        improvement_threshold_percent=cfg.improvement_threshold_percent,
    )
    states, delivered, kwh = _states_maps(record)
    try:
        solve_input, notes = receding.remaining_input(base_input, states, delivered, kwh, slot)
    except RemainingInfeasible as exc:
        return {"replanned": False, "error": exc.reason or str(exc), "job_id": exc.job_id}
    pool = pool_summary(None)
    if record.context.get("kind", "single") == "single":
        # Steer around everyone else's live plans; this record is left out of its own pool.
        solve_input, pool = attach_pool(store, solve_input, [record.schedule_id])
    previous = _placement_of(record)
    result, changes, notes2, lifted = receding.replan(
        base_input, previous, solve_input, slot, reason.value,
        never_freeze=frozenset(j for j, st in states.items() if st is JobStatus.MISSED),
    )
    if result.status not in ("FEASIBLE", "OPTIMAL"):
        return {"replanned": False, "error": result.reason, "status": result.status.value}
    if not changes:
        return {"replanned": False, "notes": notes + notes2, "status": result.status.value}
    # Archive the full pre-replan input once so completed-job history is never
    # lost when the live input is narrowed to remaining work.
    if "original_input" not in record.context:
        try:
            record.context["original_input"] = base_input.model_dump(mode="json")
        except Exception:
            pass
    terminal = (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED)
    done_ids = sorted(jid for jid, st in states.items() if st in terminal)
    if done_ids:
        record.context["completed_job_ids"] = sorted(
            set(record.context.get("completed_job_ids", [])) | set(done_ids)
        )
    version = store.append_version(record, result, reason, changes, notes + notes2)
    # The stored input keeps the ORIGINAL job specs: remaining work (energy, duration,
    # thermal state) is always re-derived from execution truth, so persisting the
    # narrowed jobs would double-count what was already delivered on the next replan.
    _refresh_lifecycle(record, now)
    return {
        "replanned": True,
        "version": version.version,
        "changes": [c.model_dump(mode="json") for c in changes],
        "notes": notes + notes2,
        "frozen_lifted": lifted,
        "status": result.status.value,
        "pool": pool,
    }


def _solve_record_input(record: ScheduleRecord, solve_input: SchedulerInput):
    kind = record.context.get("kind", "single")
    if kind == "coordinated":
        body = CoordinationRequest(**record.context["coordination_request"])
        return coordinator._solve_coordinated(body, solve_input)
    name = SchedulerName(record.context.get("scheduler", "CPSAT"))
    raw_cfg = record.context.get("solver_config")
    cfg = SchedulerConfig(**raw_cfg) if raw_cfg else None
    return service.run(solve_input, name, config=cfg, explain=True)


@router.post("/schedules/{schedule_id}/replan")
def post_replan(schedule_id: str, body: ReplanBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None and record.scheduler_input is not None
    now = body.now or utcnow()
    # Phase 1: validate everything against the untouched record.
    base = record.scheduler_input
    update: dict = {}
    if body.capacity_kw is not None or body.capacity_profile_kw is not None:
        from ...domain.scaling import to_power_w

        n = base.horizon.slot_count
        if body.capacity_kw is not None:
            update["capacity_w"] = to_power_w(body.capacity_kw)
        if body.capacity_profile_kw is not None:
            if len(body.capacity_profile_kw) != n:
                return _err(
                    f"capacity profile has {len(body.capacity_profile_kw)} entries "
                    f"but the horizon has {n} slots",
                    "invalid_request",
                    422,
                )
            if any((not math.isfinite(c)) or c < 0 for c in body.capacity_profile_kw):
                return _err(
                    "capacity profile entries must be finite numbers >= 0",
                    "invalid_request",
                    422,
                )
            update["capacity_profile_w"] = [to_power_w(c) for c in body.capacity_profile_kw]
        elif body.capacity_kw is not None:
            # A scalar-only replan clears any previous profile so the new
            # scalar actually takes effect instead of being overridden.
            update["capacity_profile_w"] = None
    jobs = list(base.jobs)
    removed = set(body.removed_job_ids)
    cancel_states: list[JobExecutionState] = []
    if removed:
        jobs = [j for j in jobs if j.id not in removed]
        for jid in removed:
            state = record.execution.get(jid)
            if state is not None:
                try:
                    transition(state.model_copy(deep=True), JobStatus.CANCELLED, "removed by replan request")
                except ValueError as exc:
                    return _err(str(exc), "invalid_transition", 422)
                cancel_states.append(state)
    added: list = []
    baseline_add: list[int] = []
    if body.added_jobs:
        try:
            added, baseline_add = _normalize_added(record, body.added_jobs)
        except NormalizationError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request", "message": str(exc)})
        new_ids = [j.id for j in added]
        existing = {j.id for j in base.jobs} | set(record.execution)
        dupes = sorted({i for i in new_ids if new_ids.count(i) > 1 or i in existing})
        if dupes:
            return _err(f"duplicate job id(s): {', '.join(dupes)}", "invalid_request", 422)
        jobs = [*jobs, *added]
    if added or removed:
        update["jobs"] = jobs
    if any(baseline_add):
        update["baseline"] = BaselineProfile(
            power_w=[b + a for b, a in zip(base.baseline.power_w, baseline_add)],
            source=base.baseline.source,
        )
    try:
        new_input = base.model_copy(update=update) if update else base
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request", "message": str(exc)})
    # Phase 2: mutate.
    saved_input = record.scheduler_input
    saved_execution = {jid: st.model_copy(deep=True) for jid, st in record.execution.items()}
    record.scheduler_input = new_input
    for state in cancel_states:
        transition(state, JobStatus.CANCELLED, "removed by replan request")
    for job in added:
        record.execution[job.id] = JobExecutionState(
            job_id=job.id, participant_id=job.participant_id,
            expected_energy_kwh=job.energy_kwh() or 0.0, last_updated=now,
        )
    out = _do_replan(record, now, body.reason)
    if out.get("error") is not None:
        # Infeasible replan: keep the old version AND its input. The candidate
        # above already mutated the record, so roll that back before persisting.
        record.scheduler_input = saved_input
        for jid in [jid for jid in record.execution if jid not in saved_execution]:
            del record.execution[jid]
        for jid, st in saved_execution.items():
            record.execution[jid] = st
    # Phase 3: persist once, whichever way the replan went.
    store._persist(record)
    out["state"] = _state_payload(record)
    return JSONResponse(status_code=200, content=out)


def _normalize_added(record: ScheduleRecord, specs: list[LoadSpec]):
    from ...services.scheduler_normalizer import SchedulerNormalizer

    assert record.scheduler_input is not None
    base = record.scheduler_input
    signal = _signal_from_input(base)
    normalizer = SchedulerNormalizer()
    added_input, _report = normalizer.normalize(
        specs, signal, base.capacity_w / 1000.0, horizon=base.horizon
    )
    return added_input.jobs, list(added_input.baseline.power_w)


# --- overrides ------------------------------------------------------------------------


class OverrideBody(BaseModel):
    job_id: str
    command: OverrideCommand
    new_release_at: Optional[datetime] = None
    new_deadline_at: Optional[datetime] = None

    @field_validator("new_release_at", "new_deadline_at")
    @classmethod
    def _aware_window(cls, v: Optional[datetime], info) -> Optional[datetime]:
        return _require_aware_dt(v, info.field_name)

    @field_validator("job_id")
    @classmethod
    def _non_empty_job(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("job_id must be a non-empty string")
        return v


@router.post("/schedules/{schedule_id}/override")
def post_override(schedule_id: str, body: OverrideBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None and record.scheduler_input is not None
    now = utcnow()
    slot = now_slot(record.scheduler_input.horizon, now)

    placement = _placement_of(record)
    inactive = (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED)

    def headroom(s: int) -> float:
        base = record.scheduler_input
        assert base is not None
        # Other active jobs' planned draw in this slot is not free capacity.
        others = sum(
            slots.get(s, 0)
            for jid, slots in placement.items()
            if jid != body.job_id
            and (record.execution.get(jid) is None or record.execution[jid].status not in inactive)
        )
        return (base.headroom_w(s) - others) / 1000.0

    allowed, explanation = check_override(record, body.job_id, body.command, slot, headroom)
    if not allowed:
        return JSONResponse(status_code=422, content={"detail": explanation, "code": "override_rejected", "message": explanation})
    state = record.execution.get(body.job_id)
    reshape = body.command in (OverrideCommand.MOVE, OverrideCommand.START_NOW, OverrideCommand.RUN_ASAP)
    # Validate the window change before anything is mutated.
    patched = None
    if reshape and not (
        body.command is OverrideCommand.MOVE and not body.new_release_at and not body.new_deadline_at
    ):
        horizon = record.scheduler_input.horizon
        patched = []
        for job in record.scheduler_input.jobs:
            if job.id != body.job_id:
                patched.append(job)
                continue
            update: dict = {}
            if body.command in (OverrideCommand.START_NOW, OverrideCommand.RUN_ASAP) or body.new_release_at:
                ref = body.new_release_at or now
                update["release_slot"] = max(0, sum(
                    1 for s in range(horizon.slot_count) if horizon.slot_end(s) <= ref
                ))
            if body.new_deadline_at:
                update["deadline_slot"] = sum(
                    1 for s in range(horizon.slot_count) if horizon.slot_start(s) < body.new_deadline_at
                )
            if (
                body.command in (OverrideCommand.START_NOW, OverrideCommand.RUN_ASAP)
                and not body.new_deadline_at
                and job.job_type is not LoadType.THERMAL
                and not (state is not None and state.status is JobStatus.RUNNING)
            ):
                # "Run now" means run now: close the window right after the shortest run
                # that can deliver the job, so the optimizer cannot slide it to a cleaner
                # hour later. Thermal loads keep their window (their run length is physics).
                start = update["release_slot"]
                shortest = (
                    job.duration_slots or 1
                    if job.job_type is LoadType.DEFERRABLE_ATOMIC
                    else max(1, job.minimum_slots())
                )
                update["deadline_slot"] = min(horizon.slot_count, max(start + shortest, start + 1))
            release = update.get("release_slot", job.release_slot)
            deadline = update.get("deadline_slot", job.deadline_slot)
            if (
                body.command is OverrideCommand.MOVE
                and release == job.release_slot
                and deadline == job.deadline_slot
            ):
                return _err(
                    "cannot apply: the window is unchanged (the new time is past the end of "
                    "the planning horizon or within the current slot); plan again for a longer window",
                    "override_rejected", 422,
                )
            if release >= deadline:
                return _err(
                    f"cannot apply: the new window is empty (release slot {release}, "
                    f"deadline slot {deadline})",
                    "override_rejected", 422,
                )
            running = state is not None and state.status is JobStatus.RUNNING
            if not running:
                need = 1
                if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                    need = job.duration_slots or 1
                elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                    need = max(1, job.minimum_slots())
                if deadline - release < need:
                    return _err(
                        f"cannot apply: the new window has {deadline - release} slot(s) "
                        f"but the job needs {need}",
                        "override_rejected", 422,
                    )
            patched.append(job.model_copy(update=update))
    if body.command is OverrideCommand.CANCEL and state is not None:
        try:
            transition(state, JobStatus.CANCELLED, "user override")
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition", "message": str(exc)})
    if body.command is OverrideCommand.PAUSE and state is not None:
        try:
            transition(state, JobStatus.PAUSED, "user override")
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition", "message": str(exc)})
        store.record_event(
            record,
            ScheduleEvent(
                event_type=ScheduleEventType.USER_OVERRIDE, timestamp=now,
                job_id=body.job_id, payload={"command": body.command.value},
            ),
        )
        return JSONResponse(status_code=200, content={"accepted": True, "state": _state_payload(record)})
    prior_status = (state.status, state.note) if state is not None else None
    if body.command in (OverrideCommand.START_NOW, OverrideCommand.RUN_ASAP) and state is not None:
        try:
            if state.status is JobStatus.PAUSED:
                transition(state, JobStatus.RUNNING, "user override")
            else:
                transition(state, JobStatus.READY, "user override: start now")
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition", "message": str(exc)})
    # MOVE with no window change is a no-op: don't reshape or replan spuriously.
    if body.command is OverrideCommand.MOVE and not body.new_release_at and not body.new_deadline_at:
        store.record_event(
            record,
            ScheduleEvent(
                event_type=ScheduleEventType.USER_OVERRIDE, timestamp=now,
                job_id=body.job_id, payload={"command": body.command.value, "noop": True},
            ),
        )
        return JSONResponse(
            status_code=200,
            content={
                "accepted": True,
                "replanned": False,
                "note": "empty MOVE window: no change, replan skipped",
                "explanation": explanation,
                "state": _state_payload(record),
            },
        )
    # MOVE / START_NOW / RUN_ASAP reshape the window, then replan; the new
    # window is kept only if the replan is feasible.
    previous_input = record.scheduler_input
    if patched is not None:
        record.scheduler_input = previous_input.model_copy(update={"jobs": patched})
    out = _do_replan(record, now, RescheduleReason.USER_OVERRIDE)
    if out.get("error") is not None and patched is not None:
        record.scheduler_input = previous_input
        if state is not None and prior_status is not None:
            state.status, state.note = prior_status
        return JSONResponse(
            status_code=422,
            content={
                "detail": out["error"], "code": "override_rejected",
                "message": out["error"], "state": _state_payload(record),
            },
        )
    store.record_event(
        record,
        ScheduleEvent(
            event_type=ScheduleEventType.USER_OVERRIDE, timestamp=now,
            job_id=body.job_id, payload={"command": body.command.value},
        ),
    )
    out["accepted"] = True
    out["explanation"] = explanation
    out["state"] = _state_payload(record)
    return JSONResponse(status_code=200, content=out)


# --- simulation ---------------------------------------------------------------------------


class AdvanceBody(BaseModel):
    to_time: datetime
    script: list[dict] = Field(default_factory=list)
    carbon_actual: list[dict] = Field(default_factory=list)

    @field_validator("to_time")
    @classmethod
    def _aware_to(cls, v: datetime) -> datetime:
        result = _require_aware_dt(v, "to_time")
        assert result is not None
        return result


@router.post("/simulation/{schedule_id}/advance")
def simulation_advance(schedule_id: str, body: AdvanceBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    try:
        trace = simulator.run(record, body.script, body.carbon_actual or None, body.to_time)
    except ValueError as exc:
        return _err(str(exc), "invalid_request", 422)
    _refresh_lifecycle(record, body.to_time)
    metrics = _execution_metrics(record)
    store._persist(record)
    return JSONResponse(
        status_code=200,
        content={"simulated": True, "trace": trace, "state": _state_payload(record), "metrics": metrics},
    )


def _execution_metrics(record: ScheduleRecord) -> dict:
    planned_energy = sum(s.expected_energy_kwh for s in record.execution.values())
    actual_energy = round(sum(s.energy_delivered_kwh for s in record.execution.values()), 4)
    counts: dict[str, int] = {}
    for s in record.execution.values():
        counts[s.status.value] = counts.get(s.status.value, 0) + 1
    planned_co2 = record.versions[0].carbon_estimate_kg if record.versions else None
    realized_co2 = None
    slots_scored = 0
    slots_total = 0
    if record.scheduler_input is not None and record.actual_carbon:
        # Score actual draws against ACTUAL carbon only; slots without an
        # actual observation are excluded, never filled from the forecast.
        horizon = record.scheduler_input.horizon
        total = 0.0
        for s in record.execution.values():
            for slot, power in s.delivered_slots.items():
                slots_total += 1
                key = horizon.slot_start(slot).isoformat()
                ci = record.actual_carbon.get(key)
                if ci is None:
                    continue
                slots_scored += 1
                total += power * record.scheduler_input.horizon.slot_minutes * float(ci) / 60_000_000
        if slots_scored:
            realized_co2 = round(total, 4)
    else:
        if record.scheduler_input is not None:
            slots_total = sum(len(s.delivered_slots) for s in record.execution.values())
    return {
        "planned_energy_kwh": round(planned_energy, 4),
        "actual_energy_kwh": actual_energy,
        "planned_co2_kg": planned_co2,
        "realized_co2_kg": realized_co2,
        "realized_co2_slots_scored": slots_scored,
        "realized_co2_slots_total": slots_total,
        "realized_co2_coverage": (slots_scored / slots_total if slots_total else 0.0),
        "job_counts": counts,
        "versions": len(record.versions),
    }
