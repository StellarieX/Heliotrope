"""Time-driven scheduler ticks: CLOCK_ADVANCED + periodic auto-replan."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

from .fixtures import ev_job, washing_machine_job

client = TestClient(app)
DAY = "2026-10-05"
CARBON_START = f"{DAY}T18:00:00+00:00"
CARBON_END = "2026-10-06T08:00:00+00:00"


def spec_dict(spec) -> dict:
    d = spec.model_dump(mode="json")
    d.pop("participant_id", None)
    return d


def plan(body_jobs, execution=None, capacity_kw=20.0):
    body = {
        "jobs": [spec_dict(j) for j in body_jobs],
        "capacity_kw": capacity_kw,
        "scheduler": "CPSAT",
        "carbon_start": CARBON_START,
        "carbon_end": CARBON_END,
    }
    if execution is not None:
        body["execution"] = execution
    res = client.post("/api/v1/schedules/plan", json=body)
    assert res.status_code == 200, res.text
    return res.json()["schedule_id"]


def test_tick_advances_clock_and_marks_missed():
    sid = plan([washing_machine_job()], execution={"policy": "MANUAL"})
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    sched_start = state["jobs"][0]["scheduled_start"]
    assert sched_start is not None
    # Tick well past the atomic window without ever starting the job.
    res = client.post(
        f"/api/v1/schedules/{sid}/tick", json={"now": "2026-10-05T23:30:00+00:00"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ticked"] is True
    statuses = {j["job_id"]: j["status"] for j in body["state"]["jobs"]}
    assert statuses["wm-1"] == "MISSED"


def test_tick_auto_replans_under_periodic():
    sid = plan(
        [ev_job(energy_required_kwh=18.0)],
        execution={"policy": "PERIODIC", "reoptimization_interval_minutes": 15},
    )
    res = client.post(
        f"/api/v1/schedules/{sid}/tick", json={"now": "2026-10-06T02:00:00+00:00"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # CLOCK_ADVANCED alone advises a replan under PERIODIC once due.
    assert body["periodic_due"] is True
    assert body["replan_advised"] is True
    assert body["replanned"] is True
    assert body["version"] == 2
    statuses = {j["job_id"]: j["status"] for j in body["state"]["jobs"]}
    assert statuses["ev-1"] == "MISSED"
    history = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(history["versions"]) == 2
    assert history["versions"][1]["reason"] == "PERIODIC"


def test_tick_does_not_replan_under_manual():
    sid = plan([ev_job(energy_required_kwh=18.0)], execution={"policy": "MANUAL"})
    res = client.post(
        f"/api/v1/schedules/{sid}/tick", json={"now": "2026-10-06T02:00:00+00:00"}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["periodic_due"] is False
    assert body.get("replanned", False) is False
    # Clock still advanced: the missed start is recorded even without replan.
    statuses = {j["job_id"]: j["status"] for j in body["state"]["jobs"]}
    assert statuses["ev-1"] == "MISSED"
    history = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(history["versions"]) == 1


def test_tick_respects_reoptimization_interval():
    sid = plan(
        [ev_job(energy_required_kwh=18.0)],
        execution={"policy": "PERIODIC", "reoptimization_interval_minutes": 15},
    )
    first = client.post(
        f"/api/v1/schedules/{sid}/tick", json={"now": "2026-10-06T02:00:00+00:00"}
    ).json()
    assert first["periodic_due"] is True
    # 5 min later: interval (15 min) has not elapsed since last_replan_at.
    second = client.post(
        f"/api/v1/schedules/{sid}/tick", json={"now": "2026-10-06T02:05:00+00:00"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["periodic_due"] is False
    history = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(history["versions"]) == first.get("version", 2)


def test_tick_unknown_schedule_404():
    res = client.post(
        "/api/v1/schedules/doesnotexist/tick", json={"now": "2026-10-05T20:00:00+00:00"}
    )
    assert res.status_code == 404
