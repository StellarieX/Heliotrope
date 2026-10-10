"""Graceful planning fallback: drop loads until the plan fits (§20).

One bad (oversized, impossible-window) load must not fail the whole plan.
`drop_until_feasible` repeatedly solves and, while the result is not feasible,
removes the single largest-energy job — biggest first, because one big load is
the most likely cause of a capacity breach — recording each removal as a plain
`{"job_id", "name", "reason"}` dict in the caller-supplied `left_out` list.

The caller owns the `run` callable (any scheduler, any config) and the final
explained re-run; this module only decides *what* to drop, never *how* to solve.
"""

from __future__ import annotations

from typing import Callable

from ..domain.scheduling import NormalizedJob, SchedulerInput, SchedulerResult


def job_energy_wmin(job: NormalizedJob, slot_minutes: int) -> int:
    """Energy footprint of one normalized job, in watt-minutes.

    The stored requirement when the normalizer computed one; otherwise the
    rectangular footprint (power x duration), which is the honest upper bound
    for atomic jobs that have no separate energy figure.
    """
    if job.energy_required_wmin is not None:
        return job.energy_required_wmin
    return job.power_w * (job.duration_slots or 1) * slot_minutes


def drop_until_feasible(
    run: Callable[[SchedulerInput], SchedulerResult],
    solve_input: SchedulerInput,
    capacity_kw: float,
    left_out: list[dict],
) -> SchedulerResult:
    """Solve, dropping the largest-energy job per round until feasible.

    `left_out` is appended to in place with one plain dict per dropped job, so
    the caller can reconstruct the surviving input as the original minus the
    dropped ids and can surface the reasons verbatim in the API payload.
    """
    while True:
        result = run(solve_input)
        if result.status in ("OPTIMAL", "FEASIBLE"):
            return result
        if not solve_input.jobs:
            return result
        victim = max(
            solve_input.jobs,
            key=lambda j: job_energy_wmin(j, j.slot_minutes or 15),
        )
        left_out.append(
            {
                "job_id": victim.id,
                "name": victim.name,
                "reason": (
                    f"{victim.name} was left out to fit the {capacity_kw:g} kW limit. "
                    f"{result.reason or result.status}"
                ),
            }
        )
        solve_input = solve_input.model_copy(
            update={"jobs": [j for j in solve_input.jobs if j.id != victim.id]}
        )
