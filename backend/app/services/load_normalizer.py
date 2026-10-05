"""Classification -> canonical LoadSpec (§9, §17, §18).

The second half of the pipeline, deliberately separate from classification:

    classifier  -> WHAT KIND OF LOAD IS THIS?
    normalizer  -> WHAT EXACTLY DOES THE USER NEED?

USER INTENT vs PHYSICAL MODEL (§17) is the whole point of this module. "EV
charged by 7am" is intent; it becomes `energy_required_kwh`, `deadline_at` and
`max_power_kw`, which is physical requirement. The intent string is preserved on
`LoadSpec.user_input`, and every step taken to get from the sentence to the
numbers leaves an `Assumption` behind. Nothing is inferred silently: the
normalizer NEVER invents a power rating, an energy total or a duration. If the
user did not say it, the field stays `None` and the load is reported as not yet
fully specified.
"""

from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from ..domain.loads import (
    Assumption,
    LoadSpec,
    LoadType,
    ParameterOrigin,
    Recurrence,
    RequiredField,
    ThermalSpec,
    required_fields_for,
    semantics_for,
)
from ..domain.thermal_examples import example_for
from ..utils.time import (
    WallClockError,
    parse_wall_clock,
    resolve_wall_window,
)
from .classification import Classification, RuleBasedLoadClassifier


class LoadRequest(BaseModel):
    """A user's description of one load, plus whatever numbers they supplied.

    Wall-clock strings are accepted alongside absolute timestamps so the
    frontend's existing "ready by HH:MM" input keeps working (§15).
    """

    name: str = Field(min_length=1, max_length=400)
    power_kw: Optional[float] = Field(default=None, ge=0)
    max_power_kw: Optional[float] = Field(default=None, ge=0)
    duration_minutes: Optional[int] = Field(default=None, gt=0)
    energy_required_kwh: Optional[float] = Field(default=None, ge=0)
    min_chunk_minutes: Optional[int] = Field(default=None, gt=0)
    release_wall: Optional[str] = None
    deadline_wall: Optional[str] = None
    release_at: Optional[datetime] = None
    deadline_at: Optional[datetime] = None
    crosses_midnight: Optional[bool] = None
    timezone: str = Field(default="UTC", min_length=1)
    thermal: Optional[ThermalSpec] = None
    job_type: Optional[LoadType] = None
    category: Optional[str] = Field(default=None, max_length=60)
    recurrence: Optional[Recurrence] = None
    id: str = Field(default="", max_length=120)


class LoadIntelligenceService:
    """Compose classification and normalization behind one seam.

    The Protocol in services/load_intelligence.py is the public contract; this
    class is the rules-first implementation of it.
    """

    name = "rule_based"

    def __init__(self, classifier: RuleBasedLoadClassifier | None = None) -> None:
        self.classifier = classifier or RuleBasedLoadClassifier()

    def classify(self, text: str) -> Classification:
        return self.classifier.classify(text)

    def normalize(self, request: LoadRequest, now: Optional[datetime] = None) -> LoadSpec:
        return normalize_request(request, now=now, classifier=self.classifier)


def _resolve_timezone(name: str) -> tuple[tzinfo, Optional[str]]:
    """Return (tz, warning). Unknown zones fall back to UTC and say so."""
    if name.upper() == "UTC":
        return timezone.utc, None
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc, f"timezone {name!r} is not recognized; UTC was used instead"


def _window(
    request: LoadRequest, tz: tzinfo, now: datetime
) -> tuple[Optional[datetime], Optional[datetime], list[Assumption], Optional[str]]:
    """Resolve release/deadline from whichever form the caller supplied."""
    assumptions: list[Assumption] = []
    if request.release_at is not None or request.deadline_at is not None:
        release_at, deadline_at = request.release_at, request.deadline_at
        for field in ("release_at", "deadline_at"):
            value = getattr(request, field)
            if value is None:
                continue
            if value.tzinfo is None:
                coerced = value.replace(tzinfo=timezone.utc)
                if field == "release_at":
                    release_at = coerced
                else:
                    deadline_at = coerced
                assumptions.append(
                    Assumption(
                        field=field,
                        origin=ParameterOrigin.DERIVED,
                        detail=(
                            f"supplied as a timezone-naive timestamp {value.isoformat()}; "
                            "interpreted as UTC rather than the server's local zone"
                        ),
                    )
                )
            else:
                assumptions.append(
                    Assumption(
                        field=field,
                        origin=ParameterOrigin.USER_CONFIGURED,
                        detail="supplied as an absolute timezone-aware timestamp",
                    )
                )
        if release_at is not None and deadline_at is not None and release_at > deadline_at:
            raise ValueError(
                f"release_at {release_at.isoformat()} is after deadline_at "
                f"{deadline_at.isoformat()}; the window is empty"
            )
        return release_at, deadline_at, assumptions, None

    if request.release_wall and request.deadline_wall:
        try:
            window = resolve_wall_window(
                parse_wall_clock(request.release_wall),
                parse_wall_clock(request.deadline_wall),
                now,
                tz=tz,
                crosses_midnight=request.crosses_midnight,
            )
        except WallClockError as exc:
            return None, None, assumptions, str(exc)
        rollover = (
            "deadline rolled over to the next day"
            if window.crosses_midnight
            else "both ends of the window fall on the same day"
        )
        assumptions.append(
            Assumption(
                field="deadline_at",
                origin=ParameterOrigin.DERIVED,
                detail=f"resolved from local times {request.release_wall} -> {request.deadline_wall}: {rollover} ({window.rollover_source})",
            )
        )
        return window.release, window.deadline, assumptions, None

    for field, wall in (("release_at", request.release_wall), ("deadline_at", request.deadline_wall)):
        if wall:
            assumptions.append(
                Assumption(
                    field=field,
                    origin=ParameterOrigin.USER_CONFIGURED,
                    detail=f"supplied as local time {wall}",
                )
            )
    return None, None, assumptions, None


def normalize_request(
    request: LoadRequest,
    now: Optional[datetime] = None,
    classifier: RuleBasedLoadClassifier | None = None,
) -> LoadSpec:
    """Turn a description plus explicit values into a canonical LoadSpec."""
    classifier = classifier or RuleBasedLoadClassifier()
    now = now or datetime.now(timezone.utc)

    classification = classifier.classify(request.name)
    assumptions: list[Assumption] = list(classification.assumptions)
    warnings: list[str] = []

    # --- class: the user may override the classifier outright -------------
    job_type = request.job_type or classification.job_type
    if request.job_type is not None and request.job_type is not classification.job_type:
        assumptions.append(
            Assumption(
                field="job_type",
                origin=ParameterOrigin.USER_CONFIGURED,
                detail=(
                    f"user set {request.job_type.value}, overriding the classifier's "
                    f"{classification.job_type.value} for this load"
                ),
            )
        )
    category = request.category or classification.category.value

    # --- timezone ----------------------------------------------------------
    tz, tz_warning = _resolve_timezone(request.timezone)
    if tz_warning:
        warnings.append(tz_warning)
        assumptions.append(
            Assumption(
                field="timezone",
                origin=ParameterOrigin.SYNTHETIC_DEFAULT,
                detail=tz_warning,
            )
        )

    # --- window ------------------------------------------------------------
    release_at, deadline_at, window_assumptions, window_error = _window(request, tz, now)
    assumptions.extend(window_assumptions)
    if window_error:
        warnings.append(window_error)

    # --- explicitly supplied numbers ---------------------------------------
    for field in (
        "power_kw",
        "max_power_kw",
        "duration_minutes",
        "energy_required_kwh",
        "min_chunk_minutes",
    ):
        if getattr(request, field) is not None:
            assumptions.append(
                Assumption(
                    field=field,
                    origin=ParameterOrigin.USER_CONFIGURED,
                    detail=f"{field} = {getattr(request, field)}",
                )
            )

    # --- thermal -----------------------------------------------------------
    thermal: Optional[ThermalSpec] = None
    if job_type is LoadType.THERMAL:
        if request.thermal is not None:
            thermal = request.thermal
            assumptions.append(
                Assumption(
                    field="thermal",
                    origin=ParameterOrigin.USER_CONFIGURED,
                    detail="comfort band and dynamics supplied by the user",
                )
            )
        elif classification.thermal_example:
            thermal = example_for(classification.thermal_example)
            assumptions.append(
                Assumption(
                    field="thermal",
                    origin=ParameterOrigin.SYNTHETIC_DEFAULT,
                    detail=(
                        f"placeholder dynamics from the {classification.thermal_example} synthetic "
                        "example; not calibrated to any real appliance"
                    ),
                )
            )
        else:
            warnings.append(
                "A thermal load needs a comfort band and dynamics before it can be simulated."
            )
    elif request.thermal is not None:
        warnings.append(
            f"Thermal parameters were supplied for a {job_type.value} load and were ignored; "
            "they only apply to thermal loads."
        )

    # --- explanation (§20) --------------------------------------------------
    semantics = semantics_for(job_type)
    explanation = classification.reason
    if job_type is not classification.job_type:
        explanation = f"Overridden to {job_type.value}. {semantics.notes}"
    elif job_type is LoadType.THERMAL and thermal is not None:
        explanation = f"{classification.reason} Comfort band: {thermal.band_text()}."

    required_fields: list[RequiredField] = list(classification.required_fields)
    if job_type is not classification.job_type:
        required_fields = required_fields_for(job_type)

    confidence = 1.0 if request.job_type is not None else classification.confidence
    ambiguous = False if request.job_type is not None else classification.ambiguous

    spec = LoadSpec(
        id=request.id,
        user_input=request.name,
        normalized_name=classification.name,
        category=category,
        job_type=job_type,
        confidence=confidence,
        ambiguous=ambiguous,
        power_kw=request.power_kw,
        max_power_kw=request.max_power_kw,
        duration_minutes=request.duration_minutes,
        energy_required_kwh=request.energy_required_kwh,
        min_chunk_minutes=request.min_chunk_minutes,
        release_at=release_at,
        deadline_at=deadline_at,
        timezone=request.timezone,
        recurrence=request.recurrence,
        thermal=thermal,
        explanation=explanation,
        assumptions=assumptions,
        warnings=warnings,
        required_fields=required_fields,
        alternatives=[a.category.value for a in classification.alternatives],
    )
    return spec


def _required_fields_for(job_type: LoadType) -> list[RequiredField]:
    """Deprecated local alias kept so older imports keep working."""
    return required_fields_for(job_type)
