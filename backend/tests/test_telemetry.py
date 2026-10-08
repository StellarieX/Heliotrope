"""Real-telemetry ingestion alongside the deterministic simulator."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

from .fixtures import day_str, ev_job, washing_machine_job

client = TestClient(app)
DAY = day_str()                                        # the anchor day, always today
NEXT = day_str(1)                                      # the following day
CARBON_START = f"{DAY}T18:00:00+00:00"
CARBON_END = f"{NEXT}T08:00:00+00:00"


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


def job_of(state: dict, job_id: str) -> dict:
    for j in state["jobs"]:
        if j["job_id"] == job_id:
            return j
    raise AssertionError(f"job {job_id} missing from state")


def test_telemetry_push_accepted_and_measured():
    sid = plan([washing_machine_job()])
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    assert job_of(state, "wm-1")["source"] == "SIMULATED"
    res = client.post(
        f"/api/v1/schedules/{sid}/telemetry",
        json={"job_id": "wm-1", "timestamp": f"{DAY}T19:00:00+00:00",
              "energy_kwh": 0.5, "power_kw": 2.1},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["accepted"] is True
    assert body["source"] == "MEASURED"
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    job = job_of(state, "wm-1")
    assert job["source"] == "MEASURED"
    assert job["energy_delivered_kwh"] == 0.5
    assert job["status"] == "RUNNING"


def test_telemetry_completes_when_target_met():
    sid = plan([washing_machine_job()])
    expected = job_of(client.get(f"/api/v1/schedules/{sid}/state").json(), "wm-1")[
        "expected_energy_kwh"
    ]
    assert expected > 0
    res = client.post(
        f"/api/v1/schedules/{sid}/telemetry",
        json={"job_id": "wm-1", "timestamp": f"{DAY}T19:30:00+00:00",
              "energy_kwh": expected},
    )
    assert res.status_code == 200, res.text
    assert res.json()["completed"] is True
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    job = job_of(state, "wm-1")
    assert job["status"] == "COMPLETED"
    assert job["source"] == "MEASURED"


def test_telemetry_negative_energy_rejected():
    sid = plan([washing_machine_job()])
    res = client.post(
        f"/api/v1/schedules/{sid}/telemetry",
        json={"job_id": "wm-1", "timestamp": f"{DAY}T19:00:00+00:00",
              "energy_kwh": -1.0},
    )
    assert res.status_code == 422


def test_telemetry_unknown_job_404():
    sid = plan([washing_machine_job()])
    res = client.post(
        f"/api/v1/schedules/{sid}/telemetry",
        json={"job_id": "nope", "timestamp": f"{DAY}T19:00:00+00:00",
              "energy_kwh": 0.5},
    )
    assert res.status_code == 404


def test_simulator_path_stays_simulated():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    res = client.post(
        f"/api/v1/simulation/{sid}/advance",
        json={
            "to_time": f"{DAY}T22:00:00+00:00",
            "script": [
                {"at": f"{DAY}T19:00:00+00:00", "do": "start", "job": "ev-1"},
            ],
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["simulated"] is True
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    assert job_of(state, "ev-1")["source"] == "SIMULATED"
