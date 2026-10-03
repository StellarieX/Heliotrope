"""CarbonAccountingService — the one place emissions are computed (§25, §26, §27, §28).

Every scheduler, the comparison service and the validator all route their
numbers through here. If ASAP and CP-SAT each had their own emission loop they
would drift by rounding, and every "CO2 saved" figure in the product would become
untrustworthy.

THE SCHEDULE IS THE SOURCE OF TRUTH (§26). Energy comes from the placement that
was actually produced — `power_w x slot_minutes`, summed — not from
`energy_required_kwh`. A job that needed 18 kWh and was scheduled using 18 kWh
reports 18; a schedule that quietly under-delivers reports what it actually did,
which is the whole point of an independent accounting layer.

    energy_kwh    = sum over slots of  power_w * slot_minutes / WMIN_PER_KWH
    total_co2_kg  = sum over slots of  power_w * slot_minutes * carbon[t] / 3.6e9

BASELINE IS ALWAYS INCLUDED. A fixed fridge emits whether or not anything was
scheduled around it. Savings are therefore measured against the full ASAP
baseline, not against a fictional zero-load world.

CARBON AND COST ARE SEPARATE (§27). `co2_cost` and `energy_cost` are computed
independently from their own signals. A cheap hour that happens to be dirty is
still charged for its carbon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..domain.scaling import CO2_KG_DIVISOR, WMIN_PER_KWH
from ..domain.scheduling import SchedulerInput


@dataclass
class Placement:
    """What a scheduler actually decided: watts per job per slot.

    Deliberately a plain mapping rather than a model object, so a placement can
    be diffed, serialized and handed to the validator without translation.

    It also maintains a running per-slot total (`committed_w`). Schedulers place
    jobs one at a time and MUST see what earlier jobs already claimed — without
    this, two jobs each independently observe the full connection headroom and
    both take it, which is how a 10 kW connection silently ends up drawing
    10.3 kW. The counter makes that impossible rather than merely unlikely.
    """

    slot_count: int = 0
    power_by_job: dict[str, dict[int, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._committed: list[int] = [0] * self.slot_count if self.slot_count else []

    def set(self, job_id: str, slot: int, power_w: int) -> None:
        slots = self.power_by_job.setdefault(job_id, {})
        previous = slots.get(slot, 0)
        slots[slot] = power_w
        if self._committed and 0 <= slot < len(self._committed):
            self._committed[slot] += power_w - previous

    def set_range(self, job_id: str, start: int, end: int, power_w: int) -> None:
        """Fill [start, end) with a constant power. Atomic jobs use this."""
        for slot in range(start, end):
            self.set(job_id, slot, power_w)

    def clear_slot(self, job_id: str, slot: int) -> None:
        self.set(job_id, slot, 0)

    def committed_w(self, slot: int) -> int:
        """Total flexible power already claimed at `slot`, in watts."""
        if not self._committed:
            return sum(
                power_w
                for slots in self.power_by_job.values()
                for s, power_w in slots.items()
                if s == slot and power_w > 0
            )
        return self._committed[slot] if 0 <= slot < len(self._committed) else 0

    def job_slots(self, job_id: str) -> dict[int, int]:
        return self.power_by_job.get(job_id, {})

    def job_power(self, job_id: str) -> int:
        return sum(self.power_by_job.get(job_id, {}).values())

    def occupied_slots(self, job_id: str) -> list[int]:
        return sorted(s for s, p in self.power_by_job.get(job_id, {}).items() if p > 0)

    def is_empty(self) -> bool:
        return not any(power_w > 0 for slots in self.power_by_job.values() for power_w in slots.values())


@dataclass
class AccountingResult:
    """Everything measured about one placement."""

    total_energy_kwh: float
    flexible_energy_kwh: float
    baseline_energy_kwh: float
    total_co2_kg: float
    flexible_co2_kg: float
    baseline_co2_kg: float
    peak_w: int
    flexible_peak_w: int
    baseline_peak_w: int
    energy_cost: Optional[float]
    co2_cost: Optional[float]
    slot_load_w: list[int]
    slot_flexible_w: list[int]
    slot_baseline_w: list[int]
    slot_co2_kg: list[float]


class CarbonAccountingService:
    """Single canonical accounting implementation."""

    def account(
        self, scheduler_input: SchedulerInput, placement: Placement
    ) -> AccountingResult:
        horizon = scheduler_input.horizon
        slot_minutes = horizon.slot_minutes
        n = horizon.slot_count

        slot_flexible_w = [0] * n
        for job_id, slots in placement.power_by_job.items():
            for slot, power_w in slots.items():
                if 0 <= slot < n and power_w > 0:
                    slot_flexible_w[slot] += power_w

        baseline = scheduler_input.baseline
        slot_baseline_w = [baseline.at(s) for s in range(n)]
        slot_load_w = [slot_flexible_w[s] + slot_baseline_w[s] for s in range(n)]

        slot_co2_kg: list[float] = []
        total_co2_kg = 0.0
        flexible_co2_kg = 0.0
        baseline_co2_kg = 0.0
        for s in range(n):
            carbon = scheduler_input.carbon.at(s)
            slot_kg = slot_load_w[s] * slot_minutes * carbon / CO2_KG_DIVISOR
            slot_co2_kg.append(slot_kg)
            total_co2_kg += slot_kg
            flexible_co2_kg += slot_flexible_w[s] * slot_minutes * carbon / CO2_KG_DIVISOR
            baseline_co2_kg += slot_baseline_w[s] * slot_minutes * carbon / CO2_KG_DIVISOR

        total_energy = sum(slot_load_w) * slot_minutes / WMIN_PER_KWH
        flexible_energy = sum(slot_flexible_w) * slot_minutes / WMIN_PER_KWH
        baseline_energy = sum(slot_baseline_w) * slot_minutes / WMIN_PER_KWH

        energy_cost = self._cost(scheduler_input, slot_flexible_w, slot_load_w)
        co2_cost = self._co2_cost(scheduler_input, slot_co2_kg)

        return AccountingResult(
            total_energy_kwh=total_energy,
            flexible_energy_kwh=flexible_energy,
            baseline_energy_kwh=baseline_energy,
            total_co2_kg=total_co2_kg,
            flexible_co2_kg=flexible_co2_kg,
            baseline_co2_kg=baseline_co2_kg,
            peak_w=max(slot_load_w) if slot_load_w else 0,
            flexible_peak_w=max(slot_flexible_w) if slot_flexible_w else 0,
            baseline_peak_w=max(slot_baseline_w) if slot_baseline_w else 0,
            energy_cost=energy_cost,
            co2_cost=co2_cost,
            slot_load_w=slot_load_w,
            slot_flexible_w=slot_flexible_w,
            slot_baseline_w=slot_baseline_w,
            slot_co2_kg=slot_co2_kg,
        )

    def _cost(
        self,
        scheduler_input: SchedulerInput,
        flexible_w: list[int],
        total_w: list[int],
    ) -> Optional[float]:
        """Cost is charged on TOTAL consumption, baseline included (§27)."""
        tariff = scheduler_input.tariff
        if tariff is None:
            return None
        prices = tariff.price_micro_per_kwh
        slot_minutes = scheduler_input.horizon.slot_minutes
        total = 0.0
        for s in range(len(total_w)):
            price_micro = prices[s] if s < len(prices) else 0
            kwh = total_w[s] * slot_minutes / WMIN_PER_KWH
            total += kwh * (price_micro / 1_000_000.0)
        return total

    def _co2_cost(
        self, scheduler_input: SchedulerInput, slot_co2_kg: list[float]
    ) -> Optional[float]:
        tariff = scheduler_input.tariff
        if tariff is None:
            return None
        total = 0.0
        for s in range(len(slot_co2_kg)):
            price_micro = tariff.price_micro_per_kwh[s] if s < len(tariff.price_micro_per_kwh) else 0
            total += slot_co2_kg[s] * (price_micro / 1_000_000.0)
        return total

    # --- per-job attribution (§31, §51, §52) -------------------------------

    def job_accounting(
        self, scheduler_input: SchedulerInput, job_id: str, placement: Placement
    ) -> dict:
        """Emissions attributable to one job, for the explanation objects."""
        horizon = scheduler_input.horizon
        slot_minutes = horizon.slot_minutes
        slots = placement.job_slots(job_id)
        energy_kwh = sum(slots.values()) * slot_minutes / WMIN_PER_KWH
        co2_kg = sum(
            power_w * slot_minutes * scheduler_input.carbon.at(slot) / CO2_KG_DIVISOR
            for slot, power_w in slots.items()
        )
        peak_w = max(slots.values()) if slots else 0
        return {
            "energy_kwh": energy_kwh,
            "co2_kg": co2_kg,
            "peak_w": peak_w,
            "slots": sorted(slots),
        }

    def savings_against(
        self, baseline: AccountingResult, candidate: AccountingResult
    ) -> tuple[Optional[float], Optional[float]]:
        """(kg saved, percent saved) against a reference accounting.

        §28: `(baseline - candidate) / baseline * 100`, with the zero-baseline
        case returning `None` rather than dividing by zero or claiming 100%
        savings out of nothing.
        """
        if baseline.total_co2_kg <= 0:
            return (None, None)
        saved = baseline.total_co2_kg - candidate.total_co2_kg
        percent = (saved / baseline.total_co2_kg) * 100.0
        return (saved, percent)


def empty_baseline(scheduler_input: SchedulerInput) -> AccountingResult:
    """Accounting for an empty flexible placement: the unavoidable emissions."""
    return CarbonAccountingService().account(scheduler_input, Placement())
