"""Load intelligence provider boundary (Phase 3, §11, §26; M4 live).

    LoadIntelligenceProvider
            |
    RuleBasedLoadIntelligence   <-- the default, always available
            |
    JevLoadIntelligence         <-- Gemini-structured classifier with honest fallback

WHY RULES FIRST. The backend must boot, classify and validate with no network,
no API key and no model. That buys four things this phase needs: deterministic
tests, an offline demo, low latency, and graceful degradation when an external
service is down.

Jev/Gemini is a live adapter, not a fabrication (§26): when a key is present it
attempts one structured Gemini call and strictly validates the JSON against the
Classification enums. ANY failure — no key, HTTP error, bad JSON, unknown enum —
falls back to the RuleBasedLoadClassifier and records the fallback as an
Assumption (ParameterOrigin.ESTIMATED), so the response always says what it is.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

from ..core import config
from ..domain.loads import (
    Assumption,
    LoadCategory,
    LoadType,
    ParameterOrigin,
    required_fields_for,
    semantics_for,
)
from .classification import (
    Classification,
    RuleBasedLoadClassifier,
    display_name,
    normalize_text,
)
from .load_normalizer import LoadIntelligenceService, LoadRequest, normalize_request
from ..domain.loads import LoadSpec

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_TIMEOUT_S = 10.0


class IntelligenceUnavailable(RuntimeError):
    """The selected intelligence provider cannot serve this request."""


class IntelligenceNotConfigured(IntelligenceUnavailable):
    """Selected provider has no credentials."""


class IntelligenceNotIntegrated(IntelligenceUnavailable):
    """Credentials exist but no upstream contract is verified yet."""


class LoadIntelligenceProvider:
    """What the API and the frontend depend on."""

    name: str

    def classify(self, text: str) -> Classification: ...

    def normalize(self, request: LoadRequest, now=None) -> LoadSpec: ...


class RuleBasedLoadIntelligence(LoadIntelligenceService):
    """The default. Deterministic, offline, dependency-free."""

    name = "rule_based"

    def __init__(self, classifier: RuleBasedLoadClassifier | None = None) -> None:
        super().__init__(classifier=classifier)


def resolve_api_key(explicit: str | None = None) -> str | None:
    """Precedence: explicit arg > GEMINI_API_KEY > JEV_API_KEY (legacy alias)."""
    if explicit:
        return explicit
    return config.GEMINI_API_KEY or config.JEV_API_KEY


class _GeminiUnusable(RuntimeError):
    """Internal: Gemini did not yield a trustworthy classification."""


def _gemini_prompt(text: str) -> str:
    categories = sorted(c.value for c in LoadCategory)
    job_types = sorted(t.value for t in LoadType)
    return (
        "Classify this household load for energy scheduling. "
        "Respond with ONLY a JSON object, no markdown, no explanation, with keys: "
        '"job_type" (one of ' + ", ".join(job_types) + "), "
        '"category" (one of ' + ", ".join(categories) + '), '
        'optional "power_kw" (number >= 0), optional "duration_minutes" (integer > 0). '
        f'Load description: "{text}"'
    )


def _extract_gemini_text(payload: dict) -> str:
    try:
        candidates = payload["candidates"]
        text = candidates[0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise _GeminiUnusable(f"unexpected Gemini response shape: {exc}") from exc
    if not isinstance(text, str) or not text.strip():
        raise _GeminiUnusable("empty Gemini response text")
    return text.strip()


def _parse_gemini_payload(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise _GeminiUnusable(f"Gemini response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _GeminiUnusable("Gemini response JSON is not an object")
    return data


def _validate_gemini_data(data: dict) -> tuple[LoadType, LoadCategory, Optional[float], Optional[int]]:
    try:
        job_type = LoadType(str(data["job_type"]))
    except (KeyError, ValueError) as exc:
        raise _GeminiUnusable(f"unknown job_type {data.get('job_type')!r}: {exc}") from exc
    try:
        category = LoadCategory(str(data["category"]))
    except (KeyError, ValueError) as exc:
        raise _GeminiUnusable(f"unknown category {data.get('category')!r}: {exc}") from exc
    power_kw: Optional[float] = None
    if data.get("power_kw") is not None:
        try:
            power_kw = float(data["power_kw"])
        except (TypeError, ValueError) as exc:
            raise _GeminiUnusable(f"bad power_kw {data.get('power_kw')!r}") from exc
        if power_kw < 0:
            raise _GeminiUnusable(f"negative power_kw {power_kw!r}")
    duration_minutes: Optional[int] = None
    if data.get("duration_minutes") is not None:
        try:
            duration_minutes = int(data["duration_minutes"])
        except (TypeError, ValueError) as exc:
            raise _GeminiUnusable(f"bad duration_minutes {data.get('duration_minutes')!r}") from exc
        if duration_minutes <= 0:
            raise _GeminiUnusable(f"non-positive duration_minutes {duration_minutes!r}")
    return job_type, category, power_kw, duration_minutes


def _gemini_classify_raw(text: str, api_key: str) -> tuple[LoadType, LoadCategory, Optional[float], Optional[int]]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": _gemini_prompt(text)}]}]}
    try:
        response = httpx.post(
            url, params={"key": api_key}, json=body, timeout=GEMINI_TIMEOUT_S
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise _GeminiUnusable(f"Gemini request failed: {exc}") from exc
    raw_text = _extract_gemini_text(payload)
    data = _parse_gemini_payload(raw_text)
    return _validate_gemini_data(data)


def _fallback_classification(text: str, reason: str) -> Classification:
    base = RuleBasedLoadClassifier().classify(text)
    note = Assumption(
        field="classifier",
        origin=ParameterOrigin.ESTIMATED,
        detail=f"Gemini unavailable ({reason}); rule-based fallback used",
    )
    return base.model_copy(update={"assumptions": [*base.assumptions, note]})


def _gemini_classification(
    text: str, job_type: LoadType, category: LoadCategory,
    power_kw: Optional[float], duration_minutes: Optional[int],
) -> Classification:
    sem = semantics_for(job_type)
    assumptions = [
        Assumption(
            field="category",
            origin=ParameterOrigin.ESTIMATED,
            detail="category and job type proposed by Gemini structured output; verify before relying on it",
        )
    ]
    if power_kw is not None:
        assumptions.append(
            Assumption(
                field="power_kw",
                origin=ParameterOrigin.ESTIMATED,
                detail=f"Gemini suggested power_kw={power_kw}; not measured, confirm it",
            )
        )
    if duration_minutes is not None:
        assumptions.append(
            Assumption(
                field="duration_minutes",
                origin=ParameterOrigin.ESTIMATED,
                detail=f"Gemini suggested duration_minutes={duration_minutes}; not measured, confirm it",
            )
        )
    return Classification(
        name=display_name(text),
        input=text or "",
        category=category,
        job_type=job_type,
        shiftable=sem.shiftable,
        # Unverified LLM output stays below the rule-based low-confidence
        # threshold (0.70) and is flagged ambiguous: a valid enum is not
        # evidence the label is right, and 0.75 would claim otherwise.
        confidence=0.6,
        ambiguous=True,
        reason=(
            "Gemini structured classification of the load description. "
            "Unverified model output — confirm the category before relying on it."
        ),
        matched_rule="gemini",
        alternatives=[],
        required_fields=list(required_fields_for(job_type)),
        thermal_example=None,
        assumptions=assumptions,
    )


class JevLoadIntelligence:
    """Gemini-backed classifier with per-call honest fallback.

    `available()` is True when a key is configured (GEMINI_API_KEY or the
    JEV_API_KEY legacy alias). Every `classify`/`normalize` call attempts one
    Gemini structured call and falls back to the rule-based classifier on any
    failure, recording the fallback as an assumption.
    """

    name = "jev"

    def __init__(self, api_key: str | None = None) -> None:
        self._explicit_key = api_key
        self._rules = RuleBasedLoadClassifier()

    @property
    def configured(self) -> bool:
        return bool(resolve_api_key(self._explicit_key))

    def available(self) -> bool:
        """True when a key is present. Per-call success is not cached."""
        return self.configured

    def _classify_inner(self, text: str) -> tuple[Classification, dict[str, Any]]:
        key = resolve_api_key(self._explicit_key)
        if not key:
            return _fallback_classification(text, "no API key configured"), {}
        try:
            job_type, category, power_kw, duration = _gemini_classify_raw(text, key)
        except _GeminiUnusable as exc:
            return _fallback_classification(text, str(exc)), {}
        classification = _gemini_classification(text, job_type, category, power_kw, duration)
        hints: dict[str, Any] = {}
        if power_kw is not None:
            hints["power_kw"] = power_kw
        if duration is not None:
            hints["duration_minutes"] = duration
        hints["_classification"] = classification
        return classification, hints

    def classify(self, text: str) -> Classification:
        classification, _ = self._classify_inner(text or "")
        return classification

    def normalize(self, request: LoadRequest, now=None) -> LoadSpec:
        classification, hints = self._classify_inner(request.name)
        patched = request
        fill: dict[str, Any] = {}
        if hints.get("power_kw") is not None and request.power_kw is None:
            fill["power_kw"] = hints["power_kw"]
        if hints.get("duration_minutes") is not None and request.duration_minutes is None:
            fill["duration_minutes"] = hints["duration_minutes"]
        if fill:
            patched = request.model_copy(update=fill)

        class _Fixed:
            name = "gemini_or_rules"

            def __init__(self, fixed: Classification, rules: RuleBasedLoadClassifier) -> None:
                self._fixed = fixed
                self._rules = rules

            def classify(self, text: str) -> Classification:
                if normalize_text(text) == normalize_text(self._fixed.input):
                    return self._fixed
                return self._rules.classify(text)

        spec = normalize_request(
            patched, now=now, classifier=_Fixed(classification, self._rules)  # type: ignore[arg-type]
        )
        if fill:
            extra = [
                Assumption(
                    field=field,
                    origin=ParameterOrigin.ESTIMATED,
                    detail=f"{field} suggested by Gemini; not measured, confirm it",
                )
                for field in fill
            ]
            spec = spec.model_copy(update={"assumptions": [*spec.assumptions, *extra]})
        return spec


def get_load_intelligence() -> LoadIntelligenceProvider:
    """The provider the API uses.

    Rule-based by default and whenever unconfigured. `"jev"` is honored only
    when a Gemini/Jev key is present; the Jev provider itself still falls back
    per call, so callers never see an exception for a missing model.
    """
    requested = (config.LOAD_INTELLIGENCE_PROVIDER or "rule_based").lower()
    if requested == "jev":
        jev = JevLoadIntelligence(resolve_api_key())
        if jev.available():
            return jev  # type: ignore[return-value]
    return RuleBasedLoadIntelligence()
