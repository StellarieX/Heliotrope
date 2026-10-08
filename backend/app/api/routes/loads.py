"""POST /api/v1/loads/classify and POST /api/v1/loads/validate (Phase 3, §18).

    /classify   free text (+ whatever numbers the user has) -> classification
                + confidence + assumptions + a canonical LoadSpec.
                Never depends on an external service, so it works offline and
                returns the same answer every time.

    /validate   a fully specified LoadSpec -> a structured feasibility verdict.

    /prioritize loads -> which one matters first (Jev + a transparent composite).

Nothing here schedules anything. Classification is not prioritization and
neither is optimization; use POST /schedule for the real engines.

Error vocabulary, matching the Phase 2 routes:
    422 invalid request
    503 selected intelligence provider unavailable
"""

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import math
from datetime import datetime, timezone

from ...core.feasibility import FeasibilityReport, is_thermal_profile_feasible, validate_load
from ...domain.loads import Assumption, LoadSemantics, LoadSpec
from ...services.classification import Classification
from ...services.load_intelligence import IntelligenceUnavailable, get_load_intelligence
from ...services.load_normalizer import LoadRequest
from ...services.prioritization import PriorityRequest, prioritize

router = APIRouter()


def _err(detail: str, code: str, status: int) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"detail": detail, "code": code, "message": detail},
    )


class ClassifyResponse(BaseModel):
    provider: str
    classification: Classification
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguous: bool
    assumptions: list[Assumption] = Field(default_factory=list)
    normalized_load_spec: LoadSpec
    feasibility: FeasibilityReport


class ValidateRequest(LoadSpec):
    """A fully specified load. Produce one with POST /loads/classify first."""

    # Optional concrete power profile; when present the thermal check simulates
    # it instead of asking whether the target is reachable at all.
    power_profile: Optional[list[float]] = None

    @field_validator("power_profile")
    @classmethod
    def _profile_sane(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is None:
            return v
        if not v:
            raise ValueError("power_profile must not be empty when provided")
        for entry in v:
            if not math.isfinite(entry):
                raise ValueError("power_profile entries must be finite numbers")
            if entry < 0:
                raise ValueError("power_profile entries must be >= 0")
        return v


class ValidateResponse(BaseModel):
    feasible: bool
    checks_run: list[str] = Field(default_factory=list)
    errors: list = Field(default_factory=list)
    warnings: list = Field(default_factory=list)
    semantics: LoadSemantics
    explanation: str
    summary: str
    metric_inputs: dict = Field(default_factory=dict)


@router.post("/loads/classify")
def classify_load(body: LoadRequest) -> JSONResponse:
    provider = get_load_intelligence()
    try:
        classification = provider.classify(body.name)
        spec = provider.normalize(body)
    except IntelligenceUnavailable as exc:
        detail = str(exc)
        return _err(detail, "provider_unavailable", 503)
    except ValueError as exc:
        # e.g. a release time after the deadline: the request is wrong, not the server.
        return _err(str(exc), "invalid_request", 422)

    report = validate_load(spec, now=datetime.now(timezone.utc))
    response = ClassifyResponse(
        # Say who actually answered: the provider may have fallen back to the rules for this call.
        provider=provider.name if classification.matched_rule == provider.name else "rule_based",
        classification=classification,
        confidence=spec.confidence,
        ambiguous=spec.ambiguous,
        assumptions=spec.assumptions,
        normalized_load_spec=spec,
        feasibility=report,
    )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


@router.post("/loads/prioritize")
def prioritize_loads(body: PriorityRequest) -> JSONResponse:
    """Order loads by how much each needs attention. Jev judges how essential each
    appliance is; time pressure, size and rigidity are computed here. Falls back to a
    labelled heuristic when Jev is unconfigured or unreachable, never to an error."""
    return JSONResponse(status_code=200, content=prioritize(body.loads).model_dump(mode="json"))


@router.post("/loads/validate")
def validate(body: ValidateRequest) -> JSONResponse:
    power_profile = body.power_profile
    # `power_profile` is a validation aid, not part of the load itself.
    body.power_profile = None

    report = validate_load(body, now=datetime.now(timezone.utc))
    if power_profile is not None:
        report.merge(is_thermal_profile_feasible(body, power_profile))

    response = ValidateResponse(
        feasible=report.feasible,
        checks_run=list(dict.fromkeys(report.checks_run)),
        errors=[i.model_dump(mode="json") for i in report.errors],
        warnings=[i.model_dump(mode="json") for i in report.warnings],
        semantics=body.semantics,
        explanation=body.explanation,
        summary=report.explain(),
        metric_inputs=body.metric_inputs(),
    )
    payload = response.model_dump(mode="json")
    # lib/api/types.ts names this field `load_type`; the domain calls it
    # `job_type`. Emit both so the TS contract and existing consumers agree.
    sem = payload.get("semantics")
    if isinstance(sem, dict) and "job_type" in sem and "load_type" not in sem:
        sem["load_type"] = sem["job_type"]
    return JSONResponse(status_code=200, content=payload)
