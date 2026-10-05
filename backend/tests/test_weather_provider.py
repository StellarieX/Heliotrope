"""Weather-derived carbon proxy, Jev auto-selection and chunked forecast history.

No network: the weather upstream is replaced by a canned payload.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.domain.carbon import Quality, SignalType
from app.services.carbon_service import CarbonService, CarbonUnavailable
from app.services.forecast_service import ForecastService
from app.services.providers import weather
from app.services.providers.external import ProviderNotAvailable
from app.services.providers.weather import WeatherProxyConfig, WeatherProxyProvider


def _hourly(start: datetime, hours: int):
    """Clear-sky midday, calm night, steady 6 m/s wind: a plain daily shape."""
    times, sw, wind = [], [], []
    for h in range(hours):
        t = start + timedelta(hours=h)
        local = (t.hour + 5.5) % 24
        sun = max(0.0, 900.0 * (1 - abs(local - 12.5) / 6.0)) if 6 <= local <= 19 else 0.0
        times.append(t.strftime("%Y-%m-%dT%H:%M"))
        sw.append(sun)
        wind.append(6.0)
    return {"hourly": {"time": times, "shortwave_radiation": sw, "wind_speed_100m": wind}}


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


@pytest.fixture()
def fake_weather(monkeypatch):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    payload = _hourly(now - timedelta(days=31), 24 * 38)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _Resp(payload)

    weather._cache.clear()
    monkeypatch.setattr(weather.httpx, "get", fake_get)
    yield calls
    weather._cache.clear()


def test_more_sun_and_wind_mean_lower_intensity():
    p = WeatherProxyProvider(WeatherProxyConfig())
    assert p.intensity(900, 6, 12) < p.intensity(0, 6, 12)
    assert p.intensity(0, 12, 12) < p.intensity(0, 0, 12)


def test_evening_demand_raises_intensity():
    p = WeatherProxyProvider(WeatherProxyConfig())
    assert p.intensity(0, 5, 20) > p.intensity(0, 5, 3)


def test_intensity_never_negative_or_above_bound():
    p = WeatherProxyProvider(WeatherProxyConfig())
    for sw in (0, 450, 5000):
        for w in (0, 6, 80):
            for h in range(0, 24, 3):
                v = p.intensity(sw, w, h)
                assert 0 <= v <= 700 * 1.13


def test_signal_is_labelled_as_an_estimate(fake_weather):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    resp = CarbonService(provider_name="weather").get_signal(now, now + timedelta(hours=24), 15)
    assert resp.signal_type == SignalType.PROXY
    assert resp.source == weather.SOURCE
    assert resp.quality.complete and resp.quality.is_forecast
    assert len(resp.points) == 96


def test_midday_is_cleaner_than_evening(fake_weather):
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    pts = CarbonService(provider_name="weather").get_signal(now, now + timedelta(hours=24), 60).points
    by_local = {(p.timestamp.hour + 5.5) % 24: p.carbon_intensity_gco2_per_kwh for p in pts}
    midday = min(v for h, v in by_local.items() if 11 <= h <= 14)
    evening = max(v for h, v in by_local.items() if 19 <= h <= 21)
    assert midday < evening


def test_points_carry_estimated_quality_and_proxy_flag(fake_weather):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    pts = WeatherProxyProvider().get_signal(now, now + timedelta(hours=2), 30)
    assert all(p.quality == Quality.ESTIMATED and p.is_proxy for p in pts)


def test_upstream_is_fetched_once_then_cached(fake_weather):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    p = WeatherProxyProvider()
    p.get_signal(now, now + timedelta(hours=1), 15)
    p.get_signal(now + timedelta(hours=2), now + timedelta(hours=3), 15)
    assert fake_weather["n"] == 1


def test_outside_the_weather_window_is_refused_not_extrapolated(fake_weather):
    far = datetime.now(timezone.utc) + timedelta(days=30)
    with pytest.raises(ProviderNotAvailable):
        WeatherProxyProvider().get_signal(far, far + timedelta(hours=1), 15)


def test_upstream_failure_is_a_503_not_a_made_up_signal(monkeypatch):
    weather._cache.clear()

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(weather.httpx, "get", boom)
    now = datetime.now(timezone.utc)
    with pytest.raises(CarbonUnavailable):
        CarbonService(provider_name="weather").get_signal(now, now + timedelta(hours=1), 15)
    weather._cache.clear()


def test_gaps_in_weather_data_are_skipped_not_filled(monkeypatch):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    payload = _hourly(now - timedelta(days=2), 24 * 6)
    payload["hourly"]["shortwave_radiation"][50] = None
    weather._cache.clear()
    monkeypatch.setattr(weather.httpx, "get", lambda *a, **k: _Resp(payload))
    centres, solar, _ = weather._fetch(WeatherProxyConfig(past_days=2, forecast_days=4))
    assert len(centres) == 24 * 6 - 1 and None not in solar
    weather._cache.clear()


# ---- forecast history is read in capped windows, never silently synthetic -----

class _FakeService:
    max_range_days = 7

    def __init__(self):
        self.calls = []

    def get_signal(self, start, end, res):
        self.calls.append((start, end))
        from app.domain.carbon import CarbonPointOut, CarbonSignalResponse, SignalQuality
        from app.utils.time import generate_slots

        pts = [
            CarbonPointOut(timestamp=s, carbon_intensity_gco2_per_kwh=300.0)
            for s in generate_slots(start, end, res)
        ]
        return CarbonSignalResponse(
            start=start, end=end, resolution_minutes=res, signal_type=SignalType.PROXY,
            source="fake", points=pts,
            quality=SignalQuality(signal_type=SignalType.PROXY, source="fake"),
        )


def test_long_history_is_read_in_windows_without_duplicates():
    svc = _FakeService()
    end = datetime(2026, 10, 5, 12, 7, tzinfo=timezone.utc)  # deliberately unaligned
    hist = ForecastService(carbon_service=svc).history_from_provider(end, days=14, resolution_minutes=15)
    assert len(svc.calls) == 2
    times = [p.time for p in hist.points]
    assert len(times) == len(set(times)) and times == sorted(times)
    assert all(p.quality == Quality.ESTIMATED for p in hist.points)
