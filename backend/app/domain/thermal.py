"""Deterministic thermal state model (Phase 3, §4, §6, §7).

    T[t+1] = a * T[t] + b * P[t] + c

The point of a thermal load is that it stores useful energy in a state, so it
cannot be reduced to a movable block. This module provides the deterministic
transition and simulation that Phase 4's optimizer will consume. It does NOT
optimize: it answers "if I apply this power profile, where does the state go?"

Electrical input is non-negative by definition, so `b` is the SIGNED
sensitivity of state to power: positive for a heater banking heat, negative for
a cooler removing it. Both cases run through the same arithmetic.

Every coefficient here is a SYNTHETIC default. None of it is calibrated to a
real appliance and no module may claim otherwise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

from .loads import ThermalSpec


class ThermalModelError(ValueError):
    """An impossible power input, a diverged trajectory, or a misconfigured model."""


@dataclass(frozen=True)
class ThermalProfile:
    """The result of simulating one power profile.

    `temperatures` has len(power_kw) + 1 entries: the initial state followed by
    the state after each step. Keeping the initial state makes trajectories
    directly comparable without the caller re-deriving it.
    """

    temperatures: list[float]
    power_kw: list[float]
    spec: ThermalSpec

    def __len__(self) -> int:
        return len(self.power_kw)

    @property
    def initial(self) -> float:
        return self.temperatures[0]

    def final(self) -> float:
        return self.temperatures[-1]

    def min_temperature(self) -> float:
        return min(self.temperatures)

    def max_temperature(self) -> float:
        return max(self.temperatures)

    def within_band(self) -> bool:
        return self.band_violations() == []

    def band_violations(self) -> list[dict]:
        """Every step that left the comfort band, with the step index.

        Returned (not just counted) so the UI can say "too cold at 02:15"
        instead of "infeasible".
        """
        out = []
        for i, t in enumerate(self.temperatures):
            if t < self.spec.temperature_min_c:
                out.append({"step": i, "temperature_c": t, "violation": "below_min"})
            elif t > self.spec.temperature_max_c:
                out.append({"step": i, "temperature_c": t, "violation": "above_max"})
        return out

    def first_step_at_or_above(self, threshold_c: float) -> Optional[int]:
        for i, t in enumerate(self.temperatures):
            if t >= threshold_c:
                return i
        return None

    def first_step_below(self, threshold_c: float) -> Optional[int]:
        for i, t in enumerate(self.temperatures):
            if t < threshold_c:
                return i
        return None

    def energy_kwh(self) -> float:
        slot_hours = self.spec.resolution_minutes / 60.0
        return sum(self.power_kw) * slot_hours

    def timestamp_for_step(self, start, step: int):
        from datetime import timedelta

        return start + timedelta(minutes=self.spec.resolution_minutes * step)


class ThermalModel:
    """First-order thermal dynamics over a fixed slot resolution.

    Stateless with respect to simulation: `next_state` is a pure function, so
    the same (state, power) always yields the same next state. This is what
    makes the model testable and Phase 4 reproducible.
    """

    def __init__(self, spec: ThermalSpec, name: str = "thermal load") -> None:
        self.spec = spec
        self.name = name

    # --- parameters --------------------------------------------------------

    @property
    def a(self) -> float:
        return self.spec.a

    @property
    def b(self) -> float:
        return self.spec.b

    @property
    def c(self) -> float:
        return self.spec.c

    @property
    def max_power_kw(self) -> float:
        return self.spec.max_power_kw

    @property
    def resolution_minutes(self) -> int:
        return self.spec.resolution_minutes

    @classmethod
    def from_spec(cls, spec: ThermalSpec, name: str = "thermal load") -> "ThermalModel":
        return cls(spec, name=name)

    # --- dynamics ----------------------------------------------------------

    def next_state(self, current_temperature: float, power_kw: float) -> float:
        """T[t+1] = a*T[t] + b*P[t] + c.

        Raises ThermalModelError for negative power, power above the model's
        ceiling, or a non-finite state. Refusing is deliberate: silently
        clamping would hide a real infeasibility from Phase 4.
        """
        if not math.isfinite(current_temperature):
            raise ThermalModelError(f"current temperature must be finite (got {current_temperature})")
        if not math.isfinite(power_kw):
            raise ThermalModelError(f"power must be finite (got {power_kw})")
        if power_kw < 0:
            raise ThermalModelError(
                f"power must be >= 0 (got {power_kw}); electrical input cannot be negative"
            )
        if power_kw > self.max_power_kw + 1e-9:
            raise ThermalModelError(
                f"power {power_kw:.3f} kW exceeds this model's maximum of {self.max_power_kw:.3f} kW"
            )
        nxt = self.a * current_temperature + self.b * power_kw + self.c
        if not math.isfinite(nxt):
            raise ThermalModelError(
                "thermal state diverged to a non-finite value; check the a/b/c coefficients"
            )
        return nxt

    def settled_temperature(self, power_kw: float) -> float:
        """The steady state this model reaches if `power_kw` is held forever.

        Only meaningful when the state converges (a != 1), which ThermalSpec
        enforces. Returns the fixed point (b*P + c) / (1 - a).
        """
        if not 0.0 <= power_kw <= self.max_power_kw + 1e-9:
            raise ThermalModelError(
                f"power {power_kw} is outside the model's range [0, {self.max_power_kw}]"
            )
        return (self.b * power_kw + self.c) / (1.0 - self.a)

    def simulate(
        self,
        initial_temperature: float,
        power_profile: Sequence[float],
    ) -> ThermalProfile:
        """Run a power profile forward from an initial state.

        Does not clamp to the comfort band: out-of-band states are returned so
        the caller can see them. Use `ThermalProfile.band_violations()` to
        report them.
        """
        states = [float(initial_temperature)]
        power = [float(p) for p in power_profile]
        for p in power:
            states.append(self.next_state(states[-1], p))
        return ThermalProfile(temperatures=states, power_kw=power, spec=self.spec)

    # --- reachability (used by feasibility, NOT by an optimizer) -----------

    def extreme_reachable(self, initial_temperature: float, steps: int) -> tuple[float, float]:
        """(coldest, hottest) state reachable in `steps` slots at full power.

        Because the dynamics are linear and monotone in P, the extremes are
        exactly the two full-power corners. Closed form, not a search.
        """
        if steps < 0:
            raise ThermalModelError("steps must be >= 0")
        power = self.max_power_kw if self.b >= 0 else 0.0
        other = 0.0 if self.b >= 0 else self.max_power_kw
        hi = self._closed_form(initial_temperature, power, steps)
        lo = self._closed_form(initial_temperature, other, steps)
        return (lo, hi) if lo <= hi else (hi, lo)

    def _closed_form(self, initial: float, power: float, steps: int) -> float:
        a, c = self.a, self.c
        if abs(1.0 - a) < 1e-12:
            return initial + steps * (self.b * power + c)
        steady = (self.b * power + c) / (1.0 - a)
        return steady + (initial - steady) * (a ** steps)

    def can_reach(self, initial_temperature: float, target_c: float, steps: int) -> bool:
        """Whether `target_c` lies inside the reachable envelope.

        A reachability check, not a plan: it says the target is physically
        possible, not that some schedule attains it.
        """
        lo, hi = self.extreme_reachable(initial_temperature, steps)
        return lo - 1e-9 <= target_c <= hi + 1e-9

    def minimum_steps_to_reach(
        self, initial_temperature: float, target_c: float, max_steps: int = 10_000
    ) -> Optional[int]:
        """Fewest full-power slots needed to reach `target_c`, or None.

        The dynamics are linear and monotone in power, so the extremal
        trajectory (full power when we need to rise, off when we need to fall)
        crosses the target at the earliest possible step. Scanning the closed
        form keeps this exact and deterministic with no search heuristic.
        """
        if abs(target_c - initial_temperature) < 1e-9:
            return 0
        rising = target_c > initial_temperature
        # Drive the state toward the target with whatever sign of power helps:
        # a heater (b > 0) rises under power and falls by drifting toward a
        # cooler ambient, while a cooler (b < 0) is the mirror image. Using the
        # wrong extremum is what makes a reachable target look unreachable.
        power = self.max_power_kw if (self.b > 0) == rising else 0.0
        if not self.can_reach(initial_temperature, target_c, max_steps):
            return None
        for steps in range(1, max_steps + 1):
            value = self._closed_form(initial_temperature, power, steps)
            if rising and value >= target_c - 1e-9:
                return steps
            if not rising and value <= target_c + 1e-9:
                return steps
        return None
