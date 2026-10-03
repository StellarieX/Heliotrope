"""Classification: the matrix, confidence, ambiguity, determinism (§9-§12, §23).

The classifier is the piece most likely to be "improved" into something less
honest, so these tests pin down not just WHAT it returns but that it admits when
it is unsure.
"""

import pytest

from app.domain.loads import LoadCategory, LoadType
from app.services.classification import RuleBasedLoadClassifier, normalize_text

CLASSIFIER = RuleBasedLoadClassifier()


# --- §23 required classification matrix -----------------------------------

MATRIX = [
    # (input, category, job_type)
    ("EV", LoadCategory.EV_CHARGING, LoadType.DEFERRABLE_INTERRUPTIBLE),
    ("my EV", LoadCategory.EV_CHARGING, LoadType.DEFERRABLE_INTERRUPTIBLE),
    ("EV charger", LoadCategory.EV_CHARGING, LoadType.DEFERRABLE_INTERRUPTIBLE),
    ("electric car", LoadCategory.EV_CHARGING, LoadType.DEFERRABLE_INTERRUPTIBLE),
    ("washing machine", LoadCategory.LAUNDRY, LoadType.DEFERRABLE_ATOMIC),
    ("laundry", LoadCategory.LAUNDRY, LoadType.DEFERRABLE_ATOMIC),
    ("tumble dryer", LoadCategory.LAUNDRY, LoadType.DEFERRABLE_ATOMIC),
    ("dishwasher", LoadCategory.DISHWASHING, LoadType.DEFERRABLE_ATOMIC),
    ("geyser", LoadCategory.WATER_HEATING, LoadType.THERMAL),
    ("water heater", LoadCategory.WATER_HEATING, LoadType.THERMAL),
    ("AC", LoadCategory.COOLING, LoadType.THERMAL),
    ("air conditioner", LoadCategory.COOLING, LoadType.THERMAL),
    ("heat pump", LoadCategory.COOLING, LoadType.THERMAL),
    ("pump", LoadCategory.PUMPING, LoadType.DEFERRABLE_INTERRUPTIBLE),
    ("water pump", LoadCategory.PUMPING, LoadType.DEFERRABLE_INTERRUPTIBLE),
    ("battery", LoadCategory.BATTERY, LoadType.DEFERRABLE_INTERRUPTIBLE),
    ("fan", LoadCategory.ALWAYS_ON, LoadType.FIXED),
    ("lights", LoadCategory.ALWAYS_ON, LoadType.FIXED),
    ("router", LoadCategory.ALWAYS_ON, LoadType.FIXED),
    ("server", LoadCategory.ALWAYS_ON, LoadType.FIXED),
    ("fridge", LoadCategory.REFRIGERATION, LoadType.FIXED),
    ("freezer", LoadCategory.REFRIGERATION, LoadType.FIXED),
    ("oven", LoadCategory.SPACE_HEATING, LoadType.DEFERRABLE_ATOMIC),
    ("iron", LoadCategory.SPACE_HEATING, LoadType.DEFERRABLE_ATOMIC),
]


@pytest.mark.parametrize("text,category,job_type", MATRIX)
def test_classification_matrix(text, category, job_type):
    result = CLASSIFIER.classify(text)
    assert result.category is category
    assert result.job_type is job_type


@pytest.mark.parametrize("text,_category,job_type", MATRIX)
def test_shiftability_follows_the_taxonomy(text, _category, job_type):
    result = CLASSIFIER.classify(text)
    assert result.shiftable is (job_type is not LoadType.FIXED)


# --- unknown and ambiguous input (§10, §23) -------------------------------


def test_unknown_appliance_is_flagged_low_confidence():
    result = CLASSIFIER.classify("quantum flux capacitor")
    assert result.category is LoadCategory.UNKNOWN
    assert result.ambiguous is True
    assert result.confidence < 0.5


def test_empty_input_does_not_crash():
    for value in ("", "   ", "\t\n"):
        result = CLASSIFIER.classify(value)
        assert result.category is LoadCategory.UNKNOWN


def test_heater_is_reported_as_ambiguous_with_low_confidence():
    """"heater" is the §10 case: it must not pretend to know which heater."""
    result = CLASSIFIER.classify("heater")
    assert result.ambiguous is True
    assert result.confidence < 0.7
    assert "ambiguous" in result.reason.lower()
    assert any(a.field == "category" for a in result.assumptions)


def test_specific_water_heater_is_not_dampened():
    """A vague phrase is damped; a specific one is not. Otherwise every rule
    with a more generic sibling would be permanently penalized."""
    result = CLASSIFIER.classify("water heater")
    assert result.job_type is LoadType.THERMAL
    assert result.ambiguous is False
    assert result.confidence > 0.9


def test_bare_charger_is_ambiguous_between_vehicle_and_device():
    result = CLASSIFIER.classify("charger")
    assert result.ambiguous is True
    assert result.confidence < 0.7


def test_heat_pump_is_cooling_not_pumping():
    """A lexicon guard: "pump" inside "heat pump" must not make it a water
    pump, which would classify a thermal device as interruptible."""
    result = CLASSIFIER.classify("heat pump")
    assert result.category is LoadCategory.COOLING
    assert result.job_type is LoadType.THERMAL
    assert result.alternatives == []


# --- determinism and explainability (§11, §20, §23) ------------------------


def test_classification_is_deterministic():
    a, b = RuleBasedLoadClassifier(), RuleBasedLoadClassifier()
    for text, _, _ in MATRIX:
        assert a.classify(text).model_dump() == b.classify(text).model_dump()


def test_every_classification_returns_a_usable_reason():
    for text, _, _ in MATRIX:
        reason = CLASSIFIER.classify(text).reason
        assert len(reason) > 30, f"{text!r} has no usable explanation"
        assert reason.endswith("."), f"{text!r} explanation is not a sentence"


def test_every_classification_explains_itself_about_shiftability():
    """"Ceiling fan" must say why it is fixed; §12 gives that example."""
    result = CLASSIFIER.classify("ceiling fan")
    assert result.shiftable is False
    assert "shift" in result.reason.lower() or "baseline" in result.reason.lower()


def test_thermal_classification_labels_its_placeholder_dynamics():
    result = CLASSIFIER.classify("geyser")
    assert result.thermal_example == "geyser"
    assumption = next(a for a in result.assumptions if a.field == "thermal_coefficients")
    assert assumption.origin.value == "synthetic default"
    assert "not calibrated" in assumption.detail


def test_non_thermal_classification_proposes_no_dynamics():
    result = CLASSIFIER.classify("washing machine")
    assert result.thermal_example is None
    assert result.assumptions == []


def test_classifier_invents_no_numeric_physical_values():
    """§30: a name must never yield a power, duration or energy number."""
    numeric_keys = {"power_kw", "duration_minutes", "energy_required_kwh", "max_power_kw"}
    for text, _, _ in MATRIX:
        dumped = CLASSIFIER.classify(text).model_dump()
        assert numeric_keys.isdisjoint(dumped)


def test_normalize_text_is_punctuation_insensitive():
    assert normalize_text("  Washing-Machine!! ") == "washing machine"
    assert normalize_text("EV's charger") == "ev s charger"


def test_required_fields_match_the_class():
    ev = CLASSIFIER.classify("EV")
    assert "energy_required_kwh" in ev.required_fields
    assert "duration_minutes" not in ev.required_fields

    washer = CLASSIFIER.classify("washing machine")
    assert "duration_minutes" in washer.required_fields
    assert "energy_required_kwh" not in washer.required_fields

    geyser = CLASSIFIER.classify("geyser")
    assert "temperature_band" in geyser.required_fields
