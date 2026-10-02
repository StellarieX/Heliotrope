"""Synthetic duck-curve provider (default for dev/demo).

Models the concept Heliotrope is built on: solar surplus midday depresses
carbon intensity; the evening ramp peaks it. Values are illustrative —
labeled SYNTHETIC everywhere, never a real region's grid.

Deterministic given (seed + config). Smooth by construction: the curve is a
sum of Gaussians plus low-frequency seeded sinusoids, so slot-to-slot jumps
stay small and physical-looking.
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime

from ...domain.carbon import CarbonPoint, Quality, SignalType
from ...utils.time import generate_slots


@dataclass(frozen=True)
class SyntheticConfig:
    baseline: float = 340.0
    solar_dip: float = 190.0
    evening_peak: float = 220.0
    morning_bump: float = 70.0
    noise: float = 12.0
    seed: int = 7
    solar_center_hour: float = 13.0
    evening_center_hour: float = 19.2


def _gauss(h: float, center: float, width: float) -> float:
    return math.exp(-((h - center) ** 2) / width)


class SyntheticDuckCurveProvider:
    name = "synthetic"
    source = "synthetic_duck_curve"

    def __init__(self, config: SyntheticConfig | None = None):
        self.config = config or SyntheticConfig()

    def _phases(self) -> tuple[float, float]:
        rng = random.Random(self.config.seed)
        return rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)

    def intensity_at(self, hour: float) -> float:
        c = self.config
        p1, p2 = self._phases()
        v = (
            c.baseline
            + c.evening_peak * _gauss(hour, c.evening_center_hour, 5.5)
            + c.morning_bump * _gauss(hour, 8.0, 6.0)
            - c.solar_dip * _gauss(hour, c.solar_center_hour, 7.5)
            + c.noise * math.sin(hour * 1.7 + p1) * 0.5
            + c.noise * 0.5 * math.sin(hour * 0.6 + p2)
        )
        return max(0.0, v)

    def get_signal(self, start: datetime, end: datetime, resolution_minutes: int) -> list[CarbonPoint]:
        slots = generate_slots(start, end, resolution_minutes)
        return [
            CarbonPoint(
                time=s,
                gco2_per_kwh=self.intensity_at(s.hour + s.minute / 60),
                signal_type=SignalType.SYNTHETIC,
                quality=Quality.SYNTHETIC,
                source=self.source,
            )
            for s in slots
        ]
