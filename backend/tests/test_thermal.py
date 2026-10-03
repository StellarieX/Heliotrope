"""Thermal model: transitions, bounds, invalid coefficients, trajectories (§6, §7, §23).

The thermal layer is the only place in Phase 3 that does real numerical work, so
it gets both example-based tests and a brute-force cross-check of the analytic
reachability formulas.
"""

import pytest
from pydantic import ValidationError

from app.domain.loads import ThermalSpec
from app.domain.thermal import ThermalModel, ThermalModelError
from app.domain.thermal_examples import AC_SYNTHETIC, GEYSER_SYNTHETIC, example_for


@pytest.fixture()
def geyser() -> ThermalModel:
    return ThermalModel.from_spec(GEYSER_SYNTHETIC, "geyser")


@pytest.fixture()
def ac() -> ThermalModel:
    return ThermalModel.from_spec(AC_SYNTHETIC, "ac")


# --- state transition (§6) -------------------------------------------------


def test_next_state_follows_the_documented_equation(geyser):
    expected = GEYSER_SYNTHETIC.a * 50.0 + GEYSER_SYNTHETIC.b * 1.5 + GEYSER_SYNTHETIC.c
    assert geyser.next_state(50.0, 1.5) == pytest.approx(expected)


def test_next_state_is_pure(geyser):
    first = [geyser.next_state(45.0, 2.0) for _ in range(5)]
    assert len(set(first)) == 1


def test_next_state_rejects_negative_power(geyser):
    with pytest.raises(ThermalModelError, match="cannot be negative"):
        geyser.next_state(45.0, -0.5)


def test_next_state_rejects_power_above_the_model_max(geyser):
    with pytest.raises(ThermalModelError, match="exceeds this model's maximum"):
        geyser.next_state(45.0, GEYSER_SYNTHETIC.max_power_kw + 0.1)


def test_next_state_rejects_non_finite_input(geyser):
    with pytest.raises(ThermalModelError):
        geyser.next_state(float("inf"), 1.0)
    with pytest.raises(ThermalModelError):
        geyser.next_state(45.0, float("nan"))


def test_geyser_bands_are_synthetic_but_physically_ordered():
    assert GEYSER_SYNTHETIC.b > 0, "a heater's power must raise its stored state"
    assert GEYSER_SYNTHETIC.temperature_min_c < GEYSER_SYNTHETIC.temperature_target_c
    assert GEYSER_SYNTHETIC.temperature_target_c < GEYSER_SYNTHETIC.temperature_max_c


def test_ac_bands_are_synthetic_but_physically_ordered():
    assert AC_SYNTHETIC.b < 0, "a cooler's power must remove heat, not add it"
    assert AC_SYNTHETIC.temperature_min_c < AC_SYNTHETIC.temperature_target_c
    assert AC_SYNTHETIC.temperature_target_c < AC_SYNTHETIC.temperature_max_c


def test_off_power_relaxes_to_the_implied_ambient(geyser, ac):
    """c = (1 - a) * ambient, so zero power must settle at the ambient the
    coefficients were built around. This is the check that a synthetic example
    is at least self-consistent."""
    assert geyser.settled_temperature(0.0) == pytest.approx(20.0, abs=0.5)
    assert ac.settled_temperature(0.0) == pytest.approx(34.0, abs=0.5)


def test_settled_temperature_is_monotonic_in_power(geyser):
    low = geyser.settled_temperature(0.5)
    high = geyser.settled_temperature(2.0)
    assert low < high


# --- multi-step simulation (§6, §23) --------------------------------------


def test_simulate_returns_initial_plus_one_state_per_step(geyser):
    profile = geyser.simulate(40.0, [2.0] * 5)
    assert len(profile.temperatures) == 6
    assert len(profile) == 5
    assert profile.initial == 40.0


def test_simulate_is_deterministic(geyser):
    a = geyser.simulate(40.0, [1.0, 2.0, 0.0, 2.0])
    b = geyser.simulate(40.0, [1.0, 2.0, 0.0, 2.0])
    assert a.temperatures == b.temperatures


def test_simulate_does_not_clamp_to_the_band(geyser):
    """Out-of-band states must be visible, not silently corrected."""
    profile = geyser.simulate(45.0, [2.0] * 40)
    assert profile.max_temperature() > GEYSER_SYNTHETIC.temperature_max_c
    assert profile.band_violations()


def test_geyser_banks_heat_then_holds_it(geyser):
    """The core geyser idea: charge at low-carbon time, serve hot later.

    The coast is deliberately short. With a = 0.90 the stored water relaxes
    toward ambient quickly, so how long it holds is a real result of the
    coefficients rather than something to assert loosely.
    """
    charged = geyser.simulate(40.0, [2.0] * 8)
    assert charged.final() > GEYSER_SYNTHETIC.temperature_target_c
    charged_kwh = charged.energy_kwh()
    assert charged_kwh > 0

    coasted = geyser.simulate(charged.final(), [0.0] * 3)
    assert coasted.final() >= GEYSER_SYNTHETIC.temperature_min_c
    assert coasted.energy_kwh() == 0.0, "holding stored heat costs no electricity"


def test_geyser_cools_back_toward_ambient_over_a_long_idle_period(geyser):
    """Stored heat is not free of time: the tank relaxes toward ambient."""
    long_coast = geyser.simulate(60.0, [0.0] * 12)
    assert long_coast.final() < 60.0
    assert long_coast.final() > 20.0


def test_ac_precools_then_relaxes_toward_ambient(ac):
    profile = ac.simulate(30.0, [1.5] * 4)
    assert profile.final() < profile.initial, "cooling input must lower room temperature"
    off = ac.simulate(profile.final(), [0.0] * 6)
    assert off.final() > profile.final(), "an uncooled room warms back toward ambient"


def test_ac_precooling_can_reach_the_comfort_floor(ac):
    profile = ac.simulate(30.0, [1.5] * 8)
    assert profile.first_step_at_or_above(AC_SYNTHETIC.temperature_min_c) is not None


def test_profile_reports_the_first_step_out_of_band(geyser):
    profile = geyser.simulate(45.0, [2.0] * 40)
    first = profile.band_violations()[0]
    assert first["violation"] == "above_max"


# --- invalid coefficients (§5, §23) ---------------------------------------


def _spec(**overrides) -> dict:
    base = dict(
        a=0.9,
        b=2.75,
        c=2.0,
        max_power_kw=2.0,
        temperature_min_c=40.0,
        temperature_max_c=65.0,
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("decay", [-0.1, 1.1, 2.0])
def test_invalid_decay_rejected(decay):
    with pytest.raises(ValidationError):
        ThermalSpec(**_spec(a=decay))


def test_zero_sensitivity_rejected():
    """b = 0 would mean electricity does nothing, i.e. not a thermal load."""
    with pytest.raises(ValidationError, match="no.*effect|non-zero"):
        ThermalSpec(**_spec(b=0.0))


def test_inverted_band_rejected():
    with pytest.raises(ValidationError, match="temperature_min_c"):
        ThermalSpec(**_spec(temperature_min_c=65.0, temperature_max_c=40.0))


def test_target_outside_the_band_rejected():
    with pytest.raises(ValidationError, match="temperature_target_c"):
        ThermalSpec(**_spec(temperature_target_c=90.0))


def test_negative_max_power_rejected():
    with pytest.raises(ValidationError):
        ThermalSpec(**_spec(max_power_kw=-1.0))


def test_non_finite_coefficients_rejected():
    with pytest.raises(ValidationError):
        ThermalSpec(**_spec(b=float("inf")))


# --- reachability (used by feasibility, not by an optimizer) --------------


def _brute_force_min_steps(model: ThermalModel, initial: float, target: float, horizon: int = 400):
    """Reference implementation: walk BOTH constant-power extremal trajectories
    and take the earliest crossing in the direction of travel.

    Uses the same 1e-9 tolerance the closed form uses, so this compares the two
    implementations rather than two different notions of "reached".
    """
    best = None
    for power in (0.0, model.max_power_kw):
        temperature = initial
        for step in range(1, horizon + 1):
            nxt = model.next_state(temperature, power)
            reached = (
                (target > initial and nxt >= target - 1e-9)
                or (target < initial and nxt <= target + 1e-9)
            )
            if reached:
                best = step if best is None else min(best, step)
                break
            temperature = nxt
    return best


@pytest.mark.parametrize(
    "example,initial,target",
    [
        ("geyser", 40.0, 60.0),
        ("geyser", 40.0, 65.0),
        ("geyser", 60.0, 45.0),
        ("ac", 30.0, 24.0),
        ("ac", 30.0, 22.0),
        ("ac", 30.0, 21.0),
    ],
)
def test_minimum_steps_matches_brute_force(example, initial, target):
    model = ThermalModel.from_spec(example_for(example))
    assert model.minimum_steps_to_reach(initial, target) == _brute_force_min_steps(
        model, initial, target
    )


def test_asymptotic_steady_state_is_not_a_practical_target(ac):
    """The AC converges toward 20 °C under full power but only asymptotically.
    It sits below the comfort floor anyway, so it is never a real target; the
    reachable envelope is what feasibility actually reports."""
    assert ac.settled_temperature(ac.max_power_kw) == pytest.approx(20.0)
    assert ac.settled_temperature(ac.max_power_kw) < AC_SYNTHETIC.temperature_min_c


def test_unreachable_target_reports_none(ac):
    assert ac.minimum_steps_to_reach(30.0, 5.0) is None


def test_extreme_reachable_brackets_the_trajectory(ac):
    low, high = ac.extreme_reachable(30.0, 6)
    full = ac.simulate(30.0, [ac.max_power_kw] * 6)
    idle = ac.simulate(30.0, [0.0] * 6)
    assert low <= min(full.temperatures[1:]) + 1e-9
    assert high >= max(idle.temperatures[1:]) - 1e-9


def test_example_lookup_returns_an_independent_copy():
    first = example_for("geyser")
    first.temperature_min_c = -100.0
    assert example_for("geyser").temperature_min_c == 40.0


def test_unknown_example_name_raises():
    with pytest.raises(KeyError):
        example_for("toaster")
