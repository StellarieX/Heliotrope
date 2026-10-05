"""External carbon-intensity adapter (Electricity Maps).

Live adapter with honest errors: unconfigured (no key) vs. unavailable
(upstream HTTP/parse failure). Never returns synthetic data silently —
`carbon_service` converts these errors into a labeled 503, never a fake
low-carbon signal.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from ...domain.carbon import CarbonPoint, Quality, SignalType
from ...utils.time import generate_slots, to_utc

HISTORY_URL = "https://api.electricitymap.org/v3/carbon-intensity/history"
REQUEST_TIMEOUT_S = 10.0


class ProviderNotConfigured(RuntimeError):
    """External provider selected but credentials are absent."""


class ProviderNotIntegrated(RuntimeError):
    """Credentials exist but no external API contract is verified yet."""


class ProviderNotAvailable(ProviderNotIntegrated):
    """Configured, but the upstream call failed (HTTP error or bad payload).

    Subclasses ProviderNotIntegrated so `carbon_service`'s existing
    `except (ProviderNotConfigured, ProviderNotIntegrated)` seam converts it
    into CarbonUnavailable (HTTP 503) without touching any other file.
    """


def _resolve_zone(explicit: str | None) -> str:
    if explicit:
        return explicit
    from ...core import config as _config

    return _config.ELECTRICITY_MAPS_ZONE or "US-CAL-CISO"


class ExternalProvider:
    name = "external"

    def __init__(self, api_key: str | None, zone: str | None = None):
        self._api_key = api_key
        self._explicit_zone = zone

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def zone(self) -> str:
        return _resolve_zone(self._explicit_zone)

    def get_signal(
        self, start: datetime, end: datetime, resolution_minutes: int
    ) -> list[CarbonPoint]:
        if not self.configured:
            raise ProviderNotConfigured(
                "external carbon provider selected but no API key is configured"
            )
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        if end <= start:
            raise ValueError("start must be < end")
        if resolution_minutes not in (5, 15, 30, 60):
            raise ValueError("resolution_minutes must be one of 5, 15, 30, 60")

        try:
            response = httpx.get(
                HISTORY_URL,
                params={"zone": self.zone},
                headers={"auth-token": self._api_key or ""},
                timeout=REQUEST_TIMEOUT_S,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderNotAvailable(
                f"electricity-maps request failed for zone {self.zone!r}: {exc}"
            ) from exc

        try:
            records = payload["history"] if isinstance(payload, dict) else None
            if not isinstance(records, list) or not records:
                raise ValueError("missing 'history' array")
            history = self.normalize(records, zone=self.zone)
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderNotAvailable(
                f"electricity-maps response unparseable for zone {self.zone!r}: {exc}"
            ) from exc

        try:
            slots = generate_slots(start, end, resolution_minutes)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

        by_time = sorted(history, key=lambda p: p.time)
        aligned: list[CarbonPoint] = []
        idx = 0
        latest: CarbonPoint | None = None
        for slot in slots:
            slot_utc = to_utc(slot)
            while idx < len(by_time) and to_utc(by_time[idx].time) <= slot_utc:
                latest = by_time[idx]
                idx += 1
            if latest is None:
                raise ProviderNotAvailable(
                    f"electricity-maps history starts after requested start for zone {self.zone!r}; "
                    "refusing to invent earlier points"
                )
            aligned.append(
                CarbonPoint(
                    time=slot,
                    gco2_per_kwh=latest.gco2_per_kwh,
                    signal_type=latest.signal_type,
                    is_proxy=latest.is_proxy,
                    is_forecast=latest.is_forecast,
                    quality=latest.quality,
                    source=latest.source,
                )
            )
        return aligned

    @staticmethod
    def normalize(records: list[dict], zone: str = "") -> list[CarbonPoint]:
        """Electricity Maps history records -> canonical measured points."""
        source_zone = zone or _resolve_zone(None)
        points: list[CarbonPoint] = []
        for rec in records:
            try:
                ts_raw = rec["datetime"]
                value = rec["carbonIntensity"]
            except (KeyError, TypeError) as exc:
                raise ValueError(f"record missing datetime/carbonIntensity: {exc}") from exc
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"bad datetime {ts_raw!r}: {exc}") from exc
            if ts.tzinfo is None:
                raise ValueError(f"naive datetime {ts_raw!r}")
            try:
                intensity = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"bad carbonIntensity {value!r}: {exc}") from exc
            if intensity < 0:
                raise ValueError(f"negative carbonIntensity {value!r}")
            points.append(
                CarbonPoint(
                    time=ts,
                    gco2_per_kwh=intensity,
                    signal_type=SignalType.AVERAGE,
                    is_proxy=False,
                    is_forecast=False,
                    quality=Quality.MEASURED,
                    source=f"electricity-maps:{source_zone}",
                )
            )
        if not points:
            raise ValueError("no records to normalize")
        return points
