"""Pytest configuration and fixtures for Heliotrope E2E Test Suite.

Ensures backend modules are on sys.path and provides shared fixtures
for TestClient, domain generators, and mocks.
"""

from __future__ import annotations
# Tests must never write into the developer's real execution database: a local demo
# would otherwise count hundreds of test schedules as other users' load.
import os as _os
import tempfile as _tempfile

_os.environ.setdefault(
    "HELIOTROPE_EXECUTION_DB",
    _os.path.join(_tempfile.mkdtemp(prefix="heliotrope-tests-"), "execution.db"),
)

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import pytest
from fastapi.testclient import TestClient

# Ensure backend root is on sys.path
_repo_root = Path(__file__).resolve().parent.parent.parent
_backend_dir = _repo_root / "backend"
if str(_backend_dir) not in sys.path:
    sys.path.insert(0, str(_backend_dir))

from app.main import app
from app.domain.loads import LoadSpec, LoadType, ThermalSpec
from app.domain.coordination import CoordinationRequest, Participant, SharedResource, CoordinationWeights
from app.services.execution_store import ExecutionStore


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "live: hits a deployed frontend/backend over HTTP; skipped unless the "
        "matching LIVE env var is set (see tests/e2e/test_live_deploy.py)",
    )


@pytest.fixture(scope="session", autouse=True)
def execution_test_adapter():
    from app.api.routes import execution
    from app.core.auth import execution_session

    async def test_session():
        yield None

    original = execution.store
    execution.store = ExecutionStore(_os.environ["HELIOTROPE_EXECUTION_DB"])
    app.dependency_overrides[execution_session] = test_session
    yield
    app.dependency_overrides.pop(execution_session, None)
    execution.store = original


@pytest.fixture(scope="session")
def client():
    """HTTP client for the Heliotrope API.

    Default: in-process FastAPI TestClient (no network, no ports).
    Live mode: set HELIOTROPE_E2E_BASE_URL to a deployed backend origin
    (e.g. https://api.example.com or http://localhost:8000 for a Docker-run
    backend) and this fixture returns an httpx.Client with the same
    get/post interface the tier tests use, so the whole suite can run
    against a real deployment: HELIOTROPE_E2E_BASE_URL=... pytest tests/e2e
    """
    live_base = os.environ.get("HELIOTROPE_E2E_BASE_URL", "").strip().rstrip("/")
    if live_base:
        import httpx

        return httpx.Client(base_url=live_base, timeout=30.0)
    return TestClient(app)


@pytest.fixture
def clean_execution_store():
    """Provides a fresh ExecutionStore instance isolated per test."""
    return ExecutionStore()


class LocalStorageSimulator:
    """Emulates browser window.localStorage for client session durability tests."""

    def __init__(self):
        self._store: Dict[str, str] = {}
        self._quota_limit_bytes = 5 * 1024 * 1024  # 5MB
        self.is_disabled = False

    def set_item(self, key: str, value: str) -> None:
        if self.is_disabled:
            raise RuntimeError("SecurityError: localStorage is disabled")
        val_str = str(value)
        total_size = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in self._store.items())
        total_size += len(key.encode("utf-8")) + len(val_str.encode("utf-8"))
        if total_size > self._quota_limit_bytes:
            raise RuntimeError("QuotaExceededError: localStorage quota exceeded")
        self._store[key] = val_str

    def get_item(self, key: str) -> Optional[str]:
        if self.is_disabled:
            raise RuntimeError("SecurityError: localStorage is disabled")
        return self._store.get(key)

    def remove_item(self, key: str) -> None:
        if self.is_disabled:
            raise RuntimeError("SecurityError: localStorage is disabled")
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


@pytest.fixture
def local_storage() -> LocalStorageSimulator:
    return LocalStorageSimulator()


def make_load_spec(
    load_id: str = "job_test",
    name: str = "Test Load",
    job_type: LoadType = LoadType.DEFERRABLE_ATOMIC,
    power_kw: float = 2.0,
    energy_kwh: Optional[float] = None,
    duration_minutes: Optional[int] = 60,
    release_offset_minutes: int = 0,
    deadline_offset_minutes: int = 480,
    thermal: Optional[ThermalSpec] = None,
) -> LoadSpec:
    """Helper to construct valid LoadSpec objects for tests."""
    now = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)
    rel = now + timedelta(minutes=release_offset_minutes)
    dead = now + timedelta(minutes=deadline_offset_minutes)
    return LoadSpec(
        id=load_id,
        normalized_name=name,
        category="Flexible",
        job_type=job_type,
        power_kw=power_kw,
        max_power_kw=power_kw,
        energy_required_kwh=energy_kwh,
        duration_minutes=duration_minutes,
        release_at=rel,
        deadline_at=dead,
        thermal=thermal,
    )


def make_thermal_spec(
    min_temp: float = 45.0,
    max_temp: float = 65.0,
    initial_temp: float = 50.0,
    target_temp: Optional[float] = 58.0,
    power_kw: float = 3.0,
    a: float = 0.98,
    b: float = 1.5,
    c: float = 0.2,
) -> ThermalSpec:
    """Helper to construct valid ThermalSpec objects for tests."""
    return ThermalSpec(
        a=a,
        b=b,
        c=c,
        max_power_kw=power_kw,
        temperature_initial_c=initial_temp,
        temperature_min_c=min_temp,
        temperature_max_c=max_temp,
        temperature_target_c=target_temp,
        resolution_minutes=15,
    )
