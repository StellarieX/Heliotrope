"""MultiUserCoordinator — joint scheduling under one shared connection (Phase 6).

Sits ABOVE the Phase 4/5 primitives and reuses them, never duplicating:

  * SchedulerNormalizer builds ONE shared SchedulerInput for all participants
    (one horizon, one carbon signal, one capacity number)
  * independent mode runs the regular ASAP/GREEDY/CPSAT engines per
    participant, each with the optimistic whole-connection view
  * coordinated mode runs CoordinatedCPSAT once over the merged input, with
    the shared capacity as a HARD constraint inside the model
  * ScheduleValidator + CarbonAccountingService measure everything, so no
    metric is computed twice in two ways

Inconvenience reference: each job's ASAP start on the merged input ("what
you'd get first-come-first-served"). Delay is measured against that, never
against an arbitrary baseline.
"""

from __future__ import annotations

import time

from ..domain.carbon import CarbonSignal
from ..domain.coordination import (
    AggregatePoint,
    CongestionPoint,
    CoordinationMetrics,
    CoordinationMode,
    CoordinationRequest,
    CoordinationResult,
    FairnessMode,
    ParticipantJobResult,
    ParticipantMetrics,
)
from ..domain.loads import LoadSpec
from ..domain.scaling import to_power_w
from ..domain.scheduling import (
    BaselineProfile,
    ReasonCode,
    SchedulerInput,
    ScheduleStatus,
)
from .carbon_accounting import CarbonAccountingService, Placement
from .coordinated_cpsat import MILLI, CoordinatedCPSATScheduler
from .schedule_validator import ScheduleValidator
from .scheduler_normalizer import NormalizationError, SchedulerNormalizer
from .schedulers.asap import ASAPScheduler
from .schedulers.greedy import GreedyScheduler


class CoordinationError(ValueError):
    """Malformed coordination request (maps to 422)."""


class MultiUserCoordinator:
    def __init__(self) -> None:
        self.normalizer = SchedulerNormalizer()
        self.accounting = CarbonAccountingService()
        self.validator = ScheduleValidator()

    # --- entry ------------------------------------------------------------

    def coordinate(
        self,
        request: CoordinationRequest,
        signal: CarbonSignal,
        solver_time_limit: float | None = None,
    ) -> CoordinationResult:
        started = time.perf_counter()
        self._validate_request(request)
        merged = self._merged_input(request, signal)
        by_participant = self._split_inputs(request, merged)

        if request.coordination_mode is CoordinationMode.INDEPENDENT:
            result = self._run_independent(request, merged, by_participant, solver_time_limit)
        else:
            result = self._run_coordinated(request, merged, by_participant, solver_time_limit)

        result.metrics.solve_time_ms = int((time.perf_counter() - started) * 1000)
        result.solve_time_ms = result.metrics.solve_time_ms
        return result

    def compare(
        self,
        request: CoordinationRequest,
        signal: CarbonSignal,
        solver_time_limit: float | None = None,
    ):
        from ..domain.coordination import CoordinationComparison

        indep = self.coordinate(
            request.model_copy(update={"coordination_mode": CoordinationMode.INDEPENDENT}),
            signal,
            solver_time_limit,
        )
        coord = self.coordinate(
            request.model_copy(update={"coordination_mode": CoordinationMode.COORDINATED}),
            signal,
            solver_time_limit,
        )
        return CoordinationComparison(independent=indep, coordinated=coord)

    # --- validation ---------------------------------------------------------

    def _validate_request(self, request: CoordinationRequest) -> None:
        pids = [p.id for p in request.participants]
        if len(set(pids)) != len(pids):
            raise CoordinationError("participant ids must be unique")
        pmap = {p.id: p for p in request.participants}
        jids: set[str] = set()
        for spec in request.jobs:
            pid = spec.participant_id or ""
            if pid not in pmap:
                raise CoordinationError(
                    f"job {spec.normalized_name!r} names unknown participant {pid!r}"
                )
            jid = spec.id or spec.normalized_name
            if jid in jids:
                raise CoordinationError(f"job id {jid!r} is used more than once")
            jids.add(jid)
        if not any(spec.participant_id for spec in request.jobs):
            raise CoordinationError("no job is assigned to a participant")
        if request.shared_resource.capacity_profile_kw is not None:
            raise CoordinationError(
                "time-varying shared capacity is not supported in Phase 6; "
                "supply a constant capacity_kw"
            )
        if request.baseline_kw and any(b < 0 for b in request.baseline_kw):
            raise CoordinationError("baseline entries must be >= 0")

    # --- input construction ---------------------------------------------------

    def _merged_input(self, request: CoordinationRequest, signal: CarbonSignal) -> SchedulerInput:
        try:
            scheduler_input, _warnings = self.normalizer.normalize(
                request.jobs, signal, request.shared_resource.capacity_kw
            )
        except Exception as exc:
            raise CoordinationError(str(exc)) from exc
        if request.baseline_kw:
            if len(request.baseline_kw) != scheduler_input.horizon.slot_count:
                raise CoordinationError(
                    f"baseline has {len(request.baseline_kw)} entries but the horizon "
                    f"has {scheduler_input.horizon.slot_count} slots"
                )
            scheduler_input = scheduler_input.model_copy(
                update={"baseline": BaselineProfile(power_w=[to_power_w(b) for b in request.baseline_kw])}
            )
        return scheduler_input

    def _split_inputs(
        self, request: CoordinationRequest, merged: SchedulerInput
    ) -> dict[str, SchedulerInput]:
        """Per-participant views of the SAME horizon/signal/capacity (the
        optimistic whole-connection view each independent optimizer sees)."""
        by_pid: dict[str, list] = {}
        for job in merged.jobs:
            by_pid.setdefault(job.participant_id, []).append(job)
        return {
            pid: merged.model_copy(update={"jobs": jobs}) for pid, jobs in by_pid.items()
        }

    # --- independent mode -------------------------------------------------------

    def _run_independent(
        self,
        request: CoordinationRequest,
        merged: SchedulerInput,
        by_participant: dict[str, SchedulerInput],
        solver_time_limit: float | None,
    ) -> CoordinationResult:
        # Each participant optimizes ALONE with the optimistic whole-connection
        # view — exactly the behavior that herds everyone into the same clean
        # window. The aggregate is then measured, not repaired.
        engine = GreedyScheduler()
        placements: dict[str, Placement] = {}
        ok = True
        reasons: list[str] = []
        for pid, sub in by_participant.items():
            result = engine.schedule(sub)
            if result.status not in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL):
                ok = False
                reasons.append(f"participant {pid}: {result.reason}")
                continue
            placement = Placement(slot_count=merged.horizon.slot_count)
            for scheduled in result.schedule:
                for alloc in scheduled.allocations:
                    placement.set(scheduled.job_id, alloc.slot, alloc.power_w)
            placements[pid] = placement
        return self._assemble(
            request, merged, placements, CoordinationMode.INDEPENDENT,
            "FEASIBLE" if ok else "INFEASIBLE",
            "; ".join(reasons),
            solver_time_limit,
        )

    # --- coordinated mode ---------------------------------------------------------

    def _preferred_starts(self, merged: SchedulerInput) -> dict[str, int]:
        """ASAP start per job on the merged input: first-come-first-served
        reference that delay is measured against."""
        result = ASAPScheduler().schedule(merged)
        starts = {}
        for scheduled in result.schedule:
            slots = [a.slot for a in scheduled.allocations if a.power_w > 0]
            if slots:
                starts[scheduled.job_id] = min(slots)
        for job in merged.jobs:
            starts.setdefault(job.id, job.release_slot)
        return starts

    def _run_coordinated(
        self,
        request: CoordinationRequest,
        merged: SchedulerInput,
        by_participant: dict[str, SchedulerInput],
        solver_time_limit: float | None,
    ) -> CoordinationResult:
        from ..domain.scheduling import SchedulerConfig

        preferred = self._preferred_starts(merged)
        capacity_w = merged.capacity_w
        target_w = int(capacity_w * request.weights.target_utilization)
        participant_of = {j.id: j.participant_id for j in merged.jobs}
        priorities = {p.id: p.priority_weight for p in request.participants}
        caps = {
            p.id: p.max_inconvenience_slots
            for p in request.participants
            if p.max_inconvenience_slots is not None
        }
        config = request.solver_config or SchedulerConfig()
        if solver_time_limit is not None:
            config = config.model_copy(update={"time_limit_seconds": solver_time_limit})
        engine = CoordinatedCPSATScheduler(
            preferred_starts=preferred,
            target_w=target_w,
            congestion_weight=request.weights.congestion,
            inconvenience_weight=request.weights.inconvenience,
            fairness_mode=request.fairness_mode,
            participant_of=participant_of,
            priority_weight=priorities,
            max_inconvenience_millislots=caps,
            config=config,
        )
        result = engine.schedule(merged)
        if result.status not in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL):
            return self._infeasible_result(request, merged, by_participant, result)
        placement = Placement(slot_count=merged.horizon.slot_count)
        for scheduled in result.schedule:
            for alloc in scheduled.allocations:
                placement.set(scheduled.job_id, alloc.slot, alloc.power_w)
        placements = self._placements_by_participant(merged, placement)
        return self._assemble(
            request, merged, placements, CoordinationMode.COORDINATED,
            "OPTIMAL" if result.status is ScheduleStatus.OPTIMAL else "FEASIBLE",
            "", solver_time_limit, solver_status=result.solver.status.value,
            solve_ms=result.solver.solve_time_ms, preferred=preferred,
        )

    def _placements_by_participant(
        self, merged: SchedulerInput, placement: Placement
    ) -> dict[str, Placement]:
        out: dict[str, Placement] = {}
        for job in merged.jobs:
            sub = out.setdefault(
                job.participant_id, Placement(slot_count=merged.horizon.slot_count)
            )
            for slot, power in placement.job_slots(job.id).items():
                sub.set(job.id, slot, power)
        return out

    def _infeasible_result(
        self,
        request: CoordinationRequest,
        merged: SchedulerInput,
        by_participant: dict[str, SchedulerInput],
        result,
    ) -> CoordinationResult:
        """Name who conflicts: participants feasible alone but not together
        point at shared capacity, not at any one user's jobs."""
        engine = GreedyScheduler()
        alone_ok, alone_bad = [], []
        for pid, sub in by_participant.items():
            r = engine.schedule(sub)
            (alone_ok if r.status in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL) else alone_bad).append(pid)
        if alone_bad:
            reason = (
                "infeasible for individual participants independent of sharing: "
                + ", ".join(alone_bad)
            )
        else:
            reason = (
                "the shared resource cannot fit every participant at once; each "
                "participant is feasible alone (" + ", ".join(alone_ok) + "). "
                + result.reason
            )
        res = CoordinationResult(
            status="INFEASIBLE",
            coordination_mode=CoordinationMode.COORDINATED,
            reason=reason,
            solver_status=result.solver.status.value,
            solve_time_ms=result.solver.solve_time_ms,
            fairness_mode=request.fairness_mode,
            signal_provenance={
                "signal_type": merged.carbon.signal_type,
                "source": merged.carbon.source,
            },
        )
        res.metrics.participant_count = len(request.participants)
        res.metrics.job_count = len(merged.jobs)
        return res

    # --- assembly: profiles, metrics, explanations ----------------------------------

    def _assemble(
        self,
        request: CoordinationRequest,
        merged: SchedulerInput,
        placements: dict[str, Placement],
        mode: CoordinationMode,
        status: str,
        reason: str,
        solver_time_limit: float | None,
        solver_status: str = "FEASIBLE",
        solve_ms: int | None = None,
        preferred: dict[str, int] | None = None,
    ) -> CoordinationResult:
        horizon = merged.horizon
        n = horizon.slot_count
        slot_minutes = horizon.slot_minutes
        slot_hours = slot_minutes / 60.0
        capacity_kw = merged.capacity_w / 1000.0
        target_w = int(merged.capacity_w * request.weights.target_utilization)

        agg_flex_w = [0] * n
        for placement in placements.values():
            for job in merged.jobs:
                for slot, power in placement.job_slots(job.id).items():
                    agg_flex_w[slot] += power
        baseline_kw = [merged.baseline.at(s) / 1000.0 for s in range(n)]

        carbon = merged.carbon.gco2_per_kwh
        aggregate, congestion = [], []
        violations = 0
        for s in range(n):
            flex_kw = agg_flex_w[s] / 1000.0
            total_kw = baseline_kw[s] + flex_kw
            cap_kw = merged.capacity_w / 1000.0
            util = total_kw / cap_kw if cap_kw else 0.0
            over = max(0.0, total_kw - target_w / 1000.0)
            score = over / (target_w / 1000.0) if target_w else 0.0
            if total_kw - cap_kw > 1e-9:
                violations += 1
            ts = horizon.slot_start(s)
            aggregate.append(
                AggregatePoint(
                    timestamp=ts, baseline_kw=baseline_kw[s], flexible_kw=flex_kw,
                    total_kw=total_kw, capacity_kw=cap_kw, utilization=util,
                    carbon_intensity=float(carbon[s]) if s < len(carbon) else 0.0,
                    congestion_score=score,
                )
            )
            congestion.append(
                CongestionPoint(
                    timestamp=ts, aggregate_load=total_kw, capacity_kw=cap_kw,
                    utilization=util, congestion_score=score,
                )
            )

        # Per-job results + participant metrics, all from real accounting.
        jobs_out: list[ParticipantJobResult] = []
        parts: dict[str, ParticipantMetrics] = {}
        total_delay_min = 0.0
        worst = 0.0
        for job in merged.jobs:
            pid = job.participant_id
            placement = placements.get(pid)
            slots = placement.job_slots(job.id) if placement else {}
            active = sorted(s for s, p in slots.items() if p > 0)
            stats = self.accounting.job_accounting(merged, job.id, placement) if placement else {"energy_kwh": 0.0, "co2_kg": 0.0}
            if active:
                start, end = horizon.slot_start(active[0]), horizon.slot_end(active[-1])
                energy = stats["energy_kwh"]
                peak = max(slots.values()) / 1000.0
            else:
                start = horizon.slot_start(job.release_slot)
                end = start
                energy, peak = 0.0, 0.0
            pref = (preferred or {}).get(job.id, job.release_slot)
            delay_min = max(0.0, ((active[0] if active else job.release_slot) - pref)) * slot_minutes
            m = parts.setdefault(pid, ParticipantMetrics(participant_id=pid))
            m.job_count += 1
            m.delay_minutes += delay_min
            m.co2_kg += stats["co2_kg"]
            if active and active[0] != pref:
                m.jobs_shifted += 1
            total_delay_min += delay_min
            jobs_out.append(
                ParticipantJobResult(
                    participant_id=pid, job_id=job.id, name=job.name,
                    scheduled_start=start, scheduled_end=end,
                    energy_kwh=energy, power_kw=job.power_w / 1000.0,
                    delay_minutes=delay_min, carbon_kg=stats["co2_kg"],
                    reason=self._job_reason(request, merged, job, active, pref, aggregate, target_w),
                )
            )
        job_counts: dict[str, int] = {}
        for job in merged.jobs:
            job_counts[job.participant_id] = job_counts.get(job.participant_id, 0) + 1
        for m in parts.values():
            nj = max(1, job_counts.get(m.participant_id, 1))
            m.inconvenience_score = round(m.delay_minutes / slot_minutes * MILLI / nj, 1)
            worst = max(worst, m.inconvenience_score)

        total_co2 = sum(
            self.accounting.job_accounting(merged, j.id, placements.get(j.participant_id))["co2_kg"]
            if placements.get(j.participant_id) else 0.0
            for j in merged.jobs
        )
        total_energy = sum(
            self.accounting.job_accounting(merged, j.id, placements.get(j.participant_id))["energy_kwh"]
            if placements.get(j.participant_id) else 0.0
            for j in merged.jobs
        )
        peak_kw = max((a.total_kw for a in aggregate), default=0.0)
        result = CoordinationResult(
            status=status,
            coordination_mode=mode,
            participants=[parts.get(p.id, ParticipantMetrics(participant_id=p.id)) for p in request.participants],
            jobs=jobs_out,
            aggregate_profile=aggregate,
            congestion_profile=congestion,
            metrics=CoordinationMetrics(
                total_energy_kwh=round(total_energy, 4),
                total_co2_kg=round(total_co2, 4),
                peak_kw=round(peak_kw, 3),
                capacity_violations=violations if mode is CoordinationMode.INDEPENDENT else 0,
                total_delay_minutes=round(total_delay_min, 1),
                worst_inconvenience=round(worst, 1),
                participant_count=len(request.participants),
                job_count=len(merged.jobs),
            ),
            fairness_mode=request.fairness_mode,
            solver_status=solver_status,
            solve_time_ms=solve_ms,
            reason=reason,
            signal_provenance={
                "signal_type": merged.carbon.signal_type,
                "source": merged.carbon.source,
                "is_forecast": merged.carbon.is_forecast,
            },
        )
        if mode is CoordinationMode.COORDINATED and violations:
            # Belt and braces: the model forbids this, so report, don't hide.
            result.status = "INTERNAL_ERROR"
            result.reason = f"coordinated schedule violates shared capacity in {violations} slot(s)"
        return result

    def _job_reason(self, request, merged, job, active, pref, aggregate, target_w) -> str:
        if not active:
            return f"{job.name} could not be placed in this run."
        if active[0] == pref:
            return (
                f"{job.name} kept its earliest feasible window starting "
                f"{merged.horizon.slot_start(active[0]).isoformat()}."
            )
        window_utils = [
            aggregate[s].utilization
            for s in range(job.release_slot, min(active[0], len(aggregate)))
        ]
        target_util = target_w / max(1, merged.capacity_w)
        if window_utils and max(window_utils) >= target_util:
            return (
                f"{job.name} moved from its earliest window to a later one because "
                f"the shared connection was already near its utilization target "
                f"({target_util:.0%}) there; its deadline is preserved."
            )
        return (
            f"{job.name} moved from its earliest window into a lower-carbon "
            f"window while preserving its deadline."
        )
