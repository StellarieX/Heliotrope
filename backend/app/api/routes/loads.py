"""POST /api/v1/loads/classify and POST /api/v1/loads/validate (Phase 3, §18).

    /classify   free text (+ whatever numbers the user has) -> classification
                + confidence + assumptions + a canonical LoadSpec.
                Never depends on an external service, so it works offline and
                returns the same answer every time.

    /validate   a fully specified LoadSpec -> a structured feasibility verdict.

Nothing here schedules anything. Classification is not prioritization and
neither is optimization; POST /schedule still returns 501 until Phase 4.

Error vocabulary, matching the Phase 2 routes:
    422 invalid request
    503 selected intelligence provider unavailable
"""

from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ...core.feasibility import FeasibilityReport, is_thermal_profile_feasible, validate_load
from ...domain.loads import Assumption, LoadSemantics, LoadSpec
from ...services.classification import Classification
from ...services.load_intelligence import IntelligenceUnavailable, get_load_intelligence
from ...services.load_normalizer import LoadRequest

router = APIRouter()


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
        return JSONResponse(
            status_code=503, content={"detail": str(exc), "code": "provider_unavailable"}
        )

    report = validate_load(spec)
    response = ClassifyResponse(
        provider=provider.name,
        classification=classification,
        confidence=spec.confidence,
        ambiguous=spec.ambiguous,
        assumptions=spec.assumptions,
        normalized_load_spec=spec,
        feasibility=report,
    )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))


@router.post("/loads/validate")
def validate(body: ValidateRequest) -> JSONResponse:
    power_profile = body.power_profile
    # `power_profile` is a validation aid, not part of the load itself.
    body.power_profile = None

    report = validate_load(body)
    if power_profile is not None:
        report.merge(is_thermal_profile_feasible(body, power_profile))

    response = ValidateResponse(
        feasible=report.feasible,
        checks_run=report.checks_run,
        errors=[i.model_dump(mode="json") for i in report.errors],
        warnings=[i.model_dump(mode="json") for i in report.warnings],
        semantics=body.semantics,
        explanation=body.explanation,
        summary=report.explain(),
        metric_inputs=body.metric_inputs(),
    )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
