"""CORS: the frontend runs on a different origin than the API.

Regression tests for a silent failure: `curl` succeeded while every browser
`fetch()` was blocked, so the dashboard reported "backend unreachable" for a
backend that was running perfectly. Nothing failed loudly; the UI just lied.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.core import config


@pytest.fixture()
def cors_client() -> TestClient:
    return TestClient(app)


PREFLIGHT = {
    "Origin": "http://localhost:3000",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type",
}


def test_browser_preflight_is_answered(cors_client):
    response = cors_client.options(
        "/api/v1/loads/classify", headers=PREFLIGHT
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_classify_response_carries_the_origin_header(cors_client):
    response = cors_client.post(
        "/api/v1/loads/classify", json={"name": "EV"}, headers={"Origin": "http://localhost:3000"}
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_carbon_endpoint_is_browser_reachable_too(cors_client):
    """The Phase 2 chart was broken by the same missing header."""
    response = cors_client.get(
        "/api/v1/carbon",
        params={"start": "2026-10-05T00:00:00+00:00", "end": "2026-10-05T01:00:00+00:00"},
        headers={"Origin": "http://localhost:3000"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_dev_allows_any_loopback_port(cors_client):
    """The Next dev server picks a free port, so pinning 3000 would be wrong."""
    for origin in ("http://localhost:49404", "http://127.0.0.1:3000", "http://localhost:3000"):
        response = cors_client.post(
            "/api/v1/loads/classify", json={"name": "EV"}, headers={"Origin": origin}
        )
        assert response.headers.get("access-control-allow-origin") == origin, origin


def test_non_loopback_origin_is_not_allowed_in_development(cors_client):
    """Loopback-only is a development convenience, not a blanket open door."""
    response = cors_client.post(
        "/api/v1/loads/classify",
        json={"name": "EV"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_production_defaults_to_same_origin_only(monkeypatch):
    """With no explicit list, production must not inherit the dev convenience."""
    assert config.HELIOTROPE_ENV == "development"
    monkeypatch.setattr(config, "HELIOTROPE_ENV", "production")
    monkeypatch.setattr(config, "CORS_ALLOW_LOCALHOST_IN_DEVELOPMENT", False)
    monkeypatch.setattr(config, "CORS_ALLOW_ORIGINS", [])

    import importlib

    module = importlib.reload(importlib.import_module("app.main"))
    try:
        client = TestClient(module.app)
        response = client.post(
            "/api/v1/loads/classify",
            json={"name": "EV"},
            headers={"Origin": "http://localhost:3000"},
        )
        assert "access-control-allow-origin" not in response.headers
    finally:
        importlib.reload(module)
