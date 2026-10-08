"""Carbon service: selection, validation, normalization, quality, cache.

Pipeline: API route -> CarbonService -> provider -> canonical points.
Provider-specific logic never leaks into routes. Failures are structured,
never silent: unconfigured external never falls back to synthetic.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from ..core import config
from ..domain.carbon import (
    CARBON_PROVIDER_NAMES,
    CarbonSignalResponse,
    CarbonPointOut,
    SignalQuality,
)
from ..utils.time import generate_slots, to_utc, validate_range
from .providers.csv_provider import CSVConfig, CSVProvider, ProviderDataInvalid
from .providers.external import ExternalProvider, ProviderNotConfigured, ProviderNotIntegrated
from .providers.synthetic import SyntheticConfig, SyntheticDuckCurveProvider
from .providers.weather import WeatherProxyConfig, WeatherProxyProvider

log = logging.getLogger("heliotrope.carbon")


class CarbonUnavailable(RuntimeError):
    """Provider selected but cannot serve (config/data/integration)."""


class CarbonBadRequest(ValueError):
    """Caller sent an invalid query."""


@dataclass
class _CacheEntry:
    response: CarbonSignalResponse
    expires_at: float


_SHARED_CACHE: dict = {}
_SHARED_LOCK = threading.Lock()


@dataclass
class CarbonService:
    provider_name: str = ""
    csv_path: str = ""
    max_range_days: int = 7
    cache_ttl_s: int = 300
    # Shared across instances: routes build a fresh service per request, so a
    # per-instance cache would never hit and CARBON_CACHE_TTL_S would be inert.
    _cache: dict = field(default_factory=lambda: _SHARED_CACHE, repr=False)
    _lock: threading.Lock = field(default_factory=lambda: _SHARED_LOCK, repr=False, compare=False)

    #: Upper bound on cached responses. Expired entries are pruned on every
    #: access, so without this the dict only grows for distinct queries.
    MAX_CACHE_ENTRIES = 512

    def __post_init__(self) -> None:
        self.provider_name = (self.provider_name or config.CARBON_PROVIDER or "synthetic").lower()
        self.csv_path = self.csv_path or config.CARBON_CSV_PATH
        if self.provider_name not in CARBON_PROVIDER_NAMES:
            raise CarbonBadRequest(f"unknown provider {self.provider_name!r}")

    @classmethod
    def default(cls) -> "CarbonService":
        return cls(
            max_range_days=config.CARBON_MAX_RANGE_DAYS,
            cache_ttl_s=config.CARBON_CACHE_TTL_S,
        )

    def _provider(self):
        if self.provider_name == "synthetic":
            return SyntheticDuckCurveProvider(SyntheticConfig(seed=config.CARBON_SYNTHETIC_SEED))
        if self.provider_name == "csv":
            if not self.csv_path:
                raise CarbonUnavailable("csv provider selected but no CSV path configured")
            return CSVProvider(CSVConfig(path=self.csv_path))
        if self.provider_name == "weather":
            return WeatherProxyProvider(
                WeatherProxyConfig(
                    latitude=config.CARBON_LAT,
                    longitude=config.CARBON_LON,
                    utc_offset_hours=config.CARBON_UTC_OFFSET_HOURS,
                    base_gco2=config.CARBON_WEATHER_BASE_GCO2,
                    solar_max_share=config.CARBON_WEATHER_SOLAR_SHARE,
                    wind_max_share=config.CARBON_WEATHER_WIND_SHARE,
                )
            )
        return ExternalProvider(config.ELECTRICITY_MAPS_API_KEY)

    def get_signal(self, start: datetime, end: datetime, resolution_minutes: int) -> CarbonSignalResponse:
        t0 = time.perf_counter()
        try:
            validate_range(start, end, self.max_range_days)
        except ValueError as exc:
            raise CarbonBadRequest(str(exc)) from exc
        if resolution_minutes not in (5, 15, 30, 60):
            raise CarbonBadRequest("resolution_minutes must be one of 5, 15, 30, 60")

        key = (self.provider_name, to_utc(start).isoformat(), to_utc(end).isoformat(), resolution_minutes, self.csv_path)
        now = time.time()
        with self._lock:
            expired = [k for k, entry in self._cache.items() if entry.expires_at <= now]
            for k in expired:
                del self._cache[k]
            cached = self._cache.get(key)
            if cached and cached.expires_at > now:
                log.info("carbon cache hit provider=%s resolution=%s", self.provider_name, resolution_minutes)
                return cached.response

        provider = self._provider()
        try:
            result = provider.get_signal(start, end, resolution_minutes)
        except (ProviderNotConfigured, ProviderNotIntegrated) as exc:
            raise CarbonUnavailable(str(exc)) from exc
        except ProviderDataInvalid as exc:
            raise CarbonUnavailable(f"provider data invalid: {exc}") from exc

        if isinstance(result, tuple):
            points, missing, interpolated = result
        else:
            points, missing, interpolated = result, 0, 0

        expected = generate_slots(start, end, resolution_minutes)
        have = {p.time for p in points}
        if any(s not in have for s in expected):
            raise CarbonUnavailable("provider returned an incomplete series without reporting it")

        first = points[0]
        signal_type = first.signal_type
        if any(p.signal_type != signal_type for p in points):
            raise CarbonUnavailable("provider mixed signal types in one response")

        response = CarbonSignalResponse(
            start=to_utc(start),
            end=to_utc(end),
            resolution_minutes=resolution_minutes,
            signal_type=signal_type,
            source=first.source,
            points=[CarbonPointOut(timestamp=p.time, carbon_intensity_gco2_per_kwh=p.gco2_per_kwh) for p in points],
            quality=SignalQuality(
                complete=missing == 0,
                missing_points=missing,
                interpolated_points=interpolated,
                source=first.source,
                signal_type=signal_type,
                is_forecast=any(p.is_forecast for p in points),
            ),
        )
        with self._lock:
            self._cache[key] = _CacheEntry(response, time.time() + self.cache_ttl_s)
            while len(self._cache) > self.MAX_CACHE_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
        ms = (time.perf_counter() - t0) * 1000
        log.info(
            "carbon served provider=%s points=%d missing=%d interpolated=%d latency_ms=%.1f",
            self.provider_name, len(points), missing, interpolated, ms,
        )
        return response
