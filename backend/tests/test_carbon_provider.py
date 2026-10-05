"""Carbon provider selection: the service owns the allowlist, the route passes through.

GET /api/v1/carbon takes an optional `provider`. An omitted provider serves the
configured default (`CarbonService.default()`); an explicit name goes to the
service unchanged, where an unknown name is a 422 — there is exactly one place
where provider names are accepted or rejected.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

START = "2026-10-05T00:00:00+00:00"
END = "2026-10-05T06:00:00+00:00"


def test_omitted_provider_serves_the_configured_default():
    res = client.get("/api/v1/carbon", params={"start": START, "end": END})
    assert res.status_code == 200
    assert len(res.json()["points"]) == 24


def test_explicit_synthetic_provider_works():
    res = client.get(
        "/api/v1/carbon", params={"start": START, "end": END, "provider": "synthetic"}
    )
    assert res.status_code == 200
    assert len(res.json()["points"]) == 24


def test_unknown_provider_is_422_not_500():
    res = client.get(
        "/api/v1/carbon", params={"start": START, "end": END, "provider": "bogus"}
    )
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_request"
