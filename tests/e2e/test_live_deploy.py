"""Live deployment smoke tests (end-to-end over HTTP).

These are the ONLY e2e tests that touch the network. They are marked `live`
and skip unless the target origins are provided, so the default suite stays
fully hermetic (in-process TestClient):

    HELIOTROPE_E2E_BASE_URL=https://<backend-host> \\
      python -m pytest tests/e2e -q -m live

    # Frontend same-origin check (Vercel): the deployed page must proxy
    # /api/v1/health to the backend via next.config.ts rewrites.
    HELIOTROPE_E2E_FRONTEND_URL=https://<app>.vercel.app \\
      python -m pytest tests/e2e/test_live_deploy.py -q -m live

Run the hermetic suite without these via `-k "not live"`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

BACKEND_URL = os.environ.get("HELIOTROPE_E2E_BASE_URL", "").strip().rstrip("/")
FRONTEND_URL = os.environ.get("HELIOTROPE_E2E_FRONTEND_URL", "").strip().rstrip("/")

requires_backend = pytest.mark.skipif(
    not BACKEND_URL, reason="HELIOTROPE_E2E_BASE_URL not set"
)
requires_frontend = pytest.mark.skipif(
    not FRONTEND_URL, reason="HELIOTROPE_E2E_FRONTEND_URL not set"
)


@requires_backend
def test_live_backend_health():
    import httpx

    res = httpx.get(f"{BACKEND_URL}/api/v1/health", timeout=30.0)
    assert res.status_code == 200, f"backend health unreachable: {res.status_code}"
    body = res.json()
    assert body.get("status") == "ok"
    assert body.get("service") == "heliotrope-backend"


@requires_backend
def test_live_backend_cors_allows_frontend():
    """Preflight from the deployed frontend origin must not be rejected."""
    import httpx

    if not FRONTEND_URL:
        pytest.skip("HELIOTROPE_E2E_FRONTEND_URL not set; CORS check needs an Origin")
    res = httpx.options(
        f"{BACKEND_URL}/api/v1/health",
        headers={
            "Origin": FRONTEND_URL,
            "Access-Control-Request-Method": "GET",
        },
        timeout=30.0,
    )
    assert res.status_code < 500, f"CORS preflight failed: {res.status_code}"
    allow = res.headers.get("access-control-allow-origin", "")
    assert allow in (FRONTEND_URL, "*"), f"origin not allowed: {allow!r}"


@requires_frontend
def test_live_frontend_proxies_api_to_backend():
    """Same-origin /api/v1/health on the Vercel app must reach the backend."""
    import httpx

    res = httpx.get(f"{FRONTEND_URL}/api/v1/health", timeout=30.0)
    assert res.status_code == 200, (
        f"frontend same-origin /api/v1/health failed ({res.status_code}); "
        "check BACKEND_URL and next.config.ts rewrites"
    )
    assert res.json().get("status") == "ok"
