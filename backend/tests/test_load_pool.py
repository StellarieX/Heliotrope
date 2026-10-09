"""Anti-herding pool: other users' planned load tilts a new plan away from crowded slots."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import app.api.routes.execution as execution
from app.domain.execution import ScheduleLifecycle
from app.domain.loads import LoadSpec, LoadType
from app.domain.scheduling import SchedulerConfig
from app.main import app
from app.services.execution_store import ExecutionStore
from app.services.load_pool import attach_pool, compute_pool
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SchedulerName

from .fixtures import DAY_START
from .test_cpsat import constant_signal

client = TestClient(app)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    fresh = ExecutionStore(db_path=str(tmp_path / "pool.db"))
    monkeypatch.setattr(execution, "store", fresh)
    return fresh


def oven(job_id: str) -> LoadSpec:
    # A 30-min, 10 kW run that may start in slots 0..4. Slots 1-2 and 3-4 are near-equal.
    return LoadSpec(
        id=job_id, normalized_name="Oven", category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC, power_kw=10.0, max_power_kw=10.0,
        duration_minutes=30, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(minutes=90),
    )


SIGNAL = constant_signal([500.0, 100.0, 100.0, 102.0, 102.0, 500.0])


def solve(service, store, job_id, pooled=True):
    inp, _ = service.build_input([oven(job_id)], SIGNAL, capacity_kw=50.0)
    if pooled:
        inp, summary = attach_pool(store, inp)
    else:
        summary = None
    result = service.run(inp, SchedulerName.CPSAT, config=SchedulerConfig(time_limit_seconds=20.0))
    return inp, result, summary


def test_no_pool_leaves_the_objective_untouched():
    service = SchedulerService()
    inp, _ = service.build_input([oven("a")], SIGNAL, capacity_kw=50.0)
    assert inp.objective_carbon() is inp.carbon
    empty = inp.model_copy(update={"pooled_load_w": [0] * inp.horizon.slot_count})
    assert empty.objective_carbon().gco2_per_kwh == inp.carbon.gco2_per_kwh


def test_surcharge_raises_crowded_slots_and_stays_bounded():
    service = SchedulerService()
    inp, _ = service.build_input([oven("a")], SIGNAL, capacity_kw=50.0)
    crowded = [0] * inp.horizon.slot_count
    crowded[1] = 10**9
    out = inp.model_copy(update={"pooled_load_w": crowded, "pool_beta": 0.3}).objective_carbon()
    assert out.gco2_per_kwh[1] > inp.carbon.gco2_per_kwh[1]
    assert out.gco2_per_kwh[1] <= int(inp.carbon.gco2_per_kwh[1] * 1.3) + 1
    assert out.gco2_per_kwh[2] == inp.carbon.gco2_per_kwh[2]


def test_second_user_is_steered_off_the_first_users_slot(store):
    service = SchedulerService()
    first_input, first, _ = solve(service, store, "a", pooled=False)
    assert first.schedule[0].start_slot == 1
    store.create(first_input, first)

    inp, second, summary = solve(service, store, "b")
    assert summary["applied"] and summary["active_schedules"] == 1
    assert second.schedule[0].start_slot == 3, "should move to the near-equal uncrowded window"
    # Reported CO2 comes from the real signal, not the surcharged objective.
    expected_kg = 10_000 * 15 * (102 + 102) / 60_000_000
    assert second.metrics.total_co2_kg == pytest.approx(expected_kg, rel=1e-6)


def test_pool_skips_cancelled_and_excluded_records(store):
    service = SchedulerService()
    inp, result, _ = solve(service, store, "a", pooled=False)
    rec = store.create(inp, result)
    assert compute_pool(store, inp.horizon).active_schedules == 1
    assert compute_pool(store, inp.horizon, [rec.schedule_id]).active_schedules == 0
    rec.lifecycle = ScheduleLifecycle.CANCELLED
    store._persist(rec)
    assert compute_pool(store, inp.horizon).active_schedules == 0


def test_pool_never_enters_the_serialized_input(store):
    service = SchedulerService()
    inp, _ = service.build_input([oven("a")], SIGNAL, capacity_kw=50.0)
    pooled = inp.model_copy(update={"pooled_load_w": [5] * inp.horizon.slot_count})
    assert "pooled_load_w" not in pooled.model_dump(mode="json")
    assert pooled.model_dump(mode="json") == inp.model_dump(mode="json")


def test_replanning_cancels_the_users_previous_plan(store):
    from .fixtures import ev_job
    from .test_execution import CARBON_END, CARBON_START, spec_dict

    body = {
        "jobs": [spec_dict(ev_job(energy_required_kwh=7.2))],
        "capacity_kw": 20.0,
        "scheduler": "CPSAT",
        "carbon_start": CARBON_START,
        "carbon_end": CARBON_END,
    }
    first = client.post("/api/v1/schedules/plan", json=body).json()["schedule_id"]
    res = client.post("/api/v1/schedules/plan", json={**body, "replaces_schedule_id": first})
    assert res.status_code == 200, res.text
    assert res.json()["pool"]["active_schedules"] == 0  # the old plan is not someone else's load
    assert store.get(first).lifecycle is ScheduleLifecycle.CANCELLED


# --- GET /pool/stats ---------------------------------------------------------------


def stats(**params):
    q = {"start": DAY_START.isoformat(), "end": (DAY_START + timedelta(hours=2)).isoformat(), **params}
    return client.get("/api/v1/pool/stats", params=q)


def plan_oven(store, job_id):
    service = SchedulerService()
    inp, result, _ = solve(service, store, job_id, pooled=False)
    return store.create(inp, result), result


def test_pool_stats_are_zeros_with_no_plans(store):
    res = stats()
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["active_schedules"] == 0 and body["active_loads"] == 0
    assert body["total_planned_kwh"] == 0 and body["peak_kw"] == 0
    assert body["peak_at"] is None and body["peak_to_average"] is None
    assert body["average_kw"] == 0
    assert body["resolution_minutes"] == 15
    assert len(body["slots"]) == 8 and all(s["kw"] == 0 for s in body["slots"])
    assert set(body["pool"]) == {"enabled", "beta", "ref_kw", "scale_kw"}


def test_pool_stats_aggregate_two_plans_per_slot(store):
    rec_a, first = plan_oven(store, "a")
    # Second plan on the same constant-signal slots (both ovens run in slot 1-2).
    rec_b, second = plan_oven(store, "b")
    body = stats().json()
    by_ts = {s["timestamp"]: s["kw"] for s in body["slots"]}
    expected: dict = {}
    for res in (first, second):
        for sched in res.schedule:
            for a in sched.allocations:
                key = a.timestamp.isoformat().replace("+00:00", "Z")
                expected[key] = expected.get(key, 0.0) + a.power_w / 1000.0
    for ts, kw in expected.items():
        assert by_ts[ts] == pytest.approx(kw)
    assert body["active_schedules"] == 2 and body["active_loads"] == 2
    assert body["total_planned_kwh"] == pytest.approx(sum(expected.values()) * 0.25)
    assert body["peak_kw"] == pytest.approx(max(expected.values()))
    assert body["peak_at"] in expected
    loaded = [k for k in expected.values() if k > 0]
    assert body["average_kw"] == pytest.approx(sum(loaded) / len(loaded))
    assert body["peak_to_average"] == pytest.approx(max(loaded) / (sum(loaded) / len(loaded)), rel=1e-3)


def test_pool_stats_exclude_cancelled_plans(store):
    rec_a, _ = plan_oven(store, "a")
    plan_oven(store, "b")
    assert stats().json()["active_schedules"] == 2
    rec_a.lifecycle = ScheduleLifecycle.CANCELLED
    store._persist(rec_a)
    assert stats().json()["active_schedules"] == 1


def test_pool_stats_resolution_preserves_energy(store):
    plan_oven(store, "a")
    fine = stats().json()
    hourly = stats(resolution_minutes=60).json()
    assert len(hourly["slots"]) == 2 and hourly["resolution_minutes"] == 60
    assert hourly["total_planned_kwh"] == pytest.approx(fine["total_planned_kwh"])
    assert sum(s["kw"] for s in hourly["slots"]) * 1.0 == pytest.approx(hourly["total_planned_kwh"])


def test_pool_stats_payload_carries_no_identifiers(store):
    rec, result = plan_oven(store, "secret-job-id")
    text = stats().text
    assert rec.schedule_id not in text
    assert "secret-job-id" not in text and "Oven" not in text
    assert set(stats().json()) == {
        "generated_at", "start", "end", "resolution_minutes", "active_schedules",
        "active_loads", "total_planned_kwh", "peak_kw", "peak_at", "average_kw",
        "peak_to_average", "slots", "pool",
    }
    assert all(set(s) == {"timestamp", "kw"} for s in stats().json()["slots"])


def test_pool_stats_default_window_is_the_next_24_hours(store):
    body = client.get("/api/v1/pool/stats").json()
    assert len(body["slots"]) == 96 and body["resolution_minutes"] == 15


@pytest.mark.parametrize(
    "params",
    [
        {"resolution_minutes": 7},
        {"start": "2024-01-01T00:00:00"},
        {"end": "2020-01-01T00:00:00Z"},
        {"end": "2025-12-31T00:00:00Z"},
    ],
)
def test_pool_stats_rejects_bad_windows(store, params):
    res = client.get("/api/v1/pool/stats", params={"start": "2024-01-01T00:00:00Z", **params})
    assert res.status_code == 422 and res.json()["code"] == "invalid_request"


# --- mixed resolution / alignment --------------------------------------------------


def test_pool_counts_plans_on_a_different_slot_length_or_offset(store):
    from app.domain.horizon import SchedulingHorizon

    service = SchedulerService()
    inp, result, _ = solve(service, store, "a", pooled=False)
    store.create(inp, result)
    alloc_w = {a.timestamp: a.power_w for s in result.schedule for a in s.allocations}
    assert alloc_w
    energy_wh = sum(w * 0.25 for w in alloc_w.values())

    # Same start, 30-minute slots: pooled watts are the mean power over each slot.
    h30 = SchedulingHorizon(start=DAY_START, end=DAY_START + timedelta(hours=3), slot_minutes=30, slot_count=6)
    snap30 = compute_pool(store, h30)
    assert len(snap30.pooled_w) == 6 and snap30.active_schedules == 1
    assert sum(snap30.pooled_w) * 0.5 == pytest.approx(energy_wh, abs=1.0)

    # Unaligned start (7.5 min shift) with 15-minute slots: nothing may be dropped.
    start = DAY_START + timedelta(minutes=7, seconds=30)
    h15 = SchedulingHorizon(start=start, end=start + timedelta(hours=3), slot_minutes=15, slot_count=12)
    snap15 = compute_pool(store, h15)
    assert len(snap15.pooled_w) == 12 and snap15.active_schedules == 1
    assert sum(snap15.pooled_w) * 0.25 == pytest.approx(energy_wh, abs=1.0)


def test_end_to_end_second_plan_sees_the_first_in_its_pool(store):
    from .fixtures import ev_job
    from .test_execution import CARBON_END, CARBON_START, spec_dict

    def body(job_id):
        return {
            "jobs": [spec_dict(ev_job(id=job_id, energy_required_kwh=7.2))],
            "capacity_kw": 20.0,
            "scheduler": "CPSAT",
            "carbon_start": CARBON_START,
            "carbon_end": CARBON_END,
        }

    a = client.post("/api/v1/schedules/plan", json=body("ev-a"))
    assert a.status_code == 200, a.text
    assert a.json()["pool"]["applied"] and a.json()["pool"]["active_schedules"] == 0
    b = client.post("/api/v1/schedules/plan", json=body("ev-b"))
    assert b.status_code == 200, b.text
    pool = b.json()["pool"]
    assert pool["applied"] and pool["active_schedules"] >= 1 and pool["peak_pooled_kw"] > 0

    res = client.get(
        "/api/v1/pool/stats",
        params={"start": CARBON_START, "end": CARBON_END},
    )
    assert res.status_code == 200, res.text
    assert res.json()["active_schedules"] == 2
