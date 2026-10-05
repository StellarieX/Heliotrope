"""Time-varying shared capacity: profile respected, mismatch 422, scalar fallback.

A per-slot `capacity_profile_kw` overrides the scalar `capacity_kw` slot by
slot. The dip test puts a low-capacity window over the cheapest carbon trough:
the scalar-only run charges at full power there, while the profile run is
shaved to the dip — structurally, never numerically asserted beyond the caps.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.domain.coordination import CoordinationRequest, Participant, SharedResource
from app.main import app
from app.services.coordinator import CoordinationError, MultiUserCoordinator

from .fixtures import at, ev_job, make_signal

client = TestClient(app)

SCALAR_KW = 30.0
DIP_KW = 3.0


def _dip_request(n: int, dip_slots: set[int]) -> CoordinationRequest:
    jobs = [ev_job(id="e1", participant_id="u1", energy_required_kwh=14.4)]
    profile = [DIP_KW if s in dip_slots else SCALAR_KW for s in range(n)]
    return CoordinationRequest(
        participants=[Participant(id="u1")],
        shared_resource=SharedResource(capacity_kw=SCALAR_KW, capacity_profile_kw=profile),
        jobs=jobs,
    )


def _trough_slots() -> tuple[int, set[int]]:
    """Slot count plus the slots covering the overnight carbon trough (02-05h)."""
    probe = CoordinationRequest(
        participants=[Participant(id="u1")],
        shared_resource=SharedResource(capacity_kw=SCALAR_KW),
        jobs=[ev_job(id="e1", participant_id="u1", energy_required_kwh=14.4)],
    )
    merged = MultiUserCoordinator().build_merged(probe, make_signal())
    dip = set(merged.horizon.slot_range_for(at(26), at(29)))
    assert dip, "trough window must cover at least one slot"
    return merged.horizon.slot_count, dip


def test_profile_respected_peak_shaved_under_dip():
    n, dip = _trough_slots()
    service = MultiUserCoordinator()
    signal = make_signal()

    profiled = service.coordinate(_dip_request(n, dip), signal)
    assert profiled.status in ("OPTIMAL", "FEASIBLE")
    assert profiled.metrics.capacity_violations == 0
    for s, point in enumerate(profiled.aggregate_profile):
        cap = DIP_KW if s in dip else SCALAR_KW
        assert point.capacity_kw == cap
        assert point.total_kw <= cap + 1e-6
    # The whole 14.4 kWh job is still scheduled; the dip reshapes, not drops.
    assert sum(j.energy_kwh for j in profiled.jobs) == pytest.approx(14.4, abs=0.05)
    dip_peak = max(p.total_kw for s, p in enumerate(profiled.aggregate_profile) if s in dip)
    assert dip_peak <= DIP_KW + 1e-6

    # The scalar fallback run is free to use the full connection in the trough,
    # proving the dip binds rather than being vacuous.
    scalar_req = CoordinationRequest(
        participants=[Participant(id="u1")],
        shared_resource=SharedResource(capacity_kw=SCALAR_KW),
        jobs=[ev_job(id="e1", participant_id="u1", energy_required_kwh=14.4)],
    )
    scalar = service.coordinate(scalar_req, signal)
    assert scalar.status in ("OPTIMAL", "FEASIBLE")
    scalar_dip_peak = max(p.total_kw for s, p in enumerate(scalar.aggregate_profile) if s in dip)
    assert scalar_dip_peak > DIP_KW + 1e-6


def test_profile_length_mismatch_422():
    n, dip = _trough_slots()
    short = [SCALAR_KW] * (n - 1)
    body = {
        "participants": [{"id": "u1"}],
        "shared_resource": {"capacity_kw": SCALAR_KW, "capacity_profile_kw": short},
        "jobs": [
            {
                "id": "e1",
                "normalized_name": "Hostel EV",
                "category": "EV charging",
                "job_type": "DEFERRABLE_INTERRUPTIBLE",
                "power_kw": 7.2,
                "max_power_kw": 7.2,
                "energy_required_kwh": 14.4,
                "min_chunk_minutes": 15,
                "release_at": at(18).isoformat(),
                "deadline_at": at(31).isoformat(),
                "participant_id": "u1",
            }
        ],
    }
    res = client.post("/api/v1/coordination/schedule", json=body)
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"

    # Same mismatch at the service layer raises CoordinationError, not a crash.
    req = CoordinationRequest(
        participants=[Participant(id="u1")],
        shared_resource=SharedResource(capacity_kw=SCALAR_KW, capacity_profile_kw=short),
        jobs=[ev_job(id="e1", participant_id="u1", energy_required_kwh=14.4)],
    )
    try:
        MultiUserCoordinator().coordinate(req, make_signal())
    except CoordinationError as exc:
        assert "capacity profile has" in str(exc)
    else:
        raise AssertionError("mismatched profile length must raise CoordinationError")


def test_scalar_fallback_unchanged():
    req = CoordinationRequest(
        participants=[Participant(id="u1")],
        shared_resource=SharedResource(capacity_kw=SCALAR_KW),
        jobs=[ev_job(id="e1", participant_id="u1", energy_required_kwh=14.4)],
    )
    service = MultiUserCoordinator()
    merged = service.build_merged(req, make_signal())
    assert merged.capacity_profile_w is None
    assert all(merged.capacity_at(s) == merged.capacity_w for s in range(merged.horizon.slot_count))
    result = service.coordinate(req, make_signal())
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert result.metrics.capacity_violations == 0
    assert all(p.capacity_kw == SCALAR_KW for p in result.aggregate_profile)
    assert all(p.total_kw <= SCALAR_KW + 1e-6 for p in result.aggregate_profile)
