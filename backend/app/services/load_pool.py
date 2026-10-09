"""Anti-herding load pool.

Aggregates the planned power of all active live schedules per horizon slot so a
new plan can steer away from slots other users have already crowded. Only
aggregate watts per slot ever leave this module: no schedule ids, job names or
per-user data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from ..core import config
from ..domain.horizon import SLOT_MINUTES
from ..domain.execution import JobStatus, ScheduleLifecycle
from ..domain.scheduling import SchedulerInput
from ..utils.time import generate_slots
from .execution_store import ExecutionStore

_DEAD_LIFECYCLES = frozenset(
    {
        ScheduleLifecycle.COMPLETED,
        ScheduleLifecycle.PARTIALLY_COMPLETED,
        ScheduleLifecycle.CANCELLED,
        ScheduleLifecycle.FAILED,
    }
)
_DEAD_JOB_STATUSES = frozenset({JobStatus.CANCELLED, JobStatus.COMPLETED, JobStatus.FAILED})


@dataclass(frozen=True)
class PoolSnapshot:
    pooled_w: list[int]
    active_schedules: int

    @property
    def peak_kw(self) -> float:
        return round(max(self.pooled_w, default=0) / 1000.0, 3)


def _live_allocations(store: ExecutionStore, exclude_schedule_ids: Iterable[str] = ()):
    """Yield (schedule_id, job_id, allocation, slot_minutes) for every planned
    allocation of every live, unfinished job. Internal: callers aggregate and never
    let the ids leave the process."""
    excluded = set(exclude_schedule_ids)
    for record in store.list_records():
        if record.schedule_id in excluded or record.lifecycle in _DEAD_LIFECYCLES:
            continue
        if not record.versions:
            continue
        slot_minutes = (
            record.scheduler_input.horizon.slot_minutes if record.scheduler_input else SLOT_MINUTES
        )
        for scheduled in record.current_version().result.schedule:
            state = record.execution.get(scheduled.job_id)
            if state is not None and state.status in _DEAD_JOB_STATUSES:
                continue
            for alloc in scheduled.allocations:
                if alloc.power_w > 0:
                    yield record.schedule_id, scheduled.job_id, alloc, slot_minutes


def compute_pool(
    store: ExecutionStore,
    horizon,
    exclude_schedule_ids: Iterable[str] = (),
) -> PoolSnapshot:
    """Pooled planned watts per slot of `horizon`.

    An allocation holds its power for the slot length of the plan it came from, so it
    is spread over every horizon slot it overlaps by time (mean power per slot). Plans
    on a different slot length or an unaligned start therefore still count; for an
    identical grid this reduces to an exact per-slot sum.
    """
    count = horizon.slot_count
    slot_len = timedelta(minutes=horizon.slot_minutes)
    origin = horizon.slot_start(0)
    energy_wh = [0.0] * count
    touched: set[str] = set()
    for schedule_id, _job_id, alloc, slot_minutes in _live_allocations(store, exclude_schedule_ids):
        a0 = alloc.timestamp
        a1 = a0 + timedelta(minutes=slot_minutes)
        first = max(0, int((a0 - origin) // slot_len))
        for i in range(first, count):
            b0 = origin + i * slot_len
            if b0 >= a1:
                break
            overlap = (min(a1, b0 + slot_len) - max(a0, b0)).total_seconds() / 3600.0
            if overlap > 0:
                energy_wh[i] += alloc.power_w * overlap
                touched.add(schedule_id)
    hours = horizon.slot_minutes / 60.0
    pooled = [int(round(e / hours)) for e in energy_wh]
    return PoolSnapshot(pooled_w=pooled, active_schedules=len(touched))


@dataclass(frozen=True)
class PoolStats:
    """Aggregate planned load of all live schedules over a window. No ids."""

    slot_starts: list[datetime]
    kw: list[float]
    energy_kwh: list[float]
    active_schedules: int
    active_loads: int

    @property
    def total_planned_kwh(self) -> float:
        return round(sum(self.energy_kwh), 3)

    @property
    def peak_kw(self) -> float:
        return round(max(self.kw, default=0.0), 3)

    @property
    def peak_at(self) -> Optional[datetime]:
        if not self.kw or max(self.kw) <= 0:
            return None
        return self.slot_starts[self.kw.index(max(self.kw))]

    def _mean_loaded(self) -> float:
        loaded = [k for k in self.kw if k > 0]
        return sum(loaded) / len(loaded) if loaded else 0.0

    @property
    def average_kw(self) -> float:
        """Mean over the slots that carry any load (0 when none do)."""
        return round(self._mean_loaded(), 3)

    @property
    def peak_to_average(self) -> Optional[float]:
        """Herding indicator: 1.0 is perfectly flat, larger is more crowded."""
        avg = self._mean_loaded()
        return round(max(self.kw) / avg, 3) if avg > 0 else None


def compute_pool_stats(
    store: ExecutionStore, start: datetime, end: datetime, resolution_minutes: int
) -> PoolStats:
    """Planned kW of all live schedules per `resolution_minutes` bucket of [start, end).

    An allocation holds its power for the slot length of the plan it came from; it is
    spread over every bucket it overlaps by time, so any resolution gives the same
    energy. Bucket kW is that energy divided by the bucket length (mean power).
    """
    starts = generate_slots(start, end, resolution_minutes)
    origin = starts[0]
    res = timedelta(minutes=resolution_minutes)
    energy_wh = [0.0] * len(starts)
    schedules: set[str] = set()
    loads: set[tuple[str, str]] = set()
    for schedule_id, job_id, alloc, slot_minutes in _live_allocations(store):
        a0 = alloc.timestamp
        a1 = a0 + timedelta(minutes=slot_minutes)
        first = max(0, int((a0 - origin) // res))
        for i in range(first, len(starts)):
            b0 = starts[i]
            if b0 >= a1:
                break
            overlap = (min(a1, b0 + res) - max(a0, b0)).total_seconds() / 3600.0
            if overlap > 0:
                energy_wh[i] += alloc.power_w * overlap
                schedules.add(schedule_id)
                loads.add((schedule_id, job_id))
    hours = resolution_minutes / 60.0
    kwh = [round(w / 1000.0, 6) for w in energy_wh]
    return PoolStats(
        slot_starts=starts,
        kw=[round(e / hours, 3) for e in kwh],
        energy_kwh=kwh,
        active_schedules=len(schedules),
        active_loads=len(loads),
    )


def attach_pool(
    store: ExecutionStore,
    scheduler_input: SchedulerInput,
    exclude_schedule_ids: Iterable[str] = (),
) -> tuple[SchedulerInput, dict]:
    """Return the input with the pool attached plus the aggregate-only summary."""
    if not config.POOL_ENABLED:
        return scheduler_input, pool_summary(None)
    snap = compute_pool(store, scheduler_input.horizon, exclude_schedule_ids)
    attached = scheduler_input.model_copy(
        update={
            "pooled_load_w": snap.pooled_w,
            "pool_beta": config.POOL_BETA,
            "pool_ref_w": config.POOL_REF_KW * 1000.0,
            "pool_scale_w": max(1.0, config.POOL_SCALE_KW * 1000.0),
        }
    )
    return attached, pool_summary(snap)


def pool_summary(snap: Optional[PoolSnapshot]) -> dict:
    if snap is None:
        return {"applied": False, "active_schedules": 0, "peak_pooled_kw": 0.0, "beta": 0.0}
    return {
        "applied": True,
        "active_schedules": snap.active_schedules,
        "peak_pooled_kw": snap.peak_kw,
        "beta": config.POOL_BETA,
    }
