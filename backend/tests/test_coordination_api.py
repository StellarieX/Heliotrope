"""Coordination API: thin, honest, structured."""

from fastapi.testclient import TestClient

from app.main import app

from .fixtures import at

client = TestClient(app)


def coord_body(n=2, **overrides):
    jobs = [
        {
            "id": f"ev-{i}",
            "normalized_name": f"EV {i}",
            "category": "EV charging",
            "job_type": "DEFERRABLE_INTERRUPTIBLE",
            "power_kw": 7.2,
            "max_power_kw": 7.2,
            "energy_required_kwh": 7.2,
            "min_chunk_minutes": 15,
            "release_at": at(18).isoformat(),
            "deadline_at": at(31).isoformat(),
            "participant_id": f"user-{i}",
        }
        for i in range(n)
    ]
    body = {
        "participants": [{"id": f"user-{i}"} for i in range(n)],
        "shared_resource": {"capacity_kw": 30.0},
        "jobs": jobs,
    }
    body.update(overrides)
    return body


def test_schedule_coordinated_200():
    res = client.post("/api/v1/coordination/schedule", json=coord_body())
    assert res.status_code == 200
    body = res.json()
    assert body["status"] in ("OPTIMAL", "FEASIBLE")
    assert len(body["jobs"]) == 2
    assert len(body["aggregate_profile"]) > 0
    point = body["aggregate_profile"][0]
    assert set(point) >= {
        "timestamp", "baseline_kw", "flexible_kw", "total_kw",
        "capacity_kw", "utilization", "carbon_intensity", "congestion_score",
    }
    assert body["metrics"]["capacity_violations"] == 0


def test_schedule_independent_200_reports_violations():
    res = client.post(
        "/api/v1/coordination/schedule",
        json=coord_body(20, coordination_mode="INDEPENDENT"),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["coordination_mode"] == "INDEPENDENT"
    assert body["metrics"]["capacity_violations"] > 0


def test_compare_both_modes():
    res = client.post("/api/v1/coordination/compare", json=coord_body(4))
    assert res.status_code == 200
    body = res.json()
    assert body["independent"]["coordination_mode"] == "INDEPENDENT"
    assert body["coordinated"]["coordination_mode"] == "COORDINATED"


def test_unknown_participant_422():
    body = coord_body()
    body["jobs"][0]["participant_id"] = "ghost"
    res = client.post("/api/v1/coordination/schedule", json=body)
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"


def test_duplicate_participants_422():
    body = coord_body()
    body["participants"] = [{"id": "user-0"}, {"id": "user-0"}]
    res = client.post("/api/v1/coordination/schedule", json=body)
    assert res.status_code == 422
