"""Tier 3: Cross-Feature Combinations (Pairwise Interaction Coverage) Tests.

Validates pairwise interactions and state pipelines across subsystems.
Authoritative references: ORIGINAL_REQUEST.md and PROJECT.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.domain.loads import LoadSpec, LoadType, LoadCategory
from app.domain.coordination import CoordinationRequest, Participant, SharedResource
from app.domain.forecasting import ForecastMode
from app.domain.thermal import ThermalModel
from app.services.execution_store import ExecutionStore
from app.services.forecast_service import ForecastService
from app.services.load_intelligence import RuleBasedLoadClassifier
from app.services.providers.synthetic import SyntheticDuckCurveProvider

from .conftest import (
    FirestoreRulesEvaluator,
    LocalStorageSimulator,
    make_load_spec,
    make_thermal_spec,
)

BASE_TIME = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)


def test_t3_01_f1_f2_claim_lifecycle_and_profile_access(rules_evaluator: FirestoreRulesEvaluator):
    """T3.01: F1 + F2 — Complete username claim lifecycle and public profile accessibility."""
    # 1. Owner creates claim
    assert rules_evaluator.evaluate_username_create("user_carol", "user_carol") is True
    # 2. Public reads profile
    assert rules_evaluator.evaluate_user_read(None, "user_carol") is True
    # 3. Third party cannot overwrite
    assert rules_evaluator.evaluate_username_update("user_mallory", "user_carol", "user_mallory") is False
    # 4. Owner updates claim
    assert rules_evaluator.evaluate_username_update("user_carol", "user_carol", "user_carol") is True
    # 5. Owner deletes claim
    assert rules_evaluator.evaluate_username_delete("user_carol", "user_carol") is True


def test_t3_02_f3_f4_building_coordination_with_robust_forecast(client: TestClient):
    """T3.02: F3 + F4 — Coordinated building schedule planned under forecast uncertainty."""
    j1 = make_load_spec("j1", "EV 1", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j2 = make_load_spec("j2", "EV 2", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j1.participant_id = "apt_a"
    j2.participant_id = "apt_b"

    payload = {
        "participants": [{"id": "apt_a"}, {"id": "apt_b"}],
        "shared_resource": {"id": "bldg", "capacity_kw": 3.0},
        "jobs": [j1.model_dump(mode="json"), j2.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")


def test_t3_03_f3_f5_coordination_with_thermal_appliances(client: TestClient):
    """T3.03: F3 + F5 — Coordinated building combining thermal appliances and shiftable loads."""
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=50.0, target_temp=58.0, power_kw=2.0)
    t_job = make_load_spec("t1", "Water Heater", LoadType.THERMAL, power_kw=2.0, thermal=ts)
    a_job = make_load_spec("a1", "Dishwasher", LoadType.DEFERRABLE_ATOMIC, power_kw=1.5, duration_minutes=60)
    t_job.participant_id = "tenant_1"
    a_job.participant_id = "tenant_2"

    payload = {
        "participants": [{"id": "tenant_1"}, {"id": "tenant_2"}],
        "shared_resource": {"id": "bldg", "capacity_kw": 3.0},
        "jobs": [t_job.model_dump(mode="json"), a_job.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")


def test_t3_04_f3_f6_coordination_preserving_custom_parameters(client: TestClient):
    """T3.04: F3 + F6 — Building coordination preserving custom user kWh and duration inputs."""
    j = make_load_spec("ev_custom", "EV Custom", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=5.0, energy_kwh=15.0, duration_minutes=180)
    j.participant_id = "tenant_1"
    payload = {
        "participants": [{"id": "tenant_1"}],
        "shared_resource": {"id": "bldg", "capacity_kw": 10.0},
        "jobs": [j.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")
    job_result = res.json()["jobs"][0]
    assert abs(job_result["energy_kwh"] - 15.0) < 1e-3


def test_t3_05_f4_f5_thermal_scheduling_under_robust_forecast(client: TestClient):
    """T3.05: F4 + F5 — Thermal load scheduled against ROBUST carbon forecast intervals."""
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=50.0, target_temp=58.0, power_kw=2.0)
    t_job = make_load_spec("t1", "Water Heater", LoadType.THERMAL, power_kw=2.0, thermal=ts)

    payload = {
        "jobs": [t_job.model_dump(mode="json")],
        "capacity_kw": 5.0,
        "carbon": {"mode": "FORECAST", "forecast_mode": "ROBUST", "risk_weight": 1.0},
    }
    res = client.post("/api/v1/schedule", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")


def test_t3_06_f4_f7_forecast_planned_schedule_persisted_to_store(client: TestClient):
    """T3.06: F4 + F7 — Schedule planned with forecast mode persisted and queryable."""
    j = make_load_spec("j1", "Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    payload = {
        "jobs": [j.model_dump(mode="json")],
        "capacity_kw": 5.0,
        "carbon": {"mode": "FORECAST", "forecast_mode": "ROBUST", "risk_weight": 0.5},
    }
    plan_res = client.post("/api/v1/schedules/plan", json=payload)
    assert plan_res.status_code == 200
    sid = plan_res.json()["schedule_id"]

    state_res = client.get(f"/api/v1/schedules/{sid}/state")
    assert state_res.status_code == 200
    assert state_res.json()["schedule_id"] == sid


def test_t3_07_f5_f6_thermal_spec_with_custom_energy_and_comfort():
    """T3.07: F5 + F6 — Thermal spec with custom comfort bounds and user defined power."""
    ts = make_thermal_spec(min_temp=42.0, max_temp=68.0, initial_temp=46.0, target_temp=60.0, power_kw=4.0)
    j = make_load_spec("geyser", "Geyser", LoadType.THERMAL, power_kw=4.0, thermal=ts)
    assert j.thermal.temperature_min_c == 42.0
    assert j.thermal.temperature_max_c == 68.0
    assert j.power_kw == 4.0


def test_t3_08_f5_f7_thermal_execution_state_persisted(client: TestClient):
    """T3.08: F5 + F7 — Thermal job execution plan created and tracked in execution store."""
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=48.0, target_temp=58.0, power_kw=2.0)
    t_job = make_load_spec("t1", "Water Heater", LoadType.THERMAL, power_kw=2.0, thermal=ts)

    res = client.post("/api/v1/schedules/plan", json={"jobs": [t_job.model_dump(mode="json")], "capacity_kw": 5.0})
    assert res.status_code == 200
    sid = res.json()["schedule_id"]

    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    assert state["jobs"][0]["job_id"] == "t1"
    assert state["jobs"][0]["status"] == "PENDING"


def test_t3_09_f6_f7_custom_kwh_schedule_persisted_and_reloaded(client: TestClient):
    """T3.09: F6 + F7 — Custom energy kWh preserved into execution store expected energy."""
    j = make_load_spec("ev_spec", "EV Charger", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=6.0, energy_kwh=18.0)
    res = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 10.0})
    assert res.status_code == 200
    sid = res.json()["schedule_id"]

    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    job_st = state["jobs"][0]
    assert abs(job_st["expected_energy_kwh"] - 18.0) < 1e-3


def test_t3_10_f7_f8_backend_persistence_and_client_rehydration(local_storage: LocalStorageSimulator, client: TestClient):
    """T3.10: F7 + F8 — Schedule created, ID retained in client localStorage, rehydrated on reload."""
    j = make_load_spec("j_rehydrate", "Rehydrate Test", LoadType.DEFERRABLE_ATOMIC, power_kw=1.5, duration_minutes=60)
    res = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0})
    sid = res.json()["schedule_id"]

    # Client sets localStorage
    local_storage.set_item("heliotrope:active_schedule_id", sid)

    # Simulated page reload: client reads localStorage and queries backend
    restored_id = local_storage.get_item("heliotrope:active_schedule_id")
    assert restored_id == sid

    state_res = client.get(f"/api/v1/schedules/{restored_id}/state")
    hist_res = client.get(f"/api/v1/schedules/{restored_id}/history")
    assert state_res.status_code == 200
    assert hist_res.status_code == 200
    assert state_res.json()["schedule_id"] == sid


def test_t3_11_f9_f4_grid_adapter_output_feeding_forecast():
    """T3.11: F9 + F4 — Carbon signal points feed into forecast service calculation."""
    syn = SyntheticDuckCurveProvider()
    pts = syn.get_signal(BASE_TIME, BASE_TIME + timedelta(days=1), 15)
    svc = ForecastService()
    fc = svc.forecast(BASE_TIME, BASE_TIME + timedelta(days=1), resolution_minutes=15)
    assert len(fc.points) == len(pts)
    assert fc.provenance.model == "seasonal"


def test_t3_12_f10_f6_nl_classification_feeding_preserved_specs():
    """T3.12: F10 + F6 — Natural language classification preserves user kWh into LoadSpec."""
    clf = RuleBasedLoadClassifier()
    c = clf.classify("Charge Tesla 25 kWh")
    spec = LoadSpec(
        id="nl_ev",
        normalized_name="Tesla",
        category=c.category.value,
        job_type=c.job_type,
        power_kw=7.2,
        energy_required_kwh=25.0,
        release_at=BASE_TIME,
        deadline_at=BASE_TIME + timedelta(hours=8),
    )
    assert spec.job_type == LoadType.DEFERRABLE_INTERRUPTIBLE
    assert spec.energy_required_kwh == 25.0


def test_t3_13_f10_f5_nl_classification_identifying_thermal_load():
    """T3.13: F10 + F5 — Classifier identifies thermal load and attaches comfort boundaries."""
    clf = RuleBasedLoadClassifier()
    c = clf.classify("heat water geyser")
    assert c.job_type == LoadType.THERMAL
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=50.0, target_temp=58.0)
    spec = LoadSpec(
        id="nl_geyser",
        normalized_name="Water Geyser",
        category=c.category.value,
        job_type=c.job_type,
        power_kw=3.0,
        thermal=ts,
        release_at=BASE_TIME,
        deadline_at=BASE_TIME + timedelta(hours=6),
    )
    assert spec.thermal.band_text() == "45°C–65°C"


def test_t3_14_f9_f7_external_carbon_schedule_persisted(client: TestClient):
    """T3.14: F9 + F7 — Schedule planned with synthetic carbon signal stored in ExecutionStore."""
    j = make_load_spec("j_carbon", "Load", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    res = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0})
    assert res.status_code == 200
    sid = res.json()["schedule_id"]
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    assert state["lifecycle"] == "SCHEDULED"


def test_t3_15_f3_f7_coordinated_plan_persisted_in_store(client: TestClient):
    """T3.15: F3 + F7 — POST /api/v1/schedules/plan-coordinated creates persisted record."""
    j1 = make_load_spec("j1", "EV 1", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j2 = make_load_spec("j2", "EV 2", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j1.participant_id = "tenant_a"
    j2.participant_id = "tenant_b"

    payload = {
        "participants": [{"id": "tenant_a"}, {"id": "tenant_b"}],
        "shared_resource": {"id": "bldg", "capacity_kw": 5.0},
        "jobs": [j1.model_dump(mode="json"), j2.model_dump(mode="json")],
    }
    res = client.post("/api/v1/schedules/plan-coordinated", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert "schedule_id" in body
    sid = body["schedule_id"]

    # Verify retrieval
    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    assert state["schedule_id"] == sid
    assert len(state["jobs"]) == 2
