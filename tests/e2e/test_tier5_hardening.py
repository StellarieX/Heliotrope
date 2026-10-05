"""Tier 5: Adversarial Hardening (opaque-box abuse tests).

Fast, opaque-box tests that abuse the public HTTP API with hostile or
nonsense input and assert the backend answers with a structured error
vocabulary (400/404/422, INFEASIBLE) instead of a 500 crash.

Authoritative references: backend error vocabulary in
`backend/app/api/routes/schedule.py` (400 invalid scheduler, 422 invalid
request, INFEASIBLE is a 200), `execution.py` (404 not_found, 422
invalid_transition / invalid_request / override_rejected), and
`forecast.py` (400 unknown_model, 422 invalid_request).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.domain.loads import LoadType

from .conftest import make_load_spec, make_thermal_spec


def _plan(client: TestClient, **kw) -> str:
    """Plan one small atomic job; returns the schedule_id (asserts 200)."""
    j = make_load_spec("j1", "Washer", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    body = {"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}
    res = client.post("/api/v1/schedules/plan", json=body)
    assert res.status_code == 200, f"plan failed: {res.text}"
    return res.json()["schedule_id"]


def test_tier5_thermal_inverted_band_no_500(client: TestClient):
    """T5.1: Inverted thermal comfort band over HTTP is a 422, never a 500."""
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=50.0, target_temp=58.0, power_kw=2.0)
    j = make_load_spec("t1", "Water Heater", LoadType.THERMAL, power_kw=2.0, thermal=ts)
    payload = j.model_dump(mode="json")
    payload["thermal"]["temperature_min_c"] = 65.0
    payload["thermal"]["temperature_max_c"] = 45.0
    res = client.post("/api/v1/schedule", json={"jobs": [payload], "capacity_kw": 5.0})
    assert res.status_code != 500, f"thermal abuse crashed server: {res.text}"
    if res.status_code == 200:
        assert res.json()["status"] == "INFEASIBLE"
    else:
        assert res.status_code == 422


def test_tier5_duplicate_completed_idempotent(client: TestClient):
    """T5.2: Duplicate JOB_COMPLETED is an idempotent no-op (200, still COMPLETED) — never a 500.

    `transition()` deliberately permits same-state repeats (`to is not
    state.status` guard in `execution_events.py`), so a retried completion
    webhook must not crash or corrupt state.
    """
    sid = _plan(client)
    assert client.post(f"/api/v1/schedules/{sid}/events", json={"event_type": "JOB_STARTED", "job_id": "j1"}).status_code == 200
    first = client.post(f"/api/v1/schedules/{sid}/events", json={"event_type": "JOB_COMPLETED", "job_id": "j1"})
    assert first.status_code == 200
    second = client.post(f"/api/v1/schedules/{sid}/events", json={"event_type": "JOB_COMPLETED", "job_id": "j1"})
    assert second.status_code != 500
    assert second.status_code == 200
    jobs = second.json()["state"]["jobs"]
    assert jobs[0]["status"] == "COMPLETED"


def test_tier5_start_after_complete_rejected(client: TestClient):
    """T5.3: JOB_STARTED after JOB_COMPLETED is an invalid transition (422)."""
    sid = _plan(client)
    assert client.post(f"/api/v1/schedules/{sid}/events", json={"event_type": "JOB_STARTED", "job_id": "j1"}).status_code == 200
    assert client.post(f"/api/v1/schedules/{sid}/events", json={"event_type": "JOB_COMPLETED", "job_id": "j1"}).status_code == 200
    res = client.post(f"/api/v1/schedules/{sid}/events", json={"event_type": "JOB_STARTED", "job_id": "j1"})
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_transition"


def test_tier5_over_capacity_participants_infeasible(client: TestClient):
    """T5.4: Aggregate demand that cannot fit shared capacity is INFEASIBLE (200), not a 500."""
    j1 = make_load_spec("j1", "HVAC 1", LoadType.DEFERRABLE_ATOMIC, power_kw=3.0, duration_minutes=60,
                         release_offset_minutes=0, deadline_offset_minutes=60)
    j2 = make_load_spec("j2", "HVAC 2", LoadType.DEFERRABLE_ATOMIC, power_kw=3.0, duration_minutes=60,
                         release_offset_minutes=0, deadline_offset_minutes=60)
    j1.participant_id = "p1"
    j2.participant_id = "p2"
    payload = {
        "participants": [{"id": "p1"}, {"id": "p2"}],
        "shared_resource": {"id": "b1", "capacity_kw": 3.0},
        "jobs": [j1.model_dump(mode="json"), j2.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code != 500, f"over-capacity crashed server: {res.text}"
    assert res.status_code == 200
    assert res.json()["status"] == "INFEASIBLE"


def test_tier5_unauthenticated_open_access_documented(client: TestClient):
    """T5.5: Backend is open-access by design: plan needs no auth (200), and a
    foreign/unknown schedule_id is a structured 404 — never a 500."""
    j = make_load_spec("j1", "Washer", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    res = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0})
    assert res.status_code == 200, f"open-access plan must work without auth: {res.text}"
    assert "schedule_id" in res.json()
    foreign = client.get("/api/v1/schedules/sched_belongs_to_someone_else_zzz/state")
    assert foreign.status_code == 404
    assert foreign.json()["code"] == "not_found"
    assert foreign.status_code != 500


def test_tier5_meter_negative_energy_rejected(client: TestClient):
    """T5.6: Negative meter energy is rejected with 422, never stored or crashed on."""
    sid = _plan(client)
    res = client.post(f"/api/v1/schedules/{sid}/telemetry", json={"job_id": "j1", "energy_kwh": -5.0})
    assert res.status_code == 422, f"negative energy must be rejected: {res.text}"
    assert res.status_code != 500


def test_tier5_capacity_profile_length_mismatch_rejected(client: TestClient):
    """T5.7: Replan capacity profile with wrong slot count is a 422, not a 500."""
    sid = _plan(client)
    res = client.post(f"/api/v1/schedules/{sid}/replan", json={"capacity_profile_kw": [1.0]})
    assert res.status_code == 422, f"length mismatch must be rejected: {res.text}"
    assert res.json()["code"] == "invalid_request"


def test_tier5_forecast_unknown_model_400(client: TestClient):
    """T5.8: Unknown forecast model is a 400 unknown_model, never substituted or crashed on."""
    payload = {
        "start": "2026-10-06T08:00:00+00:00",
        "end": "2026-10-06T12:00:00+00:00",
        "model": "nope_model_xyz",
    }
    res = client.post("/api/v1/carbon/forecast", json=payload)
    assert res.status_code == 400, f"unknown model must be 400: {res.text}"
    assert res.json()["code"] == "unknown_model"


def test_tier5_tick_unknown_id_404(client: TestClient):
    """T5.9: Tick on an unknown schedule id is a structured 404, not a 500."""
    res = client.post("/api/v1/schedules/nope_nonexistent_zzz/tick", json={})
    assert res.status_code == 404
    assert res.json()["code"] == "not_found"


def test_tier5_replan_empty_move_noop(client: TestClient):
    """T5.10: Override MOVE with no window change is a no-op (200, no replan)."""
    sid = _plan(client)
    res = client.post(f"/api/v1/schedules/{sid}/override", json={"job_id": "j1", "command": "MOVE"})
    assert res.status_code == 200, f"empty MOVE must be accepted as no-op: {res.text}"
    body = res.json()
    assert body["accepted"] is True
    assert body["replanned"] is False
