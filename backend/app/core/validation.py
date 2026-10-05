"""Reusable hard-constraint validation.

INVARIANT: forecast values are never hard constraints. These checks run on
user-declared values only; forecast data may influence cost later but can
never decide feasibility.
"""

from ..domain.jobs import Job

import math


def check_non_negative(value: float, field: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    if value < 0:
        raise ValueError(f"{field} must be >= 0")


def check_positive(value: float, field: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    if value <= 0:
        raise ValueError(f"{field} must be > 0")


def validate_job(job: Job) -> list[str]:
    """Return warnings for a structurally valid job. Raises on violation."""
    warnings: list[str] = []
    check_non_negative(job.power_kw, "power_kw")
    check_positive(job.duration_minutes, "duration_minutes")
    check_non_negative(job.energy_kwh, "energy_kwh")
    check_non_negative(job.flexibility_hours, "flexibility_hours")
    if job.window_minutes() < job.duration_minutes:
        raise ValueError(f"job {job.id}: window is shorter than duration (infeasible)")
    return warnings


def validate_capacity(capacity_kw: float) -> None:
    check_non_negative(capacity_kw, "capacity_kw")
    if capacity_kw == 0:
        raise ValueError("capacity_kw must be > 0 to place any load")
