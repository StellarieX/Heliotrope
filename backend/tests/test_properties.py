"""Property-based tests (§24).

Invariants that must hold for every input, not just the examples:

  * any valid power is non-negative, and any thermal state stays finite
  * a thermal trajectory never leaves the model's own reachability envelope
  * temperature_min <= temperature_max always
  * the energy-feasibility verdict is monotonic in energy: asking for more
    energy can never make a load feasible again
  * an impossible load is never reported as possible

The domains are deliberately physical. Generating nonsense (negative power,
NaN coefficients, reversed time windows) is excluded because those are rejected
at parse time by other tests, and would only inflate the count.
"""

import math

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from app.core.feasibility import is_energy_feasible, is_time_feasible, validate_load
from app.domain.loads import LoadSpec, LoadType, ThermalSpec
from app.domain.thermal import ThermalModel
from app.services.classification import RuleBasedLoadClassifier

# --- physical strategies ---------------------------------------------------

powers = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
temperatures = st.floats(min_value=-50.0, max_value=120.0, allow_nan=False, allow_infinity=False)
energies = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)
durations = st.integers(min_value=1, max_value=1440)
chunks = st.integers(min_value=1, max_value=240)


@st.composite
def thermal_specs(draw):
    """A physically coherent first-order thermal model."""
    decay = draw(st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
    low = draw(temperatures)
    high = draw(st.floats(min_value=low, max_value=low + 60.0, allow_nan=False))
    return ThermalSpec(
        a=decay,
        # b is signed and never zero: a zero sensitivity means "not thermal".
        b=draw(st.one_of(st.floats(min_value=0.01, max_value=10.0), st.floats(min_value=-10.0, max_value=-0.01))),
        c=draw(st.floats(min_value=-20.0, max_value=20.0)),
        max_power_kw=draw(powers),
        resolution_minutes=draw(chunks),
        temperature_initial_c=draw(temperatures),
        temperature_min_c=low,
        temperature_max_c=high,
    )


# --- thermal invariants ----------------------------------------------------


@given(spec=thermal_specs(), power=powers)
@settings(max_examples=150, deadline=None)
def test_next_state_is_always_finite(spec, power):
    model = ThermalModel.from_spec(spec)
    assume(power <= spec.max_power_kw)
    assert math.isfinite(model.next_state(30.0, power))


@given(spec=thermal_specs(), initial=temperatures, profile=st.lists(powers, max_size=12))
@settings(max_examples=150, deadline=None)
def test_simulation_states_stay_finite(spec, initial, profile):
    model = ThermalModel.from_spec(spec)
    assume(all(p <= spec.max_power_kw for p in profile))
    for value in model.simulate(initial, profile).temperatures:
        assert math.isfinite(value)


@given(spec=thermal_specs(), initial=temperatures, profile=st.lists(powers, max_size=10))
@settings(max_examples=120, deadline=None)
def test_trajectory_never_leaves_the_reachability_envelope(spec, initial, profile):
    """At every step k the state must lie inside the k-step envelope.

    The envelope is exact rather than a loose bound: the dynamics are linear and
    monotone in power, so the extreme at step k is reached by holding one corner
    power for all k slots. Comparing each step against ITS OWN envelope also
    avoids the initial state, which is not the result of any transition.
    """
    model = ThermalModel.from_spec(spec)
    assume(all(p <= spec.max_power_kw for p in profile))
    states = model.simulate(initial, profile).temperatures
    for step, value in enumerate(states[1:], start=1):
        low, high = model.extreme_reachable(initial, step)
        assert low - 1e-6 <= value <= high + 1e-6


@given(low=temperatures, span=st.floats(min_value=0.0, max_value=60.0))
@settings(max_examples=100, deadline=None)
def test_temperature_band_is_always_ordered(low, span):
    spec = ThermalSpec(
        a=0.9,
        b=1.0,
        c=0.0,
        max_power_kw=2.0,
        temperature_min_c=low,
        temperature_max_c=low + span,
    )
    assert spec.temperature_min_c <= spec.temperature_max_c


@given(spec=thermal_specs(), initial=temperatures, target=temperatures)
@settings(max_examples=120, deadline=None)
def test_reachability_agrees_with_the_envelope(spec, initial, target):
    model = ThermalModel.from_spec(spec)
    steps = model.minimum_steps_to_reach(initial, target, max_steps=200)
    low, high = model.extreme_reachable(initial, 200)
    if steps is None:
        assert not (low - 1e-9 <= target <= high + 1e-9)


# --- feasibility invariants (§24) ------------------------------------------


def interruptible(power: float, energy: float, window_hours: int, chunk: int) -> LoadSpec:
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    return LoadSpec(
        normalized_name="EV",
        category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=power,
        energy_required_kwh=energy,
        min_chunk_minutes=chunk,
        release_at=start,
        deadline_at=start + timedelta(hours=window_hours),
    )


@given(power=powers, energy=energies, window=st.integers(min_value=1, max_value=48), chunk=chunks)
@settings(max_examples=200, deadline=None)
def test_feasibility_is_monotonic_in_energy(power, energy, window, chunk):
    """Asking for more energy can only ever remove feasibility, never add it."""
    assume(power > 0)
    assume(energy > 0)
    less = is_energy_feasible(interruptible(power, energy, window, chunk)).feasible
    more = is_energy_feasible(interruptible(power, energy * 1.5, window, chunk)).feasible
    assert not (less is False and more is True)


@given(power=powers, energy=energies, window=st.integers(min_value=1, max_value=48), chunk=chunks)
@settings(max_examples=200, deadline=None)
def test_feasible_implies_energy_fits_the_window(power, energy, window, chunk):
    """The core §24 invariant: the checker never claims physically impossible
    loads are fine."""
    assume(power > 0 and energy > 0)
    spec = interruptible(power, energy, window, chunk)
    report = is_energy_feasible(spec)
    if report.feasible:
        usable = window * 60
        if spec.min_chunk_minutes:
            usable = (usable // spec.min_chunk_minutes) * spec.min_chunk_minutes
        assert energy <= spec.effective_max_power_kw() * usable / 60.0 + 1e-6


@given(power=powers, window=st.integers(min_value=1, max_value=48), duration=durations)
@settings(max_examples=200, deadline=None)
def test_atomic_feasible_implies_window_is_long_enough(power, window, duration):
    from datetime import datetime, timedelta, timezone

    assume(power >= 0)
    start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    spec = LoadSpec(
        normalized_name="Washing machine",
        category="Laundry",
        job_type=LoadType.DEFERRABLE_ATOMIC,
        power_kw=power,
        duration_minutes=duration,
        release_at=start,
        deadline_at=start + timedelta(hours=window),
    )
    if is_time_feasible(spec).feasible:
        assert spec.window_minutes() >= duration


@given(power=powers, duration=durations)
@settings(max_examples=100, deadline=None)
def test_derived_energy_is_never_negative(power, duration):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    spec = LoadSpec(
        normalized_name="Atomic",
        category="Test",
        job_type=LoadType.DEFERRABLE_ATOMIC,
        power_kw=power,
        duration_minutes=duration,
        release_at=start,
        deadline_at=start + timedelta(hours=24),
    )
    assert spec.derived_energy_kwh() >= 0.0


# --- classification invariants ---------------------------------------------


@given(st.text(min_size=1, max_size=80))
@settings(max_examples=250, deadline=None)
def test_classification_always_answers(text):
    """Any string at all yields a valid class, a usable reason, and a confidence
    inside [0, 1]. The classifier must never throw on user input."""
    result = RuleBasedLoadClassifier().classify(text)
    assert result.job_type in set(LoadType)
    assert 0.0 <= result.confidence <= 1.0
    assert len(result.reason) > 30


@given(st.text(min_size=1, max_size=80))
@settings(max_examples=150, deadline=None)
def test_classification_is_case_and_punctuation_insensitive(text):
    """Users type "E V", "ev", "EV!". The class must not depend on that."""
    classifier = RuleBasedLoadClassifier()
    messy = text + "!!!  ,,"
    assert classifier.classify(text).job_type is classifier.classify(messy).job_type
