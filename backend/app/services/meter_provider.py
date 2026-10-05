"""Real-telemetry ingestion path (meters, not simulation).

The deterministic simulator (services/simulator.py) replays scripted draws and
labels everything ``SIMULATED``. This module is the other ingestion path: it
accepts validated readings from real metering hardware (MQTT/OCPP/Modbus
drivers push here; no driver is bundled yet) and aggregates them into the
existing event payload path — ``JOB_STARTED`` / ``JOB_COMPLETED`` with
``energy_delivered_kwh`` — so no new state machine is introduced. Provenance
is ``MEASURED`` end to end.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Optional, Protocol

from pydantic import BaseModel, Field, field_validator


class MeterReading(BaseModel):
    """One validated meter sample for a job on a schedule."""

    schedule_id: str
    job_id: str
    timestamp: datetime
    energy_kwh: Optional[float] = Field(default=None, ge=0)
    power_kw: Optional[float] = Field(default=None, ge=0)
    source: str = "MEASURED"

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        # Naive hardware clocks are read as UTC, explicitly, so a reading can
        # never silently land in the server's local zone.
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class MeterProvider(Protocol):
    """Driver contract: validate + store one reading, return it."""

    def push_reading(
        self,
        schedule_id: str,
        job_id: str,
        timestamp: datetime,
        energy_kwh: Optional[float] = None,
        power_kw: Optional[float] = None,
        source: str = "MEASURED",
    ) -> MeterReading:
        ...


class InMemoryMeterProvider:
    """Test/default provider: validates ranges, keeps readings in memory."""

    #: Upper bounds guarding against side-channel injection of absurd values.
    #: Real household/commercial jobs are kWh-scale; anything at/above this
    #: is a data error, not a reading.
    MAX_ENERGY_KWH = 1_000_000.0
    MAX_POWER_KW = 1_000_000.0

    def __init__(self) -> None:
        self._readings: dict[tuple[str, str], list[MeterReading]] = {}
        self._lock = threading.Lock()

    def push_reading(
        self,
        schedule_id: str,
        job_id: str,
        timestamp: datetime,
        energy_kwh: Optional[float] = None,
        power_kw: Optional[float] = None,
        source: str = "MEASURED",
    ) -> MeterReading:
        import math

        if energy_kwh is not None:
            if not math.isfinite(energy_kwh) or energy_kwh < 0:
                raise ValueError("energy_kwh must be >= 0")
            if energy_kwh > self.MAX_ENERGY_KWH:
                raise ValueError(f"energy_kwh {energy_kwh} exceeds plausible maximum")
        if power_kw is not None:
            if not math.isfinite(power_kw) or power_kw < 0:
                raise ValueError("power_kw must be >= 0")
            if power_kw > self.MAX_POWER_KW:
                raise ValueError(f"power_kw {power_kw} exceeds plausible maximum")
        if energy_kwh is None and power_kw is None:
            raise ValueError("at least one of energy_kwh or power_kw is required")
        reading = MeterReading(
            schedule_id=schedule_id,
            job_id=job_id,
            timestamp=timestamp,
            energy_kwh=energy_kwh,
            power_kw=power_kw,
            source=source or "MEASURED",
        )
        with self._lock:
            self._readings.setdefault((schedule_id, job_id), []).append(reading)
        return reading

    def readings_for(self, schedule_id: str, job_id: str) -> list[MeterReading]:
        with self._lock:
            return list(self._readings.get((schedule_id, job_id), []))
