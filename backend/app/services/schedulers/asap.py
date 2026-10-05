"""ASAP scheduler — the counterfactual baseline (Phase 4, §6).

    "Each schedulable job runs at the earliest feasible time after its release."

ASAP deliberately does NOT look at carbon. Its entire purpose is to be the
honest "what would happen with no optimization at all" reference that every
savings figure is measured against (§32, §24). A baseline that peeked at carbon
would make every improvement look smaller and would not answer the question users
actually care about: "what did NOT scheduling get me?"

Deterministic by construction: jobs are processed in a fixed order
(deadline, then release, then id) and every placement is earliest-fit.
"""

from __future__ import annotations

from ...domain.loads import LoadType
from ...domain.scheduling import SchedulerInput
from ..carbon_accounting import Placement
from .base import BaseScheduler, PlacementFailure, SchedulerName


class ASAPScheduler(BaseScheduler):
    """Earliest-feasible placement. No carbon awareness, no optimization."""

    name = SchedulerName.ASAP

    def job_order(self, scheduler_input: SchedulerInput) -> list:
        """Earliest-deadline-first, with id as a deterministic tiebreak.

        EDF is also the right ORDER here, not just a deterministic one: placing
        the most urgent job first is what makes the ASAP result a valid
        baseline rather than an arbitrary one.
        """
        return sorted(
            scheduler_input.jobs,
            key=lambda j: (j.deadline_slot, j.release_slot, j.id),
        )

    def build_placement(self, scheduler_input: SchedulerInput) -> Placement:
        placement = Placement(slot_count=scheduler_input.horizon.slot_count)
        for job in self.job_order(scheduler_input):
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                self.place_atomic_earliest(scheduler_input, job, placement)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                if not self.allocate_interruptible(scheduler_input, job, placement):
                    raise PlacementFailure(
                        job.id,
                        f"{job.name} could not be charged in full even at the earliest "
                        "opportunity; the window cannot deliver its energy target",
                    )
            elif job.job_type is LoadType.THERMAL:
                self.place_thermal_control(scheduler_input, job, placement, prefer_low_carbon=False)
        return placement
