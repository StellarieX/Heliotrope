"""Scheduler/carbon contracts: scheduler still unimplemented (honest 501 path);
synthetic carbon now serves real labeled data; csv/external fail honestly."""

import pytest

from app.services.carbon_provider import ProviderName, ProviderNotAvailable
from app.services.providers.external import ExternalProvider, ProviderNotConfigured
from app.services.scheduler import (
    SCHEDULERS,
    SchedulerName,
    SchedulerNotAvailable,
    get_scheduler,
)


def test_all_schedulers_registered():
    assert set(SCHEDULERS) == {SchedulerName.ASAP, SchedulerName.GREEDY, SchedulerName.CPSAT}


def test_provider_names_known():
    assert {p.value for p in ProviderName} == {"synthetic", "csv", "external"}


def test_scheduler_raises_honest_error(job, window):
    from app.domain.carbon import CarbonSignal

    start, end = window
    signal = CarbonSignal(start=start, end=end, resolution_minutes=15)
    with pytest.raises(SchedulerNotAvailable, match="not implemented"):
        get_scheduler(SchedulerName.CPSAT).schedule([job], signal, 10.0)


def test_synthetic_provider_serves_labeled_signal(window):
    from app.services.providers.synthetic import SyntheticDuckCurveProvider
    from app.domain.carbon import SignalType

    start, end = window
    points = SyntheticDuckCurveProvider().get_signal(start, end, 15)
    assert len(points) == 96
    assert all(p.signal_type == SignalType.SYNTHETIC for p in points)


def test_external_without_key_is_unconfigured(window):
    start, end = window
    with pytest.raises(ProviderNotConfigured, match="no API key"):
        ExternalProvider(None).get_signal(start, end, 15)


def test_legacy_unavailable_error_exists():
    assert issubclass(ProviderNotAvailable, RuntimeError)
