"""Legacy Firestore jobs normalize without being broken (§19, §23).

Documents written before Phase 3 have no energy target, no duration and no
thermal parameters. They must still normalize, and every gap must be visible
rather than filled with a plausible-looking number.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.loads import LoadType, ParameterOrigin
from app.services.firestore_normalizer import (
    FirestoreJobDocument,
    normalize_firestore_job,
)

NOW = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)


def legacy(**overrides) -> dict:
    """The exact shape the Phase 2 dashboard wrote."""
    base = {
        "id": "job-1",
        "name": "washing machine",
        "kind": "Laundry",
        "shiftable": True,
        "powerKw": 2.1,
        "readyBy": "23:00",
        "flexHours": 2,
    }
    base.update(overrides)
    return base


def normalize(doc, **kwargs) -> "LoadSpec":  # noqa: F821
    return normalize_firestore_job(doc, now=NOW, tz_name="Asia/Kolkata", **kwargs)


def assumption(spec, field: str):
    return next(a for a in spec.assumptions_for(field))


# --- the legacy shape still works ------------------------------------------


def test_legacy_document_normalizes():
    spec = normalize(legacy())
    assert spec.job_type is LoadType.DEFERRABLE_ATOMIC
    assert spec.category == "Laundry"
    assert spec.power_kw == pytest.approx(2.1)
    assert spec.id == "job-1"


def test_legacy_document_never_gets_an_invented_duration():
    """The Phase 2 frontend hard-coded 60 minutes. Reproducing that here would
    make an atomic load schedulable on a number no user ever chose."""
    spec = normalize(legacy())
    assert spec.duration_minutes is None
    assert spec.assumption_origin("duration_minutes") is ParameterOrigin.LEGACY_INFERRED
    assert "no duration was assumed" in assumption(spec, "duration_minutes").detail
    assert spec.is_schedulable() is False


def test_legacy_document_never_gets_an_invented_energy_target():
    """Unknown is not zero: energy_required_kwh stays None."""
    spec = normalize(legacy())
    assert spec.energy_required_kwh is None
    assert "unknown, not zero" in assumption(spec, "energy_required_kwh").detail


def test_legacy_interruptible_job_warns_that_it_has_no_target():
    spec = normalize(legacy(name="EV", kind="EV charging", shiftable=True))
    assert spec.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE
    assert any("no energy target" in w for w in spec.warnings)


def test_zero_power_is_treated_as_unknown_not_as_zero():
    """Phase 2 wrote 0 when the user left the kW field blank."""
    spec = normalize(legacy(powerKw=0))
    assert spec.power_kw is None
    assert assumption(spec, "power_kw").origin is ParameterOrigin.LEGACY_INFERRED


# --- readyBy resolution (§15) ----------------------------------------------


def test_ready_by_resolves_to_the_next_occurrence():
    # 18:00 UTC is 23:30 IST, so 23:00 today has passed.
    spec = normalize(legacy(readyBy="23:00"))
    assert spec.deadline_at is not None
    assert spec.deadline_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert spec.deadline_at.day == 6
    assert spec.deadline_at.hour == 23
    assert spec.deadline_at > spec.release_at


def test_ready_by_later_today_resolves_to_today():
    doc = legacy(readyBy="23:45")
    spec = normalize_firestore_job(doc, now=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc), tz_name="Asia/Kolkata")
    assert spec.deadline_at.day == 5
    assert spec.deadline_at.hour == 23


def test_release_defaults_to_now_and_says_so():
    spec = normalize(legacy())
    assert spec.release_at == NOW
    assert assumption(spec, "release_at").origin is ParameterOrigin.DERIVED


def test_missing_ready_by_leaves_no_deadline():
    spec = normalize(legacy(readyBy=None))
    assert spec.deadline_at is None
    assert any("no readyBy" in w for w in spec.warnings)


def test_malformed_ready_by_is_reported():
    spec = normalize(legacy(readyBy="not-a-time"))
    assert spec.deadline_at is None
    assert any("readyBy" in w for w in spec.warnings)


# --- stored class wins, and disagreements are recorded ---------------------


def test_stored_job_type_wins_over_the_classifier():
    spec = normalize(legacy(jobType="DEFERRABLE_INTERRUPTIBLE", energyKwh=18))
    assert spec.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE
    assert "overriding the classifier" in spec.explanation


def test_stored_job_type_override_moves_the_question_list_with_it():
    """An override that changed the class but kept the old questions would ask
    an EV owner for a run length, which is the wrong question entirely."""
    spec = normalize(legacy(jobType="DEFERRABLE_INTERRUPTIBLE", energyKwh=18))
    fields = [f.value for f in spec.required_fields]
    assert "energy_required_kwh" in fields
    assert "duration_minutes" not in fields


def test_stored_shiftable_false_does_not_erase_a_thermal_class():
    spec = normalize(legacy(name="geyser", kind="Water heating", shiftable=False))
    assert spec.job_type is LoadType.THERMAL
    assert "more physical meaning" in assumption(spec, "shiftable").detail


def test_stored_shiftable_true_does_not_make_a_fridge_movable():
    spec = normalize(legacy(name="fridge", kind="Refrigeration", shiftable=True))
    assert spec.job_type is LoadType.FIXED
    assert spec.shiftable is False
    assert "always-on" in assumption(spec, "shiftable").detail


def test_classifier_is_recorded_as_legacy_inferred_when_absent():
    spec = normalize(legacy())
    assert assumption(spec, "job_type").origin is ParameterOrigin.LEGACY_INFERRED


# --- Phase 3 documents -----------------------------------------------------


def test_phase3_document_keeps_its_supplied_values():
    spec = normalize(
        legacy(
            jobType="DEFERRABLE_INTERRUPTIBLE",
            energyKwh=18.0,
            maxPowerKw=7.2,
            minChunkMin=15,
            confidence=0.96,
        )
    )
    assert spec.energy_required_kwh == pytest.approx(18.0)
    assert spec.max_power_kw == pytest.approx(7.2)
    assert spec.min_chunk_minutes == 15
    assert spec.confidence == pytest.approx(0.96)
    assert spec.is_schedulable() is True
    assert assumption(spec, "energy_required_kwh").origin is ParameterOrigin.USER_CONFIGURED


def test_stored_thermal_dynamics_are_used():
    from app.domain.thermal_examples import GEYSER_SYNTHETIC

    spec = normalize(
        legacy(name="geyser", jobType="THERMAL", thermal=GEYSER_SYNTHETIC.model_dump())
    )
    assert spec.thermal is not None
    assert spec.thermal.temperature_max_c == 65.0


def test_thermal_dynamics_on_a_non_thermal_job_are_ignored():
    from app.domain.thermal_examples import GEYSER_SYNTHETIC

    spec = normalize(legacy(jobType="DEFERRABLE_ATOMIC", thermal=GEYSER_SYNTHETIC.model_dump()))
    assert spec.thermal is None
    assert any("ignored" in w for w in spec.warnings)


def test_thermal_job_without_dynamics_warns_instead_of_raising():
    spec = normalize(legacy(name="geyser", jobType="THERMAL"))
    assert spec.job_type is LoadType.THERMAL
    assert spec.thermal is None
    assert any("comfort band" in w for w in spec.warnings)


# --- schema tolerance ------------------------------------------------------


def test_unknown_extra_fields_are_tolerated():
    doc = legacy(someFutureField={"nested": True}, anotherNewThing=42)
    assert normalize(doc).job_type is LoadType.DEFERRABLE_ATOMIC


def test_document_model_accepts_both_camel_and_snake_case():
    parsed = FirestoreJobDocument(name="EV", power_kw=7.2)
    assert parsed.power_kw == pytest.approx(7.2)


def test_normalization_does_not_mutate_the_input():
    doc = legacy()
    normalize(doc)
    assert doc == legacy()
