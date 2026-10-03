"""In-memory execution store (Phase 7).

Records live here: schedule versions (immutable, appended), per-job execution
truth, events, and forecast-vs-actual carbon. Process-local by design for this
phase; the schemas are the persistence contract a database will adopt later.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from ..domain.execution import (
    JobExecutionState,
    JobStatus,
    RescheduleReason,
    ScheduleEvent,
    ScheduleLifecycle,
    ScheduleRecord,
    ScheduleVersion,
)
from ..domain.scheduling import SchedulerInput, SchedulerResult

log = logging.getLogger("heliotrope.execution")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionStore:
    def __init__(self) -> None:
        self._records: dict[str, ScheduleRecord] = {}

    def create(
        self,
        scheduler_input: SchedulerInput,
        result: SchedulerResult,
        reason: RescheduleReason = RescheduleReason.MANUAL,
    ) -> ScheduleRecord:
        schedule_id = uuid.uuid4().hex[:12]
        now = utcnow()
        record = ScheduleRecord(
            schedule_id=schedule_id,
            lifecycle=ScheduleLifecycle.SCHEDULED,
            scheduler_input=scheduler_input,
            versions=[
                ScheduleVersion(
                    version=1,
                    created_at=now,
                    reason=reason,
                    result=result,
                    carbon_estimate_kg=result.metrics.total_co2_kg,
                    peak_kw=result.metrics.peak_kw,
                    solver_status=result.solver.status.value,
                )
            ],
        )
        for scheduled in result.schedule:
            record.execution[scheduled.job_id] = JobExecutionState(
                job_id=scheduled.job_id,
                scheduled_start=scheduled.start_time,
                scheduled_end=scheduled.end_time,
                expected_energy_kwh=scheduled.energy_kwh,
                last_updated=now,
            )
        # Participant ids ride on the input jobs.
        by_id = {j.id: j.participant_id for j in scheduler_input.jobs}
        for state in record.execution.values():
            state.participant_id = by_id.get(state.job_id, "")
        self._records[schedule_id] = record
        log.info("schedule_created schedule_id=%s reason=%s", schedule_id, reason.value)
        return record

    def get(self, schedule_id: str) -> ScheduleRecord | None:
        return self._records.get(schedule_id)

    def append_version(
        self,
        record: ScheduleRecord,
        result: SchedulerResult,
        reason: RescheduleReason,
        changes,
        notes: list[str],
    ) -> ScheduleVersion:
        version = ScheduleVersion(
            version=record.current_version_number() + 1,
            created_at=utcnow(),
            reason=reason,
            result=result,
            changed_jobs=changes,
            carbon_estimate_kg=result.metrics.total_co2_kg,
            peak_kw=result.metrics.peak_kw,
            solver_status=result.solver.status.value,
        )
        record.versions.append(version)
        # Refresh scheduled windows for jobs present in the new version.
        for scheduled in result.schedule:
            state = record.execution.get(scheduled.job_id)
            if state is not None and state.status not in (
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED,
            ):
                state.scheduled_start = scheduled.start_time
                state.scheduled_end = scheduled.end_time
                state.expected_energy_kwh = scheduled.energy_kwh
                state.last_updated = utcnow()
        for note in notes:
            log.info("schedule_replanned schedule_id=%s v=%d note=%s", record.schedule_id, version.version, note)
        log.info(
            "schedule_replanned schedule_id=%s v=%d reason=%s changed=%d",
            record.schedule_id, version.version, reason.value, len(changes),
        )
        return version

    def record_event(self, record: ScheduleRecord, event: ScheduleEvent) -> ScheduleEvent:
        if not event.event_id:
            event.event_id = uuid.uuid4().hex[:8]
        record.events.append(event)
        log.info(
            "schedule_event schedule_id=%s type=%s job=%s",
            record.schedule_id, event.event_type.value, event.job_id,
        )
        return event
