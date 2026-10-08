"""ScheduleValidator — independent verification of a produced schedule (§29, §30).

    "Do NOT trust the solver blindly."

This module deliberately does NOT reuse the CP-SAT model construction, and it
re-derives every hard constraint from plain float arithmetic rather than from the
integer scaling the solver used. That is on purpose: if the model and the check
share code, a scaling mistake cancels itself out and both agree on a wrong
answer. Independent verification is the only thing that catches that.

THE ZERO-VIOLATION INVARIANT (§30). A result may be reported FEASIBLE or OPTIMAL
only when `violations` is empty. If validation fails the caller must NOT return
the schedule as successful — it returns INTERNAL_ERROR and carries the
violation text, because a schedule that violates its own hard constraints is a
bug worth surfacing loudly rather than a result worth shipping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.loads import LoadType
from ..domain.scaling import (
    THERMAL_BAND_TOLERANCE_MILLI,
    WMIN_PER_KWH,
    temperature_c_from_milli,
    to_millikw,
)
from ..domain.scheduling import SchedulerInput
from .carbon_accounting import Placement


@dataclass
class ValidationResult:
    violations: list[str] = field(default_factory=list)
    deadline_misses: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations

    def add(self, message: str) -> None:
        self.violations.append(message)


class ScheduleValidator:
    """Re-checks a placement against the input's hard constraints."""

    #: set at the start of every `validate` call from the horizon, so per-job
    #: checks never hardcode the 15-minute slot length
    _slot_minutes: int = 15

    def validate(self, scheduler_input: SchedulerInput, placement: Placement) -> ValidationResult:
        result = ValidationResult()
        horizon = scheduler_input.horizon
        n = horizon.slot_count
        self._slot_minutes = horizon.slot_minutes

        self._check_all_jobs_placed(scheduler_input, placement, result)
        self._check_capacity(scheduler_input, placement, result)
        self._check_slot_bounds(scheduler_input, placement, n, result)

        for job in scheduler_input.jobs:
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                self._check_atomic(job, placement, result)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                self._check_interruptible(job, placement, result)
            elif job.job_type is LoadType.THERMAL:
                self._check_thermal(job, placement, result)

        result.deadline_misses = sum(1 for v in result.violations if v.startswith("deadline"))
        return result

    # --- structure ----------------------------------------------------------

    def _check_all_jobs_placed(
        self, scheduler_input: SchedulerInput, placement: Placement, result: ValidationResult
    ) -> None:
        for job in scheduler_input.jobs:
            if job.id not in placement.power_by_job:
                result.add(f"{job.id}: job is absent from the schedule entirely")

    def _check_slot_bounds(
        self,
        scheduler_input: SchedulerInput,
        placement: Placement,
        n: int,
        result: ValidationResult,
    ) -> None:
        for job_id, slots in placement.power_by_job.items():
            if job_id not in {j.id for j in scheduler_input.jobs}:
                result.add(f"{job_id}: schedule contains a job that was never requested")
            for slot in slots:
                if not 0 <= slot < n:
                    result.add(f"{job_id}: slot {slot} is outside the horizon of {n} slots")

    def _check_capacity(
        self, scheduler_input: SchedulerInput, placement: Placement, result: ValidationResult
    ) -> None:
        """§5 and §14: baseline + flexible <= capacity, at every slot."""
        n = scheduler_input.horizon.slot_count
        # Precompute once: recomputing this per slot inside a loop over jobs is
        # quadratic and shows up immediately at 100 jobs x 96 slots.
        flexible_by_slot = [0] * n
        for slots in placement.power_by_job.values():
            for slot, power_w in slots.items():
                if 0 <= slot < n and power_w > 0:
                    flexible_by_slot[slot] += power_w

        for slot in range(n):
            flexible = flexible_by_slot[slot]
            total = flexible + scheduler_input.baseline.at(slot)
            cap = scheduler_input.capacity_at(slot)
            if total > cap:
                result.add(
                    f"capacity: slot {slot} draws {total / 1000:.3f} kW "
                    f"({flexible / 1000:.3f} kW flexible + "
                    f"{scheduler_input.baseline.at(slot) / 1000:.3f} kW baseline) but the "
                    f"connection is limited to {cap / 1000:.3f} kW"
                )

    # --- per job type -------------------------------------------------------

    def _check_atomic(self, job, placement: Placement, result: ValidationResult) -> None:
        slots = placement.job_slots(job.id)
        active = sorted(s for s, p in slots.items() if p > 0)

        if not active:
            result.add(f"{job.id}: atomic job was never scheduled")
            return

        duration = job.duration_slots
        if duration is None:
            result.add(f"{job.id}: atomic job has no duration to validate against")
            return

        if len(active) != duration:
            result.add(
                f"{job.id}: atomic job occupies {len(active)} slots but must occupy exactly "
                f"{duration}. A partially executed atomic job is never valid."
            )

        contiguous = active[-1] - active[0] + 1 == len(active)
        if not contiguous:
            result.add(
                f"{job.id}: atomic job occupies non-contiguous slots {active}, which would "
                "mean pausing mid-cycle"
            )

        if job.shiftable:
            if active[0] < job.release_slot:
                result.add(
                    f"{job.id}: starts at slot {active[0]} but its release is slot "
                    f"{job.release_slot}"
                )
            if active[-1] >= job.deadline_slot:
                result.add(
                    f"deadline: {job.id} runs at slot {active[-1]} but must finish before "
                    f"slot {job.deadline_slot}"
                )
                result.deadline_misses += 1

        for slot, power_w in slots.items():
            if power_w > job.max_power_w:
                result.add(
                    f"{job.id}: slot {slot} draws {power_w / 1000:.3f} kW, above its "
                    f"{job.max_power_w / 1000:.3f} kW limit"
                )

    def _check_interruptible(self, job, placement: Placement, result: ValidationResult) -> None:
        slots = placement.job_slots(job.id)
        active = sorted(s for s, p in slots.items() if p > 0)

        for slot, power_w in slots.items():
            if power_w < 0:
                result.add(f"{job.id}: slot {slot} has negative power")
            if power_w > job.max_power_w:
                result.add(
                    f"{job.id}: slot {slot} draws {power_w / 1000:.3f} kW, above its "
                    f"{job.max_power_w / 1000:.3f} kW charging limit"
                )
            if slot < job.release_slot:
                result.add(
                    f"{job.id}: charges at slot {slot} but its release is slot "
                    f"{job.release_slot}"
                )
            if slot >= job.deadline_slot:
                result.add(
                    f"deadline: {job.id} charges at slot {slot} but must stop before slot "
                    f"{job.deadline_slot}"
                )
                result.deadline_misses += 1

        if job.energy_required_wmin:
            delivered = sum(slots.values()) * self._slot_minutes
            # Power and slot length are integers, so delivered energy is an
            # exact integer watt-minute count; no rounding slack is needed.
            if delivered < job.energy_required_wmin:
                result.add(
                    f"{job.id}: delivered {delivered / WMIN_PER_KWH:.3f} kWh but needs "
                    f"{job.energy_required_wmin / WMIN_PER_KWH:.3f} kWh before its deadline"
                )

        self._check_min_chunk(job, active, result)

    def _check_min_chunk(self, job, active: list[int], result: ValidationResult) -> None:
        """§12: every maximal run of charged slots must be at least the chunk.

        Checked on RUNS, not on the schedule as a whole. Ten one-slot bursts in
        an hour satisfies a total-energy constraint while violating the reason
        the minimum chunk exists at all: the battery or the charger needs to
        settle between bursts.
        """
        if job.min_chunk_slots <= 1 or not active:
            return
        runs: list[list[int]] = [[active[0]]]
        for slot in active[1:]:
            if slot == runs[-1][-1] + 1:
                runs[-1].append(slot)
            else:
                runs.append([slot])
        for run in runs:
            if len(run) < job.min_chunk_slots:
                result.add(
                    f"{job.id}: ran for {len(run)} slot(s) at slots {run[0]}-{run[-1]}, "
                    f"below its {job.min_chunk_slots}-slot minimum chunk"
                )

    def _check_thermal(self, job, placement: Placement, result: ValidationResult) -> None:
        """Re-simulate from the thermal SPEC coefficients, not the integer scale.

        The model works in milli-degC with rounded coefficients; this check walks
        the float recurrence from `domain.thermal`. If the two disagree beyond a
        rounding tolerance, the scaled model is wrong and that must surface here
        rather than in a user's comfort report.
        """
        if job.thermal is None:
            result.add(f"{job.id}: thermal job has no thermal specification to validate")
            return

        scale = job.thermal
        slots = placement.job_slots(job.id)
        temperature_milli = scale.initial_milli

        min_m, max_m = scale.min_milli, scale.max_milli
        tol = THERMAL_BAND_TOLERANCE_MILLI
        power_cap = min(scale.max_power_millikw, job.max_power_w)
        for slot, power_w in slots.items():
            if power_w < 0:
                result.add(f"{job.id}: slot {slot} has negative power")
            if power_w > power_cap:
                result.add(
                    f"{job.id}: slot {slot} draws {power_w / 1000:.3f} kW, above its "
                    f"{power_cap / 1000:.3f} kW limit"
                )
            if power_w > 0 and not (job.release_slot <= slot < job.deadline_slot):
                result.add(
                    f"{job.id}: draws power at slot {slot}, outside its window "
                    f"[{job.release_slot}, {job.deadline_slot})"
                )
        for slot in range(job.release_slot, job.deadline_slot):
            power_millikw = to_millikw(slots.get(slot, 0) / 1000.0)
            temperature_milli = scale.next_temperature_milli(temperature_milli, power_millikw)
            temperature_c = temperature_c_from_milli(temperature_milli)
            if temperature_milli < min_m - tol:
                result.add(
                    f"{job.id}: comfort floor breached at slot {slot} — "
                    f"{temperature_c:.2f} degC is below "
                    f"{temperature_c_from_milli(min_m):.2f} degC"
                )
            elif temperature_milli > max_m + tol:
                result.add(
                    f"{job.id}: comfort ceiling breached at slot {slot} — "
                    f"{temperature_c:.2f} degC is above "
                    f"{temperature_c_from_milli(max_m):.2f} degC"
                )

        if scale.target_milli is not None:
            reached = temperature_milli >= scale.target_milli
            if not reached:
                result.add(
                    f"{job.id}: ends at {temperature_c_from_milli(temperature_milli):.2f} "
                    f"degC, short of its {temperature_c_from_milli(scale.target_milli):.2f} "
                    "degC service target by the deadline"
                )
