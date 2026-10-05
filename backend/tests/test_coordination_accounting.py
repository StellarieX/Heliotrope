"""Coordinator assembly: one accounting pass, violations as data.

  * per-job carbon/energy in the job list always sum to the reported totals
    (both come from a single accounting pass)
  * a capacity breach is reported as DATA — `capacity_violations` plus a
    warning — in both modes, never synthesized into an INTERNAL_ERROR; solver
    failures already surface as INFEASIBLE further up
  * the congestion profile carries the measured aggregate load
"""

from app.domain.coordination import (
    CoordinationMode,
    CoordinationRequest,
    Participant,
    SharedResource,
)
from app.services.carbon_accounting import Placement
from app.services.coordinator import MultiUserCoordinator

from .fixtures import at, ev_job, make_signal


def _request(**overrides):
    jobs = [
        ev_job(id="e1", participant_id="u1", energy_required_kwh=7.2),
        ev_job(id="e2", participant_id="u2", energy_required_kwh=7.2),
    ]
    params = dict(
        participants=[Participant(id="u1"), Participant(id="u2")],
        shared_resource=SharedResource(capacity_kw=30.0),
        jobs=jobs,
    )
    params.update(overrides)
    return CoordinationRequest(**params)


def test_totals_match_the_sum_of_per_job_accounting():
    service = MultiUserCoordinator()
    signal = make_signal()
    for mode in (CoordinationMode.INDEPENDENT, CoordinationMode.COORDINATED):
        result = service.coordinate(
            _request().model_copy(update={"coordination_mode": mode}), signal
        )
        assert result.status in ("OPTIMAL", "FEASIBLE")
        assert result.metrics.total_co2_kg == round(
            sum(j.carbon_kg for j in result.jobs), 4
        )
        assert result.metrics.total_energy_kwh == round(
            sum(j.energy_kwh for j in result.jobs), 4
        )


def _violating_assemble(mode):
    service = MultiUserCoordinator()
    request = _request()
    merged = service.build_merged(request, make_signal())
    n = merged.horizon.slot_count
    # Both jobs at full power in slot 0: far above the 30 kW connection.
    placements = {
        "u1": Placement(slot_count=n),
        "u2": Placement(slot_count=n),
    }
    for job in merged.jobs:
        placements[job.participant_id].set(job.id, 0, 100_000)
    return service._assemble(
        request, merged, placements, mode, "FEASIBLE", "", None
    )


def test_independent_breach_is_data_not_error():
    result = _violating_assemble(CoordinationMode.INDEPENDENT)
    assert result.status == "FEASIBLE"
    assert result.metrics.capacity_violations > 0
    assert any("capacity" in w for w in result.warnings)


def test_coordinated_breach_is_data_not_internal_error():
    result = _violating_assemble(CoordinationMode.COORDINATED)
    assert result.status == "FEASIBLE"
    assert result.status != "INTERNAL_ERROR"
    assert result.metrics.capacity_violations > 0
    assert any("capacity" in w for w in result.warnings)


def test_congestion_profile_carries_measured_aggregate():
    service = MultiUserCoordinator()
    result = service.coordinate(_request(), make_signal())
    assert result.status in ("OPTIMAL", "FEASIBLE")
    for agg, cong in zip(result.aggregate_profile, result.congestion_profile):
        assert cong.timestamp == agg.timestamp
        assert cong.aggregate_kw == agg.total_kw
        assert cong.capacity_kw == agg.capacity_kw


def test_herding_still_reports_independent_violations_only():
    jobs = [
        ev_job(
            id=f"ev-{i}",
            participant_id=f"user-{i}",
            release_at=at(18),
            deadline_at=at(31),
        )
        for i in range(20)
    ]
    request = CoordinationRequest(
        participants=[Participant(id=f"user-{i}") for i in range(20)],
        shared_resource=SharedResource(capacity_kw=30.0),
        jobs=jobs,
    )
    service = MultiUserCoordinator()
    signal = make_signal()
    indep = service.coordinate(
        request.model_copy(
            update={"coordination_mode": CoordinationMode.INDEPENDENT}
        ),
        signal,
    )
    coord = service.coordinate(
        request.model_copy(
            update={"coordination_mode": CoordinationMode.COORDINATED}
        ),
        signal,
    )
    assert indep.metrics.capacity_violations > 0
    assert coord.metrics.capacity_violations == 0
    assert coord.status in ("OPTIMAL", "FEASIBLE")
