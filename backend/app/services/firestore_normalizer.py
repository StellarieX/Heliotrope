"""Firestore job -> LoadSpec normalization (Phase 3, §19).

Existing user data must keep working, so this module reads the Phase 2 document
shape and fills in Phase 3 fields only where the user actually supplied them.

The legacy document is:

    name, kind, shiftable, powerKw, readyBy ("HH:MM"), flexHours

Phase 3 adds optional fields (jobType, energyKwh, durationMin, minChunkMin,
maxPowerKw, thermal...). Documents without them are still valid; what they are
NOT is complete.

THE RULE HERE IS "UNKNOWN IS NOT ZERO". The legacy frontend helper
`lib/jobs/normalize.ts` filled in a 60-minute duration and derived energy as
`power x duration` for every job. That is not reproduced here: a document with
no duration gets `duration_minutes = None` plus an explicit LEGACY_INFERRED
assumption saying so. Inventing a duration would make an atomic job schedulable
on a number nobody chose, which is exactly the fake precision §30 forbids.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from ..core import config
from ..domain.loads import (
    Assumption,
    LoadSpec,
    LoadType,
    ParameterOrigin,
    RequiredField,
    ThermalSpec,
    required_fields_for,
)
from ..utils.time import WallClockError, parse_wall_clock
from .classification import RuleBasedLoadClassifier


class FirestoreJobDocument(BaseModel):
    """A stored job. Unknown fields are tolerated so a future schema never
    breaks today's normalizer."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = ""
    name: str = Field(min_length=1, max_length=120)
    kind: str | None = None
    shiftable: bool | None = None
    power_kw: float = Field(default=0.0, alias="powerKw")
    ready_by: str | None = Field(default=None, alias="readyBy")
    flex_hours: float = Field(default=0.0, alias="flexHours")
    created_at: str | None = Field(default=None, alias="createdAt")

    # Phase 3 optional fields. Absent on every document written before Phase 3.
    job_type: Optional[LoadType] = Field(default=None, alias="jobType")
    max_power_kw: Optional[float] = Field(default=None, alias="maxPowerKw")
    duration_minutes: Optional[int] = Field(default=None, alias="durationMin")
    energy_required_kwh: Optional[float] = Field(default=None, alias="energyKwh")
    min_chunk_minutes: Optional[int] = Field(default=None, alias="minChunkMin")
    thermal: Optional[ThermalSpec] = None
    confidence: Optional[float] = Field(default=None, alias="confidence")


def _tz(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone.utc


def normalize_firestore_job(
    doc: FirestoreJobDocument | dict,
    now: Optional[datetime] = None,
    tz_name: Optional[str] = None,
    classifier: RuleBasedLoadClassifier | None = None,
) -> LoadSpec:
    """Normalize one stored job into a canonical LoadSpec."""
    document = doc if isinstance(doc, FirestoreJobDocument) else FirestoreJobDocument(**doc)
    classifier = classifier or RuleBasedLoadClassifier()
    now = now or datetime.now(timezone.utc)
    zone = _tz(tz_name or config.DEFAULT_LOAD_TIMEZONE)

    classification = classifier.classify(document.name)
    assumptions: list[Assumption] = []
    warnings: list[str] = []

    # --- class ------------------------------------------------------------
    job_type = document.job_type or classification.job_type
    if document.job_type is None:
        assumptions.append(
            Assumption(
                field="job_type",
                origin=ParameterOrigin.LEGACY_INFERRED,
                detail=(
                    f"this document has no jobType field; classified as {job_type.value} "
                    f"from its name"
                ),
            )
        )
    if document.shiftable is False and job_type is not LoadType.FIXED:
        assumptions.append(
            Assumption(
                field="shiftable",
                origin=ParameterOrigin.LEGACY_INFERRED,
                detail=(
                    "the stored shiftable flag is false but the classified class is "
                    f"{job_type.value}; the class was kept because it carries more physical "
                    "meaning than the flag"
                ),
            )
        )
    if document.shiftable is True and job_type is LoadType.FIXED:
        assumptions.append(
            Assumption(
                field="shiftable",
                origin=ParameterOrigin.LEGACY_INFERRED,
                detail=(
                    "the stored shiftable flag is true but this is an always-on load; it is "
                    "treated as fixed baseline load"
                ),
            )
        )

    category = document.kind or classification.category.value

    # --- window: "ready by HH:MM", anchored to now, rolling over if needed --
    release_at: Optional[datetime] = now
    deadline_at: Optional[datetime] = None
    if document.ready_by:
        try:
            deadline_at = _deadline_only(document.ready_by, now, zone)
            assumptions.append(
                Assumption(
                    field="deadline_at",
                    origin=ParameterOrigin.DERIVED,
                    detail=(
                        f"'ready by {document.ready_by}' resolved to "
                        f"{deadline_at.isoformat()} in {zone}"
                    ),
                )
            )
        except WallClockError as exc:
            warnings.append(f"readyBy {document.ready_by!r} could not be read: {exc}")
            release_at = now
    else:
        release_at = now
        warnings.append("This job has no readyBy time, so it has no deadline.")

    assumptions.append(
        Assumption(
            field="release_at",
            origin=ParameterOrigin.DERIVED,
            detail=f"release taken as 'now' ({now.isoformat()}); users do not set one today",
        )
    )

    # --- physical values: only what was stored ---------------------------
    if document.power_kw > 0:
        assumptions.append(
            Assumption(
                field="power_kw",
                origin=ParameterOrigin.USER_CONFIGURED,
                detail=f"power_kw = {document.power_kw} from the stored job",
            )
        )
    else:
        assumptions.append(
            Assumption(
                field="power_kw",
                origin=ParameterOrigin.LEGACY_INFERRED,
                detail=(
                    "the stored job has no power rating (it defaults to 0); its real draw is "
                    "unknown"
                ),
            )
        )
    if document.max_power_kw is not None:
        assumptions.append(
            Assumption(
                field="max_power_kw",
                origin=ParameterOrigin.USER_CONFIGURED,
                detail=f"max_power_kw = {document.max_power_kw}",
            )
        )

    duration = document.duration_minutes
    if duration is None:
        assumptions.append(
            Assumption(
                field="duration_minutes",
                origin=ParameterOrigin.LEGACY_INFERRED,
                detail=(
                    "this document predates duration tracking; no duration was assumed, so an "
                    "atomic load from it cannot be scheduled until the user supplies one"
                ),
            )
        )

    energy = document.energy_required_kwh
    if energy is None:
        assumptions.append(
            Assumption(
                field="energy_required_kwh",
                origin=ParameterOrigin.LEGACY_INFERRED,
                detail=(
                    "this document predates energy tracking; no energy target was assumed. "
                    "It is unknown, not zero."
                ),
            )
        )
        if job_type is LoadType.DEFERRABLE_INTERRUPTIBLE:
            warnings.append(
                "This interruptible load has no energy target yet, so there is nothing to schedule."
            )
    else:
        assumptions.append(
            Assumption(
                field="energy_required_kwh",
                origin=ParameterOrigin.USER_CONFIGURED,
                detail=f"energy_required_kwh = {energy}",
            )
        )

    thermal = document.thermal
    if job_type is LoadType.THERMAL:
        if thermal is None:
            warnings.append(
                "This thermal load has no comfort band or dynamics yet, so it cannot be simulated."
            )
    elif thermal is not None:
        thermal = None
        warnings.append("Thermal parameters were stored on a non-thermal load and were ignored.")

    explanation = classification.reason
    required_fields: list[RequiredField] = list(classification.required_fields)
    if document.job_type is not None and document.job_type is not classification.job_type:
        explanation = (
            f"Stored as {document.job_type.value} by the user, overriding the classifier's "
            f"{classification.job_type.value}. {classification.reason}"
        )
        # The override changes what the user is asked for next, so the question
        # list has to move with it.
        required_fields = required_fields_for(job_type)

    return LoadSpec(
        id=document.id,
        user_input=document.name,
        normalized_name=classification.name,
        category=category,
        job_type=job_type,
        confidence=document.confidence if document.confidence is not None else classification.confidence,
        ambiguous=classification.ambiguous and document.job_type is None,
        power_kw=document.power_kw if document.power_kw > 0 else None,
        max_power_kw=document.max_power_kw,
        duration_minutes=duration,
        energy_required_kwh=energy,
        min_chunk_minutes=document.min_chunk_minutes,
        release_at=release_at,
        deadline_at=deadline_at,
        timezone=str(zone),
        thermal=thermal,
        explanation=explanation,
        assumptions=assumptions,
        warnings=warnings,
        required_fields=required_fields,
        alternatives=[a.category.value for a in classification.alternatives],
    )


def _deadline_only(ready_by: str, now: datetime, zone: tzinfo) -> datetime:
    """Anchor a single "ready by HH:MM" onto its next occurrence after `now`.

    Same rollover rule as `resolve_wall_window`, specialized to one wall time:
    if today's occurrence has already passed (or is exactly now), the deadline
    is tomorrow's.
    """
    wall = parse_wall_clock(ready_by)
    local_now = now.astimezone(zone)
    candidate = local_now.replace(
        hour=wall.hour, minute=wall.minute, second=0, microsecond=0, fold=0
    )
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    return candidate
