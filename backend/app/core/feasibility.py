"""Feasibility layer (Phase 3, §21, §22).

Answers one question — "can this load physically be satisfied?" — and answers it
with enough structure that a UI can explain itself.

WHY NOT JUST RETURN FALSE. The Phase 1 model rejects nonsense at the type level
(`power_kw < 0`). That is a different question. A perfectly well-formed EV spec
can still be impossible: 24 kWh needed by 07:00 through a 3.3 kW supply with a
2 h window delivers 6.6 kWh. Every function here returns a `FeasibilityReport`
with a specific, quotable message and machine-readable detail, because the
Phase 4 UI has to be able to say exactly that out loud.

INVARIANT CARRIED FORWARD FROM PHASE 1: feasibility depends ONLY on
user-declared physical values. No carbon forecast, no price, and no grid
signal may ever appear in this module. A load is either physically possible or
it is not; how clean the electricity is changes WHERE it runs, never WHETHER
it can run.
"""

from __future__ import annotations

import math
from enum import Enum
from typing import Iterable, Optional, Sequence

from pydantic import BaseModel, Field

from ..domain.loads import LoadSpec, LoadType
from ..domain.thermal import ThermalModel, ThermalProfile

# Slack for float comparisons. Energy arithmetic multiplies three numbers, so
# an exact equality check would reject loadings that are correct to 1e-12.
EPS = 1e-9


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class IssueCode(str, Enum):
    """Stable machine-readable reasons. The UI can branch on these; the
    `message` is what it should read out loud."""

    RELEASE_MISSING = "release_missing"
    DEADLINE_MISSING = "deadline_missing"
    WINDOW_EMPTY = "window_empty"
    WINDOW_TOO_SHORT_FOR_DURATION = "window_too_short_for_duration"
    WINDOW_TOO_SHORT_FOR_CHUNK = "window_too_short_for_chunk"
    DURATION_MISSING = "duration_missing"
    DURATION_INVALID = "duration_invalid"
    POWER_MISSING = "power_missing"
    MAX_POWER_MISSING = "max_power_missing"
    ENERGY_MISSING = "energy_missing"
    ENERGY_EXCEEDS_WINDOW = "energy_exceeds_window"
    MIN_CHUNK_EXCEEDS_WINDOW = "min_chunk_exceeds_window"
    THERMAL_SPEC_MISSING = "thermal_spec_missing"
    THERMAL_TARGET_UNREACHABLE = "thermal_target_unreachable"
    THERMAL_BAND_VIOLATION = "thermal_band_violation"
    THERMAL_INITIAL_OUT_OF_BAND = "thermal_initial_out_of_band"
    POWER_EXCEEDS_MODEL_MAX = "power_exceeds_thermal_model_max"
    DEADLINE_IN_PAST = "deadline_in_past"
    INCOMPLETE_SPEC = "incomplete_spec"


class Issue(BaseModel):
    code: IssueCode
    severity: Severity = Severity.ERROR
    message: str = Field(min_length=1)
    field: Optional[str] = None
    detail: dict = Field(default_factory=dict)


class FeasibilityReport(BaseModel):
    """Structured verdict. `feasible` is derived, never set by hand."""

    feasible: bool = True
    errors: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    checks_run: list[str] = Field(default_factory=list)

    def with_issue(self, issue: Issue) -> "FeasibilityReport":
        if issue.severity is Severity.ERROR:
            self.errors.append(issue)
            self.feasible = False
        else:
            self.warnings.append(issue)
        return self

    def merge(self, other: "FeasibilityReport") -> "FeasibilityReport":
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        self.checks_run.extend(other.checks_run)
        self.feasible = self.feasible and other.feasible
        return self

    def codes(self) -> list[str]:
        return [i.code.value for i in self.errors]

    def explain(self) -> str:
        """One-line summary suitable for a toast or a status line."""
        if self.feasible and not self.warnings:
            return "No physical problems found."
        if self.feasible:
            return f"Feasible, with {len(self.warnings)} warning(s)."
        return "; ".join(i.message for i in self.errors)


def _fmt(value: Optional[float], unit: str, digits: int = 2) -> str:
    if value is None:
        return "unknown"
    return f"{value:.{digits}f} {unit}".strip()


def _distance_outside(thermal, temperature: float) -> float:
    """How far outside the comfort band a state sits. 0.0 when inside."""
    if temperature < thermal.temperature_min_c:
        return thermal.temperature_min_c - temperature
    if temperature > thermal.temperature_max_c:
        return temperature - thermal.temperature_max_c
    return 0.0


# --- Time feasibility (§22) ------------------------------------------------


def is_time_feasible(spec: LoadSpec) -> FeasibilityReport:
    """Can the declared window physically hold the load's runtime?

    Per-class minimum occupancy (§13): an atomic load needs its full duration
    somewhere in the window; an interruptible one needs at least one minimum
    chunk; a thermal one needs at least one model resolution step. A FIXED load
    has no window requirement at all — it is background load.
    """
    report = FeasibilityReport(checks_run=["time"])

    # A FIXED load is baseline occupancy: there is no window to schedule it
    # into, so demanding a release/deadline pair would be asking for data the
    # class can never use. Its requirements (a power rating, an occupancy
    # period) are checked elsewhere.
    if spec.job_type is LoadType.FIXED:
        return report

    if spec.release_at is None:
        report.with_issue(
            Issue(
                code=IssueCode.RELEASE_MISSING,
                field="release_at",
                message="No release time was given, so there is no window to schedule this load into.",
            )
        )
    if spec.deadline_at is None:
        report.with_issue(
            Issue(
                code=IssueCode.DEADLINE_MISSING,
                field="deadline_at",
                message="No deadline was given, so there is no window to schedule this load into.",
            )
        )

    window = spec.window_minutes()
    if window is not None and window <= 0:
        report.with_issue(
            Issue(
                code=IssueCode.WINDOW_EMPTY,
                field="deadline_at",
                message="The deadline is not after the release time, so the window is empty.",
                detail={"window_minutes": window},
            )
        )
        return report

    if window is None:
        return report

    if spec.job_type is LoadType.DEFERRABLE_ATOMIC:
        if spec.duration_minutes is None:
            report.with_issue(
                Issue(
                    code=IssueCode.DURATION_MISSING,
                    field="duration_minutes",
                    message=(
                        "This load needs to know how long it runs; an atomic load is "
                        "described by its duration, not by an energy total."
                    ),
                )
            )
            return report
        if window < spec.duration_minutes - EPS:
            report.with_issue(
                Issue(
                    code=IssueCode.WINDOW_TOO_SHORT_FOR_DURATION,
                    field="duration_minutes",
                    message=(
                        f"{spec.normalized_name} cannot finish: it needs "
                        f"{_fmt(spec.duration_minutes, 'min', 0)} to run but only "
                        f"{_fmt(window, 'min', 0)} is available before its deadline."
                    ),
                    detail={
                        "duration_minutes": spec.duration_minutes,
                        "window_minutes": window,
                    },
                )
            )
    elif spec.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
        chunk = spec.min_chunk_minutes
        if chunk is not None and window < chunk - EPS:
            report.with_issue(
                Issue(
                    code=IssueCode.WINDOW_TOO_SHORT_FOR_CHUNK,
                    field="min_chunk_minutes",
                    message=(
                        f"{spec.normalized_name} cannot charge: its minimum chunk is "
                        f"{_fmt(chunk, 'min', 0)} but only {_fmt(window, 'min', 0)} is available."
                    ),
                    detail={"min_chunk_minutes": chunk, "window_minutes": window},
                )
            )
    elif spec.job_type is LoadType.THERMAL:
        if spec.thermal is None:
            report.with_issue(
                Issue(
                    code=IssueCode.THERMAL_SPEC_MISSING,
                    field="thermal",
                    message="A thermal load needs its comfort band and dynamics before it can be simulated.",
                )
            )
        elif window < spec.thermal.resolution_minutes - EPS:
            report.with_issue(
                Issue(
                    code=IssueCode.WINDOW_TOO_SHORT_FOR_CHUNK,
                    field="release_at",
                    message=(
                        f"{spec.normalized_name} needs at least one "
                        f"{spec.thermal.resolution_minutes}-minute thermal step, but the "
                        "window is shorter than that."
                    ),
                    detail={"resolution_minutes": spec.thermal.resolution_minutes, "window_minutes": window},
                )
            )
    return report


# --- Energy feasibility (§21, §22) ----------------------------------------


def is_energy_feasible(spec: LoadSpec) -> FeasibilityReport:
    """Can `energy_required_kwh` physically be delivered before the deadline?

    The headline case from §21: 50 kWh through a 5 kW supply in a 2 h window is
    impossible, and the user should be told that in those numbers rather than
    discovering it when the scheduler fails.
    """
    report = FeasibilityReport(checks_run=["energy"])
    if spec.job_type is LoadType.FIXED:
        return report

    if spec.job_type is LoadType.DEFERRABLE_ATOMIC:
        derived = spec.derived_energy_kwh()
        if derived is not None and spec.energy_required_kwh is not None:
            if abs(derived - spec.energy_required_kwh) > 1e-6:
                report.with_issue(
                    Issue(
                        code=IssueCode.ENERGY_EXCEEDS_WINDOW,
                        severity=Severity.WARNING,
                        field="energy_required_kwh",
                        message=(
                            f"Stated energy {_fmt(spec.energy_required_kwh, 'kWh')} does not match "
                            f"{_fmt(spec.power_kw, 'kW')} for {_fmt(spec.duration_minutes, 'min', 0)} "
                            f"({_fmt(derived, 'kWh')}); duration is what defines an atomic load."
                        ),
                        detail={"stated": spec.energy_required_kwh, "derived": derived},
                    )
                )
        return report

    if spec.job_type is LoadType.THERMAL:
        return report

    # DEFERRABLE_INTERRUPTIBLE: energy is the primary requirement.
    if spec.energy_required_kwh is None:
        report.with_issue(
            Issue(
                code=IssueCode.ENERGY_MISSING,
                field="energy_required_kwh",
                message=(
                    "An interruptible load needs a total energy target; without one there is "
                    "no requirement to satisfy."
                ),
            )
        )
        return report

    max_power = spec.effective_max_power_kw()
    if max_power is None:
        report.with_issue(
            Issue(
                code=IssueCode.MAX_POWER_MISSING,
                field="max_power_kw",
                message=(
                    "No charging power limit is known, so it is impossible to tell whether "
                    "the energy target can be met in time."
                ),
            )
        )
        return report
    if max_power <= 0:
        report.with_issue(
            Issue(
                code=IssueCode.MAX_POWER_MISSING,
                field="max_power_kw",
                message="The configured charging power is zero, so no energy can be delivered.",
                detail={"max_power_kw": max_power},
            )
        )
        return report

    window = spec.window_minutes()
    if window is None:
        report.with_issue(
            Issue(
                code=IssueCode.DEADLINE_MISSING,
                field="deadline_at",
                message="Without a release time and a deadline there is no window to deliver energy in.",
            )
        )
        return report

    energy = spec.energy_required_kwh
    needed_hours = energy / max_power
    needed_minutes = needed_hours * 60.0

    # Minimum chunks quantize usable time downward: partial chunks cannot run.
    chunk = spec.min_chunk_minutes
    usable_minutes = window
    if chunk:
        usable_minutes = math.floor(window / chunk) * chunk
        if usable_minutes + EPS < chunk:
            report.with_issue(
                Issue(
                    code=IssueCode.MIN_CHUNK_EXCEEDS_WINDOW,
                    field="min_chunk_minutes",
                    message=(
                        f"The minimum chunk of {_fmt(chunk, 'min', 0)} does not fit in the "
                        f"{_fmt(window, 'min', 0)} window, so this load cannot run at all."
                    ),
                    detail={"min_chunk_minutes": chunk, "window_minutes": window},
                )
            )
            return report

    deliverable = max_power * usable_minutes / 60.0
    if energy > deliverable + 1e-6:
        report.with_issue(
            Issue(
                code=IssueCode.ENERGY_EXCEEDS_WINDOW,
                field="energy_required_kwh",
                message=(
                    f"{spec.normalized_name} cannot receive {_fmt(energy, 'kWh')} before "
                    f"{spec.deadline_at.isoformat() if spec.deadline_at else 'its deadline'} at "
                    f"{_fmt(max_power, 'kW')}: it would need "
                    f"{_fmt(needed_hours, 'h')} of charging, but the window is only "
                    f"{_fmt(window / 60.0, 'h')} and can deliver at most "
                    f"{_fmt(deliverable, 'kWh')}."
                ),
                detail={
                    "energy_required_kwh": energy,
                    "max_power_kw": max_power,
                    "window_minutes": window,
                    "usable_minutes": usable_minutes,
                    "max_deliverable_kwh": deliverable,
                    "hours_needed": needed_hours,
                    "minutes_needed": needed_minutes,
                },
            )
        )
    else:
        # The deliverable check above is the only hard bound: a target that
        # exactly fills the window is feasible, not an error.
        slack_minutes = window - needed_minutes
        if slack_minutes > 0 and slack_minutes < (chunk or 0):
            report.with_issue(
                Issue(
                    code=IssueCode.MIN_CHUNK_EXCEEDS_WINDOW,
                    severity=Severity.WARNING,
                    field="min_chunk_minutes",
                    message=(
                        f"{spec.normalized_name} has only {_fmt(slack_minutes, 'min', 0)} of "
                        f"spare time, which is less than one {_fmt(chunk, 'min', 0)} minimum "
                        "chunk. It is feasible but leaves no room to reschedule."
                    ),
                    detail={"slack_minutes": slack_minutes, "min_chunk_minutes": chunk},
                )
            )
    return report


# --- Thermal feasibility (§7, §13, §22) -----------------------------------


def is_thermal_profile_feasible(
    spec: LoadSpec,
    power_profile: Optional[Sequence[float]] = None,
) -> FeasibilityReport:
    """Validate a thermal load, optionally against a concrete power profile.

    Two modes, both deterministic:
      * WITH a profile — simulate it and report every step that left the band.
      * WITHOUT one — check that the service target is physically reachable at
        all within the window. That is a reachability statement, not a plan:
        it does not claim any schedule attains the target.
    """
    report = FeasibilityReport(checks_run=["thermal"])
    if spec.job_type is not LoadType.THERMAL:
        return report
    if spec.thermal is None:
        report.with_issue(
            Issue(
                code=IssueCode.THERMAL_SPEC_MISSING,
                field="thermal",
                message="A thermal load needs its comfort band and dynamics before it can be checked.",
            )
        )
        return report

    thermal = spec.thermal
    model = ThermalModel.from_spec(thermal, name=spec.normalized_name)

    if spec.power_kw is not None and spec.power_kw > thermal.max_power_kw + EPS:
        report.with_issue(
            Issue(
                code=IssueCode.POWER_EXCEEDS_MODEL_MAX,
                field="power_kw",
                message=(
                    f"{spec.normalized_name} is rated at {_fmt(spec.power_kw, 'kW')} but its "
                    f"thermal model caps input at {_fmt(thermal.max_power_kw, 'kW')}."
                ),
                detail={"power_kw": spec.power_kw, "model_max_power_kw": thermal.max_power_kw},
            )
        )

    initial = thermal.temperature_initial_c
    if initial is not None and not (
        thermal.temperature_min_c - EPS <= initial <= thermal.temperature_max_c + EPS
    ):
        report.with_issue(
            Issue(
                code=IssueCode.THERMAL_INITIAL_OUT_OF_BAND,
                severity=Severity.WARNING,
                field="temperature_initial_c",
                message=(
                    f"{spec.normalized_name} starts at {_fmt(initial, '°C', 1)}, outside its "
                    f"comfort band {thermal.band_text()}. It starts out of band rather than "
                    "being unschedulable."
                ),
                detail={"initial_c": initial, "band": thermal.band_text()},
            )
        )

    if power_profile is not None:
        try:
            profile: ThermalProfile = model.simulate(
                thermal.temperature_initial_c
                if thermal.temperature_initial_c is not None
                else thermal.temperature_min_c,
                list(power_profile),
            )
        except ValueError as exc:
            report.with_issue(
                Issue(
                    code=IssueCode.THERMAL_BAND_VIOLATION,
                    field="thermal",
                    message=f"{spec.normalized_name} could not be simulated: {exc}",
                    detail={"reason": str(exc)},
                )
            )
            return report
        violations = profile.band_violations()
        if violations:
            # Not every excursion outside the band is the schedule's fault.
            # A room cooling down from 30 °C is ABOVE its comfort band on the
            # way in, and that is expected, not a failure. What matters is
            # whether the trajectory is closing the gap (transient) or pushing
            # the state further out / through the far bound (a real error).
            caused: list[dict] = []
            transient: list[dict] = []
            for v in violations:
                i = v["step"]
                distance_now = _distance_outside(thermal, v["temperature_c"])
                distance_before = (
                    _distance_outside(thermal, profile.temperatures[i - 1]) if i > 0 else distance_now
                )
                v = {**v, "distance_c": distance_now}
                if i == 0 or distance_now < distance_before - EPS:
                    transient.append(v)
                else:
                    caused.append(v)

            if transient:
                report.with_issue(
                    Issue(
                        code=IssueCode.THERMAL_INITIAL_OUT_OF_BAND,
                        severity=Severity.WARNING,
                        field="thermal",
                        message=(
                            f"{spec.normalized_name} is outside its comfort band "
                            f"{thermal.band_text()} on {len(transient)} step(s) while moving "
                            f"back toward it. The starting state is a precondition, not "
                            "something the schedule can change."
                        ),
                        detail={"transient_steps": transient},
                    )
                )
            if caused:
                first = caused[0]
                report.with_issue(
                    Issue(
                        code=IssueCode.THERMAL_BAND_VIOLATION,
                        field="thermal",
                        message=(
                            f"{spec.normalized_name} leaves its comfort band "
                            f"{thermal.band_text()} on {len(caused)} of "
                            f"{len(profile.temperatures)} steps; first at step {first['step']} "
                            f"({_fmt(first['temperature_c'], '°C', 1)}, "
                            f"{first['violation'].replace('_', ' ')})."
                        ),
                        detail={
                            "violations": caused,
                            "band_min_c": thermal.temperature_min_c,
                            "band_max_c": thermal.temperature_max_c,
                        },
                    )
                )
        return report

    target = thermal.temperature_target_c
    window = spec.window_minutes()
    if target is None or initial is None or window is None:
        return report

    steps_available = int(window // thermal.resolution_minutes)
    steps_needed = model.minimum_steps_to_reach(initial, target, max_steps=max(steps_available, 1))
    if steps_needed is None:
        lo, hi = model.extreme_reachable(initial, steps_available)
        report.with_issue(
            Issue(
                code=IssueCode.THERMAL_TARGET_UNREACHABLE,
                field="temperature_target_c",
                message=(
                    f"{spec.normalized_name} cannot reach {_fmt(target, '°C', 1)} from "
                    f"{_fmt(initial, '°C', 1)} within {_fmt(window / 60.0, 'h')}: the reachable "
                    f"range over that window is {_fmt(lo, '°C', 1)} to {_fmt(hi, '°C', 1)}."
                ),
                detail={
                    "target_c": target,
                    "initial_c": initial,
                    "reachable_min_c": lo,
                    "reachable_max_c": hi,
                    "window_minutes": window,
                },
            )
        )
    return report


# --- Whole-load validation -------------------------------------------------


def validate_load(spec: LoadSpec, now=None) -> FeasibilityReport:
    """Run every check that applies to this load's class.

    Phase 1's `core.validation.validate_job` still guards the Phase 1 `Job`
    contract used by POST /schedule; this is the Phase 3 equivalent for the
    richer `LoadSpec`. They are separate on purpose — a LoadSpec knows about
    thermal state and provenance that a Job does not carry.
    """
    report = FeasibilityReport()
    report.merge(is_time_feasible(spec))
    report.merge(is_energy_feasible(spec))
    report.merge(is_thermal_profile_feasible(spec))

    if now is not None and spec.deadline_at is not None and spec.deadline_at < now:
        report.with_issue(
            Issue(
                code=IssueCode.DEADLINE_IN_PAST,
                field="deadline_at",
                message=(
                    f"The deadline {spec.deadline_at.isoformat()} is already in the past."
                ),
                detail={"deadline_at": spec.deadline_at.isoformat(), "now": now.isoformat()},
            )
        )

    missing = spec.missing_required_fields()
    if missing:
        # This is deliberately a WARNING even though the per-check reports may
        # already have flagged the same gap as an error. The two answer
        # different questions: the errors say "this cannot be scheduled with the
        # information given", while this one says "here is exactly what to ask
        # the user for next".
        names = ", ".join(f.value for f in missing)
        report.with_issue(
            Issue(
                code=IssueCode.INCOMPLETE_SPEC,
                severity=Severity.WARNING,
                field=",".join(f.value for f in missing),
                message=(
                    f"{spec.normalized_name} is not yet fully specified: still missing "
                    f"{names}. It can be described but not scheduled."
                ),
                detail={"missing_fields": [f.value for f in missing]},
            )
        )
    return report


def iter_all_reports(spec: LoadSpec) -> Iterable[FeasibilityReport]:
    """Individually runnable checks, for callers that want them separately."""
    yield is_time_feasible(spec)
    yield is_energy_feasible(spec)
    yield is_thermal_profile_feasible(spec)
