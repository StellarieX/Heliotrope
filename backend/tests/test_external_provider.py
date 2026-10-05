"""Live external carbon adapter: mocked-transport tests (no network)."""

from datetime import datetime, timezone

import httpx
import pytest

from app.domain.carbon import Quality, SignalType
from app.services.providers.external import (
    ExternalProvider,
    ProviderNotAvailable,
    ProviderNotConfigured,
)


def _window():
    start = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    return start, end


def _history_payload():
    return {
        "zone": "US-CAL-CISO",
        "history": [
            {"datetime": "2026-01-05T00:00:00Z", "carbonIntensity": 300.0},
            {"datetime": "2026-01-05T00:30:00Z", "carbonIntensity": 320.0},
        ],
    }


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_missing_key_raises_not_configured():
    start, end = _window()
    with pytest.raises(ProviderNotConfigured, match="no API key"):
        ExternalProvider(None).get_signal(start, end, 30)


def test_success_maps_measured_points(monkeypatch):
    start, end = _window()

    def fake_get(url, params=None, headers=None, timeout=None):
        assert "electricitymap" in url
        assert params["zone"] == "US-CAL-CISO"
        assert headers["auth-token"] == "key-123"
        assert timeout == 10.0
        return _FakeResponse(_history_payload())

    monkeypatch.setattr(httpx, "get", fake_get)
    points = ExternalProvider("key-123").get_signal(start, end, 30)
    assert len(points) == 2
    assert all(p.signal_type == SignalType.AVERAGE for p in points)
    assert all(p.quality == Quality.MEASURED for p in points)
    assert all(p.source == "electricity-maps:US-CAL-CISO" for p in points)
    assert points[0].gco2_per_kwh == pytest.approx(300.0)
    assert points[1].gco2_per_kwh == pytest.approx(320.0)


def test_param_validation(monkeypatch):
    start, end = _window()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(_history_payload()))
    provider = ExternalProvider("key-123")
    with pytest.raises(ValueError, match="resolution"):
        provider.get_signal(start, end, 7)
    with pytest.raises(ValueError, match="start must be"):
        provider.get_signal(end, start, 30)
    naive = datetime(2026, 1, 5, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        provider.get_signal(naive, end, 30)


def test_http_error_raises_not_available(monkeypatch):
    start, end = _window()

    def boom(*a, **k):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "get", boom)
    with pytest.raises(ProviderNotAvailable, match="request failed"):
        ExternalProvider("key-123").get_signal(start, end, 30)


def test_bad_payload_raises_not_available(monkeypatch):
    start, end = _window()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse({"nope": []}))
    with pytest.raises(ProviderNotAvailable, match="unparseable"):
        ExternalProvider("key-123").get_signal(start, end, 30)


def test_normalize_rejects_bad_records():
    with pytest.raises(ValueError):
        ExternalProvider.normalize([{"datetime": "not-a-date", "carbonIntensity": 1}])
    with pytest.raises(ValueError):
        ExternalProvider.normalize([{"datetime": "2026-01-05T00:00:00Z", "carbonIntensity": -5}])
