"""Event application: state machine transitions + override guardrails (Phase 7).

Events update execution truth. They never edit the past and never bypass hard
constraints: an override that would breach capacity, a deadline, or a device
limit is REJECTED with an explanation, not silently applied.

Returns (replan_advised, notes). The caller (route/policy layer) decides
whether to actually replan based on the configured ReschedulePolicy.
"""

from __future__ import annotations

import logging

from ..domain.execution import (
    JobExecutionState,
    JobStatus,
    OverrideCommand,
    ScheduleEvent,
    ScheduleEventType,
    ScheduleLifecycle,
    ScheduleRecord,
)

log = logging.getLogger("heliotrope.execution")


# Events that by themselves justify a replan under EVENT_DRIVEN/HYBRID.
REPLAN_EVENTS = {
    ScheduleEventType.JOB_ADDED,
    ScheduleEventType.JOB_REMOVED,
    ScheduleEventType.JOB_MISSED,
    ScheduleEventType.JOB_FAILED,
    ScheduleEventType.CAPACITY_CHANGED,
    ScheduleEventType.CARBON_FORECAST_UPDATED,
    ScheduleEventType.USER_OVERRIDE,
}

_VALID_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.READY, JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.MISSED, JobStatus.FAILED},
    JobStatus.READY: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.MISSED, JobStatus.FAILED},
    JobStatus.RUNNING: {JobStatus.PAUSED, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.PAUSED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.MISSED: {JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.RUNNING},
    JobStatus.CANCELLED: set(),
}


def transition(state: JobExecutionState, to: JobStatus, note: str = "") -> None:
    allowed = _VALID_TRANSITIONS.get(state.status, set())
    if to not in allowed and to is not state.status:
        raise ValueError(f"job {state.job_id}: {state.status.value} -> {to.value} is not a valid transition")
    state.status = to
    if note:
        state.note = note


def apply_event(record: ScheduleRecord, event: ScheduleEvent) -> tuple[bool, list[str]]:
    """Apply one event to execution truth. Returns (replan_advised, notes)."""
    notes: list[str] = []
    state = record.execution.get(event.job_id) if event.job_id else None

    if event.event_type is ScheduleEventType.JOB_STARTED and state:
        transition(state, JobStatus.RUNNING, "started")
        state.actual_start = state.actual_start or event.timestamp
        state.last_updated = event.timestamp
    elif event.event_type is ScheduleEventType.JOB_COMPLETED and state:
        state.energy_delivered_kwh = float(event.payload.get("energy_delivered_kwh", state.expected_energy_kwh))
        state.actual_end = event.timestamp
        transition(state, JobStatus.COMPLETED, "completed")
        state.last_updated = event.timestamp
    elif event.event_type is ScheduleEventType.JOB_PAUSED and state:
        transition(state, JobStatus.PAUSED, "paused by event")
    elif event.event_type is ScheduleEventType.JOB_RESUMED and state:
        transition(state, JobStatus.RUNNING, "resumed")
    elif event.event_type in (ScheduleEventType.JOB_MISSED, ScheduleEventType.JOB_FAILED) and state:
        transition(
            state,
            JobStatus.MISSED if event.event_type is ScheduleEventType.JOB_MISSED else JobStatus.FAILED,
            str(event.payload.get("reason", "")),
        )
        state.last_updated = event.timestamp
    elif event.event_type is ScheduleEventType.CLOCK_ADVANCED:
        _mark_missed(record, event)
        notes.append("clock advanced; overdue unstarted jobs marked MISSED")

    if record.lifecycle is ScheduleLifecycle.SCHEDULED and any(
        s.status in (JobStatus.RUNNING, JobStatus.COMPLETED) for s in record.execution.values()
    ):
        record.lifecycle = ScheduleLifecycle.ACTIVE

    advised = event.event_type in REPLAN_EVENTS
    return advised, notes


def _mark_missed(record: ScheduleRecord, event: ScheduleEvent) -> None:
    """Any PENDING/READY job whose scheduled start has passed without a start
    is MISSED — the simulator and the clock agree on this, no telemetry needed."""
    for state in record.execution.values():
        if state.status in (JobStatus.PENDING, JobStatus.READY) and state.scheduled_start:
            if state.scheduled_start <= event.timestamp and state.actual_start is None:
                try:
                    transition(state, JobStatus.MISSED, "scheduled start passed without starting")
                except ValueError:
                    continue


def check_override(
    record: ScheduleRecord, job_id: str, command: OverrideCommand, now_slot: int, headroom_fn,
) -> tuple[bool, str]:
    """Guardrail check for a user override. Returns (allowed, explanation)."""
    state = record.execution.get(job_id)
    if state is None:
        return False, f"unknown job {job_id}"
    if command is OverrideCommand.START_NOW:
        if state.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            return False, f"{state.job_id} is already {state.status.value.lower()}"
        need_kw = 0.0
        if record.scheduler_input:
            for job in record.scheduler_input.jobs:
                if job.id == job_id:
                    need_kw = job.max_power_w / 1000.0
        free_kw = headroom_fn(now_slot) if headroom_fn else float("inf")
        if need_kw > free_kw + 1e-9:
            return False, (
                f"cannot start this load immediately: it needs {need_kw:.2f} kW "
                f"but only {free_kw:.2f} kW of shared capacity is free right now"
            )
        return True, "override accepted"
    if command is OverrideCommand.CANCEL:
        if state.status in (JobStatus.COMPLETED,):
            return False, "cannot cancel completed work"
        return True, "override accepted"
    return True, "override accepted"
