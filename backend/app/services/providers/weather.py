"""Weather-derived carbon-intensity PROXY (keyless, live).

Why this exists. Grid carbon intensity depends mostly on how much solar and wind
is on the system. Metered national data needs a paid or registered API, and the
Electricity Maps adapter only exposes the past day, which gives a planner nothing
to optimise over. Open-Meteo publishes *real* hourly solar radiation and wind
speed (observed for the past days, forecast for the next week) with no key, so
this provider turns that live weather into an estimated intensity curve:

    solar_cf  = clamp(shortwave_radiation / rated_wm2, 0, 1)
    wind_cf   = clamp((wind_100m - cut_in) / (rated - cut_in), 0, 1)
    intensity = base * (1 - solar_share*solar_cf - wind_share*wind_cf)
                     * (1 + evening_uplift * gauss(local_hour, 20h))

It is an ESTIMATE, not a measurement, and is labelled that way everywhere:
`signal_type=PROXY`, `quality=ESTIMATED`, `source` names the model. The shares and
base intensity are configuration (see core/config.py), not facts about a specific
grid; calibrate them or set CARBON_PROVIDER=csv/external for metered data.

Failures are honest: if the weather service is unreachable the provider raises,
`carbon_service` answers 503, and nothing is invented in its place.
"""

from __future__ import annotations

import bisect
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ...domain.carbon import CarbonPoint, Quality, SignalType
from ...utils.time import generate_slots, to_utc
from .external import ProviderNotAvailable

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_S = 10.0
#: One retry on transport-level failures only (same policy as the external adapter).
MAX_ATTEMPTS = 2
#: Weather changes slowly and the upstream is rate limited: share one fetch.
CACHE_TTL_S = 900

SOURCE = "open-meteo-weather-proxy"


@dataclass(frozen=True)
class WeatherProxyConfig:
    latitude: float = 23.2599
    longitude: float = 77.4126
    #: Local time offset from UTC, used only for the evening-demand term.
    utc_offset_hours: float = 5.5
    #: Intensity with no solar or wind on the system (gCO2/kWh).
    base_gco2: float = 700.0
    #: Largest fraction of the base that full sun / full wind can displace.
    solar_max_share: float = 0.20
    wind_max_share: float = 0.10
    #: Extra demand-driven intensity at the evening peak, as a fraction of base.
    evening_uplift: float = 0.12
    solar_rated_wm2: float = 900.0
    wind_cut_in_ms: float = 3.0
    wind_rated_ms: float = 12.0
    past_days: int = 31
    forecast_days: int = 7


# (latitude, longitude, past_days, forecast_days) -> (expires_at, centres, solar, wind)
_cache: dict[tuple, tuple[float, list[datetime], list[float], list[float]]] = {}
_cache_lock = threading.Lock()


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _fetch(cfg: WeatherProxyConfig) -> tuple[list[datetime], list[float], list[float]]:
    key = (cfg.latitude, cfg.longitude, cfg.past_days, cfg.forecast_days)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1], hit[2], hit[3]

    params = {
        "latitude": cfg.latitude,
        "longitude": cfg.longitude,
        "hourly": "shortwave_radiation,wind_speed_100m",
        "wind_speed_unit": "ms",
        "past_days": cfg.past_days,
        "forecast_days": cfg.forecast_days,
        "timezone": "UTC",
    }
    payload = None
    last_exc: Exception | None = None
    for _ in range(max(1, MAX_ATTEMPTS)):
        try:
            resp = httpx.get(FORECAST_URL, params=params, timeout=REQUEST_TIMEOUT_S)
            resp.raise_for_status()
            payload = resp.json()
            last_exc = None
            break
        except httpx.TransportError as exc:
            last_exc = exc
            continue
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderNotAvailable(f"weather service request failed: {exc}") from exc
    if last_exc is not None or payload is None:
        raise ProviderNotAvailable(f"weather service unreachable: {last_exc}") from last_exc

    try:
        hourly = payload["hourly"]
        times = hourly["time"]
        sw = hourly["shortwave_radiation"]
        wind = hourly["wind_speed_100m"]
        if not (len(times) == len(sw) == len(wind)) or len(times) < 2:
            raise ValueError("hourly arrays missing or of unequal length")
        centres: list[datetime] = []
        solar: list[float] = []
        winds: list[float] = []
        for t, s, w in zip(times, sw, wind):
            if s is None or w is None:
                continue  # a gap is skipped, never filled with a guess
            # Open-Meteo labels each hourly value at the END of the hour it averages,
            # so the value represents the hour's midpoint.
            centres.append(datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - timedelta(minutes=30))
            solar.append(float(s))
            winds.append(float(w))
        if len(centres) < 2:
            raise ValueError("fewer than two usable hourly samples")
    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderNotAvailable(f"weather response unparseable: {exc}") from exc

    with _cache_lock:
        _cache[key] = (now + CACHE_TTL_S, centres, solar, winds)
    return centres, solar, winds


class WeatherProxyProvider:
    name = "weather"
    source = SOURCE

    def __init__(self, config: WeatherProxyConfig | None = None):
        self.config = config or WeatherProxyConfig()

    def intensity(self, solar_wm2: float, wind_ms: float, local_hour: float) -> float:
        c = self.config
        solar_cf = _clamp(solar_wm2 / c.solar_rated_wm2, 0.0, 1.0)
        span = max(c.wind_rated_ms - c.wind_cut_in_ms, 1e-9)
        wind_cf = _clamp((wind_ms - c.wind_cut_in_ms) / span, 0.0, 1.0)
        renewable = _clamp(c.solar_max_share * solar_cf + c.wind_max_share * wind_cf, 0.0, 0.95)
        # Circular distance so the 20:00 peak wraps cleanly across midnight.
        d = abs(local_hour - 20.0)
        d = min(d, 24.0 - d)
        demand = 1.0 + c.evening_uplift * math.exp(-(d**2) / 8.0)
        return max(0.0, c.base_gco2 * (1.0 - renewable) * demand)

    def get_signal(self, start: datetime, end: datetime, resolution_minutes: int) -> list[CarbonPoint]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        slots = generate_slots(start, end, resolution_minutes)
        centres, solar, wind = _fetch(self.config)
        first, last = centres[0], centres[-1]
        now = datetime.now(timezone.utc)
        step = timedelta(minutes=resolution_minutes)

        points: list[CarbonPoint] = []
        for slot in slots:
            # A slot is represented by its midpoint, like the hourly samples are.
            t = to_utc(slot) + step / 2
            if t < first or t > last:
                raise ProviderNotAvailable(
                    f"requested time {slot.isoformat()} is outside the weather window "
                    f"({first.isoformat()} to {last.isoformat()}); refusing to extrapolate"
                )
            i = bisect.bisect_right(centres, t)
            i = min(max(i, 1), len(centres) - 1)
            t0, t1 = centres[i - 1], centres[i]
            span = (t1 - t0).total_seconds() or 1.0
            f = _clamp((t - t0).total_seconds() / span, 0.0, 1.0)
            s = solar[i - 1] + f * (solar[i] - solar[i - 1])
            w = wind[i - 1] + f * (wind[i] - wind[i - 1])
            local_hour = ((t.hour + t.minute / 60.0) + self.config.utc_offset_hours) % 24.0
            points.append(
                CarbonPoint(
                    time=slot,
                    gco2_per_kwh=self.intensity(s, w, local_hour),
                    signal_type=SignalType.PROXY,
                    is_proxy=True,
                    is_forecast=to_utc(slot) > now,
                    quality=Quality.ESTIMATED,
                    source=SOURCE,
                )
            )
        return points
