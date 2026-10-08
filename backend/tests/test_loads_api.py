"""Load API: classify and validate endpoints (§18, §32).

Contract-level tests. Two things they must keep proving:
  * a real answer, with real numbers, for every §32 example appliance
  * no fabricated physical values anywhere in the response
"""

import pytest

from datetime import datetime

# §32 requires these to work by name.
REQUIRED_EXAMPLES = ["EV", "washing machine", "geyser", "AC", "fan", "fridge"]


def classify(client, **body):
    return client.post("/api/v1/loads/classify", json=body)


# --- classify --------------------------------------------------------------


def test_health_still_answers(client):
    assert client.get("/api/v1/health").status_code == 200


@pytest.mark.parametrize("name", REQUIRED_EXAMPLES)
def test_every_required_example_classifies(client, name):
    response = classify(client, name=name)
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "rule_based"
    assert body["classification"]["reason"]
    assert body["classification"]["job_type"] in {
        "FIXED",
        "DEFERRABLE_ATOMIC",
        "DEFERRABLE_INTERRUPTIBLE",
        "THERMAL",
    }


def test_classify_response_has_the_documented_shape(client):
    body = classify(client, name="Hostel EV").json()
    assert set(body) == {
        "provider",
        "classification",
        "confidence",
        "ambiguous",
        "assumptions",
        "normalized_load_spec",
        "feasibility",
    }
    classification = body["classification"]
    for key in ("name", "category", "job_type", "shiftable", "confidence", "reason", "assumptions"):
        assert key in classification


def test_classify_invents_no_physical_values(client):
    """§30 enforced at the API boundary: a bare name yields no numbers."""
    spec = classify(client, name="washing machine").json()["normalized_load_spec"]
    assert spec["power_kw"] is None
    assert spec["duration_minutes"] is None
    assert spec["energy_required_kwh"] is None


def test_classify_midnight_window_is_resolved(client):
    body = classify(
        client, name="EV", release_wall="21:00", deadline_wall="07:00", timezone="UTC"
    ).json()
    spec = body["normalized_load_spec"]
    assert spec["release_at"] < spec["deadline_at"], "a 21:00 -> 07:00 window is ten hours, not negative"
    release = datetime.fromisoformat(spec["release_at"])
    deadline = datetime.fromisoformat(spec["deadline_at"])
    assert (deadline - release).total_seconds() / 3600 == pytest.approx(10.0)
    assert release.hour == 21 and deadline.hour == 7


def test_classify_records_user_values_as_user_configured(client):
    body = classify(
        client,
        name="Hostel EV",
        power_kw=7.2,
        energy_required_kwh=18.0,
        min_chunk_minutes=15,
        release_wall="18:30",
        deadline_wall="07:00",
    ).json()
    spec = body["normalized_load_spec"]
    origins = {a["field"]: a["origin"] for a in spec["assumptions"]}
    assert origins["power_kw"] == "user-configured"
    assert origins["energy_required_kwh"] == "user-configured"
    assert origins["deadline_at"] == "derived"


def test_classify_feasibility_is_included(client):
    body = classify(
        client,
        name="EV",
        power_kw=5.0,
        energy_required_kwh=50.0,
        release_wall="12:00",
        deadline_wall="14:00",
    ).json()
    assert body["feasibility"]["feasible"] is False
    assert body["feasibility"]["errors"]


def test_classify_requires_a_name(client):
    assert client.post("/api/v1/loads/classify", json={}).status_code == 422


def test_classify_rejects_nonsense_numbers(client):
    assert classify(client, name="EV", power_kw=-3).status_code == 422
    assert classify(client, name="EV", energy_required_kwh=-1).status_code == 422


# --- validate --------------------------------------------------------------


def feasible_ev(**overrides) -> dict:
    body = {
        "normalized_name": "Hostel EV",
        "category": "EV charging",
        "job_type": "DEFERRABLE_INTERRUPTIBLE",
        "power_kw": 7.2,
        "energy_required_kwh": 18.0,
        "min_chunk_minutes": 15,
        "release_at": "2099-10-05T18:30:00+00:00",
        "deadline_at": "2099-10-06T07:00:00+00:00",
    }
    body.update(overrides)
    return body


def test_validate_feasible_load(client):
    response = client.post("/api/v1/loads/validate", json=feasible_ev())
    assert response.status_code == 200
    body = response.json()
    assert body["feasible"] is True
    assert set(body["checks_run"]) == {"time", "energy", "thermal"}


def test_validate_returns_scheduling_semantics(client):
    body = client.post("/api/v1/loads/validate", json=feasible_ev()).json()
    assert body["semantics"]["decision_variable"] == "power"
    assert body["semantics"]["primary_requirement"] == "energy"


def test_validate_reports_physical_impossibility_with_numbers(client):
    body = client.post(
        "/api/v1/loads/validate",
        json=feasible_ev(
            power_kw=5.0,
            energy_required_kwh=50.0,
            release_at="2099-10-05T12:00:00+00:00",
            deadline_at="2099-10-05T14:00:00+00:00",
        ),
    ).json()
    assert body["feasible"] is False
    error = body["errors"][0]
    assert error["code"] == "energy_exceeds_window"
    assert "50.00 kWh" in error["message"]
    assert error["detail"]["max_deliverable_kwh"] == pytest.approx(10.0)


def test_validate_rejects_nonsense_with_422(client):
    assert client.post("/api/v1/loads/validate", json=feasible_ev(power_kw=-1)).status_code == 422


def test_validate_rejects_inverted_band_with_422(client):
    from app.domain.thermal_examples import GEYSER_SYNTHETIC

    bad = GEYSER_SYNTHETIC.model_dump()
    bad["temperature_min_c"] = 80.0
    bad["temperature_max_c"] = 50.0
    assert (
        client.post(
            "/api/v1/loads/validate",
            json={
                "normalized_name": "Geyser",
                "category": "Water heating",
                "job_type": "THERMAL",
                "thermal": bad,
            },
        ).status_code
        == 422
    )


def test_validate_accepts_a_thermal_power_profile(client):
    from app.domain.thermal_examples import GEYSER_SYNTHETIC

    body = {
        "normalized_name": "Geyser",
        "category": "Water heating",
        "job_type": "THERMAL",
        "thermal": GEYSER_SYNTHETIC.model_dump(),
        "release_at": "2099-10-05T00:00:00+00:00",
        "deadline_at": "2099-10-05T12:00:00+00:00",
        "power_profile": [2.0] * 8 + [0.0] * 3,
    }
    assert client.post("/api/v1/loads/validate", json=body).json()["feasible"] is True


def test_validate_rejects_an_overheating_profile(client):
    from app.domain.thermal_examples import GEYSER_SYNTHETIC

    body = {
        "normalized_name": "Geyser",
        "category": "Water heating",
        "job_type": "THERMAL",
        "thermal": GEYSER_SYNTHETIC.model_dump(),
        "release_at": "2099-10-05T00:00:00+00:00",
        "deadline_at": "2099-10-05T12:00:00+00:00",
        "power_profile": [2.0] * 40,
    }
    result = client.post("/api/v1/loads/validate", json=body).json()
    assert result["feasible"] is False
    assert any(e["code"] == "thermal_band_violation" for e in result["errors"])


def test_validate_does_not_leak_power_profile_into_the_load(client):
    body = feasible_ev()
    body["power_profile"] = [7.2] * 10
    client.post("/api/v1/loads/validate", json=body)
    # The field is a validation aid, so it must not be echoed back as load data.
    assert "power_profile" not in feasible_ev()


def test_validate_exposes_metric_inputs_without_computing_metrics(client):
    body = client.post("/api/v1/loads/validate", json=feasible_ev()).json()
    inputs = body["metric_inputs"]
    assert inputs["peak_contribution_kw"] == pytest.approx(7.2)
    assert inputs["energy_required_kwh"] == pytest.approx(18.0)
    assert not any("carbon" in k or "cost" in k for k in inputs)


# --- Phase 3 must not have built Phase 4 (§32, definition of done) ---------
#
# Phase 3's guard here asserted /schedule was still 501. Phase 4 builds the
# engine, so that placeholder is retired; what replaces it is the boundary that
# still matters -- load intelligence must not smuggle scheduling math into the
# load endpoints, and carbon/energy must still be computed only by the backend.


def test_load_endpoints_do_not_schedule(client):
    """Classification stays advisory; it never returns a placement (§48)."""
    response = classify(
        client, name="EV", power_kw=7.2, energy_required_kwh=18.0
    )
    assert response.status_code == 200
    body = response.json()
    # The load layer describes WHAT a job needs. WHERE and WHEN it runs is the
    # scheduler's job, so no placement key may appear here.
    for key in ("start_slot", "end_slot", "start_time", "end_time", "allocations"):
        assert key not in body, f"classification leaked scheduling key {key!r}"


def test_empty_schedule_request_is_422(client):
    """An empty job list is a malformed request, not an empty schedule."""
    response = client.post(
        "/api/v1/schedule",
        json={"jobs": [], "capacity_kw": 10.0},
    )
    assert response.status_code == 422
    assert "schedule" not in response.json()


def test_carbon_endpoint_is_unaffected(client):
    response = client.get(
        "/api/v1/carbon",
        params={"start": "2026-10-05T00:00:00+00:00", "end": "2026-10-06T00:00:00+00:00"},
    )
    assert response.status_code == 200
    assert response.json()["signal_type"] == "SYNTHETIC"


def test_classify_is_repeatable_over_the_api(client):
    """Same request, same answer: no randomness, no caching surprises."""
    first = classify(client, name="heater").json()
    second = classify(client, name="heater").json()
    assert first == second
