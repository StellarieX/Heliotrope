"""Deterministic natural-language load classification (Phase 3, §9-§12, §20).

RULES FIRST, on purpose. The classifier must work with no network, no API key
and no model: same input, same output, every time. That is what makes the test
suite meaningful and the offline demo work. `JevLoadIntelligence` (see
services/load_intelligence.py) is the seam where a learned classifier can be
added later without touching any caller.

TWO RESPONSIBILITIES ARE KEPT APART (§9):

    this module  -> WHAT KIND OF LOAD IS THIS?  (category, job type,
                    shiftability, how sure are we, why)
    load_spec service -> WHAT EXACTLY DOES THE USER NEED?  (power, energy,
                    duration, window, thermal band)

The classifier deliberately proposes NO numeric physical values. Guessing that
"geyser" means 2 kW would be exactly the fake precision §30 forbids, so the
only thing a rule contributes numerically is a pointer to the synthetic thermal
example, and even that is explicitly labelled.

CONFIDENCE is a simple, explainable heuristic rather than a calibrated
probability: the matched rule's own confidence, damped when a genuinely
competing rule also fired. "heater" matches only the vague heating rule and
lands near 0.62 with an ambiguity flag; "water heater" matches the specific
geyser rule and stays high.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Protocol

from pydantic import BaseModel, Field

from ..domain.loads import (
    Assumption,
    LoadCategory,
    LoadSemantics,
    LoadType,
    ParameterOrigin,
    RequiredField,
    semantics_for,
)

# An alternative only counts as a real competing reading if its weight is at
# least this fraction of the winner's. Without it, "water heater" (geyser 93,
# generic heater 45) would be reported as ambiguous when it plainly is not.
COMPETING_RULE_RATIO = 0.5

# Damping applied per competing category, and the floor confidence never drops
# below. A guess is still allowed to be a guess, just an honest one.
CONFIDENCE_DAMPING = 0.6
MAX_COMPETING_CATEGORIES = 2
CONFIDENCE_FLOOR = 0.25
LOW_CONFIDENCE_THRESHOLD = 0.70


@dataclass(frozen=True)
class ClassificationRule:
    """One deterministic mapping from a phrase to a class of load.

    `weight` is specificity, not importance: it decides which rule wins when
    several fire. `confidence` is how much this phrase alone pins the class
    down, independent of competitors.
    """

    key: str
    pattern: str
    weight: float
    category: LoadCategory
    job_type: LoadType
    confidence: float
    reason: str
    required_fields: tuple[RequiredField, ...] = ()
    thermal_example: Optional[str] = None

    def compiled(self) -> re.Pattern:
        return re.compile(self.pattern, re.IGNORECASE)


RULES: tuple[ClassificationRule, ...] = (
    ClassificationRule(
        key="ev",
        # A BARE "charger" is deliberately absent: it could be a phone or a car,
        # so it is handled by `generic_charger` below as a genuine ambiguity
        # rather than being claimed for EV at high confidence.
        pattern=r"\b(evs?|electric\s+(car|vehicle)|car\s+charger|tesla|charging\s+station|ev\s+charging)\b",
        weight=95,
        category=LoadCategory.EV_CHARGING,
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        confidence=0.96,
        reason=(
            "EV charging is movable because the vehicle only needs a total amount of "
            "energy before the deadline, so charging can be paused and resumed."
        ),
        required_fields=(RequiredField.ENERGY, RequiredField.MAX_POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="ebike",
        pattern=r"\b(e-?bikes?|electric\s+bikes?|scooters?|electric\s+scooters?|motorcycles?)\b",
        weight=82,
        category=LoadCategory.EV_CHARGING,
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        confidence=0.90,
        reason=(
            "Two-wheeler charging is interruptible: the battery needs a total energy "
            "input and can be topped up across several separate windows."
        ),
        required_fields=(RequiredField.ENERGY, RequiredField.MAX_POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="generic_charger",
        pattern=r"\b(chargers?|charging)\b",
        weight=70,
        category=LoadCategory.EV_CHARGING,
        job_type=LoadType.DEFERRABLE_ATOMIC,
        confidence=0.55,
        reason=(
            "'charger' on its own does not say what is being charged. Treated as a small "
            "movable charging block — confirm whether it is a vehicle, a battery pack, or "
            "a device."
        ),
        required_fields=(RequiredField.DURATION, RequiredField.POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="battery",
        pattern=r"\b(batter(y|ies)|bess|power\s?walls?|storage\s+inverter)\b",
        weight=90,
        category=LoadCategory.BATTERY,
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        confidence=0.92,
        reason=(
            "Battery charging is interruptible: it needs a total energy input before a "
            "deadline, so it can stop and resume without failing the requirement."
        ),
        required_fields=(RequiredField.ENERGY, RequiredField.MAX_POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="washing_machine",
        pattern=r"\b(washing\s+machines?|washers?|laundry|tumble\s+dryers?|dryers?|clothes\s+dryers?)\b",
        weight=92,
        category=LoadCategory.LAUNDRY,
        job_type=LoadType.DEFERRABLE_ATOMIC,
        confidence=0.95,
        reason=(
            "A laundry cycle runs for a fixed duration once it starts and cannot be "
            "paused, but the start time can slide anywhere before the deadline."
        ),
        required_fields=(RequiredField.DURATION, RequiredField.POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="dishwasher",
        pattern=r"\b(dishwashers?|dish\s?washers?)\b",
        weight=92,
        category=LoadCategory.DISHWASHING,
        job_type=LoadType.DEFERRABLE_ATOMIC,
        confidence=0.95,
        reason=(
            "A dishwasher runs one continuous cycle that cannot be paused, so only the "
            "start time is flexible."
        ),
        required_fields=(RequiredField.DURATION, RequiredField.POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="geyser",
        pattern=(
            r"\b(geysers?|geysers?|water\s+heaters?|hot\s+water|boilers?|"
            r"water\s+heating|immersion\s+heaters?)\b"
        ),
        weight=93,
        category=LoadCategory.WATER_HEATING,
        job_type=LoadType.THERMAL,
        confidence=0.94,
        reason=(
            "The geyser is treated as thermal because electricity can be stored as hot "
            "water and used later, so it can be charged at a low-carbon time and serve "
            "at a high-demand time."
        ),
        required_fields=(RequiredField.DEADLINE, RequiredField.TEMPERATURE_BAND),
        thermal_example="geyser",
    ),
    ClassificationRule(
        key="cooling",
        pattern=(
            r"\b(air\s+conditioners?|air\s?cons?|aircon|acs?|split\s+acs?|window\s+acs?|"
            r"hvac|heat\s+pumps?|chillers?|air\s+cooling|thermostats?)\b"
        ),
        weight=93,
        category=LoadCategory.COOLING,
        job_type=LoadType.THERMAL,
        confidence=0.94,
        reason=(
            "Cooling is thermal: pre-cooling the space before the evening peak stores "
            "comfort, so the same comfort can be bought at a lower-carbon time."
        ),
        required_fields=(RequiredField.DEADLINE, RequiredField.TEMPERATURE_BAND),
        thermal_example="ac",
    ),
    ClassificationRule(
        key="cooling_weak",
        pattern=r"\b(coolers?|cooling|chill|pre-?cool(ing)?)\b",
        weight=55,
        category=LoadCategory.COOLING,
        job_type=LoadType.THERMAL,
        confidence=0.70,
        reason=(
            "This reads as cooling, so it is treated as thermal. Confirm whether it is "
            "an air conditioner, a chilled store, or something else."
        ),
        required_fields=(RequiredField.DEADLINE, RequiredField.TEMPERATURE_BAND),
        thermal_example="ac",
    ),
    ClassificationRule(
        key="heating_ambiguous",
        pattern=r"\b(heaters?|heating|space\s+heaters?|room\s+heaters?)\b",
        weight=45,
        category=LoadCategory.SPACE_HEATING,
        job_type=LoadType.DEFERRABLE_ATOMIC,
        confidence=0.62,
        reason=(
            "'heater' is ambiguous — it can mean a water heater, a space heater, or "
            "industrial heating equipment. It is treated here as a movable heating "
            "appliance that runs one continuous block."
        ),
        required_fields=(RequiredField.DURATION, RequiredField.POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="cooking",
        pattern=r"\b(ovens?|stoves?|stovetops?|cooktops?|kilns?|induction|irons?|ironing|toasters?|microwaves?|kettles?)\b",
        weight=88,
        category=LoadCategory.SPACE_HEATING,
        job_type=LoadType.DEFERRABLE_ATOMIC,
        confidence=0.93,
        reason=(
            "Cooking and ironing heat resistively for a fixed stretch and cannot be "
            "interrupted mid-cycle, so the whole run is placed as one movable block."
        ),
        required_fields=(RequiredField.DURATION, RequiredField.POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="pumping",
        # The `(?<!heat )` guard stops "heat pump" — a thermal cooling device —
        # from also being read as a water pump. Fixed-width lookbehind, so it is
        # valid in `re`, and "water pump" still matches.
        pattern=(
            r"(?<!heat )\bpumps?\b|\bborewells?\b|\bbore\s?well\b|\btank\s+filler\b|"
            r"\bbooster\b|\bsubmersible\b"
        ),
        weight=80,
        category=LoadCategory.PUMPING,
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        confidence=0.85,
        reason=(
            "Pumping is interruptible: the tank needs a total amount of energy to fill, "
            "so pumping can stop and resume as long as the target is met in time."
        ),
        required_fields=(RequiredField.ENERGY, RequiredField.MAX_POWER, RequiredField.DEADLINE),
    ),
    ClassificationRule(
        key="refrigeration",
        pattern=r"\b(fridges?|refrigerators?|freezers?|deep\s+freeze)\b",
        weight=94,
        category=LoadCategory.REFRIGERATION,
        job_type=LoadType.FIXED,
        confidence=0.94,
        reason=(
            "The refrigerator stays fixed because shifting it would interfere with "
            "continuous cooling, so it is baseline load rather than something to optimize."
        ),
        required_fields=(RequiredField.POWER, RequiredField.OCCUPANCY),
    ),
    ClassificationRule(
        key="always_on",
        pattern=(
            r"\b(lights?|lamps?|bulbs?|fans?|ceiling\s+fans?|routers?|wi-?fi|modems?|"
            r"servers?|racks?|desktops?|computers?|laptops?|tvs?|televisions?|"
            r"projectors?|monitors?|alarms?|cameras?|set-?tops?|decoders?|"
            r"always-?on|iot|smart\s+home)\b"
        ),
        weight=80,
        category=LoadCategory.ALWAYS_ON,
        job_type=LoadType.FIXED,
        confidence=0.92,
        reason=(
            "This is needed while it is in use, so it has no useful window to shift and "
            "is treated as baseline load."
        ),
        required_fields=(RequiredField.POWER, RequiredField.OCCUPANCY),
    ),
)

_FALLBACK = ClassificationRule(
    key="unclassified",
    pattern=r"(?!)",  # never matches; the fallback is not a pattern
    weight=0,
    category=LoadCategory.UNKNOWN,
    job_type=LoadType.DEFERRABLE_ATOMIC,
    confidence=0.30,
    reason=(
        "This appliance is not recognized. It is treated as one movable block so you can "
        "still schedule it — tell us what it is and it will be classified properly."
    ),
    required_fields=(RequiredField.DURATION, RequiredField.POWER, RequiredField.DEADLINE),
)


class Alternative(BaseModel):
    """A competing reading of the same sentence."""

    category: LoadCategory
    job_type: LoadType
    weight: float
    reason: str


class Classification(BaseModel):
    """What kind of load is this, how sure are we, and why (§12, §20)."""

    name: str
    input: str
    category: LoadCategory
    job_type: LoadType
    shiftable: bool
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguous: bool
    reason: str
    matched_rule: str
    alternatives: list[Alternative] = Field(default_factory=list)
    required_fields: list[RequiredField] = Field(default_factory=list)
    thermal_example: Optional[str] = None
    assumptions: list[Assumption] = Field(default_factory=list)

    def semantics(self) -> LoadSemantics:
        return semantics_for(self.job_type)


class LoadClassifier(Protocol):
    """The interface a rules-based, LLM-based or hybrid classifier must satisfy."""

    name: str

    def classify(self, text: str) -> Classification: ...


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    if not isinstance(text, str):
        return ""
    lowered = text.lower()
    cleaned = re.sub(r"[^a-z0-9+]+", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def display_name(text: str) -> str:
    """A tidy label for the user's own words. Never rewrites meaning."""
    collapsed = re.sub(r"\s+", " ", (text or "").strip())
    if not collapsed:
        return "Unnamed load"
    if collapsed == collapsed.lower():
        return collapsed.capitalize()
    return collapsed


class RuleBasedLoadClassifier:
    """Deterministic, offline, dependency-free classification."""

    name = "rule_based"

    def __init__(self, rules: tuple[ClassificationRule, ...] = RULES) -> None:
        self._rules = rules
        self._compiled = [(r, r.compiled()) for r in rules]

    def classify(self, text: str) -> Classification:
        normalized = normalize_text(text)
        matches = [r for r, rx in self._compiled if normalized and rx.search(normalized)]

        if not matches:
            return self._from_rule(_FALLBACK, text, [])

        matches.sort(key=lambda r: (r.weight, r.confidence, len(r.pattern)), reverse=True)
        best = matches[0]

        threshold = best.weight * COMPETING_RULE_RATIO
        # Only a rule that disagrees about the CATEGORY is a competing reading.
        # A lower-weight rule that agrees on category (e.g. the generic
        # "charger" rule firing on "EV charger") adds no information, and
        # damping on it would penalise an unambiguous phrase.
        competing = [
            r for r in matches[1:] if r.weight >= threshold and r.category is not best.category
        ]
        competing_categories = {r.category for r in competing}
        alternatives = [
            Alternative(category=r.category, job_type=r.job_type, weight=r.weight, reason=r.reason)
            for r in competing
        ]
        return self._from_rule(best, text, alternatives, competing_categories)

    def _from_rule(
        self,
        rule: ClassificationRule,
        text: str,
        alternatives: list[Alternative],
        competing_categories: set[LoadCategory] | None = None,
    ) -> Classification:
        competing_categories = competing_categories or set()
        damping = CONFIDENCE_DAMPING ** min(len(competing_categories), MAX_COMPETING_CATEGORIES)
        confidence = max(CONFIDENCE_FLOOR, round(rule.confidence * damping, 4))
        ambiguous = bool(competing_categories) or rule.confidence < LOW_CONFIDENCE_THRESHOLD

        assumptions: list[Assumption] = []
        if rule.thermal_example:
            assumptions.append(
                Assumption(
                    field="thermal_coefficients",
                    origin=ParameterOrigin.SYNTHETIC_DEFAULT,
                    detail=(
                        f"placeholder dynamics from the {rule.thermal_example} synthetic example; "
                        "not calibrated to any real appliance"
                    ),
                )
            )
        if ambiguous:
            assumptions.append(
                Assumption(
                    field="category",
                    origin=ParameterOrigin.ESTIMATED,
                    detail="category inferred from an ambiguous description — confirm it",
                )
            )

        sem = semantics_for(rule.job_type)
        return Classification(
            name=display_name(text),
            input=text or "",
            category=rule.category,
            job_type=rule.job_type,
            shiftable=sem.shiftable,
            confidence=confidence,
            ambiguous=ambiguous,
            reason=rule.reason,
            matched_rule=rule.key,
            alternatives=alternatives,
            required_fields=list(rule.required_fields),
            thermal_example=rule.thermal_example,
            assumptions=assumptions,
        )
