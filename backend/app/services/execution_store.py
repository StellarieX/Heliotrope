"""Durable execution store (Phase 7, Milestone 3).

Records live here: schedule versions (immutable, appended), per-job execution
truth, events, and forecast-vs-actual carbon. Every mutation is persisted to a
SQLite database so records survive process restarts and are shared across
store instances; the in-memory dict remains as a read-through cache for fast
lookups.

The database file defaults to ``backend/data/heliotrope_execution.db`` and can
be overridden with the ``HELIOTROPE_EXECUTION_DB`` environment variable or the
``db_path`` constructor argument. Connections are opened per operation (each
FastAPI worker thread gets its own), and the schema is created up front.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

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

#: Environment variable overriding where the durable store lives.
ENV_DB_PATH = "HELIOTROPE_EXECUTION_DB"

#: backend/app/services/execution_store.py -> backend/data/heliotrope_execution.db
DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "heliotrope_execution.db"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schedules ("
    "schedule_id TEXT PRIMARY KEY, lifecycle TEXT, data TEXT, updated_at TEXT)"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._records: dict[str, ScheduleRecord] = {}
        self._db_path = (
            db_path or os.environ.get(ENV_DB_PATH) or str(DEFAULT_DB_PATH)
        )
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(_SCHEMA)
        log.info("execution_store_opened db_path=%s", self._db_path)

    # --- persistence ---------------------------------------------------------

    def _persist(self, record: ScheduleRecord) -> None:
        """Upsert the full serialized record into SQLite."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schedules "
                "(schedule_id, lifecycle, data, updated_at) VALUES (?, ?, ?, ?)",
                (
                    record.schedule_id,
                    record.lifecycle.value,
                    record.model_dump_json(),
                    utcnow().isoformat(),
                ),
            )

    def _load(self, schedule_id: str) -> ScheduleRecord | None:
        """Read one record back from SQLite into the cache, if present."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT data FROM schedules WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
        if row is None:
            return None
        record = ScheduleRecord.model_validate_json(row[0])
        self._records[schedule_id] = record
        return record

    # --- records -------------------------------------------------------------

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
        self._persist(record)
        log.info("schedule_created schedule_id=%s reason=%s", schedule_id, reason.value)
        return record

    def get(self, schedule_id: str) -> ScheduleRecord | None:
        record = self._records.get(schedule_id)
        if record is not None:
            return record
        return self._load(schedule_id)

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
        self._persist(record)
        return version

    def record_event(self, record: ScheduleRecord, event: ScheduleEvent) -> ScheduleEvent:
        if not event.event_id:
            event.event_id = uuid.uuid4().hex[:8]
        record.events.append(event)
        log.info(
            "schedule_event schedule_id=%s type=%s job=%s",
            record.schedule_id, event.event_type.value, event.job_id,
        )
        self._persist(record)
        return event
