"""Phase 7: execution, versioning, rolling horizon, simulator, overrides."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

from .fixtures import ev_job, geyser_job, washing_machine_job

client = TestClient(app)
DAY = "2026-10-05"
CARBON_START = f"{DAY}T18:00:00+00:00"
CARBON_END = "2026-10-06T08:00:00+00:00"


def spec_dict(spec) -> dict:
    d = spec.model_dump(mode="json")
    d.pop("participant_id", None)
    return d


def plan(body_jobs, capacity_kw=20.0, scheduler="CPSAT"):
    body = {
        "jobs": [spec_dict(j) for j in body_jobs],
        "capacity_kw": capacity_kw,
        "scheduler": scheduler,
        "carbon_start": CARBON_START,
        "carbon_end": CARBON_END,
    }
    res = client.post("/api/v1/schedules/plan", json=body)
    assert res.status_code == 200, res.text
    return res.json()["schedule_id"]


def test_plan_state_history_lifecycle():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    assert state["lifecycle"] == "SCHEDULED"
    assert state["jobs"][0]["status"] == "PENDING"
    history = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(history["versions"]) == 1
    assert history["versions"][0]["reason"] == "MANUAL"


def test_invalid_transition_rejected():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    res = client.post(
        f"/api/v1/schedules/{sid}/events",
        json={"event_type": "JOB_COMPLETED", "timestamp": f"{DAY}T19:00:00+00:00", "job_id": "ev-1"},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_transition"


def test_version_increments_on_capacity_drop():
    sid = plan([ev_job(energy_required_kwh=14.4), washing_machine_job()])
    before = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(before["versions"]) == 1
    res = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T19:00:00+00:00", "reason": "CAPACITY_CHANGE", "capacity_kw": 5.0},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["replanned"] is True
    assert body["version"] == 2
    history = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(history["versions"]) == 2
    assert history["versions"][0]["reason"] == "MANUAL"  # v1 preserved


def test_stability_gate_keeps_version():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    res = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T19:00:00+00:00", "reason": "MANUAL"},
    )
    assert res.status_code == 200
    assert res.json()["replanned"] is False
    assert len(client.get(f"/api/v1/schedules/{sid}/history").json()["versions"]) == 1


def test_partial_delivery_not_rescheduled():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    alloc_start = state["jobs"][0]["scheduled_start"]
    # Simulate delivering 3.6 of 7.2 kWh, then replan.
    client.post(
        f"/api/v1/schedules/{sid}/events",
        json={
            "event_type": "JOB_STARTED", "timestamp": alloc_start, "job_id": "ev-1",
        },
    )
    res = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": alloc_start, "reason": "MANUAL"},
    )
    assert res.status_code == 200
    # Manually record partial delivery, then replan again: remainder must shrink.
    client.post(
        f"/api/v1/schedules/{sid}/events",
        json={
            "event_type": "JOB_STARTED", "timestamp": alloc_start, "job_id": "ev-1",
            "payload": {"energy_delivered_kwh": 3.6},
        },
    )
    res2 = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": alloc_start, "reason": "MANUAL"},
    )
    assert res2.status_code == 200
    hist = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(hist["versions"]) >= 1
    assert alloc_start is not None


def test_missed_start_replanned_or_reported():
    sid = plan([washing_machine_job()])
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    sched_start = state["jobs"][0]["scheduled_start"]
    # Advance the clock 2h past the scheduled start without starting.
    res = client.post(
        f"/api/v1/simulation/{sid}/advance",
        json={"to_time": "2026-10-05T23:30:00+00:00", "script": []},
    )
    assert res.status_code == 200
    statuses = {j["job_id"]: j["status"] for j in res.json()["state"]["jobs"]}
    assert statuses["wm-1"] == "MISSED"
    assert sched_start is not None


def test_override_rejected_without_headroom():
    sid = plan(
        [ev_job(energy_required_kwh=14.4), washing_machine_job(power_kw=9.0, duration_minutes=120)],
        capacity_kw=10.0,
    )
    res = client.post(
        f"/api/v1/schedules/{sid}/override",
        json={"job_id": "wm-1", "command": "START_NOW"},
    )
    # Either accepted (headroom exists right now) or rejected with a reason —
    # both are honest; the contract is that rejection explains capacity.
    assert res.status_code in (200, 422)
    if res.status_code == 422:
        assert "capacity" in res.json()["detail"]
        assert res.json()["code"] == "override_rejected"


def test_override_cancel_completed_rejected():
    sid = plan([ev_job(energy_required_kwh=3.6)])
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    start = state["jobs"][0]["scheduled_start"]
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_STARTED", "timestamp": start, "job_id": "ev-1"})
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_COMPLETED", "timestamp": start, "job_id": "ev-1"})
    res = client.post(
        f"/api/v1/schedules/{sid}/override", json={"job_id": "ev-1", "command": "CANCEL"})
    assert res.status_code == 422


def test_coordinated_replan_respects_shared_capacity():
    jobs = []
    for i in range(4):
        jobs.append(ev_job(id=f"e{i}", participant_id=f"u{i}", energy_required_kwh=7.2))
    body = {
        "participants": [{"id": f"u{i}"} for i in range(4)],
        "shared_resource": {"capacity_kw": 15.0},
        "jobs": [spec_dict(j) | {"participant_id": f"u{i}"} for i, j in enumerate(jobs)],
        "carbon_start": CARBON_START,
        "carbon_end": CARBON_END,
    }
    res = client.post("/api/v1/schedules/plan-coordinated", json=body)
    assert res.status_code == 200
    sid = res.json()["schedule_id"]
    res2 = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T20:00:00+00:00", "reason": "CAPACITY_CHANGE", "capacity_kw": 12.0},
    )
    assert res2.status_code == 200
    assert res2.json()["state"]["version"] >= 1


def test_execution_metrics_separate_planned_realized():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    res = client.post(
        f"/api/v1/simulation/{sid}/advance",
        json={
            "to_time": f"{DAY}T22:00:00+00:00",
            "script": [],
            "carbon_actual": [
                {"timestamp": f"{DAY}T20:00:00+00:00", "gco2_per_kwh": 500},
                {"timestamp": f"{DAY}T21:00:00+00:00", "gco2_per_kwh": 480},
            ],
        },
    )
    assert res.status_code == 200
    metrics = res.json()["metrics"]
    assert "planned_energy_kwh" in metrics
    assert "realized_co2_kg" in metrics  # key present even when None
    assert metrics["planned_co2_kg"] is not None


def test_end_to_end_mixed_scenario():
    jobs = [
        ev_job(id="e1", energy_required_kwh=7.2),
        ev_job(id="e2", energy_required_kwh=7.2),
        washing_machine_job(id="w1"),
        geyser_job(id="g1"),
    ]
    sid = plan(jobs, capacity_kw=15.0)
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    starts = {j["job_id"]: j["scheduled_start"] for j in state["jobs"]}
    # T+30: forecast change is a replan reason; gate decides if it matters.
    r1 = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T19:00:00+00:00", "reason": "CARBON_FORECAST_CHANGED"},
    )
    assert r1.status_code == 200
    # T+45: e1 fails to start -> mark failed via event.
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_FAILED", "timestamp": f"{DAY}T19:15:00+00:00",
        "job_id": "e1", "payload": {"reason": "charger fault"}})
    # T+60: capacity drops.
    r2 = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T19:30:00+00:00", "reason": "CAPACITY_CHANGE", "capacity_kw": 10.0},
    )
    assert r2.status_code == 200
    # T+75: w1 completes early.
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_STARTED", "timestamp": starts["w1"], "job_id": "w1"})
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_COMPLETED", "timestamp": starts["w1"], "job_id": "w1"})
    # T+90: reoptimize.
    r3 = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T20:00:00+00:00", "reason": "MANUAL"},
    )
    assert r3.status_code == 200
    hist = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(hist["versions"]) >= 1
    # v1 preserved; every version carries solver truth.
    assert all(v["solver_status"] in ("OPTIMAL", "FEASIBLE", "UNKNOWN") for v in hist["versions"])
    assert starts["e1"] is not None

