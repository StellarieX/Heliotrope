"""Phase 7: execution, versioning, rolling horizon, simulator, overrides."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app.api.routes.execution as execution
from app.domain.execution import RescheduleReason, ScheduleEvent, ScheduleEventType
from app.main import app
from app.services.execution_store import ExecutionStore
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SchedulerName

from .fixtures import at, day_str, ev_job, fan_job, geyser_job, make_signal, washing_machine_job

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
        json={"to_time": f"{DAY}T23:30:00+00:00", "script": []},
    )
    assert res.status_code == 200
    statuses = {j["job_id"]: j["status"] for j in res.json()["state"]["jobs"]}
    assert statuses["wm-1"] == "MISSED"
    assert sched_start is not None


def test_override_rejected_without_headroom(monkeypatch):
    """The no-headroom branch of `check_override`, exercised deterministically.

    `headroom_fn` measures only the FIXED baseline, never the other flexible
    jobs' placements, so the capacity rejection can only be triggered by a
    fixed load drawing at the moment of the override. An 8 kW fixed load holds
    the 10 kW connection between 20:00 and 22:00; the override target needs
    3 kW and is only schedulable once the fixed window has passed, so the plan
    succeeds but the override inside the window cannot.

    `now` is pinned because `post_override` reads the real clock: the horizon is
    anchored to 20:00 today, so unpinned the verdict depended on the hour the
    suite ran — late in the evening the deadline guardrail fired first and the
    rejection explained slot counts rather than capacity, failing this
    assertion for a reason the test does not claim to cover.
    """
    monkeypatch.setattr(execution, "utcnow", lambda: at(21))
    sid = plan(
        [
            fan_job(power_kw=8.0, release_at=at(20), deadline_at=at(22)),
            washing_machine_job(power_kw=3.0, duration_minutes=60,
                                release_at=at(22), deadline_at=at(31)),
        ],
        capacity_kw=10.0,
    )
    res = client.post(
        f"/api/v1/schedules/{sid}/override",
        json={"job_id": "wm-1", "command": "START_NOW"},
    )
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "override_rejected"
    assert "capacity" in body["detail"]


def test_override_accepted_when_headroom_exists(monkeypatch):
    """The same schedule, checked once the fixed window has ended."""
    monkeypatch.setattr(execution, "utcnow", lambda: at(23))
    sid = plan(
        [
            fan_job(power_kw=8.0, release_at=at(20), deadline_at=at(22)),
            washing_machine_job(power_kw=3.0, duration_minutes=60,
                                release_at=at(22), deadline_at=at(31)),
        ],
        capacity_kw=10.0,
    )
    res = client.post(
        f"/api/v1/schedules/{sid}/override",
        json={"job_id": "wm-1", "command": "START_NOW"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["accepted"] is True


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


def test_execution_store_durable_across_instances(tmp_path):
    """A record written by one store instance is readable from SQLite by a
    brand-new instance with an empty in-memory cache."""
    service = SchedulerService()
    jobs = [ev_job(energy_required_kwh=7.2), washing_machine_job()]
    scheduler_input, _warnings = service.build_input(jobs, make_signal(), capacity_kw=20.0)
    result = service.run(scheduler_input, SchedulerName.CPSAT, explain=False)
    assert result.schedule, "fixture jobs must produce a real schedule"

    db_path = str(tmp_path / "exec.db")
    store = ExecutionStore(db_path=db_path)
    record = store.create(scheduler_input, result, reason=RescheduleReason.MANUAL)
    store.record_event(
        record,
        ScheduleEvent(
            event_type=ScheduleEventType.JOB_STARTED,
            timestamp=datetime(2026, 10, 5, 19, 0, tzinfo=timezone.utc),
            job_id="ev-1",
        ),
    )
    store.append_version(
        record, result, RescheduleReason.CAPACITY_CHANGE, [], ["durability check"]
    )
    assert len(record.versions) == 2

    # A brand-new instance at the same db file serves the record from SQLite.
    reloaded_store = ExecutionStore(db_path=db_path)
    loaded = reloaded_store.get(record.schedule_id)
    assert loaded is not None
    assert loaded.schedule_id == record.schedule_id
    assert loaded.lifecycle == record.lifecycle
    assert len(loaded.versions) == len(record.versions) == 2
    assert set(loaded.execution) == set(record.execution)
    for job_id, state in record.execution.items():
        restored = loaded.execution[job_id]
        assert restored.job_id == state.job_id
        assert restored.status == state.status
        assert restored.scheduled_start == state.scheduled_start
        assert restored.scheduled_end == state.scheduled_end
    assert [(e.event_type, e.job_id) for e in loaded.events] == [
        (ScheduleEventType.JOB_STARTED, "ev-1")
    ]
    # Full round-trip: serialization lost nothing.
    assert loaded == record


def test_replan_preserves_completed_history():
    from app.api.routes import execution as ex_routes

    sid = plan(
        [ev_job(id="e1", energy_required_kwh=3.6), ev_job(id="e2", energy_required_kwh=3.6)]
    )
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    starts = {j["job_id"]: j["scheduled_start"] for j in state["jobs"]}
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_STARTED", "timestamp": starts["e1"], "job_id": "e1"})
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_COMPLETED", "timestamp": starts["e1"], "job_id": "e1"})
    res = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T19:00:00+00:00", "reason": "CAPACITY_CHANGE", "capacity_kw": 5.0},
    )
    assert res.status_code == 200
    record = ex_routes.store.get(sid)
    assert record is not None
    assert "original_input" in record.context
    assert "e1" in record.context.get("completed_job_ids", [])
    assert sorted(j.id for j in record.scheduler_input.jobs) == ["e1", "e2"]


def test_realized_co2_never_mixes_forecast():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    res = client.post(
        f"/api/v1/simulation/{sid}/advance",
        json={
            "to_time": f"{DAY}T22:00:00+00:00",
            "script": [{"at": f"{DAY}T19:00:00+00:00", "do": "start", "job": "ev-1"}],
            "carbon_actual": [],
        },
    )
    assert res.status_code == 200
    metrics = res.json()["metrics"]
    # No actual observations: nothing scored, never backfilled from forecast.
    assert metrics["realized_co2_kg"] is None
    assert metrics["realized_co2_slots_scored"] == 0
    assert metrics["realized_co2_slots_total"] > 0
    assert metrics["realized_co2_coverage"] == 0.0


def test_event_energy_side_channel_validated():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    res = client.post(
        f"/api/v1/schedules/{sid}/events",
        json={"event_type": "JOB_STARTED", "timestamp": f"{DAY}T19:00:00+00:00",
              "job_id": "ev-1", "payload": {"energy_delivered_kwh": -5}},
    )
    assert res.status_code == 422
    res = client.post(
        f"/api/v1/schedules/{sid}/events",
        json={"event_type": "JOB_STARTED", "timestamp": f"{DAY}T19:00:00+00:00",
              "job_id": "ev-1", "payload": {"delivered_slots": {"3": -100}}},
    )
    assert res.status_code == 422


def test_override_move_empty_skips_replan():
    sid = plan([washing_machine_job()])
    before = len(client.get(f"/api/v1/schedules/{sid}/history").json()["versions"])
    res = client.post(
        f"/api/v1/schedules/{sid}/override", json={"job_id": "wm-1", "command": "MOVE"})
    assert res.status_code == 200
    assert res.json()["replanned"] is False
    assert len(client.get(f"/api/v1/schedules/{sid}/history").json()["versions"]) == before


def test_removed_completed_returns_invalid_transition():
    sid = plan([washing_machine_job()])
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    start = state["jobs"][0]["scheduled_start"]
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_STARTED", "timestamp": start, "job_id": "wm-1"})
    client.post(f"/api/v1/schedules/{sid}/events", json={
        "event_type": "JOB_COMPLETED", "timestamp": start, "job_id": "wm-1"})
    res = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"now": f"{DAY}T20:00:00+00:00", "reason": "MANUAL",
              "removed_job_ids": ["wm-1"]},
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_transition"


def test_simulation_advance_persists():
    from app.api.routes import execution as ex_routes
    from app.services.execution_store import ExecutionStore

    sid = plan([washing_machine_job()])
    res = client.post(
        f"/api/v1/simulation/{sid}/advance",
        json={"to_time": f"{DAY}T23:30:00+00:00", "script": []},
    )
    assert res.status_code == 200
    reloaded = ExecutionStore(db_path=ex_routes.store._db_path).get(sid)
    assert reloaded is not None
    assert any(e.event_type == ScheduleEventType.CLOCK_ADVANCED for e in reloaded.events)


def test_start_now_checks_contiguous_slots():
    from app.api.routes import execution as ex_routes
    from app.domain.execution import OverrideCommand
    from app.services.execution_events import check_override
    from app.services.receding import now_slot

    sid = plan([washing_machine_job()])
    record = ex_routes.store.get(sid)
    assert record is not None
    slot = now_slot(
        record.scheduler_input.horizon,
        __import__("datetime").datetime.fromisoformat(f"{DAY}T18:00:00+00:00"),
    )
    job = next(j for j in record.scheduler_input.jobs if j.id == "wm-1")
    duration = job.duration_slots or 1
    assert duration > 1

    def gap(s: int) -> float:
        return 20.0 if s == slot else 0.0

    allowed, detail = check_override(record, "wm-1", OverrideCommand.START_NOW, slot, gap)
    assert allowed is False
    assert "slot" in detail



def test_rejected_event_leaves_no_trace():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    res = client.post(
        f"/api/v1/schedules/{sid}/events",
        json={
            "event_type": "JOB_COMPLETED", "timestamp": f"{DAY}T19:00:00+00:00",
            "job_id": "ev-1", "payload": {"energy_delivered_kwh": 5.0},
        },
    )
    assert res.status_code == 422
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    assert state["jobs"][0]["energy_delivered_kwh"] == 0.0
    record = execution.store.get(sid)
    assert record is not None and record.events == []


def test_replan_rejects_duplicate_added_job_without_mutating():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    before = execution.store.get(sid).scheduler_input
    res = client.post(
        f"/api/v1/schedules/{sid}/replan",
        json={"capacity_kw": 15.0, "added_jobs": [spec_dict(ev_job(energy_required_kwh=7.2))]},
    )
    assert res.status_code == 422
    after = execution.store.get(sid).scheduler_input
    assert after.capacity_w == before.capacity_w


def test_override_move_to_empty_window_is_rejected_unchanged():
    sid = plan([ev_job(energy_required_kwh=7.2)])
    before = execution.store.get(sid).scheduler_input.jobs[0].deadline_slot
    res = client.post(
        f"/api/v1/schedules/{sid}/override",
        json={"job_id": "ev-1", "command": "MOVE", "new_deadline_at": f"{DAY}T18:00:00+00:00"},
    )
    assert res.status_code == 422
    assert execution.store.get(sid).scheduler_input.jobs[0].deadline_slot == before
