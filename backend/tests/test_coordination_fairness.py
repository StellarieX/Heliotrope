"""AVG fairness measures the same per-participant quantity as MAX."""

from datetime import timedelta

import pytest

from app.domain.coordination import FairnessMode
from app.domain.loads import LoadSpec, LoadType
from app.services.coordinated_cpsat import CoordinatedCPSATScheduler
from app.services.scheduler_service import SchedulerService

from .fixtures import DAY_START
from .test_greedy_blocks import signal


def _job(job_id: str) -> LoadSpec:
    return LoadSpec(
        id=job_id, normalized_name=job_id, category="Laundry",
        job_type=LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=15,
        release_at=DAY_START, deadline_at=DAY_START + timedelta(minutes=75),
    )


def _solve(mode: FairnessMode, priority=None):
    # Five one-slot jobs share a connection that holds one at a time, so they
    # occupy slots 0-4 in some order; everyone prefers slot 0 and carbon is flat.
    owners = {"a1": "A", "a2": "A", "a3": "A", "a4": "A", "b1": "B"}
    jobs = [_job(j) for j in owners]
    scheduler_input, _ = SchedulerService().build_input(
        jobs, signal([100.0] * 5), capacity_kw=2.0
    )
    scheduler = CoordinatedCPSATScheduler(
        preferred_starts={j: 0 for j in owners},
        target_w=10_000,
        congestion_weight=0.0,
        fairness_mode=mode,
        participant_of=owners,
        priority_weight=priority,
    )
    result = scheduler.schedule(scheduler_input)
    assert result.status.value in ("OPTIMAL", "FEASIBLE")
    return {s.job_id: s.start_slot for s in result.schedule}


def test_avg_fairness_does_not_favour_the_participant_with_many_jobs():
    starts = _solve(FairnessMode.AVG)
    # Participant B's single job is worth four of A's per-job; summing delay
    # over all jobs (the old behaviour) leaves every ordering tied.
    assert starts["b1"] == 0


def test_max_fairness_with_priority_above_one_is_not_capped():
    # Priority 5 on A scales its weighted delay well past the largest single-job
    # delay; the model must still solve and place every job.
    starts = _solve(FairnessMode.MAX, priority={"A": 5.0, "B": 1.0})
    assert sorted(starts.values()) == [0, 1, 2, 3, 4]
