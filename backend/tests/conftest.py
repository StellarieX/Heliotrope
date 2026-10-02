"""Shared fixtures: deterministic, timezone-aware, synthetic-only."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.domain.jobs import Job, JobType
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def window() -> tuple[datetime, datetime]:
    start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    return start, start + timedelta(hours=24)


@pytest.fixture()
def job(window: tuple[datetime, datetime]) -> Job:
    start, end = window
    return Job(
        id="ev-1",
        name="EV charger",
        type=JobType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=7.2,
        release_time=start,
        deadline=end,
        duration_minutes=240,
        energy_kwh=14.0,
        flexibility_hours=4.0,
        interruptible=True,
    )


def job_payload(**overrides) -> dict:
    base = {
        "id": "ev-1",
        "name": "EV charger",
        "type": "DEFERRABLE_INTERRUPTIBLE",
        "power_kw": 7.2,
        "release_time": "2026-10-05T00:00:00+00:00",
        "deadline": "2026-10-06T00:00:00+00:00",
        "duration_minutes": 240,
        "energy_kwh": 14.0,
        "flexibility_hours": 4.0,
        "interruptible": True,
    }
    base.update(overrides)
    return base
