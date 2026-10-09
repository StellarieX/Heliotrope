"""Objective terms: the peak weight must actually move the peak."""

from datetime import timedelta

import pytest

from app.domain.loads import LoadSpec, LoadType
from app.domain.scheduling import ObjectiveWeights, SchedulerConfig
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SchedulerName

from .fixtures import DAY_START
from .test_cpsat import constant_signal


def _oven(job_id: str) -> LoadSpec:
    return LoadSpec(
        id=job_id, normalized_name=job_id, category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, max_power_kw=2.0,
        duration_minutes=30, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(minutes=180),
    )


def _peak_kw(peak_weight: float) -> float:
    service = SchedulerService()
    # Slots 4-5 are slightly cleaner; stacking both ovens there is the carbon optimum.
    signal = constant_signal([300.0, 300.0, 300.0, 300.0, 290.0, 290.0, 300.0, 300.0, 300.0, 300.0, 300.0, 300.0])
    inp, _ = service.build_input(
        [_oven("a"), _oven("b")], signal, capacity_kw=10.0,
        objective=ObjectiveWeights(carbon=1.0, peak=peak_weight),
    )
    result = service.run(inp, SchedulerName.CPSAT, config=SchedulerConfig(time_limit_seconds=30.0), explain=False)
    assert result.status.value == "OPTIMAL"
    return result.metrics.peak_kw


def test_peak_weight_changes_the_peak():
    assert _peak_kw(0.0) == pytest.approx(4.0)  # stacked in the cleanest slots
    assert _peak_kw(1.0) == pytest.approx(2.0)  # spread out to flatten the peak
