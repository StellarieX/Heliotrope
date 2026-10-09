"""Anti-herding load pool.

Aggregates the planned power of all active live schedules per horizon slot so a
new plan can steer away from slots other users have already crowded. Only
aggregate watts per slot ever leave this module: no schedule ids, job names or
per-user data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from ..core import config
from ..domain.execution import JobStatus, ScheduleLifecycle
from ..domain.scheduling import SchedulerInput
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


def compute_pool(
    store: ExecutionStore,
    horizon,
    exclude_schedule_ids: Iterable[str] = (),
) -> PoolSnapshot:
    """Pooled planned watts per slot of `horizon`, matched by slot timestamp."""
    excluded = set(exclude_schedule_ids)
    index = {horizon.slot_start(i): i for i in range(horizon.slot_count)}
    pooled = [0] * horizon.slot_count
    active = 0
    for record in store.list_records():
        if record.schedule_id in excluded or record.lifecycle in _DEAD_LIFECYCLES:
            continue
        if not record.versions:
            continue
        touched = False
        for scheduled in record.current_version().result.schedule:
            state = record.execution.get(scheduled.job_id)
            if state is not None and state.status in _DEAD_JOB_STATUSES:
                continue
            for alloc in scheduled.allocations:
                i = index.get(alloc.timestamp)
                if i is not None and alloc.power_w > 0:
                    pooled[i] += alloc.power_w
                    touched = True
        if touched:
            active += 1
    return PoolSnapshot(pooled_w=pooled, active_schedules=active)


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
