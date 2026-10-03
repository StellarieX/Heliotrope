"""Normalization: description -> LoadSpec (§8, §17, §18, §23).

The rule under test throughout: the normalizer translates what the user said
and nothing more. Every value it does not receive from the user must stay None.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.loads import LoadType, ParameterOrigin, RequiredField
from app.services.load_normalizer import LoadRequest, normalize_request

NOW = datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc)


def normalize(**kwargs) -> "LoadSpec":  # noqa: F821
    request = LoadRequest(**{"name": "EV", **kwargs})
    return normalize_request(request, now=NOW)


def assumptions_for(spec, field: str):
    return [a for a in spec.assumptions if a.field == field]


# --- classification feeds the spec -----------------------------------------


def test_a_bare_sentence_produces_a_class_but_no_numbers():
    """§30: the system must not invent precise physical values."""
    spec = normalize(name="washing machine")
    assert spec.job_type is LoadType.DEFERRABLE_ATOMIC
    assert spec.power_kw is None
    assert spec.duration_minutes is None
    assert spec.energy_required_kwh is None
    assert spec.is_schedulable() is False


def test_user_input_is_preserved_alongside_the_normalized_name():
    spec = normalize(name="  Hostel   EV  ")
    assert spec.user_input == "  Hostel   EV  "
    assert spec.normalized_name == "Hostel EV"


def test_explanation_is_always_present():
    spec = normalize(name="geyser")
    assert len(spec.explanation) > 30
    assert "thermal" in spec.explanation.lower()


# --- window resolution (§15) -----------------------------------------------


def test_window_resolves_across_midnight():
    spec = normalize(name="EV", release_wall="21:00", deadline_wall="07:00")
    assert spec.release_at == datetime(2026, 10, 5, 21, 0, tzinfo=timezone.utc)
    assert spec.deadline_at == datetime(2026, 10, 6, 7, 0, tzinfo=timezone.utc)
    assert spec.window_minutes() == pytest.approx(600.0)


def test_window_rollover_is_recorded_as_an_assumption():
    spec = normalize(name="EV", release_wall="21:00", deadline_wall="07:00")
    assumption = assumptions_for(spec, "deadline_at")[0]
    assert assumption.origin is ParameterOrigin.DERIVED
    assert "next day" in assumption.detail


def test_window_honors_an_explicit_timezone():
    spec = normalize(name="EV", release_wall="21:00", deadline_wall="07:00", timezone="Asia/Kolkata")
    assert spec.release_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert spec.window_minutes() == pytest.approx(600.0)


def test_absolute_timestamps_take_precedence_over_wall_times():
    spec = normalize(
        name="EV",
        release_wall="21:00",
        deadline_wall="07:00",
        release_at=NOW,
        deadline_at=NOW + timedelta(hours=8),
    )
    assert spec.release_at == NOW
    assert spec.deadline_at == NOW + timedelta(hours=8)


def test_unknown_timezone_falls_back_and_says_so():
    spec = normalize(name="EV", release_wall="21:00", deadline_wall="07:00", timezone="Mars/Olympus")
    assert spec.warnings
    assert "UTC" in spec.warnings[0]
    assert assumptions_for(spec, "timezone")[0].origin is ParameterOrigin.SYNTHETIC_DEFAULT


def test_malformed_wall_time_is_reported_not_crashed():
    spec = normalize(name="EV", release_wall="99:99", deadline_wall="07:00")
    assert spec.warnings
    assert spec.release_at is None


# --- thermal loads get labeled placeholder dynamics (§7, §30) --------------


def test_geyser_receives_synthetic_dynamics_labelled_as_such():
    spec = normalize(name="geyser")
    assert spec.job_type is LoadType.THERMAL
    assert spec.thermal is not None
    assumption = assumptions_for(spec, "thermal")[0]
    assert assumption.origin is ParameterOrigin.SYNTHETIC_DEFAULT
    assert "not calibrated" in assumption.detail


def test_user_supplied_dynamics_are_marked_user_configured():
    base = normalize(name="geyser")
    supplied = base.thermal.model_copy(update={"temperature_max_c": 70.0})
    spec = normalize(name="geyser", thermal=supplied)
    assert assumptions_for(spec, "thermal")[0].origin is ParameterOrigin.USER_CONFIGURED
    assert spec.thermal.temperature_max_c == 70.0


def test_user_dynamics_are_not_shared_by_mutation():
    """Each normalization must get its own copy, or one user's defaults would
    leak into another's."""
    first = normalize(name="geyser")
    first.thermal.temperature_min_c = -5.0
    assert normalize(name="geyser").thermal.temperature_min_c == 40.0


def test_thermal_parameters_on_a_non_thermal_load_are_refused():
    base = normalize(name="geyser")
    spec = normalize(name="washing machine", thermal=base.thermal)
    assert spec.thermal is None
    assert any("ignored" in w for w in spec.warnings)


# --- user overrides the classifier (§10, §17) ------------------------------


def test_user_can_override_the_class():
    spec = normalize(name="EV", job_type=LoadType.DEFERRABLE_ATOMIC)
    assert spec.job_type is LoadType.DEFERRABLE_ATOMIC
    assert spec.shiftable is True
    assert spec.interruptible is False
    assert "overriding" in assumptions_for(spec, "job_type")[0].detail


def test_an_explicit_override_clears_ambiguity():
    """If the user told us the class, we are no longer guessing."""
    assert normalize(name="heater").ambiguous is True
    assert normalize(name="heater", job_type=LoadType.THERMAL).ambiguous is False
    assert normalize(name="heater", job_type=LoadType.THERMAL).confidence == 1.0


def test_override_rewrites_the_explanation():
    spec = normalize(name="EV", job_type=LoadType.DEFERRABLE_ATOMIC)
    assert "Overridden" in spec.explanation


def test_override_updates_the_required_fields():
    spec = normalize(name="EV", job_type=LoadType.DEFERRABLE_ATOMIC)
    assert RequiredField.DURATION in spec.required_fields
    assert RequiredField.ENERGY not in spec.required_fields


def test_user_category_wins():
    assert normalize(name="EV", category="Hostel fleet").category == "Hostel fleet"


# --- explicit numbers are recorded as user-configured ----------------------


def test_supplied_values_are_tracked_with_their_origin():
    spec = normalize(name="EV", power_kw=7.2, energy_required_kwh=18.0, min_chunk_minutes=15)
    for field in ("power_kw", "energy_required_kwh", "min_chunk_minutes"):
        assert assumptions_for(spec, field)[0].origin is ParameterOrigin.USER_CONFIGURED


def test_absent_values_leave_no_assumption_beyond_the_window():
    spec = normalize(name="EV", release_wall="21:00", deadline_wall="07:00")
    fields = {a.field for a in spec.assumptions}
    assert "power_kw" not in fields
    assert "energy_required_kwh" not in fields


# --- end-to-end through the service ----------------------------------------


def test_service_normalize_matches_the_module_function():
    from app.services.load_intelligence import get_load_intelligence

    provider = get_load_intelligence()
    spec = provider.normalize(LoadRequest(name="EV", release_wall="21:00", deadline_wall="07:00"), now=NOW)
    assert spec.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE
    assert spec.window_minutes() == pytest.approx(600.0)
