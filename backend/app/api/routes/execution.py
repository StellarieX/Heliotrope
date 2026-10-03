"""Execution & rescheduling API (Phase 7).

  POST /api/v1/schedules/plan                 single-user plan -> v1 record
  POST /api/v1/schedules/plan-coordinated     building plan -> v1 record
  GET  /api/v1/schedules/{id}/state           live execution truth
  GET  /api/v1/schedules/{id}/history         immutable versions + diffs
  POST /api/v1/schedules/{id}/events          apply an event (policy may auto-replan)
  POST /api/v1/schedules/{id}/replan          manual/periodic replan over remainders
  POST /api/v1/schedules/{id}/override         guarded user override
  POST /api/v1/simulation/advance             deterministic simulated execution

A version is only appended when the candidate actually changes something
worth keeping; otherwise the current version stands and the response says so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...domain.coordination import CoordinationRequest
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
from ...domain.loads import LoadSpec
from ...domain.scheduling import SchedulerInput
from ...services.carbon_service import CarbonBadRequest, CarbonUnavailable
from ...services.coordinator import CoordinationError, MultiUserCoordinator
from ...services.execution_events import apply_event, check_override, transition
from ...services.execution_store import ExecutionStore, utcnow
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


# --- helpers --------------------------------------------------------------


def _record_or_404(schedule_id: str):
    record = store.get(schedule_id)
    if record is None:
        return None, JSONResponse(status_code=404, content={"detail": "unknown schedule_id", "code": "not_found"})
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
                "energy_delivered_kwh": s.energy_delivered_kwh,
                "expected_energy_kwh": s.expected_energy_kwh,
                "note": s.note,
            }
            for s in record.execution.values()
        ],
    }


# --- planning ---------------------------------------------------------------


class PlanRequest(ScheduleRequest):
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


class CoordinatedPlanRequest(CoordinationRequest):
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)


@router.post("/schedules/plan")
def plan_single(body: PlanRequest) -> JSONResponse:
    prepared, error = _prepare(body)
    if error is not None:
        return error
    name, scheduler_input, warnings, _fc, _forecast = prepared
    result = service.run(scheduler_input, name, config=body.solver_config, explain=True)
    record = store.create(scheduler_input, result, reason=RescheduleReason.MANUAL)
    record.lifecycle = ScheduleLifecycle.SCHEDULED
    record.context = {
        "kind": "single",
        "scheduler": name.value,
        "execution": body.execution.model_dump(mode="json"),
    }
    payload = _state_payload(record)
    payload["warnings"] = warnings
    return JSONResponse(status_code=200, content=payload)


@router.post("/schedules/plan-coordinated")
def plan_coordinated(body: CoordinatedPlanRequest) -> JSONResponse:
    from .coordination import _signal_for

    try:
        signal = _signal_for(body)
    except (CoordinationError, CarbonBadRequest, ValueError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
    except CarbonUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "code": "provider_unavailable"})
    try:
        result, solved, _placement = coordinator.coordinate_detailed(body, signal)
    except (CoordinationError, NormalizationError) as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
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


def _maybe_auto_replan(record: ScheduleRecord, advised: bool) -> dict:
    policy = ReschedulePolicy(record.context.get("execution", {}).get("policy", "HYBRID"))
    if advised and policy in (ReschedulePolicy.EVENT_DRIVEN, ReschedulePolicy.HYBRID):
        return _do_replan(record, utcnow(), RescheduleReason.SYSTEM_RECOVERY, auto=True)
    return {"replanned": False}


@router.post("/schedules/{schedule_id}/events")
def post_event(schedule_id: str, body: EventBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    event = ScheduleEvent(
        event_type=body.event_type,
        timestamp=body.timestamp or utcnow(),
        participant_id=body.participant_id,
        job_id=body.job_id,
        payload=body.payload,
    )
    # Energy actuals ride on events so manual telemetry stays truthful.
    if body.job_id and isinstance(body.payload, dict):
        state = record.execution.get(body.job_id)
        if state is not None:
            if "energy_delivered_kwh" in body.payload:
                state.energy_delivered_kwh = float(body.payload["energy_delivered_kwh"])
            delivered = body.payload.get("delivered_slots")
            if isinstance(delivered, dict):
                for k, v in delivered.items():
                    state.delivered_slots[int(k)] = int(v)
    store.record_event(record, event)
    try:
        advised, notes = apply_event(record, event)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition"})
    _refresh_lifecycle(record, event.timestamp)
    out: dict = {"state": _state_payload(record), "replan_advised": advised, "notes": notes}
    auto = _maybe_auto_replan(record, advised)
    out.update(auto)
    return JSONResponse(status_code=200, content=out)


# --- replan -------------------------------------------------------------------------


class ReplanBody(BaseModel):
    now: Optional[datetime] = None
    reason: RescheduleReason = RescheduleReason.MANUAL
    capacity_kw: Optional[float] = Field(default=None, gt=0)
    added_jobs: list[LoadSpec] = Field(default_factory=list)
    removed_job_ids: list[str] = Field(default_factory=list)


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
    previous = _placement_of(record)
    result, changes, notes2, lifted = receding.replan(base_input, previous, solve_input, slot, reason.value)
    if result.status not in ("FEASIBLE", "OPTIMAL"):
        return {"replanned": False, "error": result.reason, "status": result.status.value}
    if not changes:
        return {"replanned": False, "notes": notes + notes2, "status": result.status.value}
    version = store.append_version(record, result, reason, changes, notes + notes2)
    record.scheduler_input = solve_input
    _refresh_lifecycle(record, now)
    return {
        "replanned": True,
        "version": version.version,
        "changes": [c.model_dump(mode="json") for c in changes],
        "notes": notes + notes2,
        "frozen_lifted": lifted,
        "status": result.status.value,
    }


def _solve_record_input(record: ScheduleRecord, solve_input: SchedulerInput):
    kind = record.context.get("kind", "single")
    if kind == "coordinated":
        body = CoordinationRequest(**record.context["coordination_request"])
        return coordinator._solve_coordinated(body, solve_input)
    name = SchedulerName(record.context.get("scheduler", "CPSAT"))
    return service.run(solve_input, name, explain=True)


@router.post("/schedules/{schedule_id}/replan")
def post_replan(schedule_id: str, body: ReplanBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None and record.scheduler_input is not None
    now = body.now or utcnow()
    # Capacity change and job add/remove reshape the problem before solving.
    if body.capacity_kw is not None:
        from ...domain.scaling import to_power_w

        record.scheduler_input = record.scheduler_input.model_copy(
            update={"capacity_w": to_power_w(body.capacity_kw)}
        )
    if body.removed_job_ids:
        record.scheduler_input = record.scheduler_input.model_copy(
            update={"jobs": [j for j in record.scheduler_input.jobs if j.id not in body.removed_job_ids]}
        )
        for jid in body.removed_job_ids:
            state = record.execution.get(jid)
            if state is not None:
                try:
                    transition(state, JobStatus.CANCELLED, "removed by replan request")
                except ValueError:
                    pass
    if body.added_jobs:
        try:
            added, _ = _normalize_added(record, body.added_jobs)
        except NormalizationError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_request"})
        record.scheduler_input = record.scheduler_input.model_copy(
            update={"jobs": [*record.scheduler_input.jobs, *added]}
        )
        for job in added:
            record.execution[job.id] = JobExecutionState(
                job_id=job.id, participant_id=job.participant_id,
                expected_energy_kwh=job.energy_kwh() or 0.0, last_updated=now,
            )
    out = _do_replan(record, now, body.reason)
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
    return added_input.jobs, []


# --- overrides ------------------------------------------------------------------------


class OverrideBody(BaseModel):
    job_id: str
    command: OverrideCommand
    new_release_at: Optional[datetime] = None
    new_deadline_at: Optional[datetime] = None


@router.post("/schedules/{schedule_id}/override")
def post_override(schedule_id: str, body: OverrideBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None and record.scheduler_input is not None
    now = utcnow()
    slot = now_slot(record.scheduler_input.horizon, now)

    def headroom(s: int) -> float:
        base = record.scheduler_input
        assert base is not None
        return base.headroom_w(s) / 1000.0

    allowed, explanation = check_override(record, body.job_id, body.command, slot, headroom)
    if not allowed:
        return JSONResponse(status_code=422, content={"detail": explanation, "code": "override_rejected"})
    state = record.execution.get(body.job_id)
    if body.command is OverrideCommand.CANCEL and state is not None:
        try:
            transition(state, JobStatus.CANCELLED, "user override")
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition"})
    if body.command is OverrideCommand.PAUSE and state is not None:
        try:
            transition(state, JobStatus.PAUSED, "user override")
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc), "code": "invalid_transition"})
        return JSONResponse(status_code=200, content={"accepted": True, "state": _state_payload(record)})
    if body.command in (OverrideCommand.START_NOW, OverrideCommand.RUN_ASAP) and state is not None:
        try:
            if state.status is JobStatus.PAUSED:
                transition(state, JobStatus.RUNNING, "user override")
            else:
                transition(state, JobStatus.READY, "user override: start now")
        except ValueError:
            pass
    # MOVE / START_NOW / RUN_ASAP reshape the window, then replan.
    if body.command in (OverrideCommand.MOVE, OverrideCommand.START_NOW, OverrideCommand.RUN_ASAP):
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
                update["deadline_slot"] = max(
                    update.get("release_slot", job.release_slot) + 1,
                    sum(1 for s in range(horizon.slot_count) if horizon.slot_start(s) < body.new_deadline_at),
                )
            patched.append(job.model_copy(update=update))
        record.scheduler_input = record.scheduler_input.model_copy(update={"jobs": patched})
    store.record_event(
        record,
        ScheduleEvent(
            event_type=ScheduleEventType.USER_OVERRIDE, timestamp=now,
            job_id=body.job_id, payload={"command": body.command.value},
        ),
    )
    out = _do_replan(record, now, RescheduleReason.USER_OVERRIDE)
    out["accepted"] = True
    out["explanation"] = explanation
    out["state"] = _state_payload(record)
    return JSONResponse(status_code=200, content=out)


# --- simulation ---------------------------------------------------------------------------


class AdvanceBody(BaseModel):
    to_time: datetime
    script: list[dict] = Field(default_factory=list)
    carbon_actual: list[dict] = Field(default_factory=list)


@router.post("/simulation/{schedule_id}/advance")
def simulation_advance(schedule_id: str, body: AdvanceBody) -> JSONResponse:
    record, error = _record_or_404(schedule_id)
    if error is not None:
        return error
    assert record is not None
    trace = simulator.run(record, body.script, body.carbon_actual or None, body.to_time)
    _refresh_lifecycle(record, body.to_time)
    metrics = _execution_metrics(record)
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
    if record.scheduler_input is not None and record.actual_carbon:
        # Score actual draws against ACTUAL carbon, never against the forecast.
        horizon = record.scheduler_input.horizon
        carbon = record.scheduler_input.carbon.gco2_per_kwh
        total = 0.0
        for s in record.execution.values():
            for slot, power in s.delivered_slots.items():
                key = horizon.slot_start(slot).isoformat()
                ci = record.actual_carbon.get(key, carbon[slot] if slot < len(carbon) else 0)
                total += power * record.scheduler_input.horizon.slot_minutes * ci / 60_000_000
        realized_co2 = round(total, 4)
    return {
        "planned_energy_kwh": round(planned_energy, 4),
        "actual_energy_kwh": actual_energy,
        "planned_co2_kg": planned_co2,
        "realized_co2_kg": realized_co2,
        "job_counts": counts,
        "versions": len(record.versions),
    }
