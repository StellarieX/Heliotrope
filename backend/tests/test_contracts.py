"""Registry contracts: the real engines are registered, named, and honest."""

import pytest

from app.services.carbon_provider import ProviderName, ProviderNotAvailable
from app.services.providers.external import ExternalProvider, ProviderNotConfigured
from app.services.scheduler_service import resolve_scheduler
from app.services.schedulers import SCHEDULERS, SchedulerName


def test_all_schedulers_registered():
    assert set(SCHEDULERS) == {SchedulerName.ASAP, SchedulerName.GREEDY, SchedulerName.CPSAT}


def test_each_engine_implements_schedule():
    for engine in SCHEDULERS.values():
        assert callable(getattr(engine, "schedule", None))


def test_resolve_scheduler_rejects_unknown():
    with pytest.raises(KeyError, match="unknown scheduler"):
        resolve_scheduler("quantum")


def test_resolve_scheduler_case_insensitive():
    assert resolve_scheduler("cpsat") is SchedulerName.CPSAT


def test_provider_names_known():
    assert {p.value for p in ProviderName} == {"synthetic", "csv", "external"}


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
