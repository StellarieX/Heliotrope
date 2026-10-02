"""API honesty: valid-but-unimplemented -> 501, invalid -> 422, never fabricated."""

from .conftest import job_payload


def test_schedule_valid_returns_501_not_fake(client):
    res = client.post(
        "/api/v1/schedule",
        json={"jobs": [job_payload()], "capacity_kw": 10.0, "scheduler": "CPSAT"},
    )
    assert res.status_code == 501
    body = res.json()
    assert body["solver"] == "CPSAT"
    assert body["jobs_received"] == 1
    assert "schedule" not in body
    assert "metrics" not in body


def test_schedule_rejects_bad_job_with_422(client):
    res = client.post(
        "/api/v1/schedule",
        json={"jobs": [job_payload(power_kw=-3)], "capacity_kw": 10.0},
    )
    assert res.status_code == 422


def test_schedule_rejects_zero_capacity(client):
    res = client.post(
        "/api/v1/schedule",
        json={"jobs": [job_payload()], "capacity_kw": 0},
    )
    assert res.status_code == 422


def test_carbon_valid_returns_real_signal(client):
    res = client.get(
        "/api/v1/carbon",
        params={
            "start": "2026-10-05T00:00:00+00:00",
            "end": "2026-10-06T00:00:00+00:00",
            "resolution_minutes": 15,
            "provider": "synthetic",
        },
    )
    assert res.status_code == 200
    body = res.json()
    assert body["signal_type"] == "SYNTHETIC"
    assert body["resolution_minutes"] == 15
    assert len(body["points"]) == 96
    assert body["quality"]["complete"] is True
    first = body["points"][0]
    assert set(first) == {"timestamp", "carbon_intensity_gco2_per_kwh"}


def test_carbon_rejects_bad_window(client):
    res = client.get(
        "/api/v1/carbon",
        params={
            "start": "2026-10-06T00:00:00+00:00",
            "end": "2026-10-05T00:00:00+00:00",
        },
    )
    assert res.status_code == 422


def test_carbon_rejects_bad_resolution(client):
    res = client.get(
        "/api/v1/carbon",
        params={
            "start": "2026-10-05T00:00:00+00:00",
            "end": "2026-10-06T00:00:00+00:00",
            "resolution_minutes": 7,
        },
    )
    assert res.status_code == 422


def test_carbon_rejects_oversize_range(client):
    res = client.get(
        "/api/v1/carbon",
        params={
            "start": "2026-10-01T00:00:00+00:00",
            "end": "2026-10-20T00:00:00+00:00",
        },
    )
    assert res.status_code == 422


def test_carbon_external_without_key_is_503(client):
    res = client.get(
        "/api/v1/carbon",
        params={
            "start": "2026-10-05T00:00:00+00:00",
            "end": "2026-10-06T00:00:00+00:00",
            "provider": "external",
        },
    )
    assert res.status_code == 503
    assert res.json()["code"] == "provider_unavailable"
