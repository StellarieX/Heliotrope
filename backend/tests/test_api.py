"""API honesty: a real schedule or a truthful refusal, never a fabricated one.

Phase 1 answered `/schedule` with 501 because no engine existed. Phase 4 builds
the engine, so the 501 contract is gone -- but the honesty that contract encoded
is not. These tests now check the stronger property it stood in for: whatever
comes back is either a genuinely scheduled and independently validated result,
or an explicit failure. There is no middle ground where a partial or invented
schedule is dressed up as success (§8, §20).
"""

from .conftest import job_payload, load_spec_payload


def test_schedule_really_schedules_instead_of_501(client):
    """The endpoint is live: it returns a real, validated schedule."""
    res = client.post(
        "/api/v1/schedule",
        json={"jobs": [load_spec_payload()], "capacity_kw": 10.0, "scheduler": "ASAP"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["scheduler"] == "ASAP"
    assert body["status"] in ("FEASIBLE", "OPTIMAL")
    assert body["schedule"], "a feasible run must place the job"
    # A placed schedule has been through the independent validator, which
    # forces INTERNAL_ERROR on any hard-constraint breach.
    assert body["violations"] == []
    assert body["metrics"]["feasibility_violations"] == 0
    assert body["metrics"]["total_co2_kg"] is not None


def test_schedule_never_fabricates_an_optimum(client):
    """`is_optimal` is only ever true when the solver PROVED it (§8, §46)."""
    res = client.post(
        "/api/v1/schedule",
        json={"jobs": [load_spec_payload()], "capacity_kw": 10.0, "scheduler": "CPSAT"},
    )
    assert res.status_code == 200
    body = res.json()
    solver = body["solver"]
    if solver["is_optimal"]:
        assert solver["status"] == "OPTIMAL"
        assert body["status"] == "OPTIMAL"
    # ASAP and Greedy are heuristics and must never claim a proof.
    assert body["status"] != "OPTIMAL" or solver["is_optimal"]


def test_schedule_unknown_scheduler_is_400_never_substituted(client):
    """An unknown engine name is refused, not quietly swapped for a default."""
    res = client.post(
        "/api/v1/schedule",
        json={"jobs": [load_spec_payload()], "capacity_kw": 10.0, "scheduler": "MAGIC"},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "invalid_scheduler"


def test_schedule_infeasible_is_200_with_a_reason(client):
    """INFEASIBLE is a valid answer, not an HTTP error (§20, §44)."""
    res = client.post(
        "/api/v1/schedule",
        # 7.2 kW on a 1 kW connection can never fit.
        json={"jobs": [load_spec_payload()], "capacity_kw": 1.0, "scheduler": "CPSAT"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "INFEASIBLE"
    assert body["reason"], "an infeasible answer must explain itself"
    assert body["violations"] or not body["schedule"]


def test_schedule_rejects_empty_job_list(client):
    res = client.post(
        "/api/v1/schedule",
        json={"jobs": [], "capacity_kw": 10.0},
    )
    assert res.status_code == 422


def test_compare_runs_every_engine_over_one_input(client):
    """§42: one input, three engines, one recorded fingerprint."""
    res = client.post(
        "/api/v1/schedule/compare",
        json={"jobs": [load_spec_payload()], "capacity_kw": 10.0},
    )
    assert res.status_code == 200
    body = res.json()
    assert set(body["results"]) == {"ASAP", "GREEDY", "CPSAT"}
    assert body["input_fingerprint"]
    assert body["reference_scheduler"] == "ASAP"
    for name, result in body["results"].items():
        assert result["violations"] == [], f"{name} produced violations"


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
