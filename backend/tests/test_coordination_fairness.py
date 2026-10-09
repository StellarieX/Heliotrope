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


def _solve(mode: FairnessMode, priority=None, capture=None):
    # Five one-slot jobs share a connection that holds one at a time, so they
    # occupy slots 0-4 in some order; everyone prefers slot 0 and carbon is flat.
    owners = {"m1": "A", "m2": "A", "m3": "A", "m4": "A", "m0": "B"}
    jobs = [_job(j) for j in owners]
    scheduler_input, _ = SchedulerService().build_input(
        jobs, signal([100.0] * 5), capacity_kw=2.0
    )
    cls = CoordinatedCPSATScheduler
    if capture is not None:

        class Capturing(CoordinatedCPSATScheduler):
            def _extra_terms(self, model, scheduler_input, ctx):
                capture["model"] = model
                return super()._extra_terms(model, scheduler_input, ctx)

        cls = Capturing
    scheduler = cls(
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
    assert starts["m0"] == 0


def test_max_fairness_with_priority_above_one_is_not_capped():
    # Priority 5 on A scales its weighted delay well past the largest single-job
    # delay; the model must still solve and place every job.
    starts = _solve(FairnessMode.MAX, priority={"A": 5.0, "B": 1.0})
    assert sorted(starts.values()) == [0, 1, 2, 3, 4]


def test_avg_objective_weighs_each_participant_once():
    capture: dict = {}
    _solve(FairnessMode.AVG, capture=capture)
    proto = capture["model"].Proto()
    names = [v.name for v in proto.variables]
    coeff = {
        names[i]: c for i, c in zip(proto.objective.vars, proto.objective.coeffs)
    }
    single = coeff["delay_m0"]
    # Participant A has four jobs, so each carries a quarter of B's weight.
    for job in ("m1", "m2", "m3", "m4"):
        assert coeff[f"delay_{job}"] * 4 == pytest.approx(single, abs=4)
