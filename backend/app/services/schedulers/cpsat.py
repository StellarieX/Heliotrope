"""CP-SAT scheduler — the real optimization engine (Phase 4, §8-§22).

Genuine OR-Tools CP-SAT, not a custom heuristic wearing the name. Every
coefficient reaching a model variable is an integer produced by
`domain.scaling`; no floating-point constant is handed to the solver.

OBJECTIVE HIERARCHY (§18). The hierarchy is realized structurally rather than as
a hand-tuned weighting:

  1. FEASIBILITY   release, deadline, duration, contiguity, energy target,
                   minimum chunk, max power, capacity and thermal comfort are
                   HARD CONSTRAINTS (§19). They are not penalties that can be
                   traded away, so no deadline is ever sacrificed for a better
                   objective value — that is enforced by the model, not by
                   hoping a weight is small enough.
  2. CARBON        the default objective. Coefficients are exact integers.
  3. PEAK          opt-in, charged at the horizon's MEAN carbon intensity so the
                   weight is unit-free (§16, §17).
  4. DELAY/COST    opt-in, normalized the same way.

Because 1 is a constraint set and 2-4 are a single integer linear objective,
there is no weight value that can trade a hard constraint away.
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from ...domain.loads import LoadType
from ...domain.scaling import (
    RoundingEnvelope,
    assert_non_negative_band,
    to_objective_weight,
)
from ...domain.scheduling import (
    NormalizedJob,
    ScheduleStatus,
    SchedulerInput,
    SolverInfo,
    SolverStatus,
)
from ..carbon_accounting import Placement
from .base import BaseScheduler, PlacementFailure, SchedulerName

#: CP-SAT status names mapped onto our vocabulary. Anything not listed becomes
#: UNKNOWN rather than being optimistically rounded up to FEASIBLE.
_STATUS_MAP = {
    cp_model.OPTIMAL: SolverStatus.OPTIMAL,
    cp_model.FEASIBLE: SolverStatus.FEASIBLE,
    cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
    cp_model.MODEL_INVALID: SolverStatus.INTERNAL_ERROR,
}


#: a relative gap at or below this counts as "proved optimal" (floating-point noise)
_PROVEN_GAP = 1e-9


class CPSATScheduler(BaseScheduler):
    """Exact optimization over the canonical Phase 3 model."""

    name = SchedulerName.CPSAT

    def build_placement(self, scheduler_input: SchedulerInput) -> Placement:
        model = cp_model.CpModel()
        n = scheduler_input.horizon.slot_count
        slot_minutes = scheduler_input.horizon.slot_minutes
        weights = scheduler_input.objective

        # --- variables -------------------------------------------------------
        run_atomic: dict[str, dict[int, cp_model.IntVar]] = {}
        start_atomic: dict[str, dict[int, cp_model.IntVar]] = {}
        pw_interruptible: dict[str, dict[int, cp_model.IntVar]] = {}
        on_interruptible: dict[str, dict[int, cp_model.IntVar]] = {}
        pw_thermal: dict[str, dict[int, cp_model.IntVar]] = {}
        temp_thermal: dict[str, dict[int, cp_model.IntVar]] = {}

        terms = []

        for job in scheduler_input.jobs:
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                self._model_atomic(model, job, run_atomic, start_atomic)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                self._model_interruptible(
                    model, job, pw_interruptible, on_interruptible, start_atomic
                )
            elif job.job_type is LoadType.THERMAL:
                self._model_thermal(model, job, pw_thermal, temp_thermal)

        # --- connection capacity (§14, §5) ----------------------------------
        # One constraint per slot spanning EVERY flexible job type plus the
        # baseline headroom. This is the constraint most worth writing twice to
        # check.
        for slot in range(n):
            terms_for_slot = self._flex_load_terms(
                scheduler_input, slot, run_atomic, pw_interruptible, pw_thermal
            )
            if terms_for_slot:
                headroom = scheduler_input.headroom_w(slot)
                model.Add(sum(terms_for_slot) <= headroom)

        # --- coordination hooks (Phase 6) ------------------------------------
        # No-ops on the single-user path: the shared capacity constraint above
        # is unchanged, and `terms` gains nothing. A coordinated scheduler
        # overrides these to add congestion/fairness structure around the same
        # variables, rather than re-implementing the model.
        ctx = {
            "run_atomic": run_atomic,
            "start_atomic": start_atomic,
            "pw_interruptible": pw_interruptible,
            "on_interruptible": on_interruptible,
            "pw_thermal": pw_thermal,
            "temp_thermal": temp_thermal,
            "n": n,
            "slot_minutes": slot_minutes,
        }
        self._extra_constraints(model, scheduler_input, ctx)

        # --- objective (§15, §16, §17) -------------------------------------
        carbon_weight = to_objective_weight(weights.carbon)
        if carbon_weight:
            terms.append(carbon_weight * self._carbon_term(scheduler_input, run_atomic, pw_interruptible, pw_thermal, slot_minutes))

        if weights.peak:
            terms.append(
                to_objective_weight(weights.peak) * self._peak_term(model, scheduler_input, run_atomic, pw_interruptible, pw_thermal, n, slot_minutes)
            )

        if weights.delay:
            terms.append(
                to_objective_weight(weights.delay) * self._delay_term(
                    scheduler_input, run_atomic, start_atomic, pw_interruptible, on_interruptible, pw_thermal, slot_minutes
                )
            )

        if weights.cost and scheduler_input.tariff is not None:
            terms.append(
                to_objective_weight(weights.cost) * self._cost_term(
                    scheduler_input, run_atomic, pw_interruptible, pw_thermal, slot_minutes
                )
            )

        if not terms:
            # Pure feasibility: the model still needs SOME objective or CP-SAT
            # will happily return any solution. Zero is correct here.
            terms.append(0)

        terms.extend(self._extra_terms(model, scheduler_input, ctx))
        model.Minimize(sum(terms))
        self._last_model = model

        # --- warm start (advisory; see `_apply_hints`) -----------------------
        hinted = self._apply_hints(
            model, scheduler_input, run_atomic, start_atomic,
            pw_interruptible, on_interruptible, pw_thermal,
        )

        # --- solve (§21, §22) ------------------------------------------------
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = self.config.time_limit_seconds
        solver.parameters.num_workers = self.config.num_workers
        solver.parameters.random_seed = self.config.random_seed
        solver.parameters.relative_gap_limit = self.config.relative_gap_limit
        if hinted:
            # After a rolling-horizon shift part of the old schedule no longer fits
            # (its early slots are in the past). Let the solver repair the hint
            # instead of discarding it.
            solver.parameters.repair_hint = True
            solver.parameters.hint_conflict_limit = 50
        status_code = solver.Solve(model)

        status = _STATUS_MAP.get(status_code, SolverStatus.UNKNOWN)

        if status is SolverStatus.INFEASIBLE:
            raise PlacementFailure(
                job_id="",
                reason=(
                    "CP-SAT proved that no schedule satisfies the hard constraints "
                    "for this input. The preflight checks passed, so the conflict is "
                    "between jobs competing for capacity, not a single impossible job."
                ),
            )
        if status in (SolverStatus.UNKNOWN, SolverStatus.INTERNAL_ERROR):
            # A timeout (or an invalid model) with no solution is NOT proof of
            # infeasibility: the schedule might exist and the solver simply did
            # not find it in time. Report UNKNOWN/INTERNAL_ERROR so the caller
            # does not read "infeasible" as "impossible".
            result_status = (
                ScheduleStatus.UNKNOWN
                if status is SolverStatus.UNKNOWN
                else ScheduleStatus.INTERNAL_ERROR
            )
            raise PlacementFailure(
                job_id="",
                reason=(
                    f"CP-SAT stopped with status {solver.StatusName(status_code)} without "
                    f"finding a feasible solution within {self.config.time_limit_seconds}s. "
                    "No schedule was produced."
                ),
                status=result_status,
            )

        # CP-SAT also answers OPTIMAL when it stops at `relative_gap_limit`. This engine
        # only calls a schedule OPTIMAL when optimality was PROVED: a result that stopped
        # inside the tolerance is FEASIBLE, with the real gap reported on the result.
        if status is SolverStatus.OPTIMAL and self._relative_gap(solver) > _PROVEN_GAP:
            status = SolverStatus.FEASIBLE
        self._status = status
        # Only a run that actually extracted a placement may publish the raw
        # solver's objective/bound/gap. This used to be assigned before the two
        # raises above, which let `schedule()` overwrite the honest INFEASIBLE /
        # UNKNOWN report — already derived from `PlacementFailure.status` by the
        # shared template — with the solved-but-unsatisfiable model's numbers:
        # status UNKNOWN, objective 0.0, bound 0.0, gap 0.0, describing a
        # solution that does not exist. A proved-infeasible must read
        # INFEASIBLE, not "ran out of time".
        self._last_solver = solver
        return self._extract(scheduler_input, run_atomic, pw_interruptible, pw_thermal)

    # --- variable construction ---------------------------------------------

    @staticmethod
    def _flex_load_terms(scheduler_input, slot, run_atomic, pw_interruptible, pw_thermal) -> list:
        """Per-slot flexible load terms in watts. Factored out of the capacity
        loop so coordination hooks build the identical expression (Phase 6)."""
        terms = []
        for job in scheduler_input.jobs:
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                var = run_atomic.get(job.id, {}).get(slot)
                if var is not None:
                    terms.append(job.power_w * var)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                var = pw_interruptible.get(job.id, {}).get(slot)
                if var is not None:
                    terms.append(var)
            elif job.job_type is LoadType.THERMAL:
                var = pw_thermal.get(job.id, {}).get(slot)
                if var is not None:
                    terms.append(var)
        return terms

    def _extra_constraints(self, model, scheduler_input, ctx) -> None:
        """Phase 6 hook. Single-user path: nothing beyond shared capacity."""

    def _extra_terms(self, model, scheduler_input, ctx) -> list:
        """Phase 6 hook. Single-user path: no extra objective terms."""
        return []

    def _apply_hints(
        self,
        model,
        scheduler_input,
        run_atomic,
        start_atomic,
        pw_interruptible,
        on_interruptible,
        pw_thermal,
    ) -> int:
        """Warm-start CP-SAT from an earlier schedule. Returns how many variables were hinted.

        Hints are ADVISORY. They add no constraint, so they cannot change which
        schedules are feasible or which one is optimal; they only tell the search where
        to start. A hint that no longer fits (the usual case after a rolling-horizon
        shift) is repaired by the solver (`repair_hint`), never trusted.

        Hints are keyed by TIMESTAMP, not slot index, and mapped onto the current
        horizon here, so they survive the horizon moving. Placements that now fall
        outside the horizon, and jobs that are not in this problem, are ignored.

        A job is hinted only if the hint tells us something about it: a hint of "no
        start at all" for an atomic job would contradict the model's own "exactly one
        start", so such a job is skipped rather than given a self-contradictory hint.
        """
        hints = scheduler_input.hints
        if not hints:
            return 0
        horizon = scheduler_input.horizon
        hinted = 0

        for job in scheduler_input.jobs:
            history = hints.get(job.id)
            if not history:
                continue
            power_by_slot: dict[int, int] = {}
            for timestamp, power in history:
                slot = horizon.index_of(timestamp)
                if 0 <= slot < horizon.slot_count:
                    power_by_slot[slot] = int(power)

            window = range(job.release_slot, job.deadline_slot)
            active = [t for t in window if power_by_slot.get(t, 0) > 0]

            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                if not active:
                    continue
                first = active[0]
                for t, var in start_atomic.get(job.id, {}).items():
                    model.AddHint(var, 1 if t == first else 0)
                    hinted += 1
                for t, var in run_atomic.get(job.id, {}).items():
                    model.AddHint(var, 1 if power_by_slot.get(t, 0) > 0 else 0)
                    hinted += 1

            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                if not active:
                    continue
                for t, var in on_interruptible.get(job.id, {}).items():
                    model.AddHint(var, 1 if power_by_slot.get(t, 0) > 0 else 0)
                    hinted += 1
                for t, var in pw_interruptible.get(job.id, {}).items():
                    model.AddHint(var, min(power_by_slot.get(t, 0), job.max_power_w))
                    hinted += 1
                # Minimum-chunk start flags: a start is "on now, off just before".
                for t, var in start_atomic.get(job.id, {}).items():
                    now_on = power_by_slot.get(t, 0) > 0
                    was_on = power_by_slot.get(t - 1, 0) > 0
                    model.AddHint(var, 1 if now_on and not was_on else 0)
                    hinted += 1

            elif job.job_type is LoadType.THERMAL:
                if not active:
                    continue
                for t, var in pw_thermal.get(job.id, {}).items():
                    model.AddHint(var, power_by_slot.get(t, 0))
                    hinted += 1

        return hinted

    @staticmethod
    def _relative_gap(solver) -> float:
        """(objective - best bound) / |objective|; 0 when the bound is tight."""
        objective = solver.ObjectiveValue()
        bound = solver.BestObjectiveBound()
        if objective == 0:
            return 0.0 if bound == 0 else float("inf")
        return max(0.0, (objective - bound) / abs(objective))

    def _model_atomic(self, model, job, run_atomic, start_atomic) -> None:
        """§10: exactly one binary start, then a contiguous run.

        `run[j,t]` is the exact OR of the starts that cover t. Together with
        `sum(start) == 1` this guarantees the job occupies exactly `duration`
        contiguous slots and can never partially execute.
        """
        duration = job.duration_slots or 1
        last_start = job.deadline_slot - duration
        starts = {
            s: model.NewBoolVar(f"start_{job.id}_{s}")
            for s in range(job.release_slot, last_start + 1)
        }
        model.Add(sum(starts.values()) == 1)
        start_atomic[job.id] = starts

        run = {
            t: model.NewBoolVar(f"run_{job.id}_{t}") for t in job.slots()
        }
        for s, var in starts.items():
            for offset in range(duration):
                t = s + offset
                if t in run:
                    model.Add(run[t] >= var)
        for t, var in run.items():
            covering = [starts[s] for s in starts if s <= t < s + duration]
            if covering:
                model.Add(var <= sum(covering))
        run_atomic[job.id] = run

    def _model_interruptible(self, model, job, pw_interruptible, on_interruptible, start_atomic) -> None:
        """§11, §12: per-slot power with an exact minimum-chunk guarantee.

        MINIMUM CHUNK. `on[t]` is exactly "this slot draws power", and `start[t]`
        is exactly "the job switched on at t" (`on[t] and not on[t-1]`).

        THE HARD CONSTRAINT (§12) is

            start[t] => on[t .. t+n-1]

        written as `sum(on[t .. min(t+n-1, end)]) >= min(n, end - t) * start[t]`.
        This is the part that actually bites: a start obliges the job to stay on
        for the whole chunk, so an isolated short burst is not a solution at all.

        THE AGGREGATED INEQUALITY

            on[t] <= sum(start[k] for k in [t-n+1 .. t]) + on[t-n]

        is ALSO added, but on its own it is INSUFFICIENT, and shipping only that
        one was a real bug. It reads "an active slot must be justified by a
        recent start, or be far enough into an existing run". For a run that
        starts at s and is shorter than n, the slot s itself is always justified
        by `start[s]`, because s lies inside the very window being checked — so
        the inequality is satisfied by a two-slot burst with n=4 and never
        forbids it. It is a valid *redundant* inequality (it excludes no legal
        schedule, which is why the answer stays correct) and it is kept because
        it propagates well, but it must not be mistaken for the guarantee.

        Proof the aggregate alone is not enough, with n=4 and a run over slots
        {2, 3}: at t=3 the justification window is [0, 3] and contains
        `start[2] = 1`, so the inequality holds; likewise at t=2. No constraint is
        violated, and the two-slot run is accepted. The test suite pins this.
        """
        window = list(job.slots())
        pw = {
            t: model.NewIntVar(0, job.max_power_w, f"pw_{job.id}_{t}") for t in window
        }
        on = {t: model.NewBoolVar(f"on_{job.id}_{t}") for t in window}
        for t in window:
            model.Add(pw[t] <= job.max_power_w * on[t])
            model.Add(pw[t] >= 1 * on[t])  # on <-> pw > 0

        model.Add(
            sum(pw[t] * job.slot_minutes for t in window) >= (job.energy_required_wmin or 0)
        )
        pw_interruptible[job.id] = pw
        on_interruptible[job.id] = on

        n = job.min_chunk_slots
        if n > 1 and window:
            starts = {
                t: model.NewBoolVar(f"pstart_{job.id}_{t}") for t in window
            }
            for t in window:
                previous = on.get(t - 1)
                model.Add(starts[t] >= on[t] - (previous if previous is not None else 0))
                model.Add(starts[t] <= on[t])
            start_atomic[job.id] = starts

            for t in window:
                # redundant but strongly propagating; see the docstring
                justification = [starts[k] for k in window if t - n + 1 <= k <= t]
                long_enough = on.get(t - n)
                if long_enough is not None:
                    model.Add(on[t] <= sum(justification) + long_enough)
                else:
                    model.Add(on[t] <= sum(justification))

            # the actual minimum-chunk guarantee: a start buys `n` on-slots.
            # A start with fewer than `n` slots left in the window cannot buy a
            # full chunk, so it is forbidden outright: accepting it would leave
            # a short tail run that the independent validator (rightly) rejects
            # as a minimum-chunk violation.
            window_set = set(window)
            for t in window:
                chunk = [on[k] for k in range(t, t + n) if k in window_set]
                if len(chunk) < n:
                    model.Add(starts[t] == 0)
                else:
                    model.Add(sum(chunk) >= len(chunk) * starts[t])

    def _model_thermal(self, model, job, pw_thermal, temp_thermal) -> None:
        """§13: power variables plus thermal-state variables.

        The recurrence is a ROUNDED division, and that is the whole difficulty.
        The physical step is

            T[t+1] = round(a * T[t] + b * P_w[t] + c)

        and the natural integer encoding of that — a single equality

            1000 * T[t+1] == a_permille*T[t] + b_scaled_w*P_w[t] + c_micro

        — is wrong in two separate ways:

        1. CORRECTNESS. An equality requires the right-hand side to be exactly
           divisible by 1000. The real recurrence rounds, so it is perfectly
           legal for the power draw to leave a remainder. The equality silently
           deletes those power levels, reporting a genuinely feasible geyser
           trajectory as infeasible. A brute-force comparison in the test suite
           shows the equality model rejecting schedules that
           `next_temperature_milli_from_watts` accepts.

        2. PROPAGATION. The divisibility requirement is a modular condition
           smuggled inside a linear equality. The LP relaxation cannot see it,
           so the search has to discover it by branching. On a 52-slot geyser
           window this alone took the solver from 0.02 s to "no solution in
           30 s".

        `RoundingEnvelope` fixes both: it expresses "rounded division" as a pair
        of linear inequalities that admit exactly the same single integer value
        the Python helper computes, but as inequalities the LP relaxation can
        propagate. Same solutions, and orders of magnitude faster.

        The comfort band is applied to EVERY state, not just the endpoints, so
        the optimizer cannot dip below the floor for one slot to buy cheap
        carbon.

        STATE COUNT. `temp[t]` is the temperature at the START of slot t, so a
        window of N slots needs N+1 states: the N start states plus `end`, the
        state after the final slot's power has been applied. This matters. With
        only N states the last slot's power would never be applied, and the
        service target would be checked one decay step too early — the model
        would happily return a trajectory that misses the target, and the
        independent validator would reject it. `ScheduleValidator` and the
        heuristic `_simulate_thermal` both apply power in EVERY slot and check
        the target on the resulting end state, so the model must agree with
        them exactly.
        """
        scale = job.thermal
        window = list(job.slots())
        if scale is None or not window:
            return

        assert_non_negative_band(scale.min_milli, f"job {job.id!r}")

        pw = {
            t: model.NewIntVar(0, scale.max_power_millikw, f"tpw_{job.id}_{t}")
            for t in window
        }
        temp = {
            t: model.NewIntVar(scale.min_milli, scale.max_milli, f"T_{job.id}_{t}")
            for t in window
        }
        end_state = model.NewIntVar(
            scale.min_milli, scale.max_milli, f"T_{job.id}_end"
        )
        pw_thermal[job.id] = pw
        temp_thermal[job.id] = temp

        model.Add(temp[window[0]] == scale.initial_milli)

        envelope = RoundingEnvelope.for_rounded_half_away()
        for previous, current in zip(window, window[1:]):
            numerator = (
                scale.a_permille * temp[previous]
                + scale.b_scaled_w * pw[previous]
                + scale.c_micro
            )
            model.Add(temp[current] * envelope.denominator >= envelope.lower_bound(numerator))
            model.Add(temp[current] * envelope.denominator <= envelope.upper_bound(numerator))

        # The final slot heats too, and the target is judged on the result.
        final_numerator = (
            scale.a_permille * temp[window[-1]]
            + scale.b_scaled_w * pw[window[-1]]
            + scale.c_micro
        )
        model.Add(end_state * envelope.denominator >= envelope.lower_bound(final_numerator))
        model.Add(end_state * envelope.denominator <= envelope.upper_bound(final_numerator))

        if scale.target_milli is not None:
            model.Add(end_state >= scale.target_milli)

    # --- objective terms ----------------------------------------------------

    def _carbon_term(
        self, scheduler_input, run_atomic, pw_interruptible, pw_thermal, slot_minutes
    ):
        """§15: sum over slots of intensity x load x slot_hours, in integers.

        §22: the coefficients come from `objective_carbon()`, not `carbon`. In
        ACTUAL mode that is the same profile; in EXPECTED mode it is the point
        forecast; in ROBUST mode it is the risk-adjusted intensity. This is the
        ONLY place carbon enters the objective, so all three modes are reachable
        without touching the model, and the hard constraints below it are
        untouched by any of them (§19).
        """
        carbon = scheduler_input.objective_carbon()
        total = []
        for job in scheduler_input.jobs:
            coefficient_base = slot_minutes
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                for t, var in run_atomic.get(job.id, {}).items():
                    coefficient = carbon.at(t) * coefficient_base * job.power_w
                    if coefficient:
                        total.append(coefficient * var)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                for t, var in pw_interruptible.get(job.id, {}).items():
                    coefficient = carbon.at(t) * coefficient_base
                    if coefficient:
                        total.append(coefficient * var)
            elif job.job_type is LoadType.THERMAL:
                for t, var in pw_thermal.get(job.id, {}).items():
                    coefficient = carbon.at(t) * coefficient_base
                    if coefficient:
                        total.append(coefficient * var)
        return sum(total) if total else 0

    def _peak_term(
        self, model, scheduler_input, run_atomic, pw_interruptible, pw_thermal, n, slot_minutes
    ):
        """§17: P_peak >= total load in every slot, minimized when weighted.

        The coefficient is scaled by the horizon's MEAN carbon intensity so a
        peak weight of 1.0 means "one average-carbon hour of peak", keeping it
        comparable with the carbon term instead of dwarfing it.
        """
        mean_carbon = int(round(scheduler_input.carbon.mean()))
        peak_cap = scheduler_input.capacity_w
        if scheduler_input.capacity_profile_w:
            peak_cap = max(peak_cap, max(scheduler_input.capacity_profile_w))
        peak = model.NewIntVar(0, peak_cap, "P_peak")
        for slot in range(n):
            terms = []
            total = scheduler_input.baseline.at(slot)
            for job in scheduler_input.jobs:
                if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                    var = run_atomic.get(job.id, {}).get(slot)
                    if var is not None:
                        terms.append(job.power_w * var)
                elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                    var = pw_interruptible.get(job.id, {}).get(slot)
                    if var is not None:
                        terms.append(var)
                elif job.job_type is LoadType.THERMAL:
                    var = pw_thermal.get(job.id, {}).get(slot)
                    if var is not None:
                        terms.append(var)
            total_expr = total + (sum(terms) if terms else 0)
            if total_expr == 0:
                continue
            model.Add(peak >= total_expr)
        return peak * mean_carbon * slot_minutes

    def _delay_term(
        self, scheduler_input, run_atomic, start_atomic, pw_interruptible, on_interruptible, pw_thermal, slot_minutes
    ):
        """Charge each slot of lateness as if the job simply ran longer.

        Delay is power-weighted lateness: every watt drawn `d` slots after
        release costs what that watt-slot would at mean carbon. The sum is
        always non-negative and grows monotonically the later power is drawn,
        so the optimizer can never reduce its "delay" penalty by extending a
        run or by shifting power later. (A difference-of-consecutive-slots
        formulation telescopes and can go negative, rewarding longer runs —
        exactly backwards.)
        """
        reference = int(round(scheduler_input.carbon.mean()))
        total = []
        for job in scheduler_input.jobs:
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                for s, var in start_atomic.get(job.id, {}).items():
                    total.append((s - job.release_slot) * reference * slot_minutes * job.power_w * var)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                for t, var in pw_interruptible.get(job.id, {}).items():
                    total.append((t - job.release_slot) * reference * slot_minutes * var)
            elif job.job_type is LoadType.THERMAL:
                for t, var in pw_thermal.get(job.id, {}).items():
                    total.append((t - job.release_slot) * reference * slot_minutes * var)
        return sum(total) if total else 0

    def _cost_term(self, scheduler_input, run_atomic, pw_interruptible, pw_thermal, slot_minutes):
        """§27: money is tracked separately from carbon, never mixed in."""
        tariff = scheduler_input.tariff
        prices = tariff.price_micro_per_kwh if tariff else []
        total = []
        for job in scheduler_input.jobs:
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                for t, var in run_atomic.get(job.id, {}).items():
                    price = prices[t] if t < len(prices) else 0
                    if price:
                        total.append(price * slot_minutes * job.power_w * var)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                for t, var in pw_interruptible.get(job.id, {}).items():
                    price = prices[t] if t < len(prices) else 0
                    if price:
                        total.append(price * slot_minutes * var)
            elif job.job_type is LoadType.THERMAL:
                for t, var in pw_thermal.get(job.id, {}).items():
                    price = prices[t] if t < len(prices) else 0
                    if price:
                        total.append(price * slot_minutes * var)
        return sum(total) if total else 0

    # --- solution extraction ------------------------------------------------

    def _extract(self, scheduler_input, run_atomic, pw_interruptible, pw_thermal) -> Placement:
        solver = self._last_solver
        placement = Placement(slot_count=scheduler_input.horizon.slot_count)
        for job in scheduler_input.jobs:
            if job.job_type is LoadType.DEFERRABLE_ATOMIC:
                for t, var in run_atomic.get(job.id, {}).items():
                    if solver.Value(var):
                        placement.set(job.id, t, job.power_w)
            elif job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
                for t, var in pw_interruptible.get(job.id, {}).items():
                    value = solver.Value(var)
                    if value > 0:
                        placement.set(job.id, t, value)
            elif job.job_type is LoadType.THERMAL:
                for t, var in pw_thermal.get(job.id, {}).items():
                    value = solver.Value(var)
                    if value > 0:
                        placement.set(job.id, t, value)
        return placement

    # --- result overrides ---------------------------------------------------

    def schedule(self, scheduler_input: SchedulerInput):
        """CP-SAT needs the raw solver status and objective, so it overrides the
        shared template's bookkeeping while reusing all of its validation."""
        self._status = SolverStatus.UNKNOWN
        # The engines are process-wide singletons: a previous run's solver and
        # model must not leak into this one. Without the reset, a preflight
        # failure (which returns before solving) would still find a stale
        # `_last_solver` below and overwrite the honest INFEASIBLE report with
        # leftover objective numbers from an unrelated run.
        self._last_solver = None
        self._last_model = None
        result = super().schedule(scheduler_input)
        solver = getattr(self, "_last_solver", None)
        if solver is not None:
            status = self._status
            info = SolverInfo(
                name=self.name.value,
                status=status,
                solve_time_ms=result.metrics.solve_time_ms,
                objective_value=round(solver.ObjectiveValue(), 6),
                best_bound=round(solver.BestObjectiveBound(), 6),
                optimality_gap=round(max(0.0, solver.ObjectiveValue() - solver.BestObjectiveBound()), 6),
                relative_gap=round(self._relative_gap(solver), 9),
                num_workers=self.config.num_workers,
                random_seed=self.config.random_seed,
                time_limit_seconds=self.config.time_limit_seconds,
                is_optimal=status is SolverStatus.OPTIMAL,
            )
            result.solver = info
            if status is SolverStatus.OPTIMAL:
                result.status = ScheduleStatus.OPTIMAL
        return result


def scale_max(job: NormalizedJob) -> int:
    """Maximum watts a thermal job can draw, for the delay coefficient."""
    if job.thermal is None:
        return job.power_w
    # 1 milli-kW == 1 W, so this is already the watt ceiling
    return job.thermal.max_power_millikw
