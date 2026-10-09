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
