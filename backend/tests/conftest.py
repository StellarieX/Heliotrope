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


def load_spec_payload(**overrides) -> dict:
    """A Phase 3 `LoadSpec` -- the shape `POST /schedule` actually accepts.

    `job_payload` is the older Phase 1 `Job` shape, kept for the endpoints that
    still take it. The scheduling endpoints take `LoadSpec` (§4).
    """
    base = {
        "id": "ev-1",
        "normalized_name": "EV charger",
        "category": "EV charging",
        "job_type": "DEFERRABLE_INTERRUPTIBLE",
        "power_kw": 7.2,
        "max_power_kw": 7.2,
        "energy_required_kwh": 18.0,
        "release_at": "2026-10-05T18:00:00+00:00",
        "deadline_at": "2026-10-06T07:00:00+00:00",
    }
    base.update(overrides)
    return base
