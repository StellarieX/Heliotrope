"""POST /schedule with a time-varying connection capacity.

`capacity_profile_kw` overrides the scalar `capacity_kw` slot by slot — the same
normalizer support replan/coordination already use. A matching profile
schedules against per-slot caps; a length mismatch is a 422, never a silent
truncation or pad.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.services.scheduler_normalizer import NormalizationError
from app.services.scheduler_service import SchedulerService

from .fixtures import ev_job, make_signal

client = TestClient(app)


def _payload(**overrides):
    body = {
        "jobs": [ev_job().model_dump(mode="json")],
        "capacity_kw": 10.0,
        "scheduler": "CPSAT",
    }
    body.update(overrides)
    return body


def _slot_count():
    service = SchedulerService()
    scheduler_input, _ = service.build_input([ev_job()], make_signal(), 10.0)
    return scheduler_input.horizon.slot_count


def test_matching_profile_schedules_200():
    n = _slot_count()
    res = client.post(
        "/api/v1/schedule", json=_payload(capacity_profile_kw=[10.0] * n)
    )
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")


def test_mismatched_profile_is_422():
    n = _slot_count()
    res = client.post(
        "/api/v1/schedule", json=_payload(capacity_profile_kw=[10.0] * (n - 1))
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"


def test_build_input_rejects_mismatch_as_normalization_error():
    service = SchedulerService()
    try:
        service.build_input(
            [ev_job()], make_signal(), 10.0, capacity_profile_kw=[10.0] * 3
        )
    except NormalizationError as exc:
        assert "capacity profile has" in str(exc)
    else:
        raise AssertionError("mismatched profile must raise NormalizationError")


def test_scalar_path_unchanged_without_profile():
    res = client.post("/api/v1/schedule", json=_payload())
    assert res.status_code == 200
    assert res.json()["status"] in ("OPTIMAL", "FEASIBLE")
