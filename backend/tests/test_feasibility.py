"""Feasibility: structured errors, not bare booleans (§21, §22, §23).

Every assertion here checks BOTH halves of the contract: the verdict is right,
AND the message says the right thing in the user's own numbers.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.feasibility import (
    FeasibilityReport,
    IssueCode,
    is_energy_feasible,
    is_thermal_profile_feasible,
    is_time_feasible,
    validate_load,
)
from app.domain.loads import LoadSpec, LoadType
from app.domain.thermal_examples import AC_SYNTHETIC, GEYSER_SYNTHETIC

D = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def ev(**overrides) -> LoadSpec:
    base = dict(
        normalized_name="Hostel EV",
        category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=7.2,
        min_chunk_minutes=15,
        release_at=D,
        deadline_at=D + timedelta(hours=12),
    )
    base.update(overrides)
    return LoadSpec(**base)


def atomic(**overrides) -> LoadSpec:
    base = dict(
        normalized_name="Washing machine",
        category="Laundry",
        job_type=LoadType.DEFERRABLE_ATOMIC,
        power_kw=2.1,
        duration_minutes=60,
        release_at=D,
        deadline_at=D + timedelta(hours=12),
    )
    base.update(overrides)
    return LoadSpec(**base)


def thermal(name="Geyser", spec=GEYSER_SYNTHETIC, **overrides) -> LoadSpec:
    base = dict(
        normalized_name=name,
        category="Thermal",
        job_type=LoadType.THERMAL,
        thermal=spec,
        release_at=D,
        deadline_at=D + timedelta(hours=12),
    )
    base.update(overrides)
    return LoadSpec(**base)


def codes(report: FeasibilityReport) -> list[str]:
    return [i.code.value for i in report.errors]


# --- energy feasibility (§21, §22) ----------------------------------------


def test_physically_impossible_energy_is_detected():
    """The §21 headline: 50 kWh through 5 kW in a 2 h window."""
    report = is_energy_feasible(
        ev(power_kw=5.0, energy_required_kwh=50.0, release_at=D, deadline_at=D + timedelta(hours=2))
    )
    assert report.feasible is False
    assert IssueCode.ENERGY_EXCEEDS_WINDOW in codes(report)


def test_impossible_energy_explains_itself_with_the_users_numbers():
    """§22 wants enough information to say: this EV cannot receive 24 kWh ..."""
    report = is_energy_feasible(
        ev(power_kw=3.3, energy_required_kwh=24.0, release_at=D, deadline_at=D + timedelta(hours=2))
    )
    message = report.errors[0].message
    assert "Hostel EV" in message
    assert "24.00 kWh" in message
    assert "3.30 kW" in message
    assert "2.00 h" in message
    assert "6.60 kWh" in message, "should state the maximum deliverable"
    assert report.errors[0].detail["max_deliverable_kwh"] == pytest.approx(6.6)


def test_feasible_energy_across_midnight():
    report = is_energy_feasible(
        ev(
            power_kw=3.3,
            energy_required_kwh=24.0,
            release_at=D.replace(hour=21),
            deadline_at=D.replace(hour=21) + timedelta(hours=10),
        )
    )
    assert report.feasible is True
    assert report.explain() == "No physical problems found."


def test_energy_that_exactly_fills_the_window_is_feasible():
    """50 kWh at 5 kW needs exactly 10 h; a window of exactly 10 h is enough."""
    report = is_energy_feasible(
        ev(power_kw=5.0, energy_required_kwh=50.0, release_at=D, deadline_at=D + timedelta(hours=10))
    )
    assert report.feasible is True


def test_missing_energy_target_is_reported_not_assumed():
    report = is_energy_feasible(ev(energy_required_kwh=None))
    assert IssueCode.ENERGY_MISSING in codes(report)


def test_missing_power_limit_blocks_the_energy_verdict():
    report = is_energy_feasible(ev(power_kw=None, energy_required_kwh=18.0))
    assert IssueCode.MAX_POWER_MISSING in codes(report)


def test_min_chunk_larger_than_the_window_is_impossible():
    report = is_energy_feasible(
        ev(
            energy_required_kwh=5.0,
            min_chunk_minutes=240,
            release_at=D,
            deadline_at=D + timedelta(hours=2),
        )
    )
    assert IssueCode.MIN_CHUNK_EXCEEDS_WINDOW in codes(report)


def test_min_chunk_quantizes_deliverable_energy_downwards():
    """Partial chunks cannot run, so usable time rounds down to whole chunks."""
    exact = is_energy_feasible(
        ev(
            power_kw=10.0,
            energy_required_kwh=20.0,
            min_chunk_minutes=60,
            release_at=D,
            deadline_at=D + timedelta(hours=2),
        )
    )
    assert exact.feasible is True  # exactly 20 kWh in 2 whole hours
    over = is_energy_feasible(
        ev(
            power_kw=10.0,
            energy_required_kwh=21.0,
            min_chunk_minutes=60,
            release_at=D,
            deadline_at=D + timedelta(hours=2),
        )
    )
    assert over.feasible is False, "one kWh more than 2 h of power cannot arrive"
    quantized = is_energy_feasible(
        ev(
            power_kw=10.0,
            energy_required_kwh=21.0,
            min_chunk_minutes=90,
            release_at=D,
            deadline_at=D + timedelta(hours=2),
        )
    )
    assert quantized.feasible is False, "a 90-min chunk cannot fit twice in 2 h"


def test_zero_power_cannot_deliver_energy():
    report = is_energy_feasible(ev(power_kw=0.0, energy_required_kwh=1.0))
    assert report.feasible is False


# --- time feasibility (§23 atomic / interruptible) -------------------------


def test_atomic_load_that_fits_is_feasible():
    assert is_time_feasible(atomic()).feasible is True


def test_atomic_load_longer_than_its_window_is_infeasible():
    report = is_time_feasible(
        atomic(duration_minutes=600, release_at=D, deadline_at=D + timedelta(hours=2))
    )
    assert IssueCode.WINDOW_TOO_SHORT_FOR_DURATION in codes(report)
    assert "600 min" in report.errors[0].message
    assert "120 min" in report.errors[0].message


def test_atomic_load_without_a_duration_is_reported():
    report = is_time_feasible(atomic(duration_minutes=None))
    assert IssueCode.DURATION_MISSING in codes(report)


def test_release_after_deadline_is_caught_by_the_model_not_the_checker():
    """Coherence is rejected at construction, so the checker never sees it."""
    with pytest.raises(Exception):
        ev(release_at=D + timedelta(hours=1), deadline_at=D)


def test_missing_window_is_reported():
    report = is_time_feasible(atomic(release_at=None, deadline_at=None))
    assert IssueCode.RELEASE_MISSING in codes(report)
    assert IssueCode.DEADLINE_MISSING in codes(report)


def test_fixed_loads_need_no_window():
    """FIXED is baseline occupancy, so demanding a schedule window would ask for
    data the class can never use."""
    report = is_time_feasible(
        LoadSpec(normalized_name="Fan", category="Always-on", job_type=LoadType.FIXED, power_kw=0.05)
    )
    assert report.feasible is True
    assert report.errors == []


# --- thermal feasibility (§7, §23) ----------------------------------------


# The shipped AC example starts inside its band at 15-minute resolution. These
# tests exercise the checker's out-of-band start and hourly steps explicitly.
HOT_HOURLY_AC = AC_SYNTHETIC.model_copy(
    update={"temperature_initial_c": 30.0, "resolution_minutes": 60}
)


def test_thermal_target_unreachable_within_the_window():
    report = is_thermal_profile_feasible(
        thermal("AC", HOT_HOURLY_AC.model_copy(update={"temperature_initial_c": 26.0}), release_at=D, deadline_at=D + timedelta(hours=2))
    )
    assert report.feasible is False
    assert IssueCode.THERMAL_TARGET_UNREACHABLE in codes(report)
    assert "reachable range" in report.errors[0].message


def test_thermal_reachable_target_passes():
    report = is_thermal_profile_feasible(
        thermal("AC", AC_SYNTHETIC, release_at=D, deadline_at=D + timedelta(hours=12))
    )
    assert report.feasible is True


def test_thermal_without_dynamics_is_reported():
    report = is_thermal_profile_feasible(
        LoadSpec(normalized_name="Geyser", category="x", job_type=LoadType.THERMAL)
    )
    assert IssueCode.THERMAL_SPEC_MISSING in codes(report)


def test_precooling_trajectory_is_allowed_even_though_it_starts_hot():
    """§7: pre-cooling must be permitted. Starting above the band and closing
    the gap is a warning, not a failure.

    The coast is one slot by design: with this decay rate the room warms back
    quickly, and a long coast genuinely drifts out of band (see the test below).
    """
    profile = [AC_SYNTHETIC.max_power_kw] * 7 + [0.0] * 1
    report = is_thermal_profile_feasible(thermal("AC", HOT_HOURLY_AC), profile)
    transient = [w for w in report.warnings if w.code is IssueCode.THERMAL_INITIAL_OUT_OF_BAND]
    assert transient, "the out-of-band start should be reported as a warning"
    assert report.feasible is True


def test_cooling_too_early_warms_the_room_back_out_of_band():
    """The flip side of permitting pre-cooling: the room must still be in band
    at the end of the window, so the coast has to be short enough."""
    report = is_thermal_profile_feasible(
        thermal("AC", AC_SYNTHETIC), [AC_SYNTHETIC.max_power_kw] * 4 + [0.0] * 4
    )
    assert report.feasible is False
    assert IssueCode.THERMAL_BAND_VIOLATION in codes(report)


def test_overcooling_below_the_comfort_floor_is_an_error():
    profile = [AC_SYNTHETIC.max_power_kw] * 14
    report = is_thermal_profile_feasible(thermal("AC", AC_SYNTHETIC), profile)
    assert report.feasible is False
    assert IssueCode.THERMAL_BAND_VIOLATION in codes(report)


def test_heating_past_the_safe_bound_is_an_error():
    report = is_thermal_profile_feasible(thermal("Geyser", GEYSER_SYNTHETIC), [2.0] * 14)
    assert report.feasible is False
    assert IssueCode.THERMAL_BAND_VIOLATION in codes(report)


def test_banking_heat_then_coasting_is_feasible():
    report = is_thermal_profile_feasible(thermal("Geyser", GEYSER_SYNTHETIC), [2.0] * 8 + [0.0] * 3)
    assert report.feasible is True


def test_power_above_the_thermal_model_max_is_rejected():
    report = is_thermal_profile_feasible(thermal("Geyser", GEYSER_SYNTHETIC, power_kw=9.0))
    assert IssueCode.POWER_EXCEEDS_MODEL_MAX in codes(report)


# --- whole-load validation -------------------------------------------------


def test_validate_load_runs_every_check_and_aggregates():
    report = validate_load(ev(energy_required_kwh=18.0))
    assert set(report.checks_run) == {"time", "energy", "thermal"}
    assert report.feasible is True


def test_validate_load_flags_missing_data_and_says_what_to_ask_for():
    """A load with no energy target cannot be scheduled, so the verdict is
    infeasible; the INCOMPLETE_SPEC warning is the actionable half, naming the
    exact fields the UI should collect."""
    report = validate_load(ev(energy_required_kwh=None))
    assert report.feasible is False
    assert IssueCode.ENERGY_MISSING in codes(report)
    incomplete = [w for w in report.warnings if w.code is IssueCode.INCOMPLETE_SPEC]
    assert incomplete
    assert "energy_required_kwh" in incomplete[0].message


def test_incomplete_but_otherwise_sound_spec_is_only_a_warning():
    """Nothing is physically wrong with a half-filled form, so the summary is
    a warning and the load stays describable."""
    report = validate_load(
        LoadSpec(normalized_name="Fan", category="Always-on", job_type=LoadType.FIXED, power_kw=0.05)
    )
    assert report.feasible is True
    assert report.errors == []


def test_validate_load_flags_a_past_deadline():
    report = validate_load(
        ev(release_at=D - timedelta(hours=10), deadline_at=D - timedelta(hours=1)),
        now=D,
    )
    assert IssueCode.DEADLINE_IN_PAST in codes(report)


def test_report_summary_reads_as_a_sentence():
    assert validate_load(ev(energy_required_kwh=18.0)).explain() == "No physical problems found."
    report = validate_load(LoadSpec(normalized_name="X", category="c", job_type=LoadType.FIXED))
    assert isinstance(report.explain(), str) and report.explain()
