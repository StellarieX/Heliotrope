"""Shared scheduler machinery (§7, §47).

Every scheduler subclasses `BaseScheduler` and implements only `build_placement`.
The shared layer owns everything that must NOT be re-implemented per scheduler:

  * feasibility preflight        (§20) — checked before any solving
  * placement primitives         (§6, §7) — atomic / interruptible / thermal
  * independent validation       (§29, §30)
  * accounting and result build  (§25, §28)

`Scheduler` -> `ASAPScheduler` / `GreedyScheduler` / `CPSATScheduler`, all
implementing the same `schedule(scheduler_input) -> SchedulerResult` interface.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional

from ...domain.loads import LoadType
from ...domain.scaling import (
    CO2_KG_DIVISOR,
    THERMAL_BAND_TOLERANCE_MILLI,
    WMIN_PER_KWH,
    temperature_c_from_milli,
    to_millikw,
)
from ...domain.scheduling import (
    CarbonProvenance,
    JobExplanation,
    NormalizedJob,
    ReasonCode,
    ScheduledJob,
    ScheduleMetrics,
    SchedulerResult,
    ScheduleStatus,
    SchedulerConfig,
    SchedulerInput,
    SlotAllocation,
    SolverInfo,
    SolverStatus,
    TemperatureSample,
)
from ..carbon_accounting import CarbonAccountingService, Placement
from ..schedule_validator import ScheduleValidator


class SchedulerName(str, Enum):
    ASAP = "ASAP"
    GREEDY = "GREEDY"
    CPSAT = "CPSAT"


class PlacementFailure(RuntimeError):
    """A job could not be placed under the hard constraints.

    Carries the reason text so the caller can report WHY, rather than returning
    a partial schedule that pretends to be complete.

    `status` is the result-level status this failure maps to. It defaults to
    INFEASIBLE (no schedule satisfies the hard constraints) and stays that for
    every heuristic path. CP-SAT overrides it when the solver stopped WITHOUT
    proving infeasibility — a timeout with no solution is UNKNOWN ("ran out of
    time with nothing to show"), not a proof that nothing exists.
    """

    def __init__(
        self, job_id: str, reason: str, status: ScheduleStatus = ScheduleStatus.INFEASIBLE
    ) -> None:
        super().__init__(reason)
        self.job_id = job_id
        self.reason = reason
        self.status = status


#: Result-level failure status back onto solver vocabulary for the except path.
#: Every entry is a failure the solver layer reports honestly: INFEASIBLE was
#: proven, UNKNOWN ran out of time, INTERNAL_ERROR broke. Anything else is a
#: bug in the caller, surfaced as INTERNAL_ERROR rather than mislabelled.
_SOLVER_STATUS_FOR_RESULT = {
    ScheduleStatus.INFEASIBLE: SolverStatus.INFEASIBLE,
    ScheduleStatus.UNKNOWN: SolverStatus.UNKNOWN,
    ScheduleStatus.INTERNAL_ERROR: SolverStatus.INTERNAL_ERROR,
}


class BaseScheduler(ABC):
    """Template method shared by all three engines."""

    name: SchedulerName

    def __init__(self, config: Optional[SchedulerConfig] = None) -> None:
        self.config = config or SchedulerConfig()
        self.accounting = CarbonAccountingService()
        self.validator = ScheduleValidator()

    # --- the interface every scheduler implements ---------------------------

    @abstractmethod
    def build_placement(self, scheduler_input: SchedulerInput) -> Placement:
        """Decide watts per job per slot. May raise PlacementFailure."""

    def solver_info(self, status: SolverStatus, elapsed_ms: int) -> SolverInfo:
        """Greedy and ASAP are exact algorithms, not a solver, so they report
        what they actually are rather than borrowing solver vocabulary."""
        return SolverInfo(
            name=self.name.value,
            status=status,
            solve_time_ms=elapsed_ms,
            is_optimal=status is SolverStatus.OPTIMAL,
        )

    # --- the template ------------------------------------------------------

    def schedule(self, scheduler_input: SchedulerInput) -> SchedulerResult:
        started = time.perf_counter()

        preflight = self.preflight(scheduler_input)
        if not preflight.ok:
            elapsed = int((time.perf_counter() - started) * 1000)
            return self._result(
                scheduler_input,
                Placement(),
                ScheduleStatus.INFEASIBLE,
                self.solver_info(SolverStatus.INFEASIBLE, elapsed),
                reason=preflight.reason,
                violations=preflight.violations,
            )

        try:
            placement = self.build_placement(scheduler_input)
        except PlacementFailure as failure:
            elapsed = int((time.perf_counter() - started) * 1000)
            solver_status = _SOLVER_STATUS_FOR_RESULT.get(
                failure.status, SolverStatus.INTERNAL_ERROR
            )
            return self._result(
                scheduler_input,
                Placement(),
                failure.status,
                self.solver_info(solver_status, elapsed),
                reason=failure.reason,
                violations=[failure.reason],
            )

        # §29/§30: verify independently, then refuse to report success if the
        # schedule breaks a hard constraint.
        validation = self.validator.validate(scheduler_input, placement)
        elapsed = int((time.perf_counter() - started) * 1000)
        if not validation.ok:
            return self._result(
                scheduler_input,
                placement,
                ScheduleStatus.INTERNAL_ERROR,
                self.solver_info(SolverStatus.INTERNAL_ERROR, elapsed),
                reason=(
                    "the produced schedule violated its own hard constraints and was "
                    "withheld rather than returned as successful"
                ),
                violations=validation.violations,
            )

        return self._result(
            scheduler_input,
            placement,
            ScheduleStatus.FEASIBLE,
            self.solver_info(SolverStatus.FEASIBLE, elapsed),
            validation=validation,
        )

    # --- feasibility preflight (§20) ---------------------------------------

    def preflight(self, scheduler_input: SchedulerInput) -> "PreflightResult":
        """Validate horizon, capacity, carbon and every job BEFORE solving."""
        result = PreflightResult()
        horizon = scheduler_input.horizon

        if horizon.slot_count <= 0:
            result.fail("the horizon contains no slots")
            return result
        if len(scheduler_input.carbon) != horizon.slot_count:
            result.fail(
                f"carbon signal has {len(scheduler_input.carbon)} points but the horizon has "
                f"{horizon.slot_count} slots"
            )
        if len(scheduler_input.baseline) != horizon.slot_count:
            result.fail(
                f"baseline has {len(scheduler_input.baseline)} slots but the horizon has "
                f"{horizon.slot_count}"
            )
        if scheduler_input.capacity_w <= 0:
            result.fail("connection capacity must be greater than zero")

        for slot in range(horizon.slot_count):
            if scheduler_input.baseline.at(slot) > scheduler_input.capacity_at(slot):
                result.fail(
                    f"baseline load at slot {slot} exceeds the connection capacity, so the "
                    "site is over capacity before any flexible load is placed"
                )
                break

        for job in scheduler_input.jobs:
            problem = self._check_job_feasible(scheduler_input, job)
            if problem:
                result.fail(problem)

        return result

    def _check_job_feasible(
        self, scheduler_input: SchedulerInput, job: NormalizedJob
    ) -> Optional[str]:
        """Return a human-readable infeasibility reason, or None if plausible."""
        if job.window_slots() <= 0:
            return f"{job.name} has no usable slot between its release and deadline"
        if job.max_power_w <= 0:
            return f"{job.name} has no power rating, so it cannot be placed"
        if job.release_slot < 0 or job.deadline_slot > scheduler_input.horizon.slot_count:
            return f"{job.name} falls outside the scheduling horizon"

        if job.job_type is LoadType.DEFERRABLE_ATOMIC:
            duration = job.duration_slots or 0
            if duration <= 0:
                return f"{job.name} is atomic but has no duration"
            if duration > job.window_slots():
                return (
                    f"{job.name} needs {duration} slots to run but only "
                    f"{job.window_slots()} are available before its deadline"
                )
            window = max_usable_atomic_slots(scheduler_input, job)
            if window < duration:
                return (
                    f"{job.name} cannot fit: the largest contiguous run with room for "
                    f"{job.power_w / 1000:.2f} kW is {window} slots, but it needs {duration}"
                )
            return None

        if job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
            if not job.energy_required_wmin:
                return f"{job.name} is interruptible but has no energy target"
            deliverable = max_interruptible_wmin(
                scheduler_input, job, reserve={}
            )
            if deliverable < job.energy_required_wmin:
                return (
                    f"{job.name} needs {job.energy_required_wmin / WMIN_PER_KWH:.2f} kWh before "
                    f"its deadline but at most {deliverable / WMIN_PER_KWH:.2f} kWh can be "
                    f"delivered at {job.max_power_w / 1000:.2f} kW within the available "
                    "window and connection capacity"
                )
            return None

        if job.job_type is LoadType.THERMAL:
            if job.thermal is None:
                return f"{job.name} is thermal but has no thermal specification"
            return None

        return None

    # --- shared placement primitives ---------------------------------------

    @staticmethod
    def _available_w(
        scheduler_input: SchedulerInput, job: NormalizedJob, placement: Placement, slot: int
    ) -> int:
        """Power this job may still draw at `slot`.

        Three limits apply at once, and all three matter:
          * the job's own ceiling
          * the connection capacity MINUS baseline (§5)
          * what earlier jobs in this same run have already committed
        Forgetting the third is how a 10 kW connection ends up drawing 14 kW.
        """
        free = scheduler_input.headroom_w(slot) - placement.committed_w(slot)
        return max(0, min(job.max_power_w, free))

    def place_atomic_earliest(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, placement: Placement
    ) -> int:
        """Earliest contiguous window with room for the whole run. Returns the
        start slot, or raises PlacementFailure (§6)."""
        start = self.find_atomic_window(scheduler_input, job, placement)
        if start is None:
            raise PlacementFailure(
                job.id,
                f"{job.name} could not be placed: no contiguous window of "
                f"{job.duration_slots} slots fits under the connection capacity",
            )
        placement.set_range(job.id, start, start + (job.duration_slots or 0), job.power_w)
        return start

    def place_atomic_lowest_carbon(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, placement: Placement
    ) -> tuple[int, float]:
        """Cheapest contiguous window (§7 atomic). Ties break by earliest start,
        so the result is reproducible."""
        duration = job.duration_slots or 0
        best_start: Optional[int] = None
        best_cost = float("inf")
        # objective_carbon() rebuilds the adjusted profile on every call in
        # forecast modes, so it is fetched once per job, not once per window.
        objective = scheduler_input.objective_carbon()
        for start in self.iter_atomic_windows(scheduler_input, job, placement):
            cost = self.window_carbon_cost(scheduler_input, job, start, duration, objective)
            if cost < best_cost - 1e-9:
                best_cost = cost
                best_start = start
        if best_start is None:
            raise PlacementFailure(
                job.id,
                f"{job.name} could not be placed: no contiguous window of "
                f"{job.duration_slots} slots fits under the connection capacity",
            )
        placement.set_range(job.id, best_start, best_start + duration, job.power_w)
        return best_start, best_cost

    def iter_atomic_windows(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, placement: Placement
    ):
        """Every feasible contiguous start slot, in ascending order."""
        duration = job.duration_slots or 0
        if duration <= 0:
            return
        last_start = job.deadline_slot - duration
        for start in range(job.release_slot, last_start + 1):
            if self.window_fits(scheduler_input, job, start, duration, placement):
                yield start

    def window_fits(
        self,
        scheduler_input: SchedulerInput,
        job: NormalizedJob,
        start: int,
        duration: int,
        placement: Optional[Placement] = None,
    ) -> bool:
        return all(
            (self._available_w(scheduler_input, job, placement, slot) if placement
             else scheduler_input.headroom_w(slot)) >= job.power_w
            for slot in range(start, start + duration)
        )

    def find_atomic_window(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, placement: Placement
    ) -> Optional[int]:
        duration = job.duration_slots or 0
        last_start = job.deadline_slot - duration
        for start in range(job.release_slot, last_start + 1):
            if self.window_fits(scheduler_input, job, start, duration, placement):
                return start
        return None

    def window_carbon_cost(
        self,
        scheduler_input: SchedulerInput,
        job: NormalizedJob,
        start: int,
        duration: int,
        objective=None,
    ) -> float:
        """Carbon of running this job across a window, in kg.

        Sums integer carbon per slot times this job's power, so the greedy ranking
        and the accounting service cannot disagree about which window is cheaper.

        §21: the per-slot values come from `objective_carbon()`, so atomic
        placement under Greedy consumes forecast uncertainty the same way the
        thermal heuristic and CP-SAT's objective do. Reading `carbon` here
        instead would leave atomic jobs silently optimizing the observed signal
        while thermal jobs optimized the forecast — a real bug, because the
        mode would appear to work while the most common job type ignored it.

        The value returned here is an OBJECTIVE ranking figure, not a reported
        emission. `CarbonAccountingService` still measures what the schedule
        actually emitted, against the observed signal (§35).
        """
        slot_minutes = scheduler_input.horizon.slot_minutes
        if objective is None:
            objective = scheduler_input.objective_carbon()
        return sum(
            job.power_w * slot_minutes * objective.at(start + i) / CO2_KG_DIVISOR
            for i in range(duration)
        )

    def usable_runs(
        self, scheduler_input: SchedulerInput, job: NormalizedJob
    ) -> list[tuple[int, int]]:
        """Maximal contiguous spans of the window with any room for this job.

        These are the units the interruptible allocation works in. Working in
        TIME-CONTIGUOUS runs is what makes the minimum chunk honest: a chunk is
        a physical stretch of charging, so you cannot build one out of slots
        that are scattered across the night just because each one happens to be
        clean.
        """
        runs: list[tuple[int, int]] = []
        start: Optional[int] = None
        for slot in job.slots():
            if scheduler_input.headroom_w(slot) > 0:
                if start is None:
                    start = slot
            elif start is not None:
                runs.append((start, slot))
                start = None
        if start is not None:
            runs.append((start, job.deadline_slot))
        return [(s, e) for s, e in runs if e - s >= job.min_chunk_slots]

    def run_order(
        self, scheduler_input: SchedulerInput, runs: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """Priority over usable runs. ASAP takes them chronologically (§6)."""
        return sorted(runs)

    def allocate_interruptible(
        self,
        scheduler_input: SchedulerInput,
        job: NormalizedJob,
        placement: Placement,
        reserve: Optional[dict[int, int]] = None,
        order: Optional[str] = None,
    ) -> bool:
        """Fill the energy target by charging contiguous prefixes of usable runs.

        Each run is charged from its start, so the slots that actually draw power
        always form one contiguous block of at least `min_chunk_slots`. Honors
        max power, connection capacity, release, deadline and the minimum chunk
        (§6, §7, §12).

        `order="time"` forces chronological run priority, which the greedy
        scheduler uses as a fallback when the cleanest runs no longer fit.

        Returns True when the target is met. A partial fill is rolled back, so a
        failed attempt never leaves half a job in the schedule.
        """
        reserve = reserve or {}
        slot_minutes = scheduler_input.horizon.slot_minutes
        needed = job.energy_required_wmin or 0
        # A zero-energy target is trivially met, but the job must still appear
        # in the placement so validation sees "nothing to draw", not "missing".
        placement.power_by_job.setdefault(job.id, {})
        runs = self.usable_runs(scheduler_input, job)
        if not runs:
            return needed <= 0

        ordered = sorted(runs) if order == "time" else self.run_order(scheduler_input, runs)
        delivered = 0
        used: list[int] = []
        for run_start, run_end in ordered:
            if delivered >= needed:
                break
            charged_this_run = False
            for slot in range(run_start, run_end):
                if delivered >= needed:
                    break
                budget = self._available_w(scheduler_input, job, placement, slot)
                if reserve.get(slot):
                    budget = max(0, budget - reserve[slot])
                if budget <= 0:
                    if charged_this_run:
                        # The contiguous prefix of this run ends here. Skipping
                        # the full slot and continuing would split the block
                        # into two runs and risk a minimum-chunk violation.
                        break
                    continue
                # Clip the final slot to what is still needed instead of
                # over-delivering a whole slot of full power.
                remaining = needed - delivered
                if remaining < budget * slot_minutes:
                    budget = max(1, -(-remaining // slot_minutes))
                placement.set(job.id, slot, budget)
                delivered += budget * slot_minutes
                used.append(slot)
                charged_this_run = True

        if delivered < needed:
            for slot in used:
                placement.clear_slot(job.id, slot)
                placement.power_by_job.get(job.id, {}).pop(slot, None)
            return False

        # Every charged block must satisfy the minimum chunk, including the
        # final partial prefix (meeting the energy target does not excuse a
        # 1-slot tail when the chunk is 4) and any prefix cut short by a full
        # slot. Repair by over-delivering forward inside the same usable run —
        # the energy target is a lower bound, so extra watts are legal. If a
        # block cannot be grown to the chunk, roll everything back and admit
        # defeat honestly instead of handing validation a broken schedule.
        if job.min_chunk_slots > 1 and used:
            run_of: dict[int, tuple[int, int]] = {}
            for run_start, run_end in runs:
                for s in range(run_start, run_end):
                    run_of[s] = (run_start, run_end)
            blocks: list[list[int]] = []
            for s in sorted(used):
                if blocks and s == blocks[-1][-1] + 1:
                    blocks[-1].append(s)
                else:
                    blocks.append([s])
            repaired = True
            for block in blocks:
                while len(block) < job.min_chunk_slots:
                    nxt = block[-1] + 1
                    span = run_of.get(nxt)
                    if span is None or nxt < block[0] or nxt >= span[1]:
                        repaired = False
                        break
                    budget = self._available_w(scheduler_input, job, placement, nxt)
                    if reserve.get(nxt):
                        budget = max(0, budget - reserve[nxt])
                    if budget <= 0:
                        repaired = False
                        break
                    placement.set(job.id, nxt, budget)
                    delivered += budget * slot_minutes
                    used.append(nxt)
                    block.append(nxt)
                if not repaired:
                    break
            if not repaired:
                for slot in used:
                    placement.clear_slot(job.id, slot)
                    placement.power_by_job.get(job.id, {}).pop(slot, None)
                return False
        return True

    def plan_cleanest_blocks(
        self,
        scheduler_input: SchedulerInput,
        job: NormalizedJob,
        placement: Placement,
        reserve: Optional[dict[int, int]] = None,
    ) -> Optional[dict[int, int]]:
        """Plan an interruptible job as the cheapest contiguous blocks.

        Prefix-charging a run pays for its first slots whether or not they are
        clean. Here a window is slid over each stretch of slots that still has
        room, and the block of at least `min_chunk_slots` that delivers the
        remaining energy at the lowest objective carbon wins (ties: earliest
        start). When no single block can deliver everything, the cleanest whole
        stretch is taken and the search repeats on what is left.

        Returns {slot: watts}, or None when the energy cannot be delivered in
        blocks of the minimum chunk. Nothing is written to `placement`.
        """
        reserve = reserve or {}
        slot_minutes = scheduler_input.horizon.slot_minutes
        remaining = job.energy_required_wmin or 0
        if remaining <= 0:
            return {}
        chunk = max(1, job.min_chunk_slots)
        objective = scheduler_input.objective_carbon()

        budget: dict[int, int] = {}
        for slot in job.slots():
            free = self._available_w(scheduler_input, job, placement, slot)
            budget[slot] = max(0, free - reserve.get(slot, 0))

        segments: list[list[int]] = []
        current: list[int] = []
        for slot in job.slots():
            if budget[slot] > 0 and (not current or slot == current[-1] + 1):
                current.append(slot)
                continue
            if len(current) >= chunk:
                segments.append(current)
            current = [slot] if budget[slot] > 0 else []
        if len(current) >= chunk:
            segments.append(current)

        plan: dict[int, int] = {}
        while remaining > 0 and segments:
            best: Optional[tuple[float, int, list[int]]] = None
            for seg in segments:
                found = self._best_block(seg, budget, objective, remaining, slot_minutes, chunk)
                if found is not None and (best is None or found[:2] < best[:2]):
                    best = found
            if best is not None:
                for slot, power in zip(best[2], self._block_powers(best[2], budget, remaining, slot_minutes)):
                    plan[slot] = power
                return plan
            # No single block is big enough: take the cleanest whole stretch.
            def mean_carbon(seg: list[int]) -> tuple[float, int]:
                weight = sum(budget[s] for s in seg)
                return (sum(budget[s] * objective.at(s) for s in seg) / weight, seg[0])

            seg = min(segments, key=mean_carbon)
            segments.remove(seg)
            for slot in seg:
                plan[slot] = budget[slot]
                remaining -= budget[slot] * slot_minutes
        return plan if remaining <= 0 else None

    @staticmethod
    def _block_powers(
        slots: list[int], budget: dict[int, int], remaining: int, slot_minutes: int
    ) -> list[int]:
        """Watts per slot of a block: full budget, clipping the slot where the
        target is reached, then padding slots (minimum chunk) at full budget."""
        powers: list[int] = []
        for slot in slots:
            if remaining <= 0:
                powers.append(budget[slot])
                continue
            power = budget[slot]
            if remaining < power * slot_minutes:
                power = max(1, -(-remaining // slot_minutes))
            powers.append(power)
            remaining -= power * slot_minutes
        return powers

    def _best_block(
        self,
        seg: list[int],
        budget: dict[int, int],
        objective,
        remaining: int,
        slot_minutes: int,
        chunk: int,
    ) -> Optional[tuple[float, int, list[int]]]:
        """Cheapest block inside one stretch that delivers `remaining`."""
        n = len(seg)
        energy = [0]
        for s in seg:
            energy.append(energy[-1] + budget[s] * slot_minutes)
        if energy[-1] < remaining:
            return None
        best: Optional[tuple[float, int, list[int]]] = None
        end = 0
        for i in range(n):
            end = max(end, i + 1)
            while end <= n and energy[end] - energy[i] < remaining:
                end += 1
            if end > n:
                break
            length = max(end - i, chunk)
            if i + length > n:
                continue
            block = seg[i : i + length]
            powers = self._block_powers(block, budget, remaining, slot_minutes)
            cost = float(sum(p * objective.at(s) for s, p in zip(block, powers)))
            if best is None or cost < best[0] - 1e-9:
                best = (cost, block[0], block)
        return best

    def place_interruptible_cleanest(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, placement: Placement
    ) -> bool:
        """Commit `plan_cleanest_blocks`; False (and nothing written) if none."""
        plan = self.plan_cleanest_blocks(scheduler_input, job, placement)
        if plan is None:
            return False
        placement.power_by_job.setdefault(job.id, {})
        for slot, power in plan.items():
            placement.set(job.id, slot, power)
        return True

    def place_thermal_control(
        self,
        scheduler_input: SchedulerInput,
        job: NormalizedJob,
        placement: Placement,
        prefer_low_carbon: bool = False,
    ) -> bool:
        """Forward control that keeps the thermal state inside its band.

        ASAP (prefer_low_carbon=False): charge as early as possible, coast once
        the target is reached.

        Greedy (prefer_low_carbon=True): start from the ASAP trajectory's demand
        and try to relocate that charging into the cleanest slots that allow it;
        if the relocated trajectory would breach the band, keep the ASAP one.
        That fallback is deliberate — a heuristic must never return a schedule
        that breaks a comfort constraint (§7).
        """
        asap_slots = self._thermal_asap_slots(scheduler_input, job, placement)
        # A job that needs no heating is still a placed job: record its (empty)
        # presence so validation sees "nothing to draw" rather than "missing".
        placement.power_by_job.setdefault(job.id, {})
        if not prefer_low_carbon:
            return True

        candidate_slots = self._thermal_candidate_slots(scheduler_input, job, len(asap_slots))
        # The ASAP trajectory is already written into the live placement. Lift
        # it out before the trial so capacity is read without the job's own
        # ASAP draw, then either replace the whole row or put it back.
        asap_row = dict(placement.power_by_job.get(job.id, {}))
        for slot in asap_row:
            placement.clear_slot(job.id, slot)
        placement.power_by_job[job.id] = {}
        trial = Placement(slot_count=scheduler_input.horizon.slot_count)
        trial_placed = self._simulate_thermal(
            scheduler_input, job, candidate_slots, trial, source=placement
        )
        if trial_placed is not None:
            for slot, power_w in trial.power_by_job.get(job.id, {}).items():
                placement.set(job.id, slot, power_w)
            return True
        for slot, power_w in asap_row.items():
            placement.set(job.id, slot, power_w)
        return bool(asap_slots)

    def _thermal_asap_slots(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, placement: Placement
    ) -> list[int]:
        """Earliest-feasible control: charge to target as soon as possible (§6)."""
        result = self._simulate_thermal(
            scheduler_input,
            job,
            None,
            placement,
            mode="asap",
        )
        if result is None:
            raise PlacementFailure(
                job.id,
                f"{job.name} could not be placed: no thermal control trajectory keeps it "
                "inside its comfort band before the deadline",
            )
        return result

    def _thermal_candidate_slots(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, count: int
    ) -> list[int]:
        """The `count` cleanest slots in the window, lowest objective cost first.

        Ties break on slot index so the choice is reproducible (§23).

        §21: ranked by `objective_carbon()`, so Greedy's thermal heuristic
        prefers low-carbon slots in ACTUAL mode and low-risk-adjusted slots under
        a forecast. It chooses WHICH SLOTS to charge; the band and target are
        still verified by simulation, so a risk preference can never buy a
        comfort violation (§19).
        """
        window = list(job.slots())
        objective = scheduler_input.objective_carbon()
        return sorted(window, key=lambda s: (objective.at(s), s))[:count]

    def _simulate_thermal(
        self,
        scheduler_input: SchedulerInput,
        job: NormalizedJob,
        charging_slots: Optional[list[int]],
        placement: Placement,
        mode: str = "fixed",
        source: Optional[Placement] = None,
    ) -> Optional[list[int]]:
        """Walk the thermal recurrence forward, choosing power per slot.

        Returns the list of slots that actually drew power, or None if the band
        or target cannot be met. Power at each slot is chosen as:
          * `mode="fixed"`    -> full power on the given slots, zero elsewhere
          * `mode="asap"`     -> the minimum power that keeps the band safe,
                                 escalating toward the target as early as possible

        `source` is the placement capacity is read from; it defaults to
        `placement` itself. A trial trajectory (greedy low-carbon relocation)
        passes the live placement as `source` while writing into an empty trial
        placement, so the trial respects what earlier jobs already committed
        instead of seeing a fictitious empty connection.
        """
        scale = job.thermal
        if scale is None:
            return None
        chosen = set(charging_slots or [])
        read = source if source is not None else placement
        temperature_milli = scale.initial_milli
        used: list[int] = []

        for slot in job.slots():
            headroom_millikw = to_millikw(
                self._available_w(scheduler_input, job, read, slot) / 1000.0
            )
            ceiling = min(scale.max_power_millikw, job.max_power_w, max(0, headroom_millikw))

            if mode == "fixed":
                power = ceiling if slot in chosen else 0
            else:
                power = self._asap_power(
                    scale, temperature_milli, ceiling, job
                )

            # The state evolves on EVERY slot, including idle ones: a heater
            # left off still drifts toward ambient. Skipping the recurrence on
            # idle slots would freeze the temperature and disagree with both
            # the CP-SAT model and the independent validator, which step every
            # slot.
            temperature_milli = scale.next_temperature_milli(temperature_milli, power)
            if not (
                scale.min_milli - THERMAL_BAND_TOLERANCE_MILLI
                <= temperature_milli
                <= scale.max_milli + THERMAL_BAND_TOLERANCE_MILLI
            ):
                return None
            if power > 0:
                used.append(slot)
                # 1 milli-kW == 1 W, so the integer is already in watts.
                placement.set(job.id, slot, power)

        if scale.target_milli is not None and temperature_milli < scale.target_milli:
            return None
        return used

    def _asap_power(
        self, scale, temperature_milli: int, ceiling_millikw: int, job: NormalizedJob
    ) -> int:
        """The SMALLEST power that keeps the state on the right side of its target.

        An earlier version of this charged to the target and then coasted, which
        looks right for a few slots and then oscillates: the state decays below
        the target, the heater kicks back on, and by the deadline it can land
        under the service target. Since the target is what the load must satisfy
        AT the deadline, the policy is "hold the target", not "reach it once".

        The recurrence is linear in power, so the exact power needed to satisfy
        the target next slot is solved directly rather than searched.
        """
        if ceiling_millikw <= 0:
            return 0
        if scale.target_milli is None:
            # No service target: just stay inside the band.
            return ceiling_millikw if self._coasts_out_of_band(scale, temperature_milli) else 0

        target = scale.target_milli
        coast = scale.next_temperature_milli(temperature_milli, 0)
        rising = scale.b_scaled > 0

        if rising:
            if temperature_milli >= target and coast >= target:
                return 0
            # need (a*T + b*p + 1000c)/1000 >= target  ->  b*p >= 1000*target - a*T - 1000c
            deficit = target * 1000 - (
                scale.a_permille * temperature_milli + 1000 * scale.c_scaled
            )
            if deficit <= 0:
                return 0
            required = -(-deficit // scale.b_scaled)
        else:
            if temperature_milli <= target and coast <= target:
                return 0
            # need next <= target  ->  -|b|*p <= 1000*target - a*T - 1000c
            surplus = (
                scale.a_permille * temperature_milli + 1000 * scale.c_scaled
            ) - target * 1000
            if surplus <= 0:
                return 0
            required = -(-surplus // abs(scale.b_scaled))

        # Never overshoot the comfort ceiling in a single step.
        if rising:
            headroom_units = scale.max_milli * 1000 - (
                scale.a_permille * temperature_milli + 1000 * scale.c_scaled
            )
            if headroom_units <= 0:
                return 0
            required = min(required, headroom_units // scale.b_scaled)
        else:
            headroom_units = (
                scale.a_permille * temperature_milli + 1000 * scale.c_scaled
            ) - scale.min_milli * 1000
            if headroom_units <= 0:
                return 0
            required = min(required, headroom_units // abs(scale.b_scaled))

        return max(0, min(ceiling_millikw, required))

    @staticmethod
    def _coasts_out_of_band(scale, temperature_milli: int) -> bool:
        """True when idling would drift the state outside its comfort band."""
        coast = scale.next_temperature_milli(temperature_milli, 0)
        return not (scale.min_milli <= coast <= scale.max_milli)

    # --- result construction -----------------------------------------------

    def _result(
        self,
        scheduler_input: SchedulerInput,
        placement: Placement,
        status: ScheduleStatus,
        solver: SolverInfo,
        reason: str = "",
        violations: Optional[list[str]] = None,
        validation=None,
        extra_metrics: Optional[dict] = None,
    ) -> SchedulerResult:
        accounting = self.accounting.account(scheduler_input, placement)
        metrics = ScheduleMetrics(
            total_energy_kwh=accounting.total_energy_kwh,
            total_co2_kg=accounting.total_co2_kg,
            peak_kw=accounting.peak_w / 1000.0,
            baseline_peak_kw=accounting.baseline_peak_w / 1000.0,
            energy_cost=accounting.energy_cost,
            co2_cost=accounting.co2_cost,
            deadline_misses=validation.deadline_misses if validation else 0,
            feasibility_violations=len(violations) if violations else 0,
            solve_time_ms=solver.solve_time_ms,
        )
        if extra_metrics:
            for key, value in extra_metrics.items():
                setattr(metrics, key, value)

        return SchedulerResult(
            status=status,
            scheduler=self.name.value,
            schedule=self.build_scheduled_jobs(scheduler_input, placement, status),
            metrics=metrics,
            solver=solver,
            explanations=self.build_explanations(scheduler_input, placement),
            violations=violations or [],
            reason=reason,
            signal=CarbonProvenance(
                signal_type=scheduler_input.carbon.signal_type,
                source=scheduler_input.carbon.source,
                is_forecast=scheduler_input.carbon.is_forecast,
                slot_count=scheduler_input.horizon.slot_count,
                mean_gco2_per_kwh=scheduler_input.carbon.mean(),
            ),
            horizon=scheduler_input.horizon,
            slot_load_w=accounting.slot_load_w,
            baseline_slot_load_w=accounting.slot_baseline_w,
        )

    def build_scheduled_jobs(
        self, scheduler_input: SchedulerInput, placement: Placement, status: ScheduleStatus
    ) -> list[ScheduledJob]:
        horizon = scheduler_input.horizon
        jobs: list[ScheduledJob] = []
        for job in scheduler_input.jobs:
            slots = placement.job_slots(job.id)
            active = sorted(s for s, p in slots.items() if p > 0)
            if not active:
                continue
            allocations = [
                SlotAllocation(
                    slot=slot,
                    timestamp=horizon.slot_start(slot),
                    power_w=slots[slot],
                )
                for slot in active
            ]
            temperature: list[TemperatureSample] = []
            if job.job_type is LoadType.THERMAL and job.thermal is not None:
                temperature = self._temperature_trace(scheduler_input, job, slots)
            stats = self.accounting.job_accounting(scheduler_input, job.id, placement)
            jobs.append(
                ScheduledJob(
                    job_id=job.id,
                    name=job.name,
                    job_type=job.job_type,
                    start_time=horizon.slot_start(active[0]),
                    end_time=horizon.slot_end(active[-1]),
                    start_slot=active[0],
                    end_slot=active[-1] + 1,
                    energy_kwh=stats["energy_kwh"],
                    peak_power_kw=max(power_w for power_w in slots.values()) / 1000.0,
                    allocations=allocations,
                    temperature=temperature,
                    reason_code=ReasonCode.SCHEDULED.value,
                    reason="",
                )
            )
        return jobs

    def _temperature_trace(
        self, scheduler_input: SchedulerInput, job: NormalizedJob, slots: dict[int, int]
    ) -> list[TemperatureSample]:
        """Replay the thermal trajectory for the future Gantt view (§50).

        Uses the same integer arithmetic the validator uses, so the trace a user
        is shown is the trace that was checked.
        """
        scale = job.thermal
        if scale is None:
            return []
        horizon = scheduler_input.horizon
        temperature_milli = scale.initial_milli
        trace = [
            TemperatureSample(
                slot=job.release_slot,
                timestamp=horizon.slot_start(min(job.release_slot, horizon.slot_count - 1)),
                temperature_c=temperature_c_from_milli(temperature_milli),
                power_w=0,
            )
        ]
        for slot in job.slots():
            power_w = slots.get(slot, 0)
            temperature_milli = scale.next_temperature_milli(
                temperature_milli, to_millikw(power_w / 1000.0)
            )
            trace.append(
                TemperatureSample(
                    slot=slot,
                    timestamp=horizon.slot_start(slot),
                    temperature_c=temperature_c_from_milli(temperature_milli),
                    power_w=power_w,
                )
            )
        return trace

    def build_explanations(
        self, scheduler_input: SchedulerInput, placement: Placement
    ) -> list[JobExplanation]:
        """Per-job explanation against the job's own earliest feasible window."""
        horizon = scheduler_input.horizon
        out: list[JobExplanation] = []
        for job in scheduler_input.jobs:
            stats = self.accounting.job_accounting(scheduler_input, job.id, placement)
            slots = placement.job_slots(job.id)
            active = sorted(s for s, p in slots.items() if p > 0)
            earliest = job.release_slot

            if active:
                start_time = horizon.slot_start(active[0])
                end_time = horizon.slot_end(active[-1])
                shifted = max(0, active[0] - earliest)
            else:
                start_time = horizon.slot_start(earliest)
                end_time = horizon.slot_start(min(earliest + 1, horizon.slot_count - 1))
                shifted = 0

            earliest_end_slot = min(
                earliest + max(1, job.minimum_slots()) - 1, horizon.slot_count - 1
            )
            out.append(
                JobExplanation(
                    job_id=job.id,
                    name=job.name,
                    job_type=job.job_type,
                    original_start=horizon.slot_start(earliest),
                    original_end=horizon.slot_end(earliest_end_slot),
                    scheduled_start=start_time,
                    scheduled_end=end_time,
                    deadline_at=(
                        horizon.end
                        if job.deadline_slot >= horizon.slot_count
                        else horizon.slot_start(job.deadline_slot)
                    ),
                    shifted_slots=shifted,
                    energy_kwh=stats["energy_kwh"],
                    co2_after_kg=stats["co2_kg"],
                    reason_code=ReasonCode.SCHEDULED,
                    reason="",
                )
            )
        return out


class PreflightResult:
    """Outcome of the pre-solve feasibility checks."""

    def __init__(self) -> None:
        self.violations: list[str] = []
        self.reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.violations

    def fail(self, reason: str) -> None:
        self.violations.append(reason)
        if not self.reason:
            self.reason = reason


def max_usable_atomic_slots(scheduler_input: SchedulerInput, job: NormalizedJob) -> int:
    """Longest contiguous run of slots in the window with room for this job."""
    best = run = 0
    for slot in job.slots():
        if scheduler_input.headroom_w(slot) >= job.power_w:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def max_interruptible_wmin(
    scheduler_input: SchedulerInput, job: NormalizedJob, reserve: dict[int, int]
) -> int:
    """Largest energy this job could physically deliver, honouring capacity.

    Uses a time-ordered greedy witness, which is an achievable allocation — so a
    shortfall here is a genuine impossibility, not a heuristic's opinion.
    """
    slot_minutes = scheduler_input.horizon.slot_minutes
    total = 0
    for slot in job.slots():
        budget = min(
            job.max_power_w, max(0, scheduler_input.headroom_w(slot) - reserve.get(slot, 0))
        )
        total += budget * slot_minutes
    return total
