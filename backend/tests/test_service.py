"""Service: selection, validation, range guards, cache, no silent fallback."""

from datetime import timedelta

import pytest

from app.services.carbon_service import CarbonBadRequest, CarbonService, CarbonUnavailable


def test_default_is_synthetic(window):
    start, end = window
    res = CarbonService().get_signal(start, end, 15)
    assert res.signal_type.value == "SYNTHETIC"
    assert len(res.points) == 96
    assert res.quality.complete is True


def test_unknown_provider_rejected():
    with pytest.raises(CarbonBadRequest, match="unknown provider"):
        CarbonService(provider_name="magic")


def test_external_without_key_never_falls_back(window):
    start, end = window
    with pytest.raises(CarbonUnavailable, match="no API key"):
        CarbonService(provider_name="external").get_signal(start, end, 15)


def test_csv_without_path_is_unavailable(window):
    start, end = window
    with pytest.raises(CarbonUnavailable, match="no CSV path"):
        CarbonService(provider_name="csv").get_signal(start, end, 15)


def test_range_and_resolution_guards(window):
    start, end = window
    svc = CarbonService()
    with pytest.raises(CarbonBadRequest, match="start must be"):
        svc.get_signal(end, start, 15)
    with pytest.raises(CarbonBadRequest, match="resolution"):
        svc.get_signal(start, end, 7)
    with pytest.raises(CarbonBadRequest, match="exceeds maximum"):
        svc.get_signal(start, start + timedelta(days=30), 15)


def test_cache_returns_same_object(window, caplog):
    start, end = window
    svc = CarbonService(cache_ttl_s=60)
    first = svc.get_signal(start, end, 15)
    second = svc.get_signal(start, end, 15)
    assert first is second
