"""Worked thermal examples (Phase 3, §7).

TWO NAMED APPLIANCES, two distinct physics stories:

  GEYSER  state = stored water temperature. Charging is positive power that
          banks heat while the grid is clean, so hot water is available later
          with no power at serving time. `b > 0`.

  AC      state = room temperature. Charging is power that REMOVES heat, so
          pre-cooling before the evening ramp is the useful move. `b < 0`.
          The comfort band is 22-26 °C and the model is allowed to overshoot
          below the band during charging, which is what makes pre-cooling
          possible at all.

CALIBRATION STATUS: every coefficient below is a SYNTHETIC DEFAULT chosen to
behave plausibly for a demo. None is measured, sourced, or fitted to a real
unit, and nothing in this project may present them as appliance ratings. The
ambient temperature is folded into `c = (1 - a) * ambient`, which is what makes
`settled_temperature` equal the ambient when power is zero.
"""

from .loads import ThermalSpec

SYNTHETIC_COEFFICIENTS = "synthetic default"


def _thermal(
    *,
    ambient_c: float,
    decay_a: float,
    b: float,
    max_power_kw: float,
    resolution_minutes: int,
    t_min: float,
    t_max: float,
    t_initial: float,
    t_target: float,
) -> ThermalSpec:
    return ThermalSpec(
        a=decay_a,
        b=b,
        c=(1.0 - decay_a) * ambient_c,
        max_power_kw=max_power_kw,
        resolution_minutes=resolution_minutes,
        temperature_initial_c=t_initial,
        temperature_min_c=t_min,
        temperature_max_c=t_max,
        temperature_target_c=t_target,
    )


# --- Geyser: state = water temperature -------------------------------------

# a = 0.90 -> an unheated tank drifts back toward 20 °C ambient.
# c = 2.0   -> (1 - a) * 20, so c/a is consistent with that ambient.
# b = +2.75 -> ~3.5 °C per 15-min slot at 2 kW, so a full charge from 40 °C
#              to 60 °C takes roughly 1.5 h / ~3 kWh. Plausible for a small
#              tank, and still a synthetic default.
GEYSER_SYNTHETIC: ThermalSpec = _thermal(
    ambient_c=20.0,
    decay_a=0.90,
    b=2.75,
    max_power_kw=2.0,
    resolution_minutes=15,
    t_min=40.0,
    t_max=65.0,
    t_initial=45.0,
    t_target=55.0,
)
GEYSER_NAME = "Geyser (synthetic)"


# --- AC: state = room temperature ------------------------------------------

# a = 0.85 -> a room drifts toward a 34 °C hot-day ambient when the cooler is
#             off, which is why pre-cooling has to be timed, not stacked.
# c = 5.10  -> (1 - a) * 34.
# b = -1.40 -> power REMOVES heat. Full 1.5 kW drives the room toward 20 °C,
#             which is below the 22 °C comfort floor; that is the whole point,
#             it makes a pre-cooled room possible before the evening peak.
AC_SYNTHETIC: ThermalSpec = _thermal(
    ambient_c=34.0,
    decay_a=0.85,
    b=-1.40,
    max_power_kw=1.5,
    resolution_minutes=60,
    t_min=22.0,
    t_max=26.0,
    t_initial=30.0,
    t_target=24.0,
)
AC_NAME = "Air conditioner (synthetic)"


THERMAL_EXAMPLES: dict[str, ThermalSpec] = {
    "geyser": GEYSER_SYNTHETIC,
    "ac": AC_SYNTHETIC,
}


def example_for(key: str) -> ThermalSpec:
    """Look up a synthetic thermal example. Returns a copy so callers cannot
    mutate the shared constant."""
    if key not in THERMAL_EXAMPLES:
        raise KeyError(f"no synthetic thermal example named {key!r}; have {sorted(THERMAL_EXAMPLES)}")
    return THERMAL_EXAMPLES[key].model_copy(deep=True)
