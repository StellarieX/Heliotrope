"""Realized-vs-forecast schedule evaluation (Phase 5, §35, §36, §37).

    schedule built from a forecast -> later scored against ACTUAL carbon

THE POINT OF THIS MODULE. A schedule optimized against a forecast can look
excellent against that forecast and be a disaster against reality. The only
honest way to compare EXPECTED scheduling against ROBUST scheduling is to build
both from the SAME forecast and then measure both against the SAME actuals. Any
comparison where each arm is scored against its own belief is circular.

So the flow is always the same, and it is enforced by the signature: you cannot
construct a `RealizedEvaluation` without passing an actual signal. A
schedule-optimized-on-a-forecast cannot report its own success.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from ..domain.carbon import CarbonSignal
from ..domain.forecasting import CarbonForecast, ForecastConfig, ForecastMode
from ..domain.scaling import CO2_KG_DIVISOR
from ..domain.scheduling import SchedulerInput, SchedulerResult, ScheduleStatus


class RealizedEvaluationError(ValueError):
    """A schedule cannot be scored against actuals as asked."""


def co2_kg_for_schedule(
    result: SchedulerResult,
    actual: CarbonSignal,
    slot_minutes: int = 15,
) -> Optional[float]:
    """Total CO2 of a produced schedule against the OBSERVED signal (§35).

    Summed over the whole schedule including baseline, because a fixed fridge
    emits whether or not anything was scheduled around it — the same convention
    `CarbonAccountingService` uses, and deliberately the same function shape.

    Returns None if the signal does not cover the schedule, rather than scoring
    a partial schedule against partial data and reporting the total.
    """
    by_time = {p.time: p.gco2_per_kwh for p in actual.points}
    total = 0.0
    seen_any = False
    for placed in result.schedule:
        for allocation in placed.allocations:
            intensity = by_time.get(allocation.timestamp)
            if intensity is None:
                return None
            seen_any = True
            total += allocation.power_w * slot_minutes * intensity / CO2_KG_DIVISOR
    # Baseline too: the fixed part of the load is part of what the site emits.
    for slot, power_w in enumerate(result.baseline_slot_load_w):
        if not power_w:
            continue
        start = result.horizon.slot_start(slot) if result.horizon else None
        if start is None:
            return None
        intensity = by_time.get(start)
        if intensity is None:
            return None
        seen_any = True
        total += power_w * slot_minutes * intensity / CO2_KG_DIVISOR
    return total if seen_any else None


def co2_kg_against_forecast(
    result: SchedulerResult,
    forecast: CarbonForecast,
    slot_minutes: int = 15,
) -> Optional[float]:
    """The same schedule scored against the FORECAST that produced it.

    This is the schedule's own opinion of itself. It is reported next to the
    realized number precisely so the gap is visible: when
    forecast_expected_co2 is far below realized_co2, the schedule looked better
    than it was, and no amount of internal metric supports it.
    """
    by_time = {p.timestamp: p.predicted_gco2_per_kwh for p in forecast.points}
    total = 0.0
    for placed in result.schedule:
        for allocation in placed.allocations:
            intensity = by_time.get(allocation.timestamp)
            if intensity is None:
                return None
            total += allocation.power_w * slot_minutes * intensity / CO2_KG_DIVISOR
    for slot, power_w in enumerate(result.baseline_slot_load_w):
        if not power_w:
            continue
        start = result.horizon.slot_start(slot) if result.horizon else None
        if start is None or start not in by_time:
            return None
        total += power_w * slot_minutes * by_time[start] / CO2_KG_DIVISOR
    return total


@dataclass
class RealizedEvaluation:
    """One schedule, scored three ways.

        realized_co2_kg     — against ACTUAL carbon. The real number.
        forecast_expected_co2_kg — against the forecast. The optimistic one.
        forecast_error_kg   — realized minus expected. Positive means the
                               forecast was too optimistic.
    """

    scheduler: str
    forecast_mode: str
    risk_weight: float
    deadline_buffer_minutes: int
    realized_co2_kg: Optional[float] = None
    forecast_expected_co2_kg: Optional[float] = None
    forecast_error_kg: Optional[float] = None
    #: how many scheduled slots the actual signal covered
    covered_slots: Optional[int] = None
    deadline_misses: Optional[int] = None
    feasibility_violations: Optional[int] = None
    status: str = ""
    #: "actual signal does not cover the schedule" etc.
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "scheduler": self.scheduler,
            "forecast_mode": self.forecast_mode,
            "risk_weight": self.risk_weight,
            "deadline_buffer_minutes": self.deadline_buffer_minutes,
            "status": self.status,
            "realized_co2_kg": self.realized_co2_kg,
            "forecast_expected_co2_kg": self.forecast_expected_co2_kg,
            "forecast_error_kg": self.forecast_error_kg,
            "covered_slots": self.covered_slots,
            "deadline_misses": self.deadline_misses,
            "feasibility_violations": self.feasibility_violations,
            "note": self.note,
        }


class RealizedScheduleEvaluator:
    """§35: scores schedules against actual carbon."""

    def evaluate(
        self,
        result: SchedulerResult,
        actual: CarbonSignal,
        forecast: Optional[CarbonForecast] = None,
        forecast_config: Optional[ForecastConfig] = None,
    ) -> RealizedEvaluation:
        config = forecast_config or ForecastConfig()
        evaluation = RealizedEvaluation(
            scheduler=result.scheduler,
            forecast_mode=config.forecast_mode.value,
            risk_weight=config.risk_weight,
            deadline_buffer_minutes=config.deadline_buffer_minutes,
            status=result.status.value,
            deadline_misses=result.metrics.deadline_misses,
            feasibility_violations=result.metrics.feasibility_violations,
        )
        slot_minutes = result.horizon.slot_minutes if result.horizon else 15

        if result.status not in (ScheduleStatus.FEASIBLE, ScheduleStatus.OPTIMAL):
            evaluation.note = (
                f"schedule status is {result.status.value}; there is nothing to score "
                f"against actual carbon"
            )
            return evaluation

        evaluation.realized_co2_kg = co2_kg_for_schedule(result, actual, slot_minutes)
        if evaluation.realized_co2_kg is None:
            evaluation.note = (
                "the actual carbon signal does not cover every slot this schedule uses, "
                "so realized emissions cannot be computed. Refusing to report a partial "
                "total as if it were the whole one."
            )
            return evaluation
        evaluation.covered_slots = sum(
            len(placed.allocations) for placed in result.schedule
        ) + sum(1 for p in result.baseline_slot_load_w if p)

        if forecast is not None:
            evaluation.forecast_expected_co2_kg = co2_kg_against_forecast(
                result, forecast, slot_minutes
            )
            if (
                evaluation.forecast_expected_co2_kg is not None
                and evaluation.realized_co2_kg is not None
            ):
                evaluation.forecast_error_kg = (
                    evaluation.realized_co2_kg - evaluation.forecast_expected_co2_kg
                )
        return evaluation


@dataclass
class RobustnessExperiment:
    """§36: EXPECTED vs ROBUST on one deterministic scenario.

    The scenario is explicit — the forecast's own numbers and the actual's own
    numbers — so the comparison can be checked by hand and cannot be confused
    with a general claim.
    """

    name: str
    description: str
    #: (timestamp, gCO2/kWh) the forecaster believed
    forecast_points: list[tuple[datetime, float]]
    #: (timestamp, gCO2/kWh) that actually happened
    actual_points: list[tuple[datetime, float]]
    #: half-width of the empirical prediction interval, gCO2/kWh, applied
    #: uniformly across the window
    interval_half_width: float = 0.0
    #: optional per-window widths, [first window, second window]. A UNIFORM width
    #: makes ROBUST mode provably inert: `forecast + λ·w` adds the same number to
    #: every slot, so no slot changes rank and no schedule can change. Real
    #: prediction intervals are not uniform — they are wide exactly where the
    #: model is least trustworthy — so scenarios that are meant to show a
    #: difference use this instead.
    interval_half_widths: Optional[list[float]] = None
    results: dict[str, RealizedEvaluation] = field(default_factory=dict)

    def width_for(self, index: int, total: int) -> float:
        """The interval half-width that applies to slot `index`."""
        if self.interval_half_widths:
            split = max(1, total // len(self.interval_half_widths))
            bucket = min(index // split, len(self.interval_half_widths) - 1)
            return self.interval_half_widths[bucket]
        return self.interval_half_width

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "interval_half_width_gco2_per_kwh": self.interval_half_width,
            "interval_half_widths_gco2_per_kwh": self.interval_half_widths,
            "forecast_points": [
                {"timestamp": t.isoformat(), "gco2_per_kwh": v}
                for t, v in self.forecast_points
            ],
            "actual_points": [
                {"timestamp": t.isoformat(), "gco2_per_kwh": v}
                for t, v in self.actual_points
            ],
            "results": {name: r.as_dict() for name, r in self.results.items()},
            "verdict": self._verdict(),
        }

    def _verdict(self) -> str:
        """State what happened. Do not state which mode is better in general.

        §36: the purpose of this experiment is to MEASURE the trade-off, not to
        demonstrate that robust scheduling wins. On a scenario where the
        uncertainty was large and the outcome turned out mild, ROBUST can lose,
        and saying so is the entire value of running it.
        """
        expected = self.results.get("EXPECTED")
        robust = self.results.get("ROBUST")
        if expected is None or robust is None:
            return "not enough results to compare"
        if expected.realized_co2_kg is None or robust.realized_co2_kg is None:
            return "one of the arms could not be scored against actual carbon"
        delta = robust.realized_co2_kg - expected.realized_co2_kg
        direction = "more" if delta > 0 else "less" if delta < 0 else "the same"
        winner = "ROBUST" if delta < 0 else "EXPECTED" if delta > 0 else "neither"
        return (
            f"On THIS scenario ROBUST scheduling emitted {abs(delta):.6g} kg "
            f"{direction} than EXPECTED, so {winner} scored better here. That is a "
            f"measurement of one scenario, not a general claim: robustness only pays "
            f"when the realized carbon actually differs from the forecast in the "
            f"direction the interval anticipated."
        )


class RobustnessExperimentRunner:
    """Builds the deterministic §36 fixtures and runs both modes over them."""

    @staticmethod
    def scenarios(
        origin: datetime, slots: int = 4, resolution_minutes: int = 60
    ) -> list[RobustnessExperiment]:
        """Three fixed scenarios: the §36 case, plus low and high error (§37).

        Every number here is written out. Nothing is sampled, so the experiment
        is byte-identical on every machine and a surprising result can be traced
        to these literals rather than to an unseeded random draw.
        """
        step = timedelta(minutes=resolution_minutes)
        times = [origin + step * i for i in range(slots)]

        # §36's stated case: forecast says midday is far cleaner than evening,
        # and in reality the gap mostly evaporates.
        #
        # The interval widths are NOT uniform. Each scenario is a WIDE first window
        # and a NARROW second one, because that is the situation robustness exists
        # for: the forecast is least trustworthy about one part of the horizon.
        # A uniform width would add the same number to every slot, change no
        # ranking, and make every scenario report a meaningless tie.
        return [
            RobustnessExperiment(
                name="diverging_forecast",
                description=(
                    "The §36 fixture: the forecast puts a large clean-vs-dirty gap at "
                    "midday versus evening, and the realized carbon turns out much less "
                    "different than predicted. The forecast is wide about the 'clean' "
                    "midday window and narrow about the evening."
                ),
                forecast_points=[(t, 200.0) for t in times[:2]] + [(t, 500.0) for t in times[2:]],
                actual_points=[(t, 350.0) for t in times[:2]] + [(t, 450.0) for t in times[2:]],
                interval_half_width=120.0,
                interval_half_widths=[260.0, 60.0],
            ),
            RobustnessExperiment(
                name="low_forecast_error",
                description=(
                    "Low error case: the forecast is close to what happened, and the "
                    "interval is narrow on both sides. Little to be gained or lost by "
                    "hedging."
                ),
                forecast_points=[(t, 210.0) for t in times[:2]] + [(t, 480.0) for t in times[2:]],
                actual_points=[(t, 225.0) for t in times[:2]] + [(t, 505.0) for t in times[2:]],
                interval_half_width=25.0,
                interval_half_widths=[40.0, 15.0],
            ),
            RobustnessExperiment(
                name="high_forecast_error",
                description=(
                    "High error case: the forecast is badly wrong and the error runs in "
                    "the direction the interval warned about — the 'clean' midday "
                    "window turns out dirty and the 'dirty' evening turns out clean. "
                    "This is where hedging is supposed to earn its keep."
                ),
                forecast_points=[(t, 120.0) for t in times[:2]] + [(t, 430.0) for t in times[2:]],
                actual_points=[(t, 470.0) for t in times[:2]] + [(t, 300.0) for t in times[2:]],
                interval_half_width=200.0,
                interval_half_widths=[420.0, 90.0],
            ),
        ]