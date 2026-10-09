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
