"""CSV carbon provider.

Expected format (header required):

    timestamp,carbon_intensity_gco2_per_kwh
    2026-01-01T00:00:00Z,510
    2026-01-01T00:15:00Z,505

Rules: ISO-8601 timestamps (naive assumed UTC), non-negative values,
no duplicates. Missing expected slots are an error in STRICT mode, or
linearly interpolated in FILL mode up to max_gap_minutes — larger gaps
are reported, never silently smoothed over.
"""

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...domain.carbon import CarbonPoint, Quality, SignalType
from ...utils.time import (
    find_missing_slots,
    generate_slots,
    group_gaps,
    linear_interpolate,
    to_utc,
)


class ProviderDataInvalid(ValueError):
    """CSV content or coverage is unusable; message explains exactly why."""


@dataclass(frozen=True)
class CSVConfig:
    path: str
    missing_data: str = "strict"  # "strict" | "fill"
    max_gap_minutes: int = 60


class CSVProvider:
    name = "csv"

    def __init__(self, config: CSVConfig):
        if config.missing_data not in ("strict", "fill"):
            raise ValueError("missing_data must be 'strict' or 'fill'")
        self.config = config
        self.source = f"csv:{Path(config.path).name}"

    def _load(self) -> dict[datetime, float]:
        try:
            text = Path(self.config.path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderDataInvalid(f"cannot read CSV at {self.config.path}: {exc}") from exc
        rows = list(csv.DictReader(text.splitlines()))
        if not rows or "timestamp" not in (rows[0] or {}) or "carbon_intensity_gco2_per_kwh" not in (rows[0] or {}):
            raise ProviderDataInvalid("CSV must have header: timestamp,carbon_intensity_gco2_per_kwh")
        values: dict[datetime, float] = {}
        for i, row in enumerate(rows, start=2):
            raw_ts = (row.get("timestamp") or "").strip()
            raw_v = (row.get("carbon_intensity_gco2_per_kwh") or "").strip()
            try:
                ts = to_utc(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")))
            except ValueError:
                raise ProviderDataInvalid(f"row {i}: invalid timestamp {raw_ts!r}") from None
            try:
                v = float(raw_v)
            except ValueError:
                raise ProviderDataInvalid(f"row {i}: invalid number {raw_v!r}") from None
            if v < 0:
                raise ProviderDataInvalid(f"row {i}: negative intensity {v}")
            if ts in values:
                raise ProviderDataInvalid(f"row {i}: duplicate timestamp {raw_ts!r}")
            values[ts] = v
        if not values:
            raise ProviderDataInvalid("CSV contains no data rows")
        return values

    def get_signal(
        self, start: datetime, end: datetime, resolution_minutes: int
    ) -> tuple[list[CarbonPoint], int, int]:
        """Returns (points, missing_count, interpolated_count)."""
        data = self._load()
        slots = generate_slots(start, end, resolution_minutes)
        missing = find_missing_slots(slots, set(data))
        interpolated = 0
        if missing:
            if self.config.missing_data == "strict":
                raise ProviderDataInvalid(f"{len(missing)} expected slot(s) missing, first: {missing[0].isoformat()}")
            for gap in group_gaps(missing, resolution_minutes):
                gap_minutes = len(gap) * resolution_minutes
                if gap_minutes > self.config.max_gap_minutes:
                    raise ProviderDataInvalid(
                        f"gap of {gap_minutes} min from {gap[0].isoformat()} exceeds max {self.config.max_gap_minutes} min — refusing to fabricate"
                    )
                known = sorted(data)
                before = max((t for t in known if t < gap[0]), default=None)
                after = min((t for t in known if t > gap[-1]), default=None)
                if before is None or after is None:
                    raise ProviderDataInvalid(f"cannot interpolate edge gap at {gap[0].isoformat()}")
                for t, v in zip(gap, linear_interpolate((before, data[before]), (after, data[after]), gap)):
                    data[t] = v
                    interpolated += 1
        points = [
            CarbonPoint(
                time=s,
                gco2_per_kwh=data[s],
                signal_type=SignalType.AVERAGE,
                quality=Quality.SYNTHETIC,
                source=self.source,
            )
            for s in slots
        ]
        # Points actually interpolated carry INTERPOLATED; the rest MEASURED.
        filled: set = set()
        if missing and self.config.missing_data == "fill":
            for g in group_gaps(missing, resolution_minutes):
                filled.update(g)
        for p in points:
            p.quality = Quality.INTERPOLATED if p.time in filled else Quality.MEASURED
        return points, len(missing), interpolated
