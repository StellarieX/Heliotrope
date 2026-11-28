"""Owner-scoped execution storage backed by Supabase Postgres."""
from __future__ import annotations

import uuid
from contextlib import nullcontext

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from ..core import config
from ..core.auth import RequestContext, current_request
from ..domain.execution import RescheduleReason, ScheduleRecord
from ..domain.scheduling import SchedulerInput, SchedulerResult
from .execution_store import ExecutionStore


_LIVE_RECORDS = """
    SELECT data || jsonb_build_object('versions', jsonb_build_array(data -> 'versions' -> -1), 'events', '[]'::jsonb)
    FROM private.execution_schedules
    WHERE lifecycle IN ('DRAFT', 'SCHEDULED', 'ACTIVE')
"""


class PostgresExecutionStore(ExecutionStore):
    def __init__(self) -> None:
        # Shared mutation helpers stage changes on the request's private record.
        # The row lock, acquired on reads for updates, guards concurrent writes.
        self._lock = nullcontext()

    @staticmethod
    def _context() -> RequestContext:
        context = current_request.get()
        if context is None:
            raise HTTPException(503, "Execution session required")
        return context

    @staticmethod
    def _connect() -> psycopg.Connection:
        if not config.DATABASE_URL:
            raise HTTPException(503, "Execution storage is not configured")
        try:
            return psycopg.connect(config.DATABASE_URL, prepare_threshold=None, connect_timeout=5)
        except psycopg.OperationalError as exc:
            raise HTTPException(503, "Execution storage unavailable") from exc

    def _connection(self, context: RequestContext) -> psycopg.Connection:
        if context.connection is None:
            connection = self._connect()
            context.connection = connection
            context.finish = lambda succeeded: self._finish(context, connection, succeeded)
        return context.connection

    @staticmethod
    def _finish(context: RequestContext, connection: psycopg.Connection, succeeded: bool) -> None:
        # psycopg's connection context rolls back on exceptions, commits on
        # success and closes in either case. Each changed record is written once.
        with connection:
            if not succeeded:
                connection.rollback()
                return
            for schedule_id, record in context.pending.items():
                payload = Jsonb(record.model_dump(mode="json"))
                if schedule_id in context.new_ids:
                    connection.execute(
                        "INSERT INTO private.execution_schedules (schedule_id, owner_id, lifecycle, data) VALUES (%s, %s, %s, %s)",
                        (schedule_id, context.owner_id, record.lifecycle.value, payload),
                    )
                else:
                    connection.execute(
                        """UPDATE private.execution_schedules SET lifecycle = %s, data = %s,
                           revision = revision + 1, updated_at = now()
                           WHERE schedule_id = %s AND owner_id = %s""",
                        (record.lifecycle.value, payload, schedule_id, context.owner_id),
                    )

    def create(
        self,
        scheduler_input: SchedulerInput,
        result: SchedulerResult,
        reason: RescheduleReason = RescheduleReason.MANUAL,
    ) -> ScheduleRecord:
        context = self._context()
        record = self._new_record(uuid.uuid4().hex, scheduler_input, result, reason)
        context.new_ids.add(record.schedule_id)
        self._persist(record)
        return record

    def _persist(self, record: ScheduleRecord) -> None:
        context = self._context()
        self._connection(context)
        context.records[record.schedule_id] = record
        context.pending[record.schedule_id] = record

    def get(self, schedule_id: str, *, for_update: bool = True) -> ScheduleRecord | None:
        context = self._context()
        if schedule_id in context.records:
            return context.records[schedule_id]
        query = "SELECT data FROM private.execution_schedules WHERE schedule_id = %s AND owner_id = %s"
        if for_update:
            query += " FOR UPDATE"
        row = self._connection(context).execute(query, (schedule_id, context.owner_id)).fetchone()
        if row is None:
            return None
        record = ScheduleRecord.model_validate(row[0])
        context.records[schedule_id] = record
        return record

    def list_records(self) -> list[ScheduleRecord]:
        # Only the aggregate pool consumes these records. Skip closed plans,
        # historical versions and event logs at the database boundary.
        context = current_request.get()
        if context is not None:
            rows = self._connection(context).execute(_LIVE_RECORDS).fetchall()
        elif config.DATABASE_URL:
            with self._connect() as connection:
                rows = connection.execute(_LIVE_RECORDS).fetchall()
        else:
            return []
        return [ScheduleRecord.model_validate(row[0]) for row in rows]

    def owner_has_schedule(self, schedule_id: str, owner_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM private.execution_schedules WHERE schedule_id = %s AND owner_id = %s",
                (schedule_id, owner_id),
            ).fetchone()
        return row is not None

    def latest_active(self) -> ScheduleRecord | None:
        context = self._context()
        row = self._connection(context).execute(
            """SELECT data FROM private.execution_schedules
               WHERE owner_id = %s AND lifecycle IN ('DRAFT', 'SCHEDULED', 'ACTIVE')
               ORDER BY updated_at DESC, schedule_id LIMIT 1""",
            (context.owner_id,),
        ).fetchone()
        return ScheduleRecord.model_validate(row[0]) if row else None
