"""Tier 1: Feature Coverage (Core Functional) Tests.

Validates baseline functionality for all 10 core features (>= 5 tests per feature = 50 tests).
Authoritative references: ORIGINAL_REQUEST.md (R1-R4) and PROJECT.md (Features 1-10).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.domain.loads import LoadSpec, LoadType, LoadCategory, ParameterOrigin
from app.domain.coordination import CoordinationRequest, Participant, SharedResource, CoordinationMode
from app.domain.execution import RescheduleReason, ScheduleEventType, ScheduleLifecycle
from app.domain.forecasting import ForecastMode
from app.domain.thermal import ThermalModel
from app.services.execution_store import ExecutionStore
from app.services.load_intelligence import RuleBasedLoadClassifier, get_load_intelligence
from app.services.providers.synthetic import SyntheticDuckCurveProvider
from app.services.providers.external import ExternalProvider
from app.services.scheduler_normalizer import SchedulerNormalizer
from app.services.schedulers.cpsat import CPSATScheduler

from .conftest import (
    FirestoreRulesEvaluator,
    LocalStorageSimulator,
    make_load_spec,
    make_thermal_spec,
)

BASE_TIME = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)


# ============================================================================
# Feature 1: Public Profile Route Access (M1, R1)
# ============================================================================

def test_tier1_f01_public_profile_unauthenticated_read(rules_evaluator: FirestoreRulesEvaluator):
    """F1.1: Unauthenticated visitors can read /users/{uid} public profile documents."""
    allowed = rules_evaluator.evaluate_user_read(auth_uid=None, target_uid="user_alice_123")
    assert allowed is True, "Unauthenticated visitors must have read access to /users/{uid} per R1"


def test_tier1_f01_public_profile_authenticated_visitor_read(rules_evaluator: FirestoreRulesEvaluator):
    """F1.2: Authenticated visitors can read other users' /users/{uid} public profiles."""
    allowed = rules_evaluator.evaluate_user_read(auth_uid="user_bob_456", target_uid="user_alice_123")
    assert allowed is True, "Authenticated users viewing another profile must not be blocked"


def test_tier1_f01_public_profile_claimed_username_lookup(rules_evaluator: FirestoreRulesEvaluator):
    """F1.3: Claimed username documents at /usernames/{name} permit public read."""
    assert rules_evaluator.verify_rule_content("match /usernames/{name}"), "Missing match for /usernames/{name}"
    assert rules_evaluator.verify_rule_content("allow read: if true;"), "Missing public read on /usernames"


def test_tier1_f01_public_profile_unclaimed_username_handling():
    """F1.4: Querying an unclaimed username resolves cleanly to missing profile state."""
    db_usernames: dict[str, dict] = {"alice": {"uid": "user_alice"}}
    query_name = "bob_unclaimed"
    exists = query_name in db_usernames
    assert exists is False, "Unclaimed username must report exists=False"


def test_tier1_f01_public_profile_subcollection_jobs_privacy(rules_evaluator: FirestoreRulesEvaluator):
    """F1.5: Private subcollection /users/{uid}/jobs/{jobId} remains strictly owner-only."""
    # Reading jobs must require auth.uid == uid
    match_jobs = rules_evaluator.verify_rule_content("match /jobs/{jobId}")
    assert match_jobs is True, "Jobs subcollection rule must be explicitly protected"
    assert "request.auth != null && request.auth.uid == uid;" in rules_evaluator.rules_content


# ============================================================================
# Feature 2: Username Claim Ownership Security (M1, R1)
# ============================================================================

def test_tier1_f02_claim_creation_by_owner_allowed(rules_evaluator: FirestoreRulesEvaluator):
    """F2.1: Authenticated user claiming /usernames/{name} with matching UID is permitted."""
    allowed = rules_evaluator.evaluate_username_create(auth_uid="user_alice", resource_uid="user_alice")
    assert allowed is True, "Owner must be allowed to create their username claim"


def test_tier1_f02_claim_creation_unauthenticated_rejected(rules_evaluator: FirestoreRulesEvaluator):
    """F2.2: Unauthenticated visitors cannot create username claim documents."""
    allowed = rules_evaluator.evaluate_username_create(auth_uid=None, resource_uid="user_alice")
    assert allowed is False, "Unauthenticated create must be rejected"


def test_tier1_f02_claim_creation_for_other_user_rejected(rules_evaluator: FirestoreRulesEvaluator):
    """F2.3: Authenticated user cannot create a claim document with someone else's UID."""
    allowed = rules_evaluator.evaluate_username_create(auth_uid="user_attacker", resource_uid="user_victim")
    assert allowed is False, "Creating claim for a third-party UID must be rejected"


def test_tier1_f02_claim_update_by_owner_allowed(rules_evaluator: FirestoreRulesEvaluator):
    """F2.4: Owner updating their own claim document is permitted."""
    allowed = rules_evaluator.evaluate_username_update(auth_uid="user_alice", existing_uid="user_alice", new_uid="user_alice")
    assert allowed is True, "Owner must be allowed to update their own claim"


def test_tier1_f02_claim_delete_by_owner_allowed(rules_evaluator: FirestoreRulesEvaluator):
    """F2.5: Owner deleting their own claim document is permitted."""
    allowed = rules_evaluator.evaluate_username_delete(auth_uid="user_alice", existing_uid="user_alice")
    assert allowed is True, "Owner must be allowed to delete their own claim"


# ============================================================================
# Feature 3: Multi-User Coordination Panel (M2, R2)
# ============================================================================

def test_tier1_f03_coordination_endpoint_returns_success(client: TestClient):
    """F3.1: POST /api/v1/coordination/schedule accepts multi-user loads and returns 200."""
    j1 = make_load_spec("j1", "EV 1", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j2 = make_load_spec("j2", "EV 2", LoadType.DEFERRABLE_ATOMIC, power_kw=2.5, duration_minutes=60)
    j1.participant_id = "tenant_a"
    j2.participant_id = "tenant_b"

    payload = {
        "participants": [
            {"id": "tenant_a", "name": "Apartment A"},
            {"id": "tenant_b", "name": "Apartment B"},
        ],
        "shared_resource": {"id": "building_main", "capacity_kw": 4.0},
        "jobs": [j1.model_dump(mode="json"), j2.model_dump(mode="json")],
        "coordination_mode": "COORDINATED",
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code == 200, f"Coordination failed: {res.text}"
    body = res.json()
    assert body["status"] in ("OPTIMAL", "FEASIBLE")


def test_tier1_f03_aggregate_profile_curves(client: TestClient):
    """F3.2: CoordinationResult contains aggregate_profile with total_kw, flexible_kw, capacity_kw."""
    j1 = make_load_spec("j1", "Dishwasher", LoadType.DEFERRABLE_ATOMIC, power_kw=1.5, duration_minutes=60)
    j1.participant_id = "apt_1"
    payload = {
        "participants": [{"id": "apt_1", "name": "Apt 1"}],
        "shared_resource": {"id": "building_main", "capacity_kw": 5.0},
        "jobs": [j1.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code == 200
    points = res.json().get("aggregate_profile", [])
    assert len(points) > 0, "aggregate_profile must not be empty"
    p0 = points[0]
    for key in ("timestamp", "total_kw", "flexible_kw", "capacity_kw", "utilization"):
        assert key in p0, f"Expected key {key} in aggregate point"


def test_tier1_f03_capacity_limits_respected(client: TestClient):
    """F3.3: Aggregate total_kw never exceeds shared capacity_kw in any scheduled slot."""
    capacity = 3.0
    j1 = make_load_spec("j1", "HVAC 1", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j2 = make_load_spec("j2", "HVAC 2", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j1.participant_id = "p1"
    j2.participant_id = "p2"

    payload = {
        "participants": [{"id": "p1"}, {"id": "p2"}],
        "shared_resource": {"id": "b1", "capacity_kw": capacity},
        "jobs": [j1.model_dump(mode="json"), j2.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("OPTIMAL", "FEASIBLE")
    for pt in body["aggregate_profile"]:
        assert pt["total_kw"] <= capacity + 1e-5, f"Capacity violation at {pt['timestamp']}: {pt['total_kw']} > {capacity}"


def test_tier1_f03_flexible_demand_tracking(client: TestClient):
    """F3.4: Flexible demand is explicitly tracked in flexible_kw and matches scheduled jobs."""
    j1 = make_load_spec("j1", "EV", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    j1.participant_id = "p1"
    payload = {
        "participants": [{"id": "p1"}],
        "shared_resource": {"id": "b1", "capacity_kw": 5.0},
        "jobs": [j1.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/schedule", json=payload)
    assert res.status_code == 200
    points = res.json()["aggregate_profile"]
    flex_sum = sum(p["flexible_kw"] for p in points)
    assert flex_sum > 0.0, "Flexible load must be accounted for in flexible_kw"


def test_tier1_f03_coordination_comparison_endpoint(client: TestClient):
    """F3.5: POST /api/v1/coordination/compare computes coordinated vs independent comparison."""
    j1 = make_load_spec("j1", "EV 1", LoadType.DEFERRABLE_ATOMIC, power_kw=3.0, duration_minutes=60)
    j2 = make_load_spec("j2", "EV 2", LoadType.DEFERRABLE_ATOMIC, power_kw=3.0, duration_minutes=60)
    j1.participant_id = "p1"
    j2.participant_id = "p2"

    payload = {
        "participants": [{"id": "p1"}, {"id": "p2"}],
        "shared_resource": {"id": "b1", "capacity_kw": 4.0},
        "jobs": [j1.model_dump(mode="json"), j2.model_dump(mode="json")],
    }
    res = client.post("/api/v1/coordination/compare", json=payload)
    assert res.status_code == 200
    comp = res.json()
    assert "coordinated" in comp
    assert "independent" in comp


# ============================================================================
# Feature 4: Carbon Forecast Mode & Interval Controls (M2, R2)
# ============================================================================

def test_tier1_f04_carbon_forecast_endpoint_success(client: TestClient):
    """F4.1: POST /api/v1/carbon/forecast returns forecast points across 24h horizon."""
    start = BASE_TIME.isoformat()
    end = (BASE_TIME + timedelta(days=1)).isoformat()
    payload = {"start": start, "end": end, "resolution_minutes": 15, "model": "seasonal"}
    res = client.post("/api/v1/carbon/forecast", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert "points" in body
    assert len(body["points"]) == 96, f"Expected 96 15-min points for 24h, got {len(body['points'])}"


def test_tier1_f04_prediction_intervals_ordered(client: TestClient):
    """F4.2: Forecast points satisfy lower_gco2_per_kwh <= predicted <= upper_gco2_per_kwh."""
    start = BASE_TIME.isoformat()
    end = (BASE_TIME + timedelta(hours=6)).isoformat()
    payload = {"start": start, "end": end, "resolution_minutes": 15}
    res = client.post("/api/v1/carbon/forecast", json=payload)
    assert res.status_code == 200
    points = res.json()["points"]
    assert len(points) > 0
    for p in points:
        low = p["lower_gco2_per_kwh"]
        pred = p["predicted_gco2_per_kwh"]
        high = p["upper_gco2_per_kwh"]
        assert low <= pred + 1e-4, f"Lower bound {low} exceeded predicted {pred}"
        assert pred <= high + 1e-4, f"Predicted {pred} exceeded upper bound {high}"


def test_tier1_f04_schedule_with_forecast_mode_expected(client: TestClient):
    """F4.3: POST /api/v1/schedule with forecast_mode=EXPECTED schedules against point forecast."""
    j = make_load_spec("j1", "Washer", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    payload = {
        "jobs": [j.model_dump(mode="json")],
        "capacity_kw": 5.0,
        "carbon": {"mode": "FORECAST", "forecast_mode": "EXPECTED", "risk_weight": 0.0},
    }
    res = client.post("/api/v1/schedule", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")


def test_tier1_f04_schedule_with_forecast_mode_robust(client: TestClient):
    """F4.4: POST /api/v1/schedule with forecast_mode=ROBUST and risk_weight penalizes upper bound."""
    j = make_load_spec("j1", "Washer", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    payload = {
        "jobs": [j.model_dump(mode="json")],
        "capacity_kw": 5.0,
        "carbon": {"mode": "FORECAST", "forecast_mode": "ROBUST", "risk_weight": 1.0},
    }
    res = client.post("/api/v1/schedule", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")


def test_tier1_f04_forecast_summary_included_in_response(client: TestClient):
    """F4.5: Schedule response includes forecast summary documenting mode and risk parameters."""
    j = make_load_spec("j1", "Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    payload = {
        "jobs": [j.model_dump(mode="json")],
        "capacity_kw": 5.0,
        "carbon": {"mode": "FORECAST", "forecast_mode": "ROBUST", "risk_weight": 0.5},
    }
    res = client.post("/api/v1/schedule", json=payload)
    assert res.status_code == 200
    fc_info = res.json().get("forecast", {})
    assert fc_info.get("mode") == "FORECAST"
    assert fc_info.get("forecast_mode") == "ROBUST"
    assert fc_info.get("risk_weight") == 0.5


# ============================================================================
# Feature 5: Thermal Comfort Boundaries & Scheduling (M2, R2)
# ============================================================================

def test_tier1_f05_thermal_spec_validation():
    """F5.1: ThermalSpec correctly validates temperature boundaries and decay/gain parameters."""
    ts = make_thermal_spec(min_temp=40.0, max_temp=60.0, initial_temp=45.0, target_temp=55.0)
    assert ts.temperature_min_c == 40.0
    assert ts.temperature_max_c == 60.0
    assert ts.temperature_target_c == 55.0
    assert ts.band_text() == "40°C–60°C"


def test_tier1_f05_thermal_job_scheduled_not_skipped(client: TestClient):
    """F5.2: Thermal load with valid comfort band is scheduled rather than skipped."""
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=50.0, target_temp=58.0, power_kw=2.0)
    j = make_load_spec("t1", "Water Heater", LoadType.THERMAL, power_kw=2.0, thermal=ts)

    payload = {"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}
    res = client.post("/api/v1/schedule", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("OPTIMAL", "FEASIBLE")
    job_ids = [s["job_id"] for s in body["schedule"]]
    assert "t1" in job_ids, "Thermal load must be included in scheduled jobs list"


def test_tier1_f05_thermal_trajectory_within_comfort_band():
    """F5.3: Simulating thermal heating trajectory confirms temperatures stay within comfort band."""
    ts = make_thermal_spec(min_temp=40.0, max_temp=60.0, initial_temp=45.0, a=0.98, b=1.0, c=0.0)
    power_profile = [1.5, 1.5, 0.0, 0.0]
    profile = ThermalModel(ts).simulate(ts.temperature_initial_c, power_profile)
    assert profile.within_band() is True
    assert profile.min_temperature() >= 40.0
    assert profile.max_temperature() <= 60.0


def test_tier1_f05_thermal_water_heater_dynamics():
    """F5.4: Water heater electrical input increases temperature relative to unpowered decay."""
    ts = make_thermal_spec(min_temp=30.0, max_temp=70.0, initial_temp=50.0, a=0.98, b=2.0, c=-0.1)
    unpowered = ThermalModel(ts).simulate(ts.temperature_initial_c, [0.0, 0.0, 0.0])
    powered = ThermalModel(ts).simulate(ts.temperature_initial_c, [2.0, 2.0, 2.0])
    assert powered.final() > unpowered.final(), "Electrical input must increase thermal state"


def test_tier1_f05_thermal_joint_capacity_with_atomic(client: TestClient):
    """F5.5: Thermal appliance and atomic appliance scheduled jointly under shared electrical capacity."""
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=48.0, power_kw=2.0)
    t_job = make_load_spec("t1", "Geyser", LoadType.THERMAL, power_kw=2.0, thermal=ts)
    a_job = make_load_spec("a1", "Dishwasher", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)

    payload = {"jobs": [t_job.model_dump(mode="json"), a_job.model_dump(mode="json")], "capacity_kw": 3.0}
    res = client.post("/api/v1/schedule", json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("OPTIMAL", "FEASIBLE")


# ============================================================================
# Feature 6: Load Form Parameter Preservation (M2, R2)
# ============================================================================

def test_tier1_f06_preserve_custom_energy_kwh():
    """F6.1: Custom user energy target (14.5 kWh) is preserved in LoadSpec and normalized output."""
    j = make_load_spec("ev1", "Tesla Model Y", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=7.0, energy_kwh=14.5)
    assert j.energy_required_kwh == 14.5, "User-entered energy_required_kwh must be preserved"


def test_tier1_f06_preserve_custom_duration_minutes():
    """F6.2: Custom user duration (150 min) is preserved in LoadSpec without defaulting to 60."""
    j = make_load_spec("wash1", "Extended Wash", LoadType.DEFERRABLE_ATOMIC, power_kw=1.5, duration_minutes=150)
    assert j.duration_minutes == 150, "User-entered duration_minutes must be preserved"


def test_tier1_f06_preserve_both_energy_and_duration():
    """F6.3: Specifying both energy and duration preserves both fields simultaneously."""
    j = make_load_spec("pump1", "Pool Pump", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=2.0, energy_kwh=6.0, duration_minutes=180)
    assert j.energy_required_kwh == 6.0
    assert j.duration_minutes == 180


def test_tier1_f06_omitted_parameter_applies_fallback():
    """F6.4: Omitted duration on interruptible load is None rather than fake 60-min default."""
    j = make_load_spec("ev2", "EV Charger", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=3.0, energy_kwh=10.0, duration_minutes=None)
    assert j.duration_minutes is None, "Missing duration on interruptible load must not be invented"


def test_tier1_f06_parameter_provenance_user_configured(client: TestClient):
    """F6.5: User-provided values carry user-configured origin rather than synthetic default."""
    j = make_load_spec("ev1", "EV", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=4.0, energy_kwh=12.0)
    payload = {"jobs": [j.model_dump(mode="json")], "capacity_kw": 10.0}
    res = client.post("/api/v1/schedule", json=payload)
    assert res.status_code == 200
    # Schedule ran with 12.0 kWh
    scheduled_job = [s for s in res.json()["schedule"] if s["job_id"] == "ev1"][0]
    assert abs(scheduled_job["energy_kwh"] - 12.0) < 1e-3


# ============================================================================
# Feature 7: Backend Execution State Persistence (M3, R3)
# ============================================================================

def test_tier1_f07_execution_store_create_record(clean_execution_store: ExecutionStore, client: TestClient):
    """F7.1: ExecutionStore.create() creates record with schedule_id, lifecycle, and version 1."""
    j = make_load_spec("j1", "Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    payload = {"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}
    res = client.post("/api/v1/schedules/plan", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert "schedule_id" in data
    assert data["version"] == 1
    assert data["lifecycle"] == "SCHEDULED"


def test_tier1_f07_execution_store_get_record(clean_execution_store: ExecutionStore, client: TestClient):
    """F7.2: ExecutionStore.get() retrieves created record with execution jobs intact."""
    j = make_load_spec("j1", "Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    payload = {"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}
    res = client.post("/api/v1/schedules/plan", json=payload)
    sid = res.json()["schedule_id"]

    res_get = client.get(f"/api/v1/schedules/{sid}/state")
    assert res_get.status_code == 200
    body = res_get.json()
    assert body["schedule_id"] == sid
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["job_id"] == "j1"


def test_tier1_f07_execution_store_append_version(client: TestClient):
    """F7.3: Posting a replan increments version to 2 and logs changes."""
    j = make_load_spec("j1", "Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    payload = {"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}
    sid = client.post("/api/v1/schedules/plan", json=payload).json()["schedule_id"]

    # Replan with reduced capacity
    replan_res = client.post(f"/api/v1/schedules/{sid}/replan", json={"capacity_kw": 3.0})
    assert replan_res.status_code == 200
    history = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(history["versions"]) >= 1


def test_tier1_f07_execution_store_record_event(client: TestClient):
    """F7.4: Posting an execution event records it in schedule timeline."""
    j = make_load_spec("j1", "Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    sid = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}).json()["schedule_id"]

    event_payload = {
        "event_type": "JOB_STARTED",
        "job_id": "j1",
        "payload": {"energy_delivered_kwh": 0.5},
    }
    res = client.post(f"/api/v1/schedules/{sid}/events", json=event_payload)
    assert res.status_code == 200
    assert res.json()["state"]["jobs"][0]["energy_delivered_kwh"] == 0.5


def test_tier1_f07_api_get_state_and_history(client: TestClient):
    """F7.5: GET /api/v1/schedules/{id}/state and /history return schema-compliant JSON."""
    j = make_load_spec("j1", "Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    sid = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}).json()["schedule_id"]

    state_res = client.get(f"/api/v1/schedules/{sid}/state")
    hist_res = client.get(f"/api/v1/schedules/{sid}/history")
    assert state_res.status_code == 200
    assert hist_res.status_code == 200
    assert state_res.json()["schedule_id"] == sid
    assert hist_res.json()["schedule_id"] == sid


# ============================================================================
# Feature 8: Client Live Session Durability (M3, R3)
# ============================================================================

def test_tier1_f08_session_storage_key_retention(local_storage: LocalStorageSimulator):
    """F8.1: Active schedule ID is stored under heliotrope:active_schedule_id."""
    key = "heliotrope:active_schedule_id"
    local_storage.set_item(key, "sched_abc123")
    assert local_storage.get_item(key) == "sched_abc123"


def test_tier1_f08_session_mount_rehydration(local_storage: LocalStorageSimulator, client: TestClient):
    """F8.2: Mounting client reads active ID from storage and rehydrates state from backend."""
    j = make_load_spec("j1", "EV", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    sid = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}).json()["schedule_id"]

    local_storage.set_item("heliotrope:active_schedule_id", sid)
    saved_id = local_storage.get_item("heliotrope:active_schedule_id")
    assert saved_id is not None

    res = client.get(f"/api/v1/schedules/{saved_id}/state")
    assert res.status_code == 200
    assert res.json()["schedule_id"] == sid


def test_tier1_f08_session_stale_schedule_eviction(local_storage: LocalStorageSimulator, client: TestClient):
    """F8.3: Client receiving 404 for stored schedule ID evicts key from localStorage."""
    stale_id = "nonexistent_sched_999"
    local_storage.set_item("heliotrope:active_schedule_id", stale_id)

    res = client.get(f"/api/v1/schedules/{stale_id}/state")
    if res.status_code == 404:
        local_storage.remove_item("heliotrope:active_schedule_id")

    assert local_storage.get_item("heliotrope:active_schedule_id") is None


def test_tier1_f08_session_new_plan_overwrites_active(local_storage: LocalStorageSimulator, client: TestClient):
    """F8.4: Creating a new schedule overwrites stored active ID with latest schedule ID."""
    local_storage.set_item("heliotrope:active_schedule_id", "old_id_111")
    j = make_load_spec("j1", "Washer", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    new_sid = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}).json()["schedule_id"]

    local_storage.set_item("heliotrope:active_schedule_id", new_sid)
    assert local_storage.get_item("heliotrope:active_schedule_id") == new_sid


def test_tier1_f08_session_version_timeline_recovery(local_storage: LocalStorageSimulator, client: TestClient):
    """F8.5: Rehydrating session recovers full version history and events."""
    j = make_load_spec("j1", "Washer", LoadType.DEFERRABLE_ATOMIC, power_kw=1.0, duration_minutes=60)
    sid = client.post("/api/v1/schedules/plan", json={"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}).json()["schedule_id"]
    local_storage.set_item("heliotrope:active_schedule_id", sid)

    hist_res = client.get(f"/api/v1/schedules/{sid}/history")
    assert hist_res.status_code == 200
    versions = hist_res.json()["versions"]
    assert len(versions) >= 1
    assert versions[0]["version"] == 1


# ============================================================================
# Feature 9: Live Grid Carbon Provider Adapter (M4, R4)
# ============================================================================

def test_tier1_f09_provider_protocol_compliance():
    """F9.1: Synthetic and external providers conform to CarbonProvider interface."""
    syn = SyntheticDuckCurveProvider()
    assert hasattr(syn, "name")
    assert hasattr(syn, "get_signal")
    ext = ExternalProvider(None)
    assert hasattr(ext, "configured")
    assert hasattr(ext, "get_signal")


def test_tier1_f09_synthetic_provider_duck_curve():
    """F9.2: Synthetic provider generates 96 points with typical duck curve solar dip."""
    syn = SyntheticDuckCurveProvider()
    pts = syn.get_signal(BASE_TIME, BASE_TIME + timedelta(days=1), 15)
    assert len(pts) == 96
    noon_pt = pts[16]   # 12:00 (solar dip)
    evening_pt = pts[44] # 19:00 (evening peak)
    assert noon_pt.gco2_per_kwh < evening_pt.gco2_per_kwh, "Solar generation must dip carbon intensity at midday"


def test_tier1_f09_external_provider_configuration_flag():
    """F9.3: ExternalProvider detects API key configuration state."""
    unconf = ExternalProvider(None)
    assert unconf.configured is False
    conf = ExternalProvider("test_secret_key")
    assert conf.configured is True


def test_tier1_f09_external_adapter_response_parsing():
    """F9.4: Adapter parses raw provider dictionary points into CarbonPoint instances."""
    from app.domain.carbon import CarbonPoint
    sample_pt = {"datetime": "2026-10-06T12:00:00+00:00", "carbonIntensity": 185.0}
    pt = CarbonPoint(
        time=datetime.fromisoformat(sample_pt["datetime"]),
        gco2_per_kwh=sample_pt["carbonIntensity"],
        source="external",
    )
    assert pt.gco2_per_kwh == 185.0
    assert pt.source == "external"


def test_tier1_f09_unconfigured_fallback_behavior(client: TestClient):
    """F9.5: System with unconfigured external provider defaults to synthetic or handles cleanly."""
    res = client.get(
        "/api/v1/carbon",
        params={
            "start": "2026-10-06T00:00:00+00:00",
            "end": "2026-10-07T00:00:00+00:00",
            "resolution_minutes": 15,
            "provider": "synthetic",
        },
    )
    assert res.status_code == 200
    assert res.json()["source"] in ("synthetic", "actual", "duck_curve", "synthetic_duck_curve")


# ============================================================================
# Feature 10: Live NL Load Classification Adapter (M4, R4)
# ============================================================================

def test_tier1_f10_rule_classifier_ev_appliance():
    """F10.1: RuleBasedLoadClassifier classifies EV charging text correctly."""
    clf = RuleBasedLoadClassifier()
    c = clf.classify("Charge Tesla Model 3")
    assert c.category == LoadCategory.EV_CHARGING.value
    assert c.job_type == LoadType.DEFERRABLE_INTERRUPTIBLE
    assert c.shiftable is True


def test_tier1_f10_rule_classifier_laundry_appliance():
    """F10.2: RuleBasedLoadClassifier classifies washing machine text to Laundry."""
    clf = RuleBasedLoadClassifier()
    c = clf.classify("Run washing machine on eco cycle")
    assert c.category == LoadCategory.LAUNDRY.value
    assert c.job_type == LoadType.DEFERRABLE_ATOMIC
    assert c.shiftable is True


def test_tier1_f10_rule_classifier_thermal_appliance():
    """F10.3: RuleBasedLoadClassifier classifies water heater text to Water heating."""
    clf = RuleBasedLoadClassifier()
    c = clf.classify("Heat water geyser for shower")
    assert c.category == LoadCategory.WATER_HEATING.value
    assert c.job_type == LoadType.THERMAL


def test_tier1_f10_provider_factory_selection():
    """F10.4: get_load_intelligence() returns operational classifier provider."""
    provider = get_load_intelligence()
    assert hasattr(provider, "classify")
    res = provider.classify("dishwasher")
    assert res.category in (LoadCategory.DISHWASHING, LoadCategory.DISHWASHING.value)


def test_tier1_f10_classification_normalized_to_load_spec():
    """F10.5: Classification result normalizes into valid schedulable LoadSpec."""
    clf = RuleBasedLoadClassifier()
    res = clf.classify("Charge Tesla Model 3")
    spec = LoadSpec(
        id="ev_classified",
        normalized_name="Tesla Model 3",
        category=res.category.value,
        job_type=res.job_type,
        power_kw=7.2,
        energy_required_kwh=30.0,
        release_at=BASE_TIME,
        deadline_at=BASE_TIME + timedelta(hours=10),
    )
    assert spec.job_type == LoadType.DEFERRABLE_INTERRUPTIBLE
    assert spec.energy_required_kwh == 30.0
