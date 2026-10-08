"""SchedulerNormalizer: LoadSpec + carbon signal -> SchedulerInput (Phase 4, §4, §5).

    User / Firestore -> LoadSpec -> SchedulerNormalizer -> SchedulerInput -> schedulers

Everything ambiguous is resolved HERE, once:

  * wall-clock windows become slot indices against one shared horizon
  * durations and chunks become slot counts, rounded UP
  * kW becomes watts, kWh becomes watt-minutes
  * FIXED loads are folded into a per-slot baseline that every scheduler then
    subtracts from capacity

A scheduler that received raw `LoadSpec` objects would have to redo this per
implementation, and three schedulers doing it three ways is exactly how the
comparison in §24 stops being trustworthy.

CONNECTION CAPACITY (§5). `headroom_w(slot) = capacity_w - baseline.at(slot)`.
Flexible jobs are placed into that headroom, never into the raw capacity, so a
4 kW baseline under a 10 kW connection leaves 6 kW — not 14 kW.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..domain.carbon import CarbonSignal
from ..domain.forecasting import ForecastMode
from ..domain.horizon import SchedulingHorizon
from ..domain.loads import LoadSpec, LoadType
from ..domain.scaling import (
    ThermalScale,
    assert_objective_headroom,
    to_carbon_int,
    to_energy_wmin,
    to_power_w,
)
from ..domain.scheduling import (
    BaselineProfile,
    CarbonProfile,
    NormalizedJob,
    ObjectiveWeights,
    SchedulerInput,
    TimeOfUseTariff,
    UncertaintyProfile,
)


class NormalizationError(ValueError):
    """The input cannot be turned into a scheduling problem at all."""


@dataclass
class NormalizationReport:
    """Diagnostics that are worth surfacing but not worth failing over."""

    warnings: list[str] = field(default_factory=list)
    baseline_sources: list[str] = field(default_factory=list)
    horizon_slots: int = 0


class SchedulerNormalizer:
    """The single translation point into slot space."""

    def normalize(
        self,
        specs: list[LoadSpec],
        carbon_signal: CarbonSignal,
        capacity_kw: float,
        horizon: Optional[SchedulingHorizon] = None,
        objective: Optional[ObjectiveWeights] = None,
        tariff: Optional[TimeOfUseTariff] = None,
        signal_type: Optional[str] = None,
        slot_minutes: int = 15,
        deadline_buffer_minutes: int = 0,
        forecast_mode: str = "ACTUAL",
        risk_weight: float = 0.0,
        uncertainty_upper: Optional[list[int]] = None,
        forecast_provenance: Optional[dict] = None,
        capacity_profile_kw: Optional[list[float]] = None,
        hints: Optional[dict[str, list[tuple[datetime, int]]]] = None,
    ) -> tuple[SchedulerInput, NormalizationReport]:
        report = NormalizationReport()
        if not specs:
            raise NormalizationError("no jobs to schedule")

        horizon = horizon or self._horizon_from(specs, slot_minutes)
        report.horizon_slots = horizon.slot_count

        baseline_w = [0] * horizon.slot_count
        jobs: list[NormalizedJob] = []

        for spec in specs:
            if spec.job_type is LoadType.FIXED:
                self._add_baseline(spec, horizon, baseline_w, report)
            else:
                jobs.append(
                    self._normalize_job(
                        spec, horizon, report, deadline_buffer_minutes=deadline_buffer_minutes
                    )
                )

        seen_ids: set[str] = set()
        for job in jobs:
            if job.id in seen_ids:
                raise NormalizationError(
                    f"two loads share the id {job.id!r}; give each load a distinct id so "
                    "their schedules are not merged"
                )
            seen_ids.add(job.id)

        if not jobs and not any(baseline_w):
            raise NormalizationError(
                "every job was rejected as FIXED with no power rating, so there is "
                "nothing to schedule"
            )

        capacity_w = to_power_w(capacity_kw)
        capacity_profile_w: Optional[list[int]] = None
        if capacity_profile_kw is not None:
            if len(capacity_profile_kw) != horizon.slot_count:
                raise NormalizationError(
                    f"capacity profile has {len(capacity_profile_kw)} entries but the "
                    f"horizon has {horizon.slot_count} slots"
                )
            if any(c < 0 for c in capacity_profile_kw):
                raise NormalizationError("capacity profile entries must be >= 0")
            capacity_profile_w = [to_power_w(c) for c in capacity_profile_kw]
        self._check_baseline_fits(baseline_w, capacity_w, capacity_profile_w)

        carbon = self._carbon_profile(carbon_signal, horizon, signal_type)

        mode = ForecastMode(forecast_mode)
        uncertainty = None
        if mode is ForecastMode.ROBUST:
            # §16: ROBUST is defined as optimizing
            #     forecast + risk_weight * (upper - forecast)
            # so without an upper bound there is literally nothing to compute, and
            # silently running the point forecast under the name ROBUST would be
            # the exact lie this check exists to prevent.
            if uncertainty_upper is None:
                raise NormalizationError(
                    "forecast_mode is ROBUST but no per-slot upper prediction bound "
                    "was supplied, so there is no uncertainty to schedule against. "
                    "Refusing to run a 'robust' schedule with no uncertainty. Use "
                    "EXPECTED to optimize the point forecast alone."
                )
            if len(uncertainty_upper) != horizon.slot_count:
                raise NormalizationError(
                    f"the forecast covers {len(uncertainty_upper)} slot(s) but the "
                    f"horizon has {horizon.slot_count}. Aligning them by padding would "
                    f"mean inventing carbon numbers at the edges."
                )
            uncertainty = UncertaintyProfile(upper_gco2_per_kwh=list(uncertainty_upper))
        elif mode is ForecastMode.EXPECTED and uncertainty_upper is not None:
            # EXPECTED optimizes the point forecast (§16). An interval supplied
            # alongside it is accepted and recorded, because a caller who has one
            # will want it later, but it does not enter the objective.
            if len(uncertainty_upper) != horizon.slot_count:
                raise NormalizationError(
                    f"the forecast covers {len(uncertainty_upper)} slot(s) but the "
                    f"horizon has {horizon.slot_count}."
                )
            uncertainty = UncertaintyProfile(upper_gco2_per_kwh=list(uncertainty_upper))

        if mode is not ForecastMode.ACTUAL:
            report.warnings.append(
                f"scheduling against a {mode.value} forecast "
                f"(risk_weight={risk_weight}). This changes which schedule is "
                f"preferred, not whether one is feasible: deadlines, energy, capacity, "
                f"thermal comfort and atomicity are unchanged."
            )
        elif risk_weight:
            report.warnings.append(
                f"risk_weight={risk_weight} was supplied but forecast_mode is ACTUAL, so "
                f"it has no effect. Use EXPECTED or ROBUST to consume forecast "
                f"uncertainty."
            )

        max_power_w = max(
            [to_power_w(capacity_kw)]
            + (capacity_profile_w or [])
            + [j.max_power_w or j.power_w for j in jobs]
        )
        # The objective headroom must cover the WORST case the objective can see,
        # which under ROBUST mode is the risk-adjusted intensity, not the
        # forecast point. Checking only the forecast point would let a robust run
        # overflow the objective's integer range.
        max_carbon = max(carbon.gco2_per_kwh) if carbon.gco2_per_kwh else 1
        if uncertainty is not None:
            max_carbon = max(
                [
                    max(0, int(v + risk_weight * (u - v) + 0.5))
                    for v, u in zip(carbon.gco2_per_kwh, uncertainty.upper_gco2_per_kwh)
                ]
                + [1]
            )
        assert_objective_headroom(
            max_jobs=len(jobs) + 1,
            max_slots=horizon.slot_count,
            max_power_w=max_power_w,
            max_carbon=max_carbon,
            max_slot_minutes=horizon.slot_minutes,
        )

        scheduler_input = SchedulerInput(
            horizon=horizon,
            jobs=jobs,
            baseline=BaselineProfile(power_w=baseline_w),
            carbon=carbon,
            capacity_w=capacity_w,
            capacity_profile_w=capacity_profile_w,
            objective=objective or ObjectiveWeights(),
            tariff=tariff,
            forecast_mode=mode,
            risk_weight=risk_weight,
            uncertainty=uncertainty,
            forecast_provenance=forecast_provenance,
            hints=hints,
        )
        return scheduler_input, report

    # --- horizon ------------------------------------------------------------

    def _horizon_from(self, specs: list[LoadSpec], slot_minutes: int) -> SchedulingHorizon:
        """A horizon that provably contains every job window.

        Built from the jobs themselves rather than from "now", so the same input
        always produces the same horizon and schedules are reproducible.
        """
        moments: list[datetime] = []
        for spec in specs:
            if spec.release_at is not None:
                moments.append(spec.release_at)
            if spec.deadline_at is not None:
                moments.append(spec.deadline_at)
        if not moments:
            raise NormalizationError(
                "no job has a release time or a deadline, so there is no horizon to schedule over"
            )
        return SchedulingHorizon.spanning(moments, slot_minutes=slot_minutes, pad_slots=1)

    # --- baseline (§5) -------------------------------------------------------

    def _add_baseline(
        self,
        spec: LoadSpec,
        horizon: SchedulingHorizon,
        baseline_w: list[int],
        report: NormalizationReport,
    ) -> None:
        power = to_power_w(spec.power_kw or 0.0)
        if power <= 0:
            report.warnings.append(
                f"{spec.normalized_name} is a FIXED load with no power rating, so it "
                "contributes nothing to the baseline. Its real draw is unknown."
            )
            return

        if spec.release_at is not None and spec.deadline_at is not None:
            # Charge each slot by the fraction of it the window actually covers.
            # The previous code used `slot_range_for`, which returns only the
            # slots FULLY CONTAINED in the window and so dropped the two boundary
            # slots: a 30-minute draw on a 15-minute grid was charged as 15
            # minutes and half the load's energy disappeared from the baseline.
            # An already-aligned window prorates to whole power on every slot,
            # so aligned inputs are unaffected.
            covered = horizon.slot_overlap_fractions(spec.release_at, spec.deadline_at)
            for slot, fraction in covered:
                baseline_w[slot] += int(round(power * fraction))
            if covered and any(f < 1.0 for _, f in covered):
                report.warnings.append(
                    f"{spec.normalized_name}'s declared window does not start and end on "
                    f"{horizon.slot_minutes}-minute boundaries, so the boundary slots "
                    "carry the fraction of it they really cover."
                )
            report.warnings.append(
                f"{spec.normalized_name} is drawn only inside its declared window; "
                "outside it the baseline is zero."
            )
        else:
            for s in range(len(baseline_w)):
                baseline_w[s] += power
            report.warnings.append(
                f"{spec.normalized_name} is always-on with no occupancy window, so it is "
                "drawn across the whole horizon."
            )
        report.baseline_sources.append(spec.normalized_name)

    def _check_baseline_fits(
        self,
        baseline_w: list[int],
        capacity_w: int,
        capacity_profile_w: Optional[list[int]] = None,
    ) -> None:
        for slot, value in enumerate(baseline_w):
            cap = capacity_profile_w[slot] if capacity_profile_w is not None else capacity_w
            if value > cap:
                raise NormalizationError(
                    f"baseline load at slot {slot} is {value / 1000:.2f} kW, which exceeds "
                    f"the {cap / 1000:.2f} kW connection capacity. The site is "
                    "already over capacity before any flexible load is scheduled."
                )

    # --- flexible jobs ------------------------------------------------------

    def _normalize_job(
        self,
        spec: LoadSpec,
        horizon: SchedulingHorizon,
        report: NormalizationReport,
        deadline_buffer_minutes: int = 0,
    ) -> NormalizedJob:
        if spec.release_at is None or spec.deadline_at is None:
            missing = "release_at" if spec.release_at is None else "deadline_at"
            raise NormalizationError(
                f"{spec.normalized_name} has no {missing}, so it cannot be placed on the grid"
            )

        release_slot = horizon.first_slot_at_or_after(spec.release_at)
        # EXCLUSIVE deadline: the slot that contains the deadline instant is not
        # usable, because it would run past the deadline.
        deadline_slot = horizon.last_slot_before(spec.deadline_at) + 1
        deadline_slot = min(deadline_slot, horizon.slot_count)

        # §20: an EXPLICIT deadline safety buffer, applied here because this is
        # the one place a wall-clock deadline becomes a slot index. The buffer
        # only ever moves the deadline EARLIER, so it can never buy a job extra
        # time it did not have. It is subtracted in whole slots and reported, so
        # the effective deadline is visible in the output rather than silently
        # replacing the user's stated one.
        buffer_slots = 0
        if deadline_buffer_minutes:
            buffer_slots = horizon.minutes_to_slots(deadline_buffer_minutes)
            buffered = min(deadline_slot - buffer_slots, horizon.slot_count)
            report.warnings.append(
                f"{spec.normalized_name}: applying a {deadline_buffer_minutes}-minute "
                f"deadline buffer, so its {spec.deadline_at.isoformat()} deadline is "
                f"treated as {horizon.slot_start(max(buffered, 0)).isoformat()} for "
                f"scheduling. The buffer is optional, configurable, and never applied "
                f"unless requested."
            )
            deadline_slot = max(buffered, 0)

        release_slot = min(release_slot, deadline_slot)

        if deadline_slot <= release_slot:
            raise NormalizationError(
                f"{spec.normalized_name} has no whole {horizon.slot_minutes}-minute slot "
                f"between its release ({spec.release_at.isoformat()}) and effective "
                f"deadline ({spec.deadline_at.isoformat()}"
                + (
                    f" after a {deadline_buffer_minutes}-minute buffer"
                    if deadline_buffer_minutes
                    else ""
                )
                + ")"
            )

        power_w = to_power_w(spec.power_kw or 0.0)
        if spec.max_power_kw is not None:
            max_power_w = to_power_w(spec.max_power_kw)
        elif spec.job_type is LoadType.THERMAL and spec.thermal is not None:
            # A thermal load's rating is the plant's own maximum; only an
            # explicit max_power_kw narrows it.
            max_power_w = to_power_w(spec.thermal.max_power_kw)
        elif spec.power_kw is not None:
            max_power_w = power_w
        else:
            max_power_w = 0
            report.warnings.append(
                f"{spec.normalized_name} has no power rating; it cannot be placed until one "
                "is supplied."
            )
        if max_power_w <= 0 and spec.job_type is LoadType.THERMAL and spec.thermal:
            max_power_w = to_power_w(spec.thermal.max_power_kw)

        min_chunk_slots = 1
        if spec.min_chunk_minutes:
            min_chunk_slots = horizon.minutes_to_slots(spec.min_chunk_minutes)

        duration_slots = None
        if spec.duration_minutes:
            duration_slots = horizon.minutes_to_slots(spec.duration_minutes)

        thermal = None
        if spec.job_type is LoadType.THERMAL:
            if spec.thermal is None:
                raise NormalizationError(
                    f"{spec.normalized_name} is THERMAL but has no comfort band or dynamics, "
                    "so it cannot be simulated"
                )
            thermal = ThermalScale.from_spec(spec.thermal, horizon.slot_minutes)

        energy_wmin = (
            to_energy_wmin(spec.energy_required_kwh)
            if spec.energy_required_kwh is not None
            else None
        )

        return NormalizedJob(
            id=spec.id or spec.normalized_name,
            name=spec.normalized_name,
            participant_id=spec.participant_id,
            job_type=spec.job_type,
            power_w=power_w or max_power_w,
            max_power_w=max_power_w,
            energy_required_wmin=energy_wmin,
            duration_slots=duration_slots,
            min_chunk_slots=min_chunk_slots,
            release_slot=release_slot,
            deadline_slot=deadline_slot,
            thermal=thermal,
            slot_minutes=horizon.slot_minutes,
            category=spec.category,
            shiftable=spec.shiftable,
        )

    # --- carbon (§37) -------------------------------------------------------

    @staticmethod
    def _point_fields(point) -> tuple[datetime, float]:
        """Read (timestamp, intensity) from either carbon point model.

        `CarbonService.get_signal` answers with a `CarbonSignalResponse` whose
        points are `CarbonPointOut` (`timestamp`, `carbon_intensity_gco2_per_kwh`),
        while the provider-level `CarbonSignal` carries `CarbonPoint` (`time`,
        `gco2_per_kwh`). The scheduler is handed whichever the caller has, so it
        reads both rather than forcing callers to reshape a signal they did not
        build. The two are the same measurement under different names.
        """
        timestamp = getattr(point, "time", None) or getattr(point, "timestamp", None)
        intensity = getattr(point, "gco2_per_kwh", None)
        if intensity is None:
            intensity = getattr(point, "carbon_intensity_gco2_per_kwh", None)
        if timestamp is None or intensity is None:
            raise NormalizationError(
                f"carbon point {point!r} exposes neither ('time', 'gco2_per_kwh') "
                "nor ('timestamp', 'carbon_intensity_gco2_per_kwh')"
            )
        return timestamp, intensity

    def _carbon_profile(
        self,
        signal: CarbonSignal,
        horizon: SchedulingHorizon,
        signal_type: Optional[str],
    ) -> CarbonProfile:
        """Resample the signal onto the horizon grid, one integer per slot.

        A missing slot is a hard error rather than a silent fill: the objective
        would otherwise be computed against an invented carbon number.
        """
        by_time = {self._point_fields(p)[0]: p for p in signal.points}
        values: list[int] = []
        missing: list[datetime] = []
        for _slot_index, start, _end in horizon.iter_slot_windows():
            point = by_time.get(start)
            if point is None:
                missing.append(start)
                values.append(0)
            else:
                values.append(to_carbon_int(self._point_fields(point)[1]))
        if missing:
            raise NormalizationError(
                f"carbon signal has no value for {len(missing)} slot(s) of the horizon "
                f"(first missing: {missing[0].isoformat()}). Refusing to schedule against "
                "an invented carbon intensity."
            )

        first = signal.points[0] if signal.points else None
        # Provenance lives on the point for a provider `CarbonSignal` and on the
        # enclosing response for a `CarbonSignalResponse`; read whichever is
        # present rather than assuming a shape the caller may not have.
        point_signal_type = getattr(first, "signal_type", None)
        response_signal_type = getattr(signal, "signal_type", None)
        resolved_signal_type = (
            signal_type
            or getattr(point_signal_type, "value", point_signal_type)
            or getattr(response_signal_type, "value", response_signal_type)
        )
        quality = getattr(signal, "quality", None)
        return CarbonProfile(
            gco2_per_kwh=values,
            signal_type=resolved_signal_type or "SYNTHETIC",
            source=(
                getattr(first, "source", None)
                or getattr(quality, "source", None)
                or getattr(signal, "source", None)
                or "unknown"
            ),
            is_forecast=(
                any(getattr(p, "is_forecast", False) for p in signal.points)
                or bool(getattr(quality, "is_forecast", False))
            ),
        )


def fingerprint_input(scheduler_input: SchedulerInput) -> str:
    """A stable digest of the exact input a scheduler saw (§42).

    The comparison service reports this so it is verifiable that all three
    schedulers really were given byte-identical input rather than three
    subtly different ones.
    """
    import hashlib
    import json

    payload = {
        "horizon": [
            scheduler_input.horizon.start.isoformat(),
            scheduler_input.horizon.end.isoformat(),
            scheduler_input.horizon.slot_minutes,
            scheduler_input.horizon.slot_count,
        ],
        "capacity_w": scheduler_input.capacity_w,
        "capacity_profile_w": scheduler_input.capacity_profile_w,
        "baseline": scheduler_input.baseline.power_w,
        "carbon": scheduler_input.carbon.gco2_per_kwh,
        "carbon_signal_type": scheduler_input.carbon.signal_type,
        "carbon_source": scheduler_input.carbon.source,
        "objective": scheduler_input.objective.model_dump(),
        "tariff": scheduler_input.tariff.price_micro_per_kwh if scheduler_input.tariff else None,
        "jobs": [
            [
                j.id,
                j.job_type.value,
                j.power_w,
                j.max_power_w,
                j.energy_required_wmin,
                j.duration_slots,
                j.min_chunk_slots,
                j.release_slot,
                j.deadline_slot,
            ]
            for j in scheduler_input.jobs
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]