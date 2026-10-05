"""Load intelligence provider boundary (Phase 3, §11, §26).

    LoadIntelligenceProvider
            |
    RuleBasedLoadIntelligence   <-- the default, always available
            |
    JevLoadIntelligence         <-- Jev (TypeSafe AI System One model), with honest fallback

WHY RULES FIRST. The backend must boot, classify and validate with no network,
no API key and no model. That buys deterministic tests, an offline demo, low
latency, and graceful degradation when an external service is down.

WHY JEV. Classifying a load is a typed decision, not a text-generation task. Jev
(see services/jev_client.py) answers two Choice questions, the scheduling
behaviour and the category, and returns calibrated probabilities and a confidence
for each. That confidence becomes the classification's confidence (it is not a
constant), and the runner-up options become its alternatives.

ANY failure (no key, HTTP error, rate limit, malformed answer) falls back to the
RuleBasedLoadClassifier and records the fallback as an Assumption
(ParameterOrigin.ESTIMATED), so the response always says what it is.
"""

from __future__ import annotations

from typing import Any

from ..core import config
from ..domain.loads import (
    Assumption,
    LoadCategory,
    LoadSpec,
    LoadType,
    ParameterOrigin,
    required_fields_for,
    semantics_for,
)
from . import jev_client
from .classification import (
    LOW_CONFIDENCE_THRESHOLD,
    Alternative,
    Classification,
    RuleBasedLoadClassifier,
    display_name,
    normalize_text,
)
from .jev_client import JevError
from .load_normalizer import LoadIntelligenceService, LoadRequest, normalize_request


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
    """Explicit argument, else JEV_API_KEY, else TYPESAFE_API_KEY."""
    return jev_client.resolve_api_key(explicit)


# --- the two decisions Jev is asked --------------------------------------------

_JOB_TYPE_HELP = {
    "FIXED": "Always-on, or must run exactly when it is used, so it can never be moved in time (fridge, lights, TV, router, fan).",
    "DEFERRABLE_ATOMIC": "Runs one continuous cycle that must not be interrupted once started (washing machine, dryer, dishwasher, oven cycle).",
    "DEFERRABLE_INTERRUPTIBLE": "Needs a total amount of energy by a deadline and can pause and resume at will (EV or battery charging, a pump filling a tank).",
    "THERMAL": "Heats or cools something that holds temperature, so it can run ahead of need within a comfort band (water heater, geyser, air conditioner, room heater).",
}
_CATEGORY_HELP = {
    "EV charging": "Charging an electric vehicle or electric two-wheeler.",
    "Laundry": "Washing machine or clothes dryer.",
    "Dishwashing": "Dishwasher.",
    "Water heating": "Water heater, geyser or boiler.",
    "Space heating": "Room heater or a heating appliance for a space.",
    "Cooling": "Air conditioner or cooler.",
    "Pumping": "Water pump, borewell or tank-filling motor.",
    "Refrigeration": "Fridge or freezer.",
    "Always-on": "Lights, fans, TV, router, computers, anything left on while in use.",
    "Battery storage": "Stationary battery, inverter or power bank charging.",
    "Unknown": "Does not clearly fit any other category.",
}


def _questions() -> dict[str, dict[str, Any]]:
    return {
        "job_type": {
            "type": "choice",
            "instructions": "How can the electricity use of this household load be scheduled in time?",
            "criteria": {t.value: _JOB_TYPE_HELP.get(t.value) for t in LoadType},
        },
        "category": {
            "type": "choice",
            "instructions": "Which kind of household load is this?",
            "criteria": {c.value: _CATEGORY_HELP.get(c.value) for c in LoadCategory},
        },
    }


def _fallback_classification(text: str, reason: str) -> Classification:
    base = RuleBasedLoadClassifier().classify(text)
    note = Assumption(
        field="classifier",
        origin=ParameterOrigin.ESTIMATED,
        detail=f"Jev unavailable ({reason}); rule-based fallback used",
    )
    return base.model_copy(update={"assumptions": [*base.assumptions, note]})


def _jev_classification(text: str, api_key: str | None = None) -> Classification:
    """Ask Jev; raises JevError on any failure."""
    answers = jev_client.ask({"load_description": text}, _questions(), api_key=api_key)
    return _classification_from_answers(text, answers)


def _classification_from_answers(text: str, answers: dict[str, dict[str, Any]]) -> Classification:
    """Pure mapping from Jev's two Choice answers to a Classification."""
    job_type_v, jt_conf, jt_probs = jev_client.choice(answers["job_type"], {t.value for t in LoadType})
    category_v, cat_conf, cat_probs = jev_client.choice(answers["category"], {c.value for c in LoadCategory})
    job_type = LoadType(job_type_v)
    category = LoadCategory(category_v)
    sem = semantics_for(job_type)

    # The reading is only as sure as its weaker half.
    confidence = max(0.0, min(1.0, min(jt_conf, cat_conf)))

    alternatives: list[Alternative] = []
    for v, p in sorted(jt_probs.items(), key=lambda kv: -kv[1]):
        if v != job_type_v and p >= 0.10 and v in {t.value for t in LoadType}:
            alternatives.append(
                Alternative(
                    category=category,
                    job_type=LoadType(v),
                    weight=round(p, 3),
                    reason=f"Jev gave this scheduling behaviour {p:.0%} probability.",
                )
            )
    for v, p in sorted(cat_probs.items(), key=lambda kv: -kv[1]):
        if v != category_v and p >= 0.10 and v in {c.value for c in LoadCategory}:
            alternatives.append(
                Alternative(
                    category=LoadCategory(v),
                    job_type=job_type,
                    weight=round(p, 3),
                    reason=f"Jev gave this category {p:.0%} probability.",
                )
            )

    return Classification(
        name=display_name(text),
        input=text or "",
        category=category,
        job_type=job_type,
        shiftable=sem.shiftable,
        confidence=confidence,
        ambiguous=confidence < LOW_CONFIDENCE_THRESHOLD,
        reason=(
            f"Jev (System One model) classified this as {category.value} / {job_type.value}: "
            f"{jt_probs.get(job_type_v, 0):.0%} and {cat_probs.get(category_v, 0):.0%} probability, "
            f"confidence {confidence:.0%}."
        ),
        matched_rule="jev",
        alternatives=alternatives,
        required_fields=list(required_fields_for(job_type)),
        thermal_example=None,
        assumptions=[
            Assumption(
                field="category",
                origin=ParameterOrigin.ESTIMATED,
                detail="type and category decided by Jev with calibrated confidence; confirm if it matters",
            )
        ],
    )


class JevLoadIntelligence:
    """Jev-backed classifier with per-call honest fallback.

    `available()` is True when a key is configured (JEV_API_KEY, or TYPESAFE_API_KEY).
    Every `classify`/`normalize` call asks Jev and falls back to the rule-based
    classifier on any failure, recording the fallback as an assumption.
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

    def _classify_inner(self, text: str) -> Classification:
        if not self.configured:
            return _fallback_classification(text, "no API key configured")
        try:
            return _jev_classification(text, self._explicit_key)
        except JevError as exc:
            return _fallback_classification(text, str(exc))

    def classify(self, text: str) -> Classification:
        return self._classify_inner(text or "")

    def normalize(self, request: LoadRequest, now=None) -> LoadSpec:
        classification = self._classify_inner(request.name)

        class _Fixed:
            name = "jev_or_rules"

            def __init__(self, fixed: Classification, rules: RuleBasedLoadClassifier) -> None:
                self._fixed = fixed
                self._rules = rules

            def classify(self, text: str) -> Classification:
                if normalize_text(text) == normalize_text(self._fixed.input):
                    return self._fixed
                return self._rules.classify(text)

        return normalize_request(
            request, now=now, classifier=_Fixed(classification, self._rules)  # type: ignore[arg-type]
        )


def get_load_intelligence() -> LoadIntelligenceProvider:
    """The provider the API uses.

    Rule-based by default and whenever unconfigured. `"jev"` or `"auto"` is honored
    only when a Jev key is present; the Jev provider itself still falls back per
    call, so callers never see an exception for an unreachable model.
    """
    requested = (config.LOAD_INTELLIGENCE_PROVIDER or "rule_based").lower()
    if requested in ("jev", "auto"):
        jev = JevLoadIntelligence(resolve_api_key())
        if jev.available():
            return jev  # type: ignore[return-value]
    return RuleBasedLoadIntelligence()
