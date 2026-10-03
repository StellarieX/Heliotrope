"""Integer scaling for CP-SAT (§9).

CP-SAT solves over the integers, so every physical quantity has to be mapped
into a solver unit by a written-down rule. Two classes of test live here:

  * ROUND-TRIP BOUNDS. Each conversion must return to its original within half
    a unit of the destination scale. Asserting exact equality would be wrong --
    the conversions are intentionally lossy, and the tests should say how lossy.
  * THE ROUNDING ENVELOPE. The thermal recurrence is a ROUNDED division. The
    CP-SAT encoding of it (`RoundingEnvelope`) is proved equivalent to the
    integer arithmetic the validator uses, by brute force. This is the test that
    matters most in this file: the envelope is load-bearing for correctness, and
    an encoding that is merely fast but subtly different from the validator
    would let the solver and the validator disagree about what is feasible.
"""

import pytest

from app.domain.scaling import (
    CO2_KG_DIVISOR,
    OBJECTIVE_WEIGHT_SCALE,
    POWER_SCALE,
    TEMPERATURE_SCALE,
    WMIN_PER_KWH,
    RoundingEnvelope,
    ScalingError,
    ThermalScale,
    assert_non_negative_band,
    assert_objective_headroom,
    assert_within_int64,
    carbon_objective_coefficient,
    co2_kg_from_objective,
    energy_kwh_from_wmin,
    energy_kwh_from_wmin_sum,
    objective_from_co2_kg,
    slot_energy_wmin,
    temperature_c_from_milli,
    to_carbon_int,
    to_energy_wmin,
    to_millikw,
    to_objective_weight,
    to_power_w,
    to_temperature_milli,
)
from app.domain.scaling import _divide_round_half_away
from app.domain.thermal_examples import GEYSER_SYNTHETIC


# --- unit factors are the documented contract --------------------------------


def test_one_kwh_is_sixty_thousand_watt_minutes():
    """The bug this guards: 3.6e6 is watt-SECONDS, not watt-minutes."""
    assert WMIN_PER_KWH == 60_000
    assert to_energy_wmin(1.0) == WMIN_PER_KWH


def test_kw_converts_to_watts():
    assert POWER_SCALE == 1_000
    assert to_power_w(1.0) == 1_000
    assert to_power_w(7.2) == 7_200


# --- round-trip bounds -------------------------------------------------------


@pytest.mark.parametrize(
    "value", [0.0, 0.1, 1.0, 2.4, 3.6, 7.2, 11.0, 13.8, 22.0, 250.0]
)
def test_power_round_trip_is_within_half_a_watt(value):
    recovered = to_power_w(value) / POWER_SCALE
    assert abs(recovered - value) < 0.5 / POWER_SCALE


@pytest.mark.parametrize("value", [0.0, 0.5, 1.0, 18.0, 24.5, 100.0])
def test_energy_round_trip_is_within_half_a_watt_minute(value):
    recovered = energy_kwh_from_wmin(to_energy_wmin(value))
    assert abs(recovered - value) < 0.5 / WMIN_PER_KWH


@pytest.mark.parametrize("value", [-273.15, 0.0, 20.0, 40.0, 55.5, 65.0])
def test_temperature_round_trip_is_within_half_a_milli_degree(value):
    recovered = temperature_c_from_milli(to_temperature_milli(value))
    assert abs(recovered - value) < 0.5 / TEMPERATURE_SCALE


@pytest.mark.parametrize("value", [0, 1, 100, 349, 350, 500, 900])
def test_carbon_conversion_rounds_to_nearest_integer(value):
    assert to_carbon_int(float(value)) == value
    assert abs(to_carbon_int(value + 0.4) - (value + 0.4)) < 0.5


def test_slot_energy_is_exact_for_any_integer_slot_length():
    """No division here, which is why watt-minutes was chosen as the unit."""
    assert slot_energy_wmin(7_200, 15) == 108_000
    assert energy_kwh_from_wmin_sum(108_000) == pytest.approx(1.8)
    for minutes in (1, 15, 30, 60, 240):
        assert slot_energy_wmin(1_000, minutes) % 1 == 0


def test_one_milli_kw_is_one_watt():
    """The identity that a previous implementation got wrong by dividing by 1000."""
    assert to_millikw(2.0) == 2_000
    assert to_millikw(0.001) == 1


# --- rejections --------------------------------------------------------------


def test_power_rejects_an_out_of_range_rating():
    """Negative power is rejected upstream by `LoadSpec(ge=0)`; the scaling
    layer's job is only to refuse values it cannot represent."""
    with pytest.raises(ScalingError):
        to_power_w(1e12)


def test_power_rejects_a_value_beyond_the_representable_range():
    with pytest.raises(ScalingError):
        to_power_w(1e12)


def test_conversions_reject_non_finite_input():
    for bad in (float("inf"), float("nan")):
        with pytest.raises(ScalingError):
            to_power_w(bad)
        with pytest.raises(ScalingError):
            to_energy_wmin(bad)
        with pytest.raises(ScalingError):
            to_temperature_milli(bad)
        with pytest.raises(ScalingError):
            to_carbon_int(bad)


def test_negative_carbon_intensity_is_rejected_not_clamped():
    """A negative intensity is a data bug, and clamping to 0 would hide it."""
    with pytest.raises(ScalingError):
        to_carbon_int(-1.0)


def test_objective_weights_must_be_finite_and_non_negative():
    assert to_objective_weight(1.0) == OBJECTIVE_WEIGHT_SCALE
    with pytest.raises(ScalingError):
        to_objective_weight(-0.1)
    with pytest.raises(ScalingError):
        to_objective_weight(float("nan"))


# --- carbon objective accounting --------------------------------------------


def test_objective_units_convert_to_kilograms_exactly():
    """1 kWh at 400 gCO2/kWh is 0.4 kgCO2."""
    # 1 kWh = 60_000 watt-minutes at 400 g/kWh.
    objective = 60_000 * 400
    assert co2_kg_from_objective(objective) == pytest.approx(0.4)


def test_carbon_objective_coefficient_is_intensity_times_slot_minutes():
    assert carbon_objective_coefficient(400, 15) == 6_000


def test_objective_and_co2_round_trip_within_the_last_bit():
    for kg in (0.0, 0.4, 1.82, 12.3456):
        assert co2_kg_from_objective(objective_from_co2_kg(kg)) == pytest.approx(
            kg, abs=1.0 / CO2_KG_DIVISOR
        )


# --- overflow guards ---------------------------------------------------------


def test_overflow_guard_fires_before_int64_wraps():
    with pytest.raises(ScalingError):
        assert_objective_headroom(
            max_jobs=100_000, max_slots=100_000, max_power_w=50_000_000,
            max_carbon=1_000, max_slot_minutes=15,
        )


def test_overflow_guard_allows_a_realistic_problem():
    """A household-scale problem must sail through the guard."""
    assert_objective_headroom(
        max_jobs=100, max_slots=96, max_power_w=22_000, max_carbon=800,
        max_slot_minutes=15,
    )


def test_int64_guard_rejects_an_absurd_value():
    assert assert_within_int64(1, "x") == 1
    with pytest.raises(ScalingError):
        assert_within_int64(10**19, "objective")


# --- the rounding envelope: the load-bearing proof --------------------------


def test_envelope_rejects_an_odd_denominator():
    """The midpoint would be ambiguous, so the envelope refuses to exist."""
    with pytest.raises(ScalingError):
        RoundingEnvelope.for_rounded_half_away(999)


def test_envelope_rejects_a_non_positive_denominator():
    with pytest.raises(ScalingError):
        RoundingEnvelope.for_rounded_half_away(0)


@pytest.mark.parametrize("numerator", list(range(0, 4_000)))
def test_envelope_matches_the_python_rounding_exactly(numerator):
    """The CP-SAT encoding and the validator's arithmetic must agree exactly.

    For a non-negative numerator the pair of inequalities
        n - (d/2 - 1) <= d*x <= n + d/2
    spans d-1 units, less than the d spacing between multiples of d, so it holds
    at most one integer x -- and always exactly one. Here we check that the
    integer it selects is the same one the rounding helper picks.
    """
    envelope = RoundingEnvelope.for_rounded_half_away()
    d = envelope.denominator

    def admitted(x: int) -> bool:
        return (
            numerator - envelope.lower_offset <= x * d <= numerator + envelope.upper_offset
        )

    candidates = [x for x in range(-2, numerator // d + 3) if admitted(x)]
    assert len(candidates) == 1, "the envelope must admit exactly one integer"
    assert candidates[0] == _divide_round_half_away(numerator, d)


def test_envelope_is_not_the_plain_divisibility_equality():
    """The naive encoding `d*x == n` drops every numerator with a remainder.

    That was a real defect: it made the geyser model report a genuinely
    reachable target as INFEASIBLE, because an ordinary power draw usually
    leaves a remainder. The envelope keeps every one of them.
    """
    d = 1_000
    # An ordinary geyser step at 45.0 degC drawing 1.375 kW.
    numerator = 900 * 45_000 + 2_750 * 1_375 + 2_000_000
    assert numerator % d != 0, "this case should have a remainder"
    # The naive equality therefore admits nothing at all...
    assert _divide_round_half_away(numerator, d) * d != numerator
    # ...while the envelope selects the correct rounded value.
    envelope = RoundingEnvelope.for_rounded_half_away()
    rounded = _divide_round_half_away(numerator, d)
    assert numerator - envelope.lower_offset <= rounded * d <= numerator + envelope.upper_offset


def test_envelope_rejects_a_negative_temperature_band():
    with pytest.raises(ScalingError):
        assert_non_negative_band(-1, "test band")
    assert_non_negative_band(0, "test band")


def test_thermal_scale_milli_kw_and_watt_forms_agree():
    """Both unit forms must be the same recurrence, or the model and the
    validator would be simulating two different appliances."""
    scale = ThermalScale.from_spec(GEYSER_SYNTHETIC, slot_minutes=15)
    for temperature in (40_000, 45_000, 52_300, 64_999):
        for watts in (0, 137, 911, 2_000):
            assert scale.next_temperature_milli_from_watts(
                temperature, watts
            ) == scale.next_temperature_milli(temperature, watts)


def test_thermal_scale_reproduces_the_float_model():
    """The integer identity must track the float recurrence to well under the
    comfort tolerance, which is what the validator docstring claims."""
    spec = GEYSER_SYNTHETIC
    scale = ThermalScale.from_spec(spec, slot_minutes=15)
    temperature = spec.temperature_initial_c
    power_kw = 1.4
    for _ in range(40):
        integer_next = scale.next_temperature_milli_from_watts(
            to_temperature_milli(temperature), int(round(power_kw * 1000))
        )
        float_next = spec.a * temperature + spec.b * power_kw + spec.c
        temperature = temperature_c_from_milli(integer_next)
        assert abs(temperature - float_next) < 0.01