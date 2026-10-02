"""Scheduler/carbon contracts: instantiable, honest about being unimplemented."""

import pytest

from app.services.carbon_provider import (
    PROVIDERS,
    ProviderName,
    ProviderNotAvailable,
    get_provider,
)
from app.services.scheduler import (
    SCHEDULERS,
    SchedulerName,
    SchedulerNotAvailable,
    get_scheduler,
)


def test_all_schedulers_registered():
    assert set(SCHEDULERS) == {SchedulerName.ASAP, SchedulerName.GREEDY, SchedulerName.CPSAT}


def test_all_providers_registered():
    assert set(PROVIDERS) == {ProviderName.SYNTHETIC, ProviderName.CSV, ProviderName.EXTERNAL}


def test_scheduler_raises_honest_error(job, window):
    from app.domain.carbon import CarbonSignal

    start, end = window
    signal = CarbonSignal(start=start, end=end, resolution_minutes=15)
    with pytest.raises(SchedulerNotAvailable, match="not implemented"):
        get_scheduler(SchedulerName.CPSAT).schedule([job], signal, 10.0)


def test_provider_raises_honest_error(window):
    start, end = window
    with pytest.raises(ProviderNotAvailable, match="not implemented"):
        get_provider(ProviderName.SYNTHETIC).get_signal(start, end, 15)
