"""Job/schedule/carbon model validation: valid passes, invalid rejected."""

import pytest
from pydantic import ValidationError

from app.core import validation
from app.domain.carbon import CarbonPoint, CarbonQuery
from app.domain.jobs import Job, JobType
from app.domain.schedules import Metrics, PlacedJob, ScheduleResponse

from .conftest import job_payload


def test_valid_job(job):
    assert job.window_minutes() == pytest.approx(24 * 60)
    assert validation.validate_job(job) == []


def test_naive_datetimes_rejected():
    with pytest.raises(ValidationError):
        Job(**job_payload(release_time="2026-10-05T00:00:00", deadline="2026-10-06T00:00:00"))


def test_release_after_deadline_rejected():
    with pytest.raises(ValidationError):
        Job(
            **job_payload(
                release_time="2026-10-06T00:00:00+00:00",
                deadline="2026-10-05T00:00:00+00:00",
            )
        )


@pytest.mark.parametrize(
    "field,value",
    [("power_kw", -1), ("duration_minutes", 0), ("energy_kwh", -0.5), ("flexibility_hours", -2)],
)
def test_nonsense_numbers_rejected(field, value):
    with pytest.raises(ValidationError):
        Job(**job_payload(**{field: value}))


def test_window_shorter_than_duration_rejected_by_validator(job):
    job.duration_minutes = int(job.window_minutes()) + 60
    with pytest.raises(ValueError, match="infeasible"):
        validation.validate_job(job)


def test_thermal_requires_comfort_band():
    payload = job_payload(type="THERMAL", temperature_min_c=40.0)
    with pytest.raises(ValidationError, match="temperature_max_c"):
        Job(**payload)


def test_thermal_band_coherent():
    payload = job_payload(
        type="THERMAL",
        temperature_min_c=60.0,
        temperature_max_c=40.0,
    )
    with pytest.raises(ValidationError, match="temperature_min_c"):
        Job(**payload)


def test_thermal_valid_boundary():
    job = Job(
        **job_payload(
            type="THERMAL",
            temperature_initial_c=30.0,
            temperature_min_c=40.0,
            temperature_max_c=60.0,
            temperature_target_c=55.0,
        )
    )
    assert job.type == JobType.THERMAL


def test_carbon_point_rejects_negative():
    with pytest.raises(ValidationError):
        CarbonPoint(time="2026-10-05T00:00:00+00:00", gco2_per_kwh=-5)


def test_carbon_query_rejects_unknown_provider():
    with pytest.raises(ValidationError):
        CarbonQuery(start="2026-10-05T00:00:00+00:00", end="2026-10-06T00:00:00+00:00", provider="magic")


def test_metrics_default_empty_not_fabricated():
    m = Metrics()
    assert m.total_co2_kg is None
    assert m.co2_saved_percent is None
    assert ScheduleResponse(solver="CPSAT").metrics.co2_saved_kg is None


def test_placed_job_roundtrip(job):
    p = PlacedJob(
        job_id=job.id,
        start_time=job.release_time,
        end_time=job.deadline,
        power_kw=job.power_kw,
        energy_kwh=job.energy_kwh,
        reason="test fixture",
    )
    assert p.status.value == "SCHEDULED"
