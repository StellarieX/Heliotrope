"""Coordinated CP-SAT: joint multi-user optimization (Phase 6).

Reuses the entire single-user CP-SAT model (variables, hard constraints,
carbon/peak/delay/cost terms) and adds, through the Phase 6 hooks:

  congestion   over[t] >= total[t] - target, minimized. The target is
               utilization * capacity, so the optimizer learns the difference
               between "clean" and "room to spare".
  inconvenience per-job normalized delay vs its ASAP start, aggregated per
               participant in AVG (sum) or MAX (minimax) fairness mode.

NORMALIZATION (all integer, documented here because the units must compose):

  delay_num[j] = sum_t (t - preferred_j) * power_j[t]      [watt-slots]
  D[j] * E_ref[j] >= delay_num[j] * MILLI, D[j] >= 0        [milli-slots]

  where E_ref is the job's reference energy in watt-slots (exact for atomic
  and interruptible, max-deliverable for thermal). D[j] is therefore "how many
  thousandths of this job's own energy moved one slot late" — comparable
  across a 7 kW EV and a 100 W fan.

  Both extra terms are converted to mean-carbon-equivalent units, the same
  convention as the peak term: one unit of congestion/inconvenience costs what
  an average-carbon slot of that size would. Weights stay unit-free.

Hard constraints are untouched: shared capacity is the SAME per-slot
constraint as single-user capacity, and every individual feasibility rule
still applies. Uncertainty still reaches only the objective.
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from ..domain.coordination import FairnessMode
from ..domain.loads import LoadType
from ..domain.scaling import to_objective_weight
from ..domain.scheduling import SolverInfo, SolverStatus
from .schedulers.cpsat import CPSATScheduler
from .schedulers.base import SchedulerName

#: delay granularity: thousandths of a slot-equivalent
MILLI = 1000


class CoordinatedCPSATScheduler(CPSATScheduler):
    """Joint optimization over every participant's jobs at once."""

    name = SchedulerName.CPSAT
    display_name = "COORDINATED_CPSAT"

    def __init__(
        self,
        preferred_starts: dict[str, int],
        target_w: int,
        congestion_weight: float = 1.0,
        inconvenience_weight: float = 1.0,
        fairness_mode: FairnessMode = FairnessMode.AVG,
        energy_ref_wslots: dict[str, int] | None = None,
        participant_of: dict[str, str] | None = None,
        priority_weight: dict[str, float] | None = None,
        max_inconvenience_millislots: dict[str, float] | None = None,
        target_profile_w: list[int] | None = None,
        config=None,
    ) -> None:
        super().__init__(config=config)
        self.preferred_starts = preferred_starts
        self.target_w = target_w
        self.target_profile_w = list(target_profile_w) if target_profile_w is not None else None
        self.congestion_weight = congestion_weight
        self.inconvenience_weight = inconvenience_weight
        self.fairness_mode = fairness_mode
        self.energy_ref_wslots = energy_ref_wslots or {}
        self.participant_of = participant_of or {}
        self.priority_weight = priority_weight or {}
        self.max_inconvenience = max_inconvenience_millislots or {}
        self._over_vars: dict[int, cp_model.IntVar] = {}
        self._delay_vars: dict[str, cp_model.IntVar] = {}
        self._fairness_var: cp_model.IntVar | None = None
        self._delay_bound = 1

    def solver_info(self, status: SolverStatus, elapsed_ms: int) -> SolverInfo:
        info = super().solver_info(status, elapsed_ms)
        info.name = self.display_name
        return info

    # -- helpers -----------------------------------------------------------

    def _power_expr(self, scheduler_input, job, slot, ctx):
        if job.job_type is LoadType.DEFERRABLE_ATOMIC:
            var = ctx["run_atomic"].get(job.id, {}).get(slot)
            return job.power_w * var if var is not None else 0
        if job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
            return ctx["pw_interruptible"].get(job.id, {}).get(slot, 0)
        if job.job_type is LoadType.THERMAL:
            return ctx["pw_thermal"].get(job.id, {}).get(slot, 0)
        return 0

    def _energy_ref(self, scheduler_input, job) -> int:
        if job.id in self.energy_ref_wslots:
            return max(1, self.energy_ref_wslots[job.id])
        window = max(1, job.window_slots())
        if job.job_type is LoadType.DEFERRABLE_ATOMIC:
            return max(1, job.power_w * (job.duration_slots or 1))
        if job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE and job.energy_required_wmin:
            return max(1, job.energy_required_wmin // max(1, job.slot_minutes))
        return max(1, job.max_power_w * window)

    # -- hooks ---------------------------------------------------------------

    def _target_at(self, slot: int) -> int:
        """Per-slot congestion target: profile entry when given, else scalar."""
        if self.target_profile_w is not None and 0 <= slot < len(self.target_profile_w):
            return self.target_profile_w[slot]
        return self.target_w

    def _extra_constraints(self, model, scheduler_input, ctx) -> None:
        n = ctx["n"]
        over = {}
        for slot in range(n):
            terms = self._flex_load_terms(
                scheduler_input,
                slot,
                ctx["run_atomic"],
                ctx["pw_interruptible"],
                ctx["pw_thermal"],
            )
            total = scheduler_input.baseline.at(slot) + (sum(terms) if terms else 0)
            cap = max(1, scheduler_input.capacity_at(slot))
            var = model.NewIntVar(0, cap, f"over_{slot}")
            # over >= total - target AND over >= 0: minimization pins it exact.
            model.Add(var >= total - self._target_at(slot))
            over[slot] = var
        self._over_vars = over

        # Per-job normalized delay D[j] (milli-slots), floored at 0: running
        # early is not inconvenience.
        for job in scheduler_input.jobs:
            if job.job_type is LoadType.FIXED:
                continue
            pref = self.preferred_starts.get(job.id, job.release_slot)
            delay_num = sum(
                (slot - pref) * self._power_expr(scheduler_input, job, slot, ctx)
                for slot in job.slots()
            )
            ref = self._energy_ref(scheduler_input, job)
            # Tight domain: |delay| <= window^2 * max_w, so D <= that * MILLI.
            # CP-SAT validates coefficient ranges in int64; a lazy 10**15 bound
            # times `ref` overflows and the model is rejected as MODEL_INVALID.
            window = max(1, job.window_slots())
            maxw = max(1, job.max_power_w)
            bound = (window * maxw * window * MILLI) // ref + 1
            var = model.NewIntVar(0, bound, f"delay_{job.id}")
            model.Add(var * ref >= delay_num * MILLI)
            self._delay_vars[job.id] = var
            self._delay_bound = max(getattr(self, "_delay_bound", 1), bound)

    def _extra_terms(self, model, scheduler_input, ctx) -> list:
        n = ctx["n"]
        slot_minutes = ctx["slot_minutes"]
        mean_carbon = max(1, int(round(scheduler_input.carbon.mean())))
        terms = []

        if self.congestion_weight > 0 and self._over_vars:
            # One watt over target for one slot costs what that watt-slot would
            # at mean carbon, divided by capacity so the weight is unit-free.
            # With a time-varying profile the mean per-slot capacity keeps the
            # same normalization instead of any single slot's value.
            if scheduler_input.capacity_profile_w:
                capacity = max(1, int(round(sum(scheduler_input.capacity_profile_w) / len(scheduler_input.capacity_profile_w))))
            else:
                capacity = max(1, scheduler_input.capacity_w)
            coef = max(
                1,
                to_objective_weight(self.congestion_weight)
                * mean_carbon
                * slot_minutes
                // capacity,
            )
            terms.append(coef * sum(self._over_vars[s] for s in range(n)))

        if self.inconvenience_weight > 0 and self._delay_vars:
            # One milli-slot of normalized delay costs what a slot of average
            # load would at mean carbon.
            coef = max(
                1,
                to_objective_weight(self.inconvenience_weight) * mean_carbon * slot_minutes // MILLI,
            )
            by_participant: dict[str, list] = {}
            for job in scheduler_input.jobs:
                var = self._delay_vars.get(job.id)
                if var is None:
                    continue
                pid = self.participant_of.get(job.id, "")
                # Priority as integer percent; folded into each variable's
                # constant coefficient so the model stays linear.
                w100 = max(1, int(round(self.priority_weight.get(pid, 1.0) * 100)))
                by_participant.setdefault(pid, []).append((var, w100))
            if self.fairness_mode is FairnessMode.MAX:
                # F never needs to exceed the largest single-job delay; a lazy
                # 10**12 domain times the objective coefficient overflows int64
                # and the model is rejected as MODEL_INVALID.
                fvar = model.NewIntVar(0, max(1, getattr(self, "_delay_bound", 1)), "max_inconvenience")
                for pid, items in by_participant.items():
                    count = max(1, len(items))
                    # F >= weighted mean delay: F * count * 100 >= sum(W * D).
                    model.Add(
                        fvar * count * 100
                        >= sum(w100 * v for v, w100 in items)
                    )
                self._fairness_var = fvar
                terms.append(coef * fvar)
            else:
                flat = [
                    (coef * w100) // 100 * v
                    for items in by_participant.values()
                    for v, w100 in items
                ]
                if flat:
                    terms.append(sum(flat))

            for pid, cap_slots in self.max_inconvenience.items():
                items = [
                    self._delay_vars[job.id]
                    for job in scheduler_input.jobs
                    if self.participant_of.get(job.id) == pid and job.id in self._delay_vars
                ]
                if items:
                    model.Add(sum(items) <= int(round(cap_slots * MILLI)) * max(1, len(items)))

        return terms
