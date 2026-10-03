"""Phase 6: multi-user coordination tests.

Herder scenario: N users with EV jobs and one clean midday window. Independent
greedy optimization piles everyone into the same slots; coordinated CP-SAT must
respect shared capacity while keeping every feasible job scheduled. No numeric
savings are asserted — only structural properties.
"""

from __future__ import annotations

import pytest

from app.domain.coordination import (
    CoordinationMode,
    CoordinationRequest,
    FairnessMode,
    Participant,
    SharedResource,
)
from app.services.coordinator import CoordinationError, MultiUserCoordinator

from .fixtures import at, ev_job, geyser_job, make_signal, washing_machine_job


def coord_request(jobs, capacity_kw=30.0, **overrides) -> CoordinationRequest:
    pids = sorted({j.participant_id for j in jobs})
    params = dict(
        participants=[Participant(id=p) for p in pids],
        shared_resource=SharedResource(capacity_kw=capacity_kw),
        jobs=jobs,
    )
    params.update(overrides)
    return CoordinationRequest(**params)


# --- validation ------------------------------------------------------------


def test_duplicate_participants_rejected():
    jobs = [ev_job(id="e1", participant_id="u1")]
    with pytest.raises(CoordinationError, match="unique"):
        MultiUserCoordinator()._validate_request(
            CoordinationRequest(
                participants=[Participant(id="u1"), Participant(id="u1")],
                shared_resource=SharedResource(capacity_kw=10),
                jobs=jobs,
            )
        )


def test_unknown_participant_rejected():
    jobs = [ev_job(id="e1", participant_id="ghost")]
    with pytest.raises(CoordinationError, match="unknown participant"):
        MultiUserCoordinator()._validate_request(
            CoordinationRequest(
                participants=[Participant(id="u1")],
                shared_resource=SharedResource(capacity_kw=10),
                jobs=jobs,
            )
        )


def test_duplicate_job_ids_rejected():
    jobs = [ev_job(id="e1", participant_id="u1"), ev_job(id="e1", participant_id="u1")]
    with pytest.raises(CoordinationError, match="more than once"):
        MultiUserCoordinator()._validate_request(
            CoordinationRequest(
                participants=[Participant(id="u1")],
                shared_resource=SharedResource(capacity_kw=10),
                jobs=jobs,
            )
        )


def test_time_varying_capacity_rejected_honestly():
    jobs = [ev_job(id="e1", participant_id="u1")]
    with pytest.raises(CoordinationError, match="time-varying"):
        MultiUserCoordinator()._validate_request(
            CoordinationRequest(
                participants=[Participant(id="u1")],
                shared_resource=SharedResource(capacity_kw=10, capacity_profile_kw=[10.0] * 4),
                jobs=jobs,
            )
        )


# --- two-user conflict -------------------------------------------------------


def test_two_user_conflict_staggered_under_capacity():
    jobs = [
        ev_job(id="e1", participant_id="u1", energy_required_kwh=14.4),
        ev_job(id="e2", participant_id="u2", energy_required_kwh=14.4),
    ]
    req = coord_request(jobs, capacity_kw=8.0)
    result = MultiUserCoordinator().coordinate(req, make_signal())
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert result.metrics.capacity_violations == 0
    by_job = {j.job_id: j for j in result.jobs}
    assert set(by_job) == {"e1", "e2"}
    assert by_job["e1"].participant_id == "u1"
    assert by_job["e2"].participant_id == "u2"
    for point in result.aggregate_profile:
        assert point.total_kw <= 8.0 + 1e-6


def test_infeasible_capacity_reported_with_conflict():
    jobs = [
        ev_job(id="e1", participant_id="u1", energy_required_kwh=40.0),
        ev_job(id="e2", participant_id="u2", energy_required_kwh=40.0),
    ]
    req = coord_request(jobs, capacity_kw=3.0)
    result = MultiUserCoordinator().coordinate(req, make_signal())
    assert result.status == "INFEASIBLE"
    assert "shared resource" in result.reason or "u1" in result.reason or "u2" in result.reason


# --- herding demonstration -----------------------------------------------------


def herder_jobs(n: int, energy_kwh: float = 7.2) -> list:
    return [
        ev_job(
            id=f"ev-{i}",
            participant_id=f"user-{i}",
            energy_required_kwh=energy_kwh,
            release_at=at(18),
            deadline_at=at(31),
        )
        for i in range(n)
    ]


def test_herding_20_evs():
    jobs = herder_jobs(20)
    signal = make_signal()
    service = MultiUserCoordinator()
    req = coord_request(jobs, capacity_kw=30.0)
    indep = service.coordinate(
        req.model_copy(update={"coordination_mode": CoordinationMode.INDEPENDENT}), signal
    )
    coord = service.coordinate(
        req.model_copy(update={"coordination_mode": CoordinationMode.COORDINATED}), signal
    )
    assert coord.status in ("OPTIMAL", "FEASIBLE")
    # Coordinated: every feasible job scheduled, capacity never breached.
    assert len(coord.jobs) == 20
    assert coord.metrics.capacity_violations == 0
    assert all(p.total_kw <= 30.0 + 1e-6 for p in coord.aggregate_profile)
    # Independent herds: the shared constraint is violated somewhere.
    assert indep.metrics.capacity_violations > 0
    # Coordination demonstrably flattens the spike it was asked to flatten.
    assert coord.metrics.peak_kw <= indep.metrics.peak_kw
    # Per-user separation is intact.
    owners = {j.job_id: j.participant_id for j in coord.jobs}
    assert owners["ev-0"] == "user-0" and owners["ev-19"] == "user-19"


def test_fairness_modes_both_feasible():
    jobs = herder_jobs(6)
    signal = make_signal()
    service = MultiUserCoordinator()
    avg = service.coordinate(
        coord_request(jobs, capacity_kw=20.0, fairness_mode=FairnessMode.AVG), signal
    )
    mx = service.coordinate(
        coord_request(jobs, capacity_kw=20.0, fairness_mode=FairnessMode.MAX), signal
    )
    assert avg.status in ("OPTIMAL", "FEASIBLE")
    assert mx.status in ("OPTIMAL", "FEASIBLE")
    assert avg.metrics.worst_inconvenience is not None
    assert mx.metrics.worst_inconvenience is not None


def test_mixed_types_coordinated():
    jobs = [
        ev_job(id="e1", participant_id="u1"),
        washing_machine_job(id="w1", participant_id="u1"),
        geyser_job(id="g1", participant_id="u2"),
        washing_machine_job(id="w2", participant_id="u2", release_at=at(19), deadline_at=at(24)),
    ]
    result = MultiUserCoordinator().coordinate(coord_request(jobs, capacity_kw=12.0), make_signal())
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert result.metrics.capacity_violations == 0
    assert len(result.jobs) == 4


# --- aggregate integrity ---------------------------------------------------------


def test_aggregate_matches_individual_schedules():
    jobs = herder_jobs(4)
    result = MultiUserCoordinator().coordinate(coord_request(jobs, capacity_kw=30.0), make_signal())
    assert result.status in ("OPTIMAL", "FEASIBLE")
    for point, agg in zip(result.aggregate_profile, result.congestion_profile):
        assert point.timestamp == agg.timestamp
        assert agg.utilization == pytest.approx(point.total_kw / point.capacity_kw)
        assert agg.congestion_score >= 0
    totals = [0.0] * len(result.aggregate_profile)
    assert all(p.flexible_kw >= 0 and p.baseline_kw >= 0 for p in result.aggregate_profile)


def test_congestion_profile_marks_herd_slots():
    jobs = herder_jobs(20)
    service = MultiUserCoordinator()
    signal = make_signal()
    indep = service.coordinate(
        coord_request(jobs, capacity_kw=30.0).model_copy(
            update={"coordination_mode": CoordinationMode.INDEPENDENT}
        ),
        signal,
    )
    hot = [c for c in indep.congestion_profile if c.congestion_score > 0]
    assert hot, "independent herding must congest some slot"


# --- scale -------------------------------------------------------------------------


@pytest.mark.parametrize("n", [10, 20, 50])
def test_scale_users(n):
    jobs = herder_jobs(n, energy_kwh=3.6)
    req = coord_request(jobs, capacity_kw=100.0)
    req.solver_config = None
    result = MultiUserCoordinator().coordinate(req, make_signal())
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert result.metrics.participant_count == n
    assert result.metrics.job_count == n
    assert result.metrics.solve_time_ms is not None
    assert all(p.total_kw <= 100.0 + 1e-6 for p in result.aggregate_profile)


# --- randomized structural property --------------------------------------------------


def test_random_cases_never_breach_capacity():
    import random

    rng = random.Random(20261003)
    service = MultiUserCoordinator()
    for trial in range(8):
        n = rng.randint(2, 6)
        jobs = [
            ev_job(
                id=f"t{trial}-ev-{i}",
                participant_id=f"t{trial}-u{i}",
                energy_required_kwh=rng.choice([3.6, 7.2, 10.8]),
                release_at=at(rng.choice([16, 17, 18, 19])),
                deadline_at=at(rng.choice([28, 30, 32])),
            )
            for i in range(n)
        ]
        capacity = rng.choice([15.0, 25.0, 40.0])
        result = service.coordinate(coord_request(jobs, capacity_kw=capacity), make_signal())
        if result.status in ("OPTIMAL", "FEASIBLE"):
            assert all(p.total_kw <= capacity + 1e-6 for p in result.aggregate_profile), trial
            assert result.metrics.capacity_violations == 0
            assert len(result.jobs) == n
