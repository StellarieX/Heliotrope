"""SchedulerService — orchestration, counterfactuals and comparison (§24, §31, §32, §33).

This is the facade the API talks to. It owns three responsibilities that would
otherwise leak into the routes:

  * turning `LoadSpec`s plus a carbon signal into one `SchedulerInput` that is
    then shared, UNMODIFIED, by every scheduler being run (§42)
  * running the ASAP counterfactual so every savings number has a real baseline
    to be measured against (§32)
  * filling in the per-job explanations from that comparison (§31, §51)

WHY THE COUNTERFUAL IS ASAP AND NOT "ZERO". Comparing an optimized schedule
against a fictional empty schedule would report savings for load that would have
happened anyway. ASAP is what actually happens with no optimization, so
`co2_before` means "what this load emits under the no-optimization baseline".
"""

from __future__ import annotations

import copy
from datetime import datetime
from contextlib import contextmanager
from typing import Iterator, Optional

from ..domain.carbon import CarbonSignal
from ..domain.forecasting import ForecastConfig
from ..domain.horizon import SchedulingHorizon
from ..domain.loads import LoadSpec, LoadType
from ..core import config as app_config
from ..domain.scheduling import (
    ObjectiveWeights,
    ReasonCode,
    ScheduleStatus,
    SchedulerComparison,
    SchedulerConfig,
    SchedulerInput,
    SchedulerResult,
    TimeOfUseTariff,
)
from .carbon_accounting import CarbonAccountingService, Placement
from .schedulers import SCHEDULERS, BaseScheduler, SchedulerName
from .scheduler_normalizer import SchedulerNormalizer, fingerprint_input


@contextmanager
def _scoped_engine(
    engine: BaseScheduler, config: Optional[SchedulerConfig]
) -> Iterator[BaseScheduler]:
    """The engine to run this call with.

    `SCHEDULERS` holds process-wide singletons, and the engines read
    `self.config` deep inside the shared template while FastAPI runs sync
    endpoints in a threadpool. Assigning a caller's config onto the shared
    instance therefore let one request's `time_limit_seconds` or `num_workers`
    replace another's mid-solve. Measured on this code path: 5 of 10 concurrent
    requests solved with a foreign config, including one that asked for 600 s
    and was aborted at 0.2 s, and one that asked for 0.2 s and ran with 600 s.

    A shallow copy gives the call its own `config` — and, with it, its own
    `_status` and `_last_solver`, so a concurrent run cannot extract a placement
    with another run's CpSolver — while sharing the stateless collaborators.
    Nothing is mutated on the singleton, so concurrent solves are isolated by
    construction rather than by lock ordering.
    """
    if config is None:
        yield engine
        return
    scoped = copy.copy(engine)
    scoped.config = config
    yield scoped


def with_default_gap(config: Optional[SchedulerConfig]) -> Optional[SchedulerConfig]:
    """Apply the deployment's gap tolerance (SOLVER_RELATIVE_GAP) unless the caller chose one."""
    gap = app_config.SOLVER_RELATIVE_GAP
    if gap <= 0:
        return config
    if config is None:
        return SchedulerConfig(relative_gap_limit=gap)
    if "relative_gap_limit" in config.model_fields_set:
        return config
    return config.model_copy(update={"relative_gap_limit": gap})


class SchedulerService:
    """One entry point for scheduling, comparison and explanation."""

    def __init__(self) -> None:
        self.normalizer = SchedulerNormalizer()
        self.accounting = CarbonAccountingService()

    # --- input --------------------------------------------------------------

    def build_input(
        self,
        specs: list[LoadSpec],
        carbon_signal: CarbonSignal,
        capacity_kw: float,
        objective: Optional[ObjectiveWeights] = None,
        horizon: Optional[SchedulingHorizon] = None,
        tariff: Optional[TimeOfUseTariff] = None,
        forecast_config: Optional[ForecastConfig] = None,
        uncertainty_upper: Optional[list[int]] = None,
        forecast_provenance: Optional[dict] = None,
        capacity_profile_kw: Optional[list[float]] = None,
        hints: Optional[dict[str, list[tuple[datetime, int]]]] = None,
    ) -> tuple[SchedulerInput, list[str]]:
        """Normalize jobs + a carbon signal into the one shared `SchedulerInput`.

        §38: `forecast_config` defaults to ACTUAL, so every existing caller — and
        every Phase 4 test — gets exactly the deterministic behaviour it had
        before. Forecast uncertainty only enters when a caller asks for it.

        `capacity_profile_kw`, when given, overrides the scalar capacity slot by
        slot; a length mismatch is a `NormalizationError` (a 422 at the route).
        """
        config = forecast_config or ForecastConfig()
        scheduler_input, report = self.normalizer.normalize(
            specs,
            carbon_signal,
            capacity_kw,
            horizon=horizon,
            objective=objective,
            tariff=tariff,
            forecast_mode=config.forecast_mode.value,
            risk_weight=config.risk_weight,
            uncertainty_upper=uncertainty_upper,
            forecast_provenance=forecast_provenance,
            deadline_buffer_minutes=config.deadline_buffer_minutes,
            capacity_profile_kw=capacity_profile_kw,
            hints=hints,
        )
        return scheduler_input, report.warnings

    # --- single run ---------------------------------------------------------

    def run(
        self,
        scheduler_input: SchedulerInput,
        scheduler: SchedulerName,
        config: Optional[SchedulerConfig] = None,
        explain: bool = True,
    ) -> SchedulerResult:
        """Run one scheduler and, optionally, attach counterfactual explanations."""
        engine = SCHEDULERS[scheduler]
        config = with_default_gap(config)
        # `SCHEDULERS` holds process-wide singletons, so config must never be
        # written onto them: see `_scoped_engine` for what that cost us.
        with _scoped_engine(engine, config) as scoped:
            result = scoped.schedule(scheduler_input)
        if explain:
            self.attach_explanations(scheduler_input, result)
        return result

    def extract_hints_from_result(self, result: SchedulerResult) -> dict[str, list[tuple[datetime, int]]]:
        """Solver hints from a finished schedule, for warm-starting the next run.

        Keyed by timestamp rather than slot index so the hints still mean the same
        thing when the horizon has moved (rolling-horizon replanning).
        """
        return {
            scheduled.job_id: [(a.timestamp, a.power_w) for a in scheduled.allocations]
            for scheduled in result.schedule
        }

    # --- comparison (§24) ---------------------------------------------------

    def compare(
        self,
        scheduler_input: SchedulerInput,
        schedulers: Optional[list[SchedulerName]] = None,
        config: Optional[SchedulerConfig] = None,
    ) -> SchedulerComparison:
        """Run several schedulers over byte-identical input.

        The same `SchedulerInput` object is handed to every engine untouched.
        `input_fingerprint` records what they all saw, so this claim is
        verifiable rather than asserted (§42).
        """
        names = schedulers or [SchedulerName.ASAP, SchedulerName.GREEDY, SchedulerName.CPSAT]
        comparison = SchedulerComparison(
            reference_scheduler=SchedulerName.ASAP.value,
            input_fingerprint=fingerprint_input(scheduler_input),
        )

        asap_result: Optional[SchedulerResult] = None
        for name in names:
            result = self.run(scheduler_input, name, config=config, explain=False)
            comparison.results[name.value] = result
            if name is SchedulerName.ASAP:
                asap_result = result

        if asap_result is not None:
            self.attach_explanations(scheduler_input, asap_result, counterfactual=asap_result)

        reference_peak = (
            asap_result.metrics.peak_kw if asap_result else None
        )
        reference_cost = (
            asap_result.metrics.energy_cost if asap_result else None
        )

        for name, result in comparison.results.items():
            if asap_result is not None and result is not asap_result:
                saved, percent = savings_from_metrics(
                    asap_result.metrics.total_co2_kg, result.metrics.total_co2_kg
                )
                result.metrics.co2_saved_kg = saved
                result.metrics.co2_saved_percent = percent
                if reference_peak is not None and result.metrics.peak_kw is not None:
                    result.metrics.peak_reduction_kw = reference_peak - result.metrics.peak_kw
                if reference_cost is not None and result.metrics.energy_cost is not None:
                    result.metrics.cost_delta = result.metrics.energy_cost - reference_cost
                comparison.co2_saved_vs_reference[name] = saved
                comparison.peak_reduction[name] = (
                    reference_peak - result.metrics.peak_kw
                    if reference_peak is not None and result.metrics.peak_kw is not None
                    else None
                )
                comparison.cost_delta[name] = result.metrics.cost_delta
            self.attach_explanations(scheduler_input, result, counterfactual=asap_result)

        comparison.best_co2_scheduler = self._best_co2(comparison)
        return comparison

    def _best_co2(self, comparison: SchedulerComparison) -> Optional[str]:
        """Lowest-carbon FEASIBLE result. Never names an infeasible run."""
        best_name = None
        best_value = None
        for name, result in comparison.results.items():
            if result.status not in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL):
                continue
            value = result.metrics.total_co2_kg
            if value is None:
                continue
            if best_value is None or value < best_value - 1e-12:
                best_value = value
                best_name = name
        return best_name

    # --- explanations (§31, §51, §52) ---------------------------------------

    def attach_explanations(
        self,
        scheduler_input: SchedulerInput,
        result: SchedulerResult,
        counterfactual: Optional[SchedulerResult] = None,
    ) -> SchedulerResult:
        """Fill per-job reasons and per-job CO2 savings from real accounting.

        When the result being explained IS the ASAP baseline, its own placement
        is the counterfactual and every job correctly reports zero savings —
        which is the honest answer for a baseline.
        """
        if result.status not in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL):
            return result

        if counterfactual is None or counterfactual is result:
            baseline_allocation = self._allocation_from(result, scheduler_input)
        else:
            baseline_allocation = self._allocation_from(counterfactual, scheduler_input)

        current_allocation = self._allocation_from(result, scheduler_input)

        horizon = scheduler_input.horizon
        for explanation in result.explanations:
            job = scheduler_input.job(explanation.job_id)
            before = self.accounting.job_accounting(
                scheduler_input, job.id, baseline_allocation
            )
            after = self.accounting.job_accounting(
                scheduler_input, job.id, current_allocation
            )
            explanation.co2_before_kg = before["co2_kg"]
            explanation.co2_after_kg = after["co2_kg"]
            explanation.co2_saved_kg = before["co2_kg"] - after["co2_kg"]
            explanation.energy_kwh = after["energy_kwh"]
            deadline_time = (
                horizon.end
                if job.deadline_slot >= horizon.slot_count
                else horizon.slot_start(job.deadline_slot)
            )
            explanation.deadline_preserved = explanation.scheduled_end <= deadline_time
            code, reason = self._reason_for(
                scheduler_input, job, result, before, after, explanation
            )
            explanation.reason_code = code
            explanation.reason = reason

        by_id = {j.job_id: j for j in result.schedule}
        for explanation in result.explanations:
            placed = by_id.get(explanation.job_id)
            if placed is not None:
                placed.reason_code = explanation.reason_code.value
                placed.reason = explanation.reason
        return result

    def _allocation_from(
        self, result: SchedulerResult, scheduler_input: SchedulerInput
    ) -> Placement:
        """Rebuild a placement from a result's own allocations."""
        placement = Placement(slot_count=scheduler_input.horizon.slot_count)
        for scheduled in result.schedule:
            for allocation in scheduled.allocations:
                placement.set(scheduled.job_id, allocation.slot, allocation.power_w)
        return placement

    def _reason_for(self, scheduler_input, job, result, before, after, explanation):
        """Pick a structured reason code and a sentence from ACTUAL numbers."""
        deadline = explanation.deadline_at
        start = explanation.scheduled_start
        saved = explanation.co2_saved_kg

        if explanation.shifted_slots == 0:
            return (
                ReasonCode.EARLIEST,
                f"{job.name} kept its earliest feasible window, starting {start.isoformat()}. "
                "No earlier option existed, or moving it would not have reduced emissions.",
            )

        if str(result.scheduler).upper() == "ASAP":
            return (
                ReasonCode.EARLIEST,
                f"{job.name} runs as early as release, capacity and its power limit "
                f"allow, starting {start.isoformat()}. ASAP does not optimise for "
                "carbon, so no CO2 saving is claimed.",
            )

        if saved is None or saved <= 0:
            return (
                ReasonCode.SCHEDULED,
                f"{job.name} was placed from {start.isoformat()} to meet its constraints "
                f"before {deadline.isoformat()}. It does not emit less than the earliest "
                f"feasible window (difference {-(saved or 0.0):.4g} kg CO2), so no CO2 "
                "saving is claimed.",
            )

        if job.job_type is LoadType.THERMAL:
            return (
                ReasonCode.THERMAL_PRECONDITIONING,
                f"{job.name} moved {explanation.shifted_slots} slot(s) later to store its "
                f"thermal state at a lower-carbon time while still meeting its service "
                f"target before {deadline.isoformat()}. Estimated CO2 avoided: "
                f"{saved:.4g} kg.",
            )

        if job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
            return (
                ReasonCode.LOWEST_CARBON_ALLOCATION,
                f"{job.name} charges in a lower-carbon window while still reaching "
                f"{after['energy_kwh']:.2f} kWh before {deadline.isoformat()}. "
                f"Estimated CO2 avoided: {saved:.4g} kg.",
            )

        return (
            ReasonCode.LOWEST_CARBON,
            f"{job.name} moved from its earliest feasible window into a lower-carbon "
            f"window while preserving the {deadline.isoformat()} deadline. "
            f"Estimated CO2 avoided: {saved:.4g} kg.",
        )


def resolve_scheduler(name: str) -> SchedulerName:
    """Map a requested scheduler name onto a registered engine (§34).

    Raises rather than falling back, because silently running greedy when CPSAT
    was asked for would make every reported result a lie.
    """
    try:
        return SchedulerName(name.strip().upper())
    except (AttributeError, ValueError):
        valid = ", ".join(s.value for s in SchedulerName)
        raise KeyError(f"unknown scheduler {name!r}; expected one of: {valid}")


def savings_from_metrics(
    baseline_co2_kg: Optional[float], candidate_co2_kg: Optional[float]
) -> tuple[Optional[float], Optional[float]]:
    """(kg saved, percent saved) against a reference total.

    §28: `(baseline - candidate) / baseline * 100`. A zero or missing baseline
    returns None rather than claiming a percentage out of nothing.
    """
    if baseline_co2_kg is None or candidate_co2_kg is None or baseline_co2_kg <= 0:
        return (None, None)
    saved = baseline_co2_kg - candidate_co2_kg
    return (saved, (saved / baseline_co2_kg) * 100.0)
