"""Receding-horizon replanning (Phase 7).

The immutable-past invariant lives here: slots ending at or before `now` are
never touched. Replanning optimizes ONLY remaining requirements:

  atomic (started)      remaining duration runs contiguously from now
  interruptible         remaining energy = target - delivered
  thermal               initial state = replayed current temperature
  missed                release moves to now; feasibility is rechecked
  completed/cancelled   excluded entirely

Stability (no thrashing) comes from two mechanisms, documented as preferences
rather than hard rules so they can never manufacture infeasibility:

  commitment freeze     jobs starting inside the commitment window keep their
                        exact remaining allocation (folded into baseline); if
                        the frozen problem is infeasible the freeze is lifted
                        and the lift is recorded
  improvement gate      a candidate that moves almost nothing for almost no
                        carbon benefit is discarded; the current version stands

An objective-level change penalty is NOT in the solver models; stability is
enforced structurally (freeze + gate) plus measured (change minutes). This is
deliberate and documented, not an oversight.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from typing import Callable, Optional

from ..domain.execution import JobStatus, ScheduleChange
from ..domain.loads import LoadType
from ..domain.scaling import to_millikw
from ..domain.scheduling import (
    BaselineProfile,
    NormalizedJob,
    SchedulerInput,
    SchedulerResult,
    ScheduleStatus,
)

log = logging.getLogger("heliotrope.receding")


class RemainingInfeasible(ValueError):
    """A job's remaining requirement no longer fits. Carries job_id + why."""

    def __init__(self, job_id: str, name: str, reason: str) -> None:
        super().__init__(reason)
        self.job_id = job_id
        self.name = name
        self.reason = reason


def now_slot(horizon, now: datetime) -> int:
    """First slot not yet finished at `now`. Everything before is immutable."""
    n = horizon.slot_count
    for slot in range(n):
        if horizon.slot_end(slot) > now:
            return slot
    return n


def replay_temperature_milli(job: NormalizedJob, delivered: dict[int, int]) -> int:
    """Current thermal state: initial state replayed through actual draws."""
    scale = job.thermal
    if scale is None:
        raise RemainingInfeasible(job.id, job.name, "thermal job lost its dynamics")
    temp = scale.initial_milli
    for slot in sorted(delivered):
        temp = scale.next_temperature_milli(temp, to_millikw(delivered[slot] / 1000.0))
    return temp


def remaining_job(
    job: NormalizedJob,
    status: JobStatus,
    delivered_kwh: float,
    delivered_slots: dict[int, int],
    current_slot: int,
) -> Optional[NormalizedJob]:
    """Adjust one job to its remaining requirement, or None if it is done."""
    if status in (JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED):
        return None
    release = max(job.release_slot, current_slot)
    patch: dict = {"release_slot": release}

    if job.job_type is LoadType.DEFERRABLE_ATOMIC:
        elapsed = len([s for s in delivered_slots if delivered_slots[s] > 0])
        duration = job.duration_slots or 0
        if status is JobStatus.RUNNING:
            # Committed: the rest of the run continues contiguously from now.
            remaining = duration - elapsed
            if remaining <= 0:
                return None
            patch["duration_slots"] = remaining
        if release + (patch.get("duration_slots") or duration) > job.deadline_slot:
            raise RemainingInfeasible(
                job.id, job.name,
                f"only {job.deadline_slot - release} slot(s) left before the deadline "
                f"but {patch.get('duration_slots') or duration} still needed",
            )
        return job.model_copy(update=patch)

    if job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
        target = job.energy_required_wmin or 0
        remaining_wmin = target - int(round(delivered_kwh * 60000))
        if remaining_wmin <= 0:
            return None
        patch["energy_required_wmin"] = remaining_wmin
        updated = job.model_copy(update=patch)
        if updated.minimum_slots() > updated.window_slots():
            raise RemainingInfeasible(
                job.id, job.name,
                f"{remaining_wmin / 60000:.2f} kWh still needed but the remaining "
                f"window cannot deliver it",
            )
        return updated

    if job.job_type is LoadType.THERMAL:
        scale = job.thermal
        if scale is None:
            raise RemainingInfeasible(job.id, job.name, "thermal job lost its dynamics")
        current = replay_temperature_milli(job, {s: p for s, p in delivered_slots.items()})
        patch["thermal"] = replace(scale, initial_milli=current)
        updated = job.model_copy(update=patch)
        if current < scale.min_milli or current > scale.max_milli:
            raise RemainingInfeasible(
                job.id, job.name,
                "thermal state drifted outside its comfort band during execution",
            )
        return updated

    return job.model_copy(update=patch)  # FIXED passes through (baseline source)


def freeze_commitment(
    scheduler_input: SchedulerInput,
    placement_slots: dict[str, dict[int, int]],
    current_slot: int,
    commit_slots: int,
) -> tuple[list[NormalizedJob], list[int], list[str]]:
    """Split jobs into (re-optimizable, frozen-baseline-additions, frozen_ids).

    Jobs whose scheduled start falls inside [now, now+commit) keep their exact
    remaining allocation: it is folded into the baseline and the job leaves
    the problem. Returns the kept jobs, per-slot watts to add to baseline,
    and the frozen job ids (for the audit trail).
    """
    n = scheduler_input.horizon.slot_count
    frozen_add = [0] * n
    kept: list[NormalizedJob] = []
    frozen_ids: list[str] = []
    horizon_end = current_slot + commit_slots
    for job in scheduler_input.jobs:
        slots = placement_slots.get(job.id, {})
        active = sorted(s for s, p in slots.items() if p > 0 and s >= current_slot)
        if active and active[0] < horizon_end and job.job_type is not LoadType.THERMAL:
            for s in active:
                frozen_add[s] += slots[s]
            frozen_ids.append(job.id)
        else:
            kept.append(job)
    return kept, frozen_add, frozen_ids


def future_carbon_cost(scheduler_input: SchedulerInput, placement_slots: dict, from_slot: int) -> float:
    """Objective-carbon cost of the future part of a placement, in kg."""
    carbon = scheduler_input.objective_carbon()
    slot_minutes = scheduler_input.horizon.slot_minutes
    total = 0.0
    for job in scheduler_input.jobs:
        for slot, power in placement_slots.get(job.id, {}).items():
            if slot >= from_slot and power > 0:
                total += power * slot_minutes * carbon.at(slot) / 60_000_000
    return total


def diff_placements(
    scheduler_input: SchedulerInput,
    previous: dict[str, dict[int, int]],
    current: dict[str, dict[int, int]],
    from_slot: int,
    reason: str,
) -> list[ScheduleChange]:
    """Per-job start/end movement between two future placements."""
    horizon = scheduler_input.horizon
    slot_minutes = horizon.slot_minutes
    changes: list[ScheduleChange] = []
    for job in scheduler_input.jobs:
        prev = sorted(s for s, p in previous.get(job.id, {}).items() if p > 0 and s >= from_slot)
        new = sorted(s for s, p in current.get(job.id, {}).items() if p > 0 and s >= from_slot)
        if prev == new:
            continue
        ps = horizon.slot_start(prev[0]) if prev else None
        ns = horizon.slot_start(new[0]) if new else None
        shift = abs((new[0] - prev[0]) * slot_minutes) if prev and new else 0.0
        changes.append(
            ScheduleChange(
                job_id=job.id,
                previous_start=ps,
                new_start=ns,
                previous_end=horizon.slot_end(prev[-1]) if prev else None,
                new_end=horizon.slot_end(new[-1]) if new else None,
                change_minutes=float(shift),
                reason=reason,
            )
        )
    return changes


class RecedingHorizon:
    """Engine-agnostic replanning: takes a solve function, returns versions."""

    def __init__(
        self,
        solve_fn: Callable[[SchedulerInput], SchedulerResult],
        commitment_slots: int = 2,
        min_shift_minutes: int = 5,
        improvement_threshold_percent: float = 1.0,
    ) -> None:
        self.solve_fn = solve_fn
        self.commitment_slots = commitment_slots
        self.min_shift_minutes = min_shift_minutes
        self.improvement_threshold_percent = improvement_threshold_percent

    def remaining_input(
        self,
        scheduler_input: SchedulerInput,
        states: dict,
        delivered: dict[str, dict[int, int]],
        delivered_kwh: dict[str, float],
        current_slot: int,
    ) -> tuple[SchedulerInput, list[str]]:
        """Rebuild the future problem from execution truth. Raises
        RemainingInfeasible naming the job when the remainder cannot fit."""
        jobs: list[NormalizedJob] = []
        notes: list[str] = []
        for job in scheduler_input.jobs:
            status = states.get(job.id)
            from ..domain.execution import JobStatus as JS

            if status is None:
                status = JS.PENDING
            try:
                updated = remaining_job(
                    job, status,
                    delivered_kwh.get(job.id, 0.0),
                    delivered.get(job.id, {}),
                    current_slot,
                )
            except RemainingInfeasible as exc:
                raise exc
            if updated is not None:
                jobs.append(updated)
            else:
                notes.append(f"{job.name} already complete; excluded from replanning")
        if not jobs:
            raise RemainingInfeasible("", "", "no remaining schedulable work")
        return scheduler_input.model_copy(update={"jobs": jobs}), notes

    def replan(
        self,
        scheduler_input: SchedulerInput,
        previous_slots: dict[str, dict[int, int]],
        solve_input: SchedulerInput,
        current_slot: int,
        reason: str,
    ) -> tuple[SchedulerResult, list[ScheduleChange], list[str], bool]:
        """Solve the remaining problem with commitment freeze + improvement gate.

        Returns (result, changes, notes, frozen_lifted). The gate may discard a
        pointless candidate: then result is the solve output but changes is
        empty and the caller should keep the current version.
        """
        notes: list[str] = []
        kept, frozen_add, frozen_ids = freeze_commitment(
            solve_input, previous_slots, current_slot, self.commitment_slots
        )
        frozen_lifted = False
        attempt = solve_input.model_copy(update={"jobs": kept})
        if frozen_add and any(frozen_add):
            base = list(solve_input.baseline.power_w)
            attempt = attempt.model_copy(
                update={"baseline": BaselineProfile(
                    power_w=[b + f for b, f in zip(base, frozen_add)]
                )}
            )
            notes.append(f"commitment freeze held {len(frozen_ids)} job(s) in place")
        result = self.solve_fn(attempt)
        if result.status not in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL) and frozen_ids:
            # The freeze manufactured the infeasibility; lift it and say so.
            notes.append("commitment freeze lifted: it made the remainder infeasible")
            frozen_lifted = True
            result = self.solve_fn(solve_input)

        if result.status not in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL):
            return result, [], notes, frozen_lifted

        current_slots = {
            s.job_id: {a.slot: a.power_w for a in s.allocations} for s in result.schedule
        }
        changes = diff_placements(solve_input, previous_slots, current_slots, current_slot, reason)
        if not self._worth_it(solve_input, previous_slots, current_slots, current_slot, changes):
            notes.append(
                "candidate discarded: movement and carbon gain both below threshold; "
                "current version stands"
            )
            return result, [], notes, frozen_lifted
        return result, changes, notes, frozen_lifted

    def _worth_it(self, scheduler_input, previous, current, current_slot, changes) -> bool:
        if not changes:
            return False
        max_shift = max(c.change_minutes for c in changes)
        if max_shift < self.min_shift_minutes:
            # Trivial movement is still worth it if the carbon gain is real.
            pass
        before = future_carbon_cost(scheduler_input, previous, current_slot)
        after = future_carbon_cost(scheduler_input, current, current_slot)
        if before <= 0:
            return True
        improvement = (before - after) / before * 100.0
        if max_shift < self.min_shift_minutes and improvement < self.improvement_threshold_percent:
            log.info(
                "replan gated: shift %.1f min, gain %.2f%% — keeping current version",
                max_shift, improvement,
            )
            return False
        return True
