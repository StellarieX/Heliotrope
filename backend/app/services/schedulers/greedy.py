"""Greedy carbon scheduler (Phase 4, §7).

    "Schedule flexible loads into the lowest-carbon feasible opportunities while
     respecting all hard constraints."

NOT OPTIMAL, and it does not pretend to be. Greedy takes each job in turn and
commits it to the cheapest opportunity it can see right now, without revisiting
earlier commitments. That is a genuine limitation — a job placed early can
consume the cleanest slot another job needed — and it is exactly why CP-SAT
exists. What greedy DOES guarantee is that its result is a valid, fully
satisfied schedule, which makes it a fair comparison baseline (§41).

DETERMINISM (§23). Every ordering decision has an explicit, documented
tiebreak, and no dictionary iteration order or unseeded randomness is consulted:

  * atomic      — cheapest window; ties go to the EARLIEST start
  * interruptible — cleanest slots first; ties go to the EARLIEST slot
  * job order   — earliest deadline first, then id
"""

from __future__ import annotations

from ...domain.loads import LoadType
from ...domain.scheduling import SchedulerInput
from ..carbon_accounting import Placement
from .base import BaseScheduler, PlacementFailure, SchedulerName


class GreedyScheduler(BaseScheduler):
    """Lowest-carbon feasible placement, one job at a time."""

    name = SchedulerName.GREEDY

    def job_order(self, scheduler_input: SchedulerInput) -> list:
        """Earliest deadline first (§7), deterministic on ties.

        Carbon awareness would be tempting here, but EDF keeps the greedy
        baseline comparable with ASAP, which is what the comparison view needs.
        """
        return sorted(
            scheduler_input.jobs,
            key=lambda j: (j.deadline_slot, j.release_slot, j.id),
        )

    def build_placement(self, scheduler_input: SchedulerInput) -> Placement:
        placement = Placement(slot_count=scheduler_input.horizon.slot_count)
        for job in self.job_order(scheduler_input):
            if job.job_type is LoadType.THERMAL:
                self.place_thermal_control(scheduler_input, job, placement, prefer_low_carbon=True)
            else:
                self._place_flexible(scheduler_input, job, placement)
        self._improve(scheduler_input, placement)
        return placement

    def _place_flexible(self, scheduler_input: SchedulerInput, job, placement: Placement) -> None:
        """Place one atomic or interruptible job (raises PlacementFailure)."""
        if job.job_type is LoadType.DEFERRABLE_ATOMIC:
            self.place_atomic_lowest_carbon(scheduler_input, job, placement)
        elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
            if self.place_interruptible_cleanest(scheduler_input, job, placement):
                return
            if not self.allocate_interruptible(scheduler_input, job, placement):
                # The low-carbon runs may be capacity-bound once earlier jobs
                # are committed. Retry chronologically rather than failing a
                # job that is in fact schedulable.
                if not self.allocate_interruptible(scheduler_input, job, placement, order="time"):
                    raise PlacementFailure(
                        job.id,
                        f"{job.name} could not be charged in full: the cleanest "
                        "opportunities were taken and the remaining window cannot "
                        "deliver its energy target",
                    )

    #: Passes of the improvement search below. One is usually enough; the bound
    #: keeps greedy fast (each pass is O(jobs^2) re-placements).
    improvement_passes = 2

    def _objective_cost(self, scheduler_input: SchedulerInput, placement: Placement) -> float:
        """Total objective carbon of a placement, in watt * gCO2/kWh * slot."""
        objective = scheduler_input.objective_carbon()
        return float(
            sum(
                power * objective.at(slot)
                for slots in placement.power_by_job.values()
                for slot, power in slots.items()
                if power > 0
            )
        )

    @staticmethod
    def _take_row(placement: Placement, job_id: str) -> dict[int, int]:
        row = dict(placement.power_by_job.get(job_id, {}))
        for slot in row:
            placement.clear_slot(job_id, slot)
        placement.power_by_job[job_id] = {}
        return row

    @staticmethod
    def _put_row(placement: Placement, job_id: str, row: dict[int, int]) -> None:
        for slot in list(placement.power_by_job.get(job_id, {})):
            placement.clear_slot(job_id, slot)
        placement.power_by_job[job_id] = {}
        for slot, power in row.items():
            placement.set(job_id, slot, power)

    def _improve(self, scheduler_input: SchedulerInput, placement: Placement) -> None:
        """Pairwise ejection search over the EDF result.

        Re-placing one job alone against the others can never help: the others
        only removed options. The loss in EDF comes from an early job taking the
        slot a later, less flexible job needed. So for each ordered pair (A, B)
        with A placed before B, both are lifted out and re-placed with B first.
        The swap is kept only if total objective carbon strictly drops and the
        independent validator still accepts the whole schedule. Deterministic:
        pairs are visited in EDF order.
        """
        movable = [
            job
            for job in self.job_order(scheduler_input)
            if job.job_type in (LoadType.DEFERRABLE_ATOMIC, LoadType.DEFERRABLE_INTERRUPTIBLE)
        ]
        for _ in range(self.improvement_passes):
            improved = False
            for i, first in enumerate(movable):
                for second in movable[i + 1 :]:
                    before = self._objective_cost(scheduler_input, placement)
                    old_first = self._take_row(placement, first.id)
                    old_second = self._take_row(placement, second.id)
                    ok = False
                    try:
                        self._place_flexible(scheduler_input, second, placement)
                        self._place_flexible(scheduler_input, first, placement)
                        ok = (
                            self._objective_cost(scheduler_input, placement) < before - 1e-9
                            and self.validator.validate(scheduler_input, placement).ok
                        )
                    except PlacementFailure:
                        ok = False
                    if ok:
                        improved = True
                    else:
                        self._put_row(placement, first.id, old_first)
                        self._put_row(placement, second.id, old_second)
            if not improved:
                break

    def run_order(
        self, scheduler_input: SchedulerInput, runs: list[tuple[int, int]]
    ) -> list[tuple[int, int]]:
        """Cleanest runs first; ties break on the earlier start (§7, §23).

        Runs are ranked by their MEAN carbon rather than their best slot,
        because a chunk is charged as one stretch — a run with one beautiful
        hour and nine filthy ones is not a cheap run.
        """
        objective = scheduler_input.objective_carbon()

        def key(run: tuple[int, int]) -> tuple[float, int]:
            start, end = run
            total = sum(objective.at(s) for s in range(start, end))
            return (total / (end - start), start)

        return sorted(runs, key=key)
