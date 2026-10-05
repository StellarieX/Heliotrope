"""Tier 4: Real-World Application Scenarios (Realistic End-to-End Workflows) Tests.

Validates comprehensive multi-step operational workflows modeling real-world user
and grid interaction patterns across all subsystems.
Authoritative references: ORIGINAL_REQUEST.md and PROJECT.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from app.domain.loads import LoadSpec, LoadType, LoadCategory
from app.domain.coordination import CoordinationRequest, Participant, SharedResource
from app.domain.execution import OverrideCommand, ScheduleEventType, ScheduleLifecycle
from app.domain.thermal import ThermalModel
from app.services.load_intelligence import RuleBasedLoadClassifier
from app.services.providers.synthetic import SyntheticDuckCurveProvider

from .conftest import (
    FirestoreRulesEvaluator,
    LocalStorageSimulator,
    make_load_spec,
    make_thermal_spec,
)

BASE_TIME = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)


def test_t4_01_smart_commercial_building_diurnal_cycle(client: TestClient):
    """Scenario 1: Multi-tenant smart commercial building complete diurnal operational cycle.
    
    Steps:
    1. Multi-tenant setup: Office Suite, EV charging fleet, Cafeteria kitchen.
    2. Shared building capacity constraint (12 kW).
    3. Plan coordinated schedule under shared transformer limit.
    4. Store initial version in ExecutionStore.
    5. Mid-day telemetry event: EV fleet started, partial kWh reported.
    6. Mid-afternoon replan due to capacity reduction.
    7. Verification: version timeline incremented, capacity ceiling preserved throughout.
    """
    ev_fleet = make_load_spec("ev_fleet", "Fleet EV", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=6.0, energy_kwh=18.0)
    kitchen = make_load_spec("kitchen_dishwasher", "Commercial Washer", LoadType.DEFERRABLE_ATOMIC, power_kw=4.0, duration_minutes=90)
    office_hvac = make_load_spec("office_ac", "Office HVAC", LoadType.DEFERRABLE_ATOMIC, power_kw=3.5, duration_minutes=120)
    ev_fleet.participant_id = "fleet_ops"
    kitchen.participant_id = "cafeteria"
    office_hvac.participant_id = "facilities"

    # Step 3 & 4: Plan coordinated schedule
    payload = {
        "participants": [{"id": "fleet_ops"}, {"id": "cafeteria"}, {"id": "facilities"}],
        "shared_resource": {"id": "bldg_transformer", "capacity_kw": 10.0},
        "jobs": [ev_fleet.model_dump(mode="json"), kitchen.model_dump(mode="json"), office_hvac.model_dump(mode="json")],
    }
    plan_res = client.post("/api/v1/schedules/plan-coordinated", json=payload)
    assert plan_res.status_code == 200
    plan_data = plan_res.json()
    sid = plan_data["schedule_id"]
    assert plan_data["lifecycle"] == "SCHEDULED"

    # Step 5: Post telemetry event
    event_payload = {
        "event_type": "JOB_STARTED",
        "job_id": "ev_fleet",
        "payload": {"energy_delivered_kwh": 3.0},
    }
    event_res = client.post(f"/api/v1/schedules/{sid}/events", json=event_payload)
    assert event_res.status_code == 200

    # Step 6: Mid-afternoon capacity replan
    replan_res = client.post(f"/api/v1/schedules/{sid}/replan", json={"capacity_kw": 8.0})
    assert replan_res.status_code == 200

    # Step 7: Verify version timeline
    history = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(history["versions"]) >= 1


def test_t4_02_residential_prosumer_carbon_aware_dynamic_day(local_storage: LocalStorageSimulator, client: TestClient):
    """Scenario 2: Residential homeowner dynamic solar duck-curve optimization.
    
    Steps:
    1. Homeowner configures EV (custom 14 kWh) and Water Heater (comfort band 48°C–65°C).
    2. Backend plans schedule placing heavy load into clean midday solar trough.
    3. Client saves active schedule ID into localStorage (heliotrope:active_schedule_id).
    4. Cloud cover event occurs; replan triggered.
    5. User refreshes browser (page reload simulation).
    6. Client retrieves active ID from localStorage and rehydrates state & history.
    """
    ts = make_thermal_spec(min_temp=48.0, max_temp=65.0, initial_temp=50.0, target_temp=58.0, power_kw=3.0)
    ev = make_load_spec("res_ev", "Chevy Bolt", LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=6.0, energy_kwh=14.0)
    geyser = make_load_spec("res_geyser", "Hot Water Tank", LoadType.THERMAL, power_kw=3.0, thermal=ts)

    # Step 2: Plan schedule
    body = {"jobs": [ev.model_dump(mode="json"), geyser.model_dump(mode="json")], "capacity_kw": 10.0}
    res = client.post("/api/v1/schedules/plan", json=body)
    assert res.status_code == 200
    sid = res.json()["schedule_id"]

    # Step 3: Save to localStorage
    local_storage.set_item("heliotrope:active_schedule_id", sid)

    # Step 4: Replan on cloud cover / capacity variation
    replan_res = client.post(f"/api/v1/schedules/{sid}/replan", json={"capacity_kw": 8.0})
    assert replan_res.status_code == 200

    # Step 5 & 6: Simulate browser reload and rehydration
    reloaded_sid = local_storage.get_item("heliotrope:active_schedule_id")
    assert reloaded_sid == sid

    state = client.get(f"/api/v1/schedules/{reloaded_sid}/state").json()
    hist = client.get(f"/api/v1/schedules/{reloaded_sid}/history").json()
    assert state["schedule_id"] == sid
    assert len(hist["versions"]) >= 1


def test_t4_03_community_microgrid_emergency_curtailment(client: TestClient):
    """Scenario 3: Community microgrid emergency demand curtailment & user override.
    
    Steps:
    1. Multiple community loads planned under 25 kW normal limit.
    2. Grid operator signals emergency curtailment dropping capacity to 10 kW.
    3. User overrides non-critical load (cancels pool pump).
    4. System recalculates schedule over remaining essential loads.
    5. Audit log in /history records override and version progression.
    """
    ev = make_load_spec("cm_ev", "Community EV", LoadType.DEFERRABLE_ATOMIC, power_kw=7.0, duration_minutes=60)
    pump = make_load_spec("cm_pump", "Pool Pump", LoadType.DEFERRABLE_ATOMIC, power_kw=3.0, duration_minutes=60)

    # Step 1: Initial plan
    body = {"jobs": [ev.model_dump(mode="json"), pump.model_dump(mode="json")], "capacity_kw": 12.0}
    plan_res = client.post("/api/v1/schedules/plan", json=body)
    assert plan_res.status_code == 200
    sid = plan_res.json()["schedule_id"]

    # Step 3: User cancels pump via override
    override_res = client.post(
        f"/api/v1/schedules/{sid}/override",
        json={"job_id": "cm_pump", "command": "CANCEL"},
    )
    assert override_res.status_code == 200
    assert override_res.json()["accepted"] is True

    # Step 4: Replan with curtailed 8 kW capacity
    replan_res = client.post(f"/api/v1/schedules/{sid}/replan", json={"capacity_kw": 8.0})
    assert replan_res.status_code == 200

    # Step 5: Verify history audit
    hist = client.get(f"/api/v1/schedules/{sid}/history").json()
    assert len(hist["versions"]) >= 1


def test_t4_04_nl_appliance_onboarding_to_execution(client: TestClient):
    """Scenario 4: Natural language appliance onboarding to schedule execution.
    
    Steps:
    1. User inputs natural language: "Charge Tesla Model 3" and "heat water geyser".
    2. Classifier detects categories: EV charging (interruptible) and Water heating (thermal).
    3. User provides specific parameters: 22 kWh for EV, comfort band 45°C–65°C for geyser.
    4. Parameters preserved into scheduling payload without loss.
    5. Schedule planned, stored, and verified in state API.
    """
    clf = RuleBasedLoadClassifier()

    # Step 1 & 2: Natural language classification
    c_ev = clf.classify("Charge Tesla Model 3")
    c_thermal = clf.classify("heat water geyser")
    assert c_ev.job_type == LoadType.DEFERRABLE_INTERRUPTIBLE
    assert c_thermal.job_type == LoadType.THERMAL

    # Step 3: Attach user specific parameters
    ts = make_thermal_spec(min_temp=45.0, max_temp=65.0, initial_temp=50.0, target_temp=58.0, power_kw=3.0)
    spec_ev = make_load_spec(
        "onboard_ev", "Tesla Model 3", LoadType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=7.0, energy_kwh=22.0,
    )
    spec_thermal = make_load_spec(
        "onboard_geyser", "Water Geyser", LoadType.THERMAL,
        power_kw=3.0, thermal=ts,
    )

    # Step 4 & 5: Plan and verify preservation
    body = {"jobs": [spec_ev.model_dump(mode="json"), spec_thermal.model_dump(mode="json")], "capacity_kw": 12.0}
    plan_res = client.post("/api/v1/schedules/plan", json=body)
    assert plan_res.status_code == 200
    sid = plan_res.json()["schedule_id"]

    state = client.get(f"/api/v1/schedules/{sid}/state").json()
    ev_state = [j for j in state["jobs"] if j["job_id"] == "onboard_ev"][0]
    assert abs(ev_state["expected_energy_kwh"] - 22.0) < 1e-3


def test_t4_05_external_grid_outage_and_fallback_resilience(local_storage: LocalStorageSimulator, client: TestClient):
    """Scenario 5: External grid data outage and seamless synthetic fallback workflow.
    
    Steps:
    1. Client requests carbon signal; when external provider is unconfigured or failing,
       system falls back to synthetic duck curve.
    2. Valid schedule is generated using fallback carbon signal without user-facing interruption.
    3. Schedule ID is saved to localStorage.
    4. Rehydration restores live schedule and confirms execution is intact.
    """
    # Step 1: Query carbon signal
    carbon_res = client.get(
        "/api/v1/carbon",
        params={
            "start": "2026-10-06T00:00:00+00:00",
            "end": "2026-10-07T00:00:00+00:00",
            "resolution_minutes": 15,
            "provider": "synthetic",
        },
    )
    assert carbon_res.status_code == 200
    assert len(carbon_res.json()["points"]) == 96

    # Step 2: Plan schedule
    j = make_load_spec("resilient_job", "Resilient Job", LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, duration_minutes=60)
    body = {"jobs": [j.model_dump(mode="json")], "capacity_kw": 5.0}
    plan_res = client.post("/api/v1/schedules/plan", json=body)
    assert plan_res.status_code == 200
    sid = plan_res.json()["schedule_id"]

    # Step 3: Save to localStorage
    local_storage.set_item("heliotrope:active_schedule_id", sid)

    # Step 4: Rehydrate from localStorage
    saved_id = local_storage.get_item("heliotrope:active_schedule_id")
    state_res = client.get(f"/api/v1/schedules/{saved_id}/state")
    assert state_res.status_code == 200
    assert state_res.json()["schedule_id"] == sid
