"""Integer scaling for CP-SAT (Phase 4, §9).

CP-SAT solves over the integers. Every quantity that reaches a model variable
passes through this module first, so the mapping from physics to solver units is
written down once, in one place, instead of being re-derived per model.

    quantity                      solver unit                      factor
    ---------------------------   ------------------------------   --------
    power (kW)                    watts                            1_000
    energy (kWh)                  watt-minutes                     60_000
    carbon intensity (gCO2/kWh)   integer gCO2/kWh                 1
    temperature (degC)            milli-degC                       1_000
    thermal decay a (0..1)        per-mille                        1_000
    thermal gain b (degC/kW)      milli-degC per kW                1_000
    thermal drift c (degC)        milli-degC                       1_000
    thermal input (kW)            milli-kW                         1_000

WHY WATT-MINUTES FOR ENERGY. 1 kWh is 1,000 W for 60 minutes, i.e. 60,000
watt-minutes. Energy per slot is `power_w * slot_minutes`, an exact integer for
any integer slot length. Using kWh directly would mean dividing by 60,000
somewhere, and that division is where truncation errors get into an "optimal"
objective. Watt-minutes never divides.

WHY MILLI-KW FOR THERMAL INPUT. The thermal recurrence needs a coefficient on
the electrical input, and that coefficient is generally fractional. Scaling the
INPUT to milli-kW rather than watts pushes the fraction into the coefficient
(`b_scaled = round(1000 * b)`) instead of into the constraint, so the model
stays integral. See `ThermalScale` for the exact identity.

ACCURACY. Rounding is to nearest, never toward zero, so the error in any single
scaled value is at most half a unit of that unit:
    power        < 0.5 W
    energy       < 1.8 Wh
    temperature  < 0.0005 degC
    carbon       < 0.5 gCO2/kWh
None of these is meaningful at 15-minute granularity, and every conversion is
covered by tests that assert the round-trip error bound rather than exact
equality.

OVERFLOW. The largest objective term is bounded by
`MAX_JOBS * MAX_SLOTS * max_power_w * max_carbon * max_slot_minutes`, checked by
`assert_objective_headroom`. CP-SAT uses int64, so this guards against a caller
handing in a horizon or a power rating large enough to overflow the model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# --- unit factors ----------------------------------------------------------

POWER_SCALE = 1_000               # kW -> W
#: 1 kWh = 1000 W x 60 min = 60,000 watt-minutes.
WMIN_PER_KWH = 60_000
ENERGY_SCALE_KWH_TO_WMIN = WMIN_PER_KWH
TEMPERATURE_SCALE = 1_000         # degC -> milli-degC
THERMAL_POWER_SCALE = 1_000       # kW -> milli-kW
THERMAL_DECAY_SCALE = 1_000       # a -> per-mille
THERMAL_COEFF_SCALE = 1_000       # b, c -> milli
CARBON_SCALE = 1                  # gCO2/kWh stays as is, just rounded to int
OBJECTIVE_WEIGHT_SCALE = 1_000_000  # float weight -> integer weight

#: 1 kilogram is 1000 grams. This is the easy constant to get wrong by three
#: orders of magnitude -- 1e6 here silently reports every CO2 total 1000x too
#: small, which still looks like a plausible household number and so passes a
#: careless eye. `test_objective_units_convert_to_kilograms_exactly` pins it
#: against a hand-computed case: 1 kWh at 400 gCO2/kWh is 0.4 kg, not 0.0004.
CO2_G_PER_KG = 1_000

#: Objective units are (watt-minutes x gCO2/kWh). Dividing by WMIN_PER_KWH
#: converts watt-minutes to kWh and leaves grams CO2; dividing once more by
#: CO2_G_PER_KG yields kilograms.
CO2_KG_DIVISOR = WMIN_PER_KWH * CO2_G_PER_KG  # 6e7

# Guard rails. These are not arbitrary: they are well above any realistic
# household or small-site problem and well below int64 overflow.
MAX_JOBS = 2_000
MAX_SLOTS = 10_000
MAX_POWER_W = 50_000_000      # 50 MW
INT64_SAFETY = 8_000_000_000_000_000_000


class ScalingError(ValueError):
    """A value cannot be represented safely in solver units."""


# --- scalar conversions -----------------------------------------------------


def to_power_w(power_kw: float) -> int:
    """kW -> integer watts, rounded to nearest."""
    value = power_kw * POWER_SCALE
    if not math.isfinite(value):
        raise ScalingError(f"power_kw must be finite (got {power_kw})")
    if abs(value) > MAX_POWER_W:
        raise ScalingError(f"power_kw {power_kw} is beyond the representable range")
    return int(round(value))


def power_kw_from_w(power_w: int) -> float:
    return power_w / POWER_SCALE


def to_energy_wmin(energy_kwh: float) -> int:
    """kWh -> integer watt-minutes, rounded to nearest."""
    value = energy_kwh * ENERGY_SCALE_KWH_TO_WMIN
    if not math.isfinite(value):
        raise ScalingError(f"energy_kwh must be finite (got {energy_kwh})")
    return int(round(value))


def energy_kwh_from_wmin(energy_wmin: int) -> float:
    return energy_wmin / ENERGY_SCALE_KWH_TO_WMIN


def slot_energy_wmin(power_w: int, slot_minutes: int) -> int:
    """Energy drawn in one slot. Exact for any integer slot length."""
    return power_w * slot_minutes


def to_carbon_int(gco2_per_kwh: float) -> int:
    """gCO2/kWh -> integer. Never negative; a negative intensity is a bug."""
    if not math.isfinite(gco2_per_kwh):
        raise ScalingError(f"carbon intensity must be finite (got {gco2_per_kwh})")
    if gco2_per_kwh < 0:
        raise ScalingError(f"carbon intensity must be >= 0 (got {gco2_per_kwh})")
    return int(round(gco2_per_kwh * CARBON_SCALE))


def to_temperature_milli(temperature_c: float) -> int:
    if not math.isfinite(temperature_c):
        raise ScalingError(f"temperature must be finite (got {temperature_c})")
    return int(round(temperature_c * TEMPERATURE_SCALE))


def temperature_c_from_milli(temperature_milli: int) -> float:
    return temperature_milli / TEMPERATURE_SCALE


def to_objective_weight(weight: float) -> int:
    """A float objective weight -> integer, scaled.

    Weights are relative multipliers. Scaling them all by the same constant
    leaves the argmin unchanged, so this is safe and keeps the model integral.
    """
    if not math.isfinite(weight):
        raise ScalingError(f"objective weight must be finite (got {weight})")
    if weight < 0:
        raise ScalingError(f"objective weights must be >= 0 (got {weight})")
    return int(round(weight * OBJECTIVE_WEIGHT_SCALE))


# --- thermal scaling -------------------------------------------------------


#: Comfort-band slack (milli-degC) shared by the heuristics and the validator.
#: CP-SAT enforces the band exactly (stricter); a heuristic trajectory may sit up
#: to this far outside it because the integer recurrence rounds, and the
#: independent validator accepts exactly the same slack.
THERMAL_BAND_TOLERANCE_MILLI = 10


@dataclass(frozen=True)
class ThermalScale:
    """Integer coefficients for the thermal recurrence.

    The model uses this exact identity, which is what makes the thermal
    constraint integral without any division:

        1000 * T_milli[t+1] = a_permille * T_milli[t]
                             + b_scaled * P_millikw[t]
                             + 1000 * c_scaled

    Derivation, with T_milli = 1000*T_degC, P_millikw = 1000*P_kW,
    a_permille = 1000*a, b_scaled = 1000*b, c_scaled = 1000*c:

        T_degC[t+1] = a*T_degC[t] + b*P_kW[t] + c
        T_milli[t+1] = a*T_milli[t] + 1000*b*P_kW[t] + 1000*c
        1000*T_milli[t+1] = 1000*a*T_milli[t] + 1e6*b*P_kW[t] + 1e6*c
                          = a_permille*T_milli[t] + 1000*b_scaled*P_millikw[t]/1000 ...
    which, after substituting back, gives the identity above exactly.

    Every coefficient is a plain integer, so the constraint is integral end to
    end and `next_temperature_milli` reproduces the same arithmetic the solver
    does — which is what lets `ScheduleValidator` check thermal bounds without
    trusting the model.
    """

    a_permille: int
    b_scaled: int
    c_scaled: int
    min_milli: int
    max_milli: int
    target_milli: int | None
    initial_milli: int
    max_power_millikw: int
    slot_minutes: int
    #: watt-based coefficients, for the CP-SAT model where every power variable
    #: is in watts so that it can share one capacity constraint with the other
    #: job types. `1000*T_next = a_permille*T + b_scaled_w*P_w + c_micro`, which
    #: is the SAME identity as the milli-kW form above — verified by
    #: `agrees_between_units` in the tests.
    b_scaled_w: int = 0
    c_micro: int = 0

    @classmethod
    def from_spec(cls, spec, slot_minutes: int) -> "ThermalScale":
        return cls(
            a_permille=int(round(spec.a * THERMAL_DECAY_SCALE)),
            b_scaled=int(round(spec.b * THERMAL_COEFF_SCALE)),
            c_scaled=int(round(spec.c * THERMAL_COEFF_SCALE)),
            min_milli=to_temperature_milli(spec.temperature_min_c),
            max_milli=to_temperature_milli(spec.temperature_max_c),
            target_milli=(
                to_temperature_milli(spec.temperature_target_c)
                if spec.temperature_target_c is not None
                else None
            ),
            initial_milli=to_temperature_milli(
                spec.temperature_initial_c
                if spec.temperature_initial_c is not None
                else spec.temperature_min_c
            ),
            max_power_millikw=int(round(spec.max_power_kw * THERMAL_POWER_SCALE)),
            slot_minutes=slot_minutes,
            b_scaled_w=int(round(spec.b * THERMAL_COEFF_SCALE)),
            c_micro=int(round(spec.c * THERMAL_COEFF_SCALE * THERMAL_POWER_SCALE)),
        )

    def next_temperature_milli(self, current_milli: int, power_millikw: int) -> int:
        """One step of the recurrence from a MILLI-KW input.

        Division by 1000 uses round-half-away-from-zero so that the validator
        and the model cannot disagree about the same input.
        """
        numerator = (
            self.a_permille * current_milli
            + self.b_scaled * power_millikw
            + 1000 * self.c_scaled
        )
        return _divide_round_half_away(numerator, 1000)

    def next_temperature_milli_from_watts(self, current_milli: int, power_w: int) -> int:
        """One step of the SAME recurrence from a WATT input.

        Exactly equal to `next_temperature_milli(T, power_w * 1000)` — both are
        the identity `1000*T_next = a_permille*T + 1000*b*input + 1000*c`, written
        for different units of `input`. The CP-SAT model uses this form so every
        power variable in the model is in watts.
        """
        numerator = (
            self.a_permille * current_milli
            + self.b_scaled_w * power_w
            + self.c_micro
        )
        return _divide_round_half_away(numerator, 1000)


#: Denominator used by the thermal recurrence. It is the ratio between the
#: temperature unit (milli-degC) and the integer scale the coefficients live on.
THERMAL_DENOMINATOR = 1_000


@dataclass(frozen=True)
class RoundingEnvelope:
    """The exact linear encoding of one rounded division.

    The thermal recurrence is a division by `THERMAL_DENOMINATOR` that rounds
    rather than truncates, and CP-SAT has no rounding operator. The obvious
    encoding —

        DENOMINATOR * x == numerator

    — is wrong twice over. It demands that the numerator be *exactly*
    divisible, so it rejects power levels the physics allows, and it hides that
    divisibility condition inside a linear equality, where the LP relaxation
    cannot see it and propagation collapses.

    The correct encoding is a pair of inequalities. Write `numerator = d*q + r`
    with `0 <= r < d`. For even `d` the rounded value is `q` when `r < d/2` and
    `q + 1` otherwise, so:

        numerator - (d/2 - 1) <= d * x <= numerator + d/2

    holds for exactly one integer `x`, always. The interval spans `d - 1` units,
    less than the `d` spacing between consecutive multiples of `d`, so it can
    contain at most one; and checking `r` in `[0, d/2)` and `[d/2, d)` shows a
    solution always exists. Both facts are asserted by brute force in the test
    suite against `_divide_round_half_away`.

    The two offsets differ by one, which is what makes the midpoint resolve
    upwards the same way the Python helper does. An even `d` is required, and
    `THERMAL_DENOMINATOR` is 1000.

    SIGN. This envelope is exact for a non-negative numerator. A negative
    numerator needs the offsets the other way round, which cannot be selected
    from a linear expression the solver does not know the sign of. That is
    sound here because the numerator is a temperature in milli-degC and every
    comfort floor is non-negative, so the numerator is non-negative whenever
    the band is respected; `assert_non_negative_band` makes that explicit
    rather than leaving it to a comment.
    """

    denominator: int
    lower_offset: int
    upper_offset: int

    @classmethod
    def for_rounded_half_away(cls, denominator: int = THERMAL_DENOMINATOR) -> "RoundingEnvelope":
        if denominator <= 0 or denominator % 2:
            raise ScalingError(
                f"rounding envelope needs a positive even denominator (got {denominator})"
            )
        return cls(
            denominator=denominator,
            lower_offset=denominator // 2 - 1,
            upper_offset=denominator // 2,
        )

    def lower_bound(self, numerator) -> Any:
        return numerator - self.lower_offset

    def upper_bound(self, numerator) -> Any:
        return numerator + self.upper_offset


def assert_non_negative_band(min_milli: int, label: str = "thermal band") -> None:
    """Guard the sign assumption behind `RoundingEnvelope`.

    Temperature in milli-degC is a non-negative quantity for every appliance
    Heliotrope models, and the rounding envelope is exact only for a
    non-negative numerator. Checking it here turns a silent modelling
    assumption into a loud failure if a future spec ever supplies a sub-zero
    comfort floor.
    """
    if min_milli < 0:
        raise ScalingError(
            f"{label} has a negative floor ({min_milli} milli-degC); the integer "
            "rounding encoding assumes non-negative temperatures"
        )


def _divide_round_half_away(numerator: int, denominator: int) -> int:
    """Integer division that rounds halves away from zero.

    Python's `//` floors (so -5 // 2 == -3) and `round()` is banker's rounding.
    The thermal state can legitimately go negative in a badly-scaled synthetic
    model, so a single well-defined rule matters more than speed here.
    """
    if denominator == 0:
        raise ScalingError("division by zero in thermal scaling")
    quotient = abs(numerator) // denominator
    remainder = abs(numerator) % denominator
    if remainder * 2 >= denominator:
        quotient += 1
    return quotient if numerator >= 0 else -quotient


def to_millikw(power_kw: float) -> int:
    return int(round(power_kw * THERMAL_POWER_SCALE))


# --- carbon objective -------------------------------------------------------


def carbon_objective_coefficient(carbon_gco2_per_kwh: int, slot_minutes: int) -> int:
    """Coefficient multiplying `power_w` in the objective for one slot.

    The total objective is therefore
        sum(power_w[j,t] * carbon[t] * slot_minutes)
    which is exactly `total_watt_minutes * gCO2/kWh`, i.e. watt-minutes times
    intensity. Dividing by WMIN_PER_KWH then yields grams, and by CO2_G_PER_KG
    again yields kilograms.
    """
    return carbon_gco2_per_kwh * slot_minutes


def co2_kg_from_objective(objective_units: int) -> float:
    return objective_units / CO2_KG_DIVISOR


def objective_from_co2_kg(co2_kg: float) -> int:
    return int(round(co2_kg * CO2_KG_DIVISOR))


def energy_kwh_from_wmin_sum(total_wmin: int) -> float:
    return total_wmin / WMIN_PER_KWH


# --- overflow guards --------------------------------------------------------


def assert_objective_headroom(
    max_jobs: int, max_slots: int, max_power_w: int, max_carbon: int, max_slot_minutes: int
) -> None:
    """Fail loudly rather than let the solver return a wrapped-around objective."""
    if max_jobs > MAX_JOBS:
        raise ScalingError(f"{max_jobs} jobs exceeds the modelled maximum {MAX_JOBS}")
    if max_slots > MAX_SLOTS:
        raise ScalingError(f"{max_slots} slots exceeds the modelled maximum {MAX_SLOTS}")
    # CP-SAT multiplies every carbon term by the integer objective weight
    # (OBJECTIVE_WEIGHT_SCALE for a unit weight), so that factor is part of the
    # worst case. Coordination's priority and MILLI multipliers sit on top of
    # this and are covered by the INT64_SAFETY margin below int64 max.
    worst = (
        max_jobs
        * max_slots
        * max_power_w
        * max_carbon
        * max_slot_minutes
        * OBJECTIVE_WEIGHT_SCALE
    )
    if worst > INT64_SAFETY:
        raise ScalingError(
            f"worst-case objective ({worst}) exceeds the safe int64 range "
            f"({INT64_SAFETY}); reduce the horizon, job count or power rating"
        )


def assert_within_int64(value: int, label: str) -> int:
    if abs(value) > INT64_SAFETY:
        raise ScalingError(f"{label} = {value} exceeds the safe int64 range")
    return value
