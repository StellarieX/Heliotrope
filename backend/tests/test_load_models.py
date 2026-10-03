"""Canonical LoadSpec: taxonomy invariants, energy-vs-duration, provenance (§3, §8, §13, §14, §28)."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.domain.loads import (
    DecisionVariable,
    LoadSpec,
    LoadType,
    PrimaryRequirement,
    Recurrence,
    RequiredField,
    semantics_for,
)
from app.domain.thermal_examples import AC_SYNTHETIC, GEYSER_SYNTHETIC

D = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def spec(**overrides) -> LoadSpec:
    base = dict(normalized_name="Load", category="Test", job_type=LoadType.DEFERRABLE_ATOMIC)
    base.update(overrides)
    return LoadSpec(**base)


# --- the taxonomy is more than labels (§2, §3, §13) -----------------------


@pytest.mark.parametrize(
    "job_type,decision,requirement,shiftable",
    [
        (LoadType.FIXED, DecisionVariable.NONE, PrimaryRequirement.BASELINE, False),
        (
            LoadType.DEFERRABLE_ATOMIC,
            DecisionVariable.START,
            PrimaryRequirement.DURATION,
            True,
        ),
        (
            LoadType.DEFERRABLE_INTERRUPTIBLE,
            DecisionVariable.POWER,
            PrimaryRequirement.ENERGY,
            True,
        ),
        (
            LoadType.THERMAL,
            DecisionVariable.POWER_AND_STATE,
            PrimaryRequirement.STATE_TRAJECTORY,
            True,
        ),
    ],
)
def test_each_class_has_its_own_semantics(job_type, decision, requirement, shiftable):
    semantics = semantics_for(job_type)
    assert semantics.decision_variable is decision
    assert semantics.primary_requirement is requirement
    assert semantics.shiftable is shiftable


def test_fixed_loads_are_never_shiftable():
    """FIXED has no scheduling variable, so the flag is derived, not trusted."""
    load = spec(job_type=LoadType.FIXED)
    assert load.shiftable is False
    assert load.interruptible is False


def test_only_interruptible_loads_are_interruptible():
    assert spec(job_type=LoadType.DEFERRABLE_INTERRUPTIBLE).interruptible is True
    assert spec(job_type=LoadType.DEFERRABLE_ATOMIC).interruptible is False
    assert spec(job_type=LoadType.FIXED).interruptible is False
    assert spec(job_type=LoadType.THERMAL, thermal=GEYSER_SYNTHETIC).interruptible is False


# --- energy vs duration is preserved (§14) ---------------------------------


def test_atomic_load_derives_energy_from_duration():
    load = spec(power_kw=2.1, duration_minutes=60)
    assert load.derived_energy_kwh() == pytest.approx(2.1)
    assert load.primary_requirement is PrimaryRequirement.DURATION


def test_interruptible_load_has_no_derived_energy():
    """An interruptible load is described by its energy, not a duration. Deriving
    an energy from a duration would destroy exactly the distinction the
    optimizer needs."""
    load = spec(job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=7.2, energy_required_kwh=18.0)
    assert load.derived_energy_kwh() is None
    assert load.primary_requirement is PrimaryRequirement.ENERGY


def test_thermal_load_has_no_derived_energy():
    load = spec(job_type=LoadType.THERMAL, thermal=GEYSER_SYNTHETIC, power_kw=2.0)
    assert load.derived_energy_kwh() is None


def test_atomic_load_needs_a_duration_not_an_energy():
    assert RequiredField.DURATION in spec(job_type=LoadType.DEFERRABLE_ATOMIC).missing_required_fields()


def test_interruptible_load_needs_energy_not_a_duration():
    load = spec(job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=7.2)
    missing = load.missing_required_fields()
    assert RequiredField.ENERGY in missing
    assert RequiredField.DURATION not in missing


def test_thermal_load_needs_a_comfort_band():
    load = spec(job_type=LoadType.THERMAL)
    assert RequiredField.THERMAL_COEFFICIENTS in load.missing_required_fields()
    assert not load.is_schedulable()


def test_minimum_runtime_hours_derives_the_charging_time_needed():
    load = spec(job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=7.2, energy_required_kwh=18.0)
    assert load.minimum_runtime_hours_for_energy() == pytest.approx(2.5)


# --- unknown is not zero (§8, §19, §30) ------------------------------------


def test_absent_physical_values_stay_none():
    load = spec()
    assert load.power_kw is None
    assert load.duration_minutes is None
    assert load.energy_required_kwh is None
    assert load.peak_contribution_kw() is None


def test_absent_window_is_reported_not_guessed():
    load = spec(power_kw=2.1, duration_minutes=60)
    assert load.window_minutes() is None
    assert RequiredField.RELEASE in load.missing_required_fields()
    assert RequiredField.DEADLINE in load.missing_required_fields()


def test_effective_max_power_is_the_highest_stated_ceiling():
    load = spec(job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=3.3, max_power_kw=7.2)
    assert load.effective_max_power_kw() == pytest.approx(7.2)


# --- coherence (§21) -------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("power_kw", -1.0),
        ("max_power_kw", -2.0),
        ("energy_required_kwh", -0.5),
        ("duration_minutes", 0),
        ("min_chunk_minutes", 0),
    ],
)
def test_nonsense_physical_values_rejected(field, value):
    with pytest.raises(ValidationError):
        spec(**{field: value})


def test_release_after_deadline_rejected():
    with pytest.raises(ValidationError, match="release_at"):
        spec(release_at=D + timedelta(hours=5), deadline_at=D)


def test_power_above_max_power_rejected():
    with pytest.raises(ValidationError, match="max_power_kw"):
        spec(power_kw=11.0, max_power_kw=7.2)


def test_naive_timestamps_rejected():
    with pytest.raises(ValidationError):
        spec(release_at="2026-10-05T12:00:00", deadline_at="2026-10-06T12:00:00")


def test_thermal_parameters_rejected_on_a_non_thermal_load():
    with pytest.raises(ValidationError, match="only meaningful for THERMAL"):
        spec(job_type=LoadType.DEFERRABLE_ATOMIC, thermal=GEYSER_SYNTHETIC)


def test_thermal_load_without_dynamics_is_incomplete_not_invalid():
    """The user has said what the appliance is; the band is simply missing, and
    feasibility reports that with a specific message."""
    load = spec(job_type=LoadType.THERMAL)
    assert load.job_type is LoadType.THERMAL
    assert load.thermal is None


# --- explainability and metrics preparation (§20, §28) ---------------------


def test_every_load_carries_a_primary_requirement():
    for job_type in LoadType:
        assert spec(job_type=job_type, **({"thermal": AC_SYNTHETIC} if job_type is LoadType.THERMAL else {})).primary_requirement


def test_metric_inputs_expose_values_without_computing_metrics():
    load = spec(
        id="ev-1",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=7.2,
        energy_required_kwh=18.0,
        release_at=D,
        deadline_at=D + timedelta(hours=12),
    )
    inputs = load.metric_inputs()
    assert inputs["load_id"] == "ev-1"
    assert inputs["peak_contribution_kw"] == pytest.approx(7.2)
    assert inputs["energy_required_kwh"] == pytest.approx(18.0)
    assert inputs["window_minutes"] == pytest.approx(720.0)
    # No carbon, cost or savings are computed: no schedule exists yet.
    assert not any("carbon" in key or "cost" in key or "saved" in key for key in inputs)


def test_semantics_constraints_are_human_readable():
    assert any("energy_required_kwh" in c for c in semantics_for(LoadType.DEFERRABLE_INTERRUPTIBLE).constraints)
    assert any("baseline" in c for c in semantics_for(LoadType.FIXED).constraints)
    assert any("T[t+1]" in c for c in semantics_for(LoadType.THERMAL).constraints)


# --- recurrence stays optional (§16) --------------------------------------


def test_recurrence_is_optional_but_representable():
    load = spec(job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, energy_required_kwh=18.0, power_kw=7.2)
    assert load.recurrence is None
    load.recurrence = Recurrence(frequency="daily", timezone="Asia/Kolkata", active_days=[0, 1, 2])
    assert load.recurrence.frequency.value == "daily"
    assert load.recurrence.active_days == [0, 1, 2]


def test_recurrence_rejects_impossible_weekdays():
    with pytest.raises(ValidationError):
        Recurrence(frequency="weekly", active_days=[7])
