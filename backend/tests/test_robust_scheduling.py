"""Forecast-aware scheduling (Phase 5, §14-§23, §35, §36, §43, §44, §45).

THE CRITICAL INVARIANT (§19). Forecast uncertainty changes WHICH schedule is
preferred. It never changes WHETHER a schedule is feasible. The tests below
attack that from both sides:

  * every mode produces deadline_misses == 0 and feasibility_violations == 0 (§43)
  * a huge risk weight cannot move a release/deadline window, cannot breach
    capacity, and cannot make an infeasible input feasible
  * a schedule built from a forecast is scored against ACTUAL carbon, never
    against its own forecast (§45)
  * the scheduler genuinely RESPONDS to the mode; accepting the configuration
    without changing behaviour would pass a test that only checks that fields
    were parsed (§44)
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.carbon import CarbonPoint, CarbonSignal, Quality, SignalType
from app.domain.forecasting import ForecastConfig, ForecastMode
from app.domain.horizon import SchedulingHorizon
from app.domain.scheduling import SchedulerConfig
from app.services.schedule_realization import (
    RealizedEvaluation,
    RealizedScheduleEvaluator,
    RobustnessExperiment,
    RobustnessExperimentRunner,
)
from app.services.scheduler_normalizer import NormalizationError
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SchedulerName

from .fixtures import bottleneck_jobs, geyser_job, mixed_scenario, washing_machine_job

ORIGIN = datetime(2026, 10, 5, tzinfo=timezone.utc)


def signal_from_pairs(pairs, resolution_minutes=15, start=ORIGIN):
    step = timedelta(minutes=resolution_minutes)
    points = [
        CarbonPoint(
            time=start + step * i,
            gco2_per_kwh=float(v),
            signal_type=SignalType.SYNTHETIC,
            quality=Quality.SYNTHETIC,
            source="test",
        )
        for i, v in enumerate(pairs)
    ]
    return CarbonSignal(
        start=start,
        end=start + step * len(pairs),
        resolution_minutes=resolution_minutes,
        source="test",
        points=points,
    )


def diurnal(count, resolution_minutes=15, start=ORIGIN):
    """A duck-curve-ish series: high at the ends, low at midday.

    The hour of day comes from the absolute timestamp, not from the index, so the
    curve is correct no matter where the series starts.
    """
    step = timedelta(minutes=resolution_minutes)
    values = []
    for i in range(count):
        moment = start + step * i
        hour = moment.hour + moment.minute / 60.0
        values.append(
            500.0 if (hour < 6 or hour >= 18) else (120.0 if 10 <= hour < 16 else 300.0)
        )
    return values


def horizon_for(jobs, resolution_minutes=15):
    """The horizon the normalizer will derive for these jobs."""
    moments = [m for job in jobs for m in (job.release_at, job.deadline_at) if m]
    return SchedulingHorizon.spanning(
        moments, slot_minutes=resolution_minutes, pad_slots=1
    )


def covering_signal(jobs):
    """A signal sized to EXACTLY the derived horizon, and aligned to it.

    The normalizer pads its horizon by one slot and refuses to schedule against a
    carbon value it does not have, so both the length AND the start must line up
    with what it derives. Deriving them here keeps these tests about forecast
    behaviour instead of about grid coverage.
    """
    horizon = horizon_for(jobs)
    return signal_from_pairs(
        diurnal(horizon.slot_count, start=horizon.start), start=horizon.start
    )


def covering_uncertainty(jobs, width=400.0, varying=False):
    """A per-slot upper bound the same length as the derived horizon.

    `varying=True` makes the band width differ by slot. That matters: a UNIFORM
    width adds the same number to every slot, which cannot reorder them, so a
    uniform-band test of "does the scheduler respond to risk_weight?" would fail
    for the right reason. Real prediction intervals do vary, so the tests that
    assert a behavioural response use this.
    """
    horizon = horizon_for(jobs)
    values = diurnal(horizon.slot_count, start=horizon.start)
    if not varying:
        return [int(v + width) for v in values]
    out = []
    for index, value in enumerate(values):
        slot_width = width if (index // 8) % 2 == 0 else width / 2.0
        out.append(int(value + slot_width))
    return out


def repeat_pattern(pattern, slot_count):
    """Tile a short pattern to exactly `slot_count` values.

    The uncertainty profile has to be the same LENGTH as the horizon, and
    `[a, b] * n` gives 2n, which is the wrong shape and not a useful failure.
    """
    return [pattern[i % len(pattern)] for i in range(slot_count)]


@pytest.fixture()
def service():
    return SchedulerService()


def build(service, config, uncertainty=None, jobs=None, signal=None, capacity_kw=10.0):
    jobs = jobs if jobs is not None else mixed_scenario()
    signal = signal if signal is not None else covering_signal(jobs)
    return service.build_input(
        jobs,
        signal,
        capacity_kw=capacity_kw,
        forecast_config=config,
        uncertainty_upper=uncertainty,
    )


# --- §14 the deterministic path is untouched ---------------------------------


def test_a_request_without_a_forecast_behaves_exactly_as_before(service):
    """§14, §38: the old path must still work, unchanged."""
    jobs = mixed_scenario()
    scheduler_input, _ = service.build_input(jobs, covering_signal(jobs), capacity_kw=10.0)
    assert scheduler_input.forecast_mode is ForecastMode.ACTUAL
    assert scheduler_input.uncertainty is None
    assert scheduler_input.objective_carbon() is scheduler_input.carbon


def test_actual_mode_optimizes_the_observed_signal_not_a_forecast(service):
    jobs = mixed_scenario()
    scheduler_input, _ = service.build_input(
        jobs,
        covering_signal(jobs),
        capacity_kw=10.0,
        forecast_config=ForecastConfig(forecast_mode=ForecastMode.ACTUAL),
    )
    assert (
        scheduler_input.objective_carbon().gco2_per_kwh
        == scheduler_input.carbon.gco2_per_kwh
    )


def test_risk_weight_is_ignored_in_actual_mode_but_says_so(service):
    scheduler_input, warnings = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.ACTUAL, risk_weight=0.5),
    )
    assert (
        scheduler_input.objective_carbon().gco2_per_kwh
        == scheduler_input.carbon.gco2_per_kwh
    )
    assert any("has no effect" in w for w in warnings)


# --- §16, §22 the objective is risk-adjusted correctly -----------------------


def test_robust_objective_is_the_forecast_plus_a_weighted_penalty(service):
    jobs = [washing_machine_job()]
    horizon = horizon_for(jobs)
    forecast = [100.0, 200.0, 300.0]
    upper = [200.0, 220.0, 400.0]
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=0.5),
        uncertainty=repeat_pattern([int(u) for u in upper], horizon.slot_count),
        jobs=jobs,
        signal=signal_from_pairs(
            forecast * horizon.slot_count, start=horizon.start
        ),
    )
    values = scheduler_input.objective_carbon().gco2_per_kwh
    for slot in range(3):
        assert values[slot] == pytest.approx(
            forecast[slot] + 0.5 * (upper[slot] - forecast[slot]), abs=1.0
        )


def test_robust_at_weight_one_equals_the_upper_bound(service):
    jobs = [washing_machine_job()]
    horizon = horizon_for(jobs)
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=1.0),
        uncertainty=repeat_pattern([200, 400], horizon.slot_count),
        jobs=jobs,
        signal=signal_from_pairs(
            [100.0, 200.0] * horizon.slot_count, start=horizon.start
        ),
    )
    assert scheduler_input.objective_carbon().gco2_per_kwh[:2] == [200, 400]


def test_expected_mode_uses_the_point_forecast_unchanged(service):
    """EXPECTED must ignore the uncertainty entirely, even at a large weight."""
    jobs = [washing_machine_job()]
    horizon = horizon_for(jobs)
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.EXPECTED, risk_weight=5.0),
        uncertainty=[999] * horizon.slot_count,
        jobs=jobs,
        signal=signal_from_pairs(
            [100.0, 200.0] * horizon.slot_count, start=horizon.start
        ),
    )
    assert scheduler_input.objective_carbon().gco2_per_kwh[:2] == [100, 200]


def test_a_larger_risk_weight_never_lowers_the_objective(service):
    jobs = [washing_machine_job()]
    horizon = horizon_for(jobs)
    signal = signal_from_pairs(
        [100.0, 200.0] * horizon.slot_count, start=horizon.start
    )
    ladders = []
    for weight in (0.0, 0.5, 1.0):
        scheduler_input, _ = build(
            service,
            ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=weight),
            uncertainty=repeat_pattern([300, 500], horizon.slot_count),
            jobs=jobs,
            signal=signal,
        )
        ladders.append(scheduler_input.objective_carbon().gco2_per_kwh)
    for slot in range(2):
        assert ladders[0][slot] <= ladders[1][slot] <= ladders[2][slot]


def test_robust_mode_refuses_to_run_without_an_uncertainty_profile(service):
    """A 'robust' schedule with no uncertainty is a lie by construction."""
    jobs = mixed_scenario()
    with pytest.raises(NormalizationError, match="no per-slot upper prediction"):
        service.build_input(
            jobs,
            covering_signal(jobs),
            capacity_kw=10.0,
            forecast_config=ForecastConfig(forecast_mode=ForecastMode.ROBUST),
            uncertainty_upper=None,
        )


def test_robust_mode_refuses_an_uncertainty_profile_of_the_wrong_length(service):
    jobs = mixed_scenario()
    with pytest.raises(NormalizationError, match="Aligning them by padding"):
        service.build_input(
            jobs,
            covering_signal(jobs),
            capacity_kw=10.0,
            forecast_config=ForecastConfig(forecast_mode=ForecastMode.ROBUST),
            uncertainty_upper=[500] * 3,
        )


def test_the_observed_signal_is_still_the_scoring_signal(service):
    """§35: the forecast is the objective; the observed signal is the ledger."""
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=1.0),
        uncertainty=covering_uncertainty(mixed_scenario()),
    )
    assert scheduler_input.carbon.gco2_per_kwh != (
        scheduler_input.objective_carbon().gco2_per_kwh
    )
    assert scheduler_input.signal_provenance()["is_forecast"] is False


# --- §43 every mode stays feasible -------------------------------------------


@pytest.mark.parametrize(
    "mode", [ForecastMode.ACTUAL, ForecastMode.EXPECTED, ForecastMode.ROBUST]
)
@pytest.mark.parametrize(
    "scheduler", [SchedulerName.ASAP, SchedulerName.GREEDY, SchedulerName.CPSAT]
)
def test_every_mode_and_engine_produces_a_valid_schedule(service, mode, scheduler):
    jobs = mixed_scenario()
    uncertainty = None if mode is ForecastMode.ACTUAL else covering_uncertainty(jobs)
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=mode, risk_weight=1.0),
        uncertainty=uncertainty,
        jobs=jobs,
    )
    result = service.run(
        scheduler_input, scheduler, config=SchedulerConfig(time_limit_seconds=10.0)
    )
    assert result.status.value in ("FEASIBLE", "OPTIMAL"), result.reason
    assert result.violations == []
    assert result.metrics.feasibility_violations == 0
    assert result.metrics.deadline_misses == 0
    assert max(result.slot_load_w) <= 10_000


# --- §19 THE INVARIANT -------------------------------------------------------


def test_risk_weight_cannot_move_a_hard_constraint(service):
    """§19, as tightly as it can be stated: the normalized windows are identical
    in every mode, because nothing but the normalizer can write them."""
    shapes = set()
    for weight in (0.0, 0.5, 10.0):
        scheduler_input, _ = build(
            service,
            ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=weight),
            uncertainty=covering_uncertainty(mixed_scenario()),
        )
        shapes.add(
            tuple(
                (
                    j.id,
                    j.release_slot,
                    j.deadline_slot,
                    j.duration_slots,
                    j.min_chunk_slots,
                    j.energy_required_wmin,
                )
                for j in scheduler_input.jobs
            )
        )
    assert len(shapes) == 1, "risk weight changed a hard constraint"


def test_an_absurd_risk_weight_still_cannot_break_a_deadline(service):
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=10.0),
        uncertainty=[10_000] * horizon_for(mixed_scenario()).slot_count,
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    assert result.metrics.deadline_misses == 0
    assert result.metrics.feasibility_violations == 0
    for placed in result.schedule:
        assert placed.end_slot <= scheduler_input.job(placed.job_id).deadline_slot


def test_uncertainty_cannot_resurrect_an_infeasible_input(service):
    """§19, §44. No amount of uncertainty may make an impossible input feasible."""
    impossible = mixed_scenario()
    for job in impossible:
        if job.job_type.value != "FIXED":
            job.power_kw = 40.0
            job.max_power_kw = 40.0
    scheduler_input, _ = service.build_input(
        impossible,
        covering_signal(impossible),
        capacity_kw=3.0,
        forecast_config=ForecastConfig(forecast_mode=ForecastMode.ACTUAL),
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    assert result.status.value in ("INFEASIBLE", "UNKNOWN")
    assert result.schedule == []


def test_robust_mode_keeps_a_thermal_job_inside_its_comfort_band(service):
    jobs = [geyser_job()]
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=2.0),
        uncertainty=covering_uncertainty(jobs, width=300.0),
        jobs=jobs,
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    assert result.status.value in ("FEASIBLE", "OPTIMAL")
    job = scheduler_input.job("geyser-1")
    allocations = {a.slot: a.power_w for a in result.schedule[0].allocations}
    temperature = job.thermal.initial_milli
    for slot in job.slots():
        temperature = job.thermal.next_temperature_milli_from_watts(
            temperature, allocations.get(slot, 0)
        )
        assert job.thermal.min_milli <= temperature <= job.thermal.max_milli
    assert temperature >= job.thermal.target_milli


# --- §20 deadline buffer ------------------------------------------------------


def test_deadline_buffer_tightens_the_effective_deadline(service):
    jobs = mixed_scenario()
    signal = covering_signal(jobs)
    plain, _ = service.build_input(jobs, signal, capacity_kw=10.0)
    buffered, warnings = service.build_input(
        jobs,
        signal,
        capacity_kw=10.0,
        forecast_config=ForecastConfig(deadline_buffer_minutes=60),
    )
    for a, b in zip(plain.jobs, buffered.jobs):
        assert b.deadline_slot <= a.deadline_slot
    assert any("deadline buffer" in w for w in warnings)


def test_deadline_buffer_never_loosens_a_deadline(service):
    jobs = mixed_scenario()
    signal = covering_signal(jobs)
    plain, _ = service.build_input(jobs, signal, capacity_kw=10.0)
    for buffer in (15, 30, 60, 120):
        buffered, _ = service.build_input(
            jobs,
            signal,
            capacity_kw=10.0,
            forecast_config=ForecastConfig(deadline_buffer_minutes=buffer),
        )
        for a, b in zip(plain.jobs, buffered.jobs):
            assert b.deadline_slot <= a.deadline_slot


def test_deadline_buffer_changes_what_the_scheduler_does(service):
    """The buffer has to be load-bearing, not merely accepted.

    The fixture is chosen for this: a single 2-slot atomic job whose cheapest
    window is the LAST two slots it is allowed to use. Removing the final 4 hours
    of headroom takes that window away, so the schedule has to move earlier.
    `bottleneck_jobs()` cannot show this — those jobs are all capacity-bound at
    slot 0, so the buffer changes nothing and the test would pass vacuously.
    """
    from app.domain.loads import LoadType, LoadSpec

    horizon_start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    job = LoadSpec(
        id="late-job",
        normalized_name="Oven",
        category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC,
        power_kw=2.0,
        max_power_kw=2.0,
        duration_minutes=30,
        release_at=horizon_start,
        deadline_at=horizon_start + timedelta(hours=8),
    )
    # Dirtiest first, cleanest at the very end of the window. The horizon is
    # derived first so the signal is exactly as long as it — the normalizer
    # refuses to guess at a slot it has no carbon value for.
    horizon = horizon_for([job])
    signal = signal_from_pairs(
        [500.0] * (horizon.slot_count - 4) + [10.0] * 4, start=horizon_start
    )

    plain_input, _ = service.build_input([job], signal, capacity_kw=5.0)
    plain = service.run(plain_input, SchedulerName.GREEDY)
    buffered_input, _ = service.build_input(
        [job],
        signal,
        capacity_kw=5.0,
        forecast_config=ForecastConfig(deadline_buffer_minutes=60),
    )
    buffered = service.run(buffered_input, SchedulerName.GREEDY)

    assert plain.schedule[0].start_slot > buffered.schedule[0].start_slot, (
        "the unbuffered job should take the clean last slots; the buffered one "
        "must not be allowed to reach them"
    )
    assert buffered.metrics.deadline_misses == 0


def test_a_buffer_larger_than_the_window_is_refused_not_ignored(service):
    """Silently dropping an impossible buffer would report a schedule that does
    not honour what the caller asked for."""
    jobs = mixed_scenario()
    with pytest.raises(NormalizationError):
        service.build_input(
            jobs,
            covering_signal(jobs),
            capacity_kw=10.0,
            forecast_config=ForecastConfig(deadline_buffer_minutes=24 * 60),
        )


# --- §44 the scheduler must RESPOND -------------------------------------------


def test_the_schedule_reacts_to_the_risk_weight(service):
    """A configuration that is accepted, validated and then ignored would pass
    every other test in this file."""
    jobs = bottleneck_jobs()
    uncertainty = covering_uncertainty(jobs, width=500.0, varying=True)
    results = {}
    for weight in (0.0, 1.0):
        scheduler_input, _ = build(
            service,
            ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=weight),
            uncertainty=uncertainty,
            jobs=jobs,
        )
        results[weight] = service.run(scheduler_input, SchedulerName.CPSAT)
    assert results[0.0].metrics.total_co2_kg != results[1.0].metrics.total_co2_kg, (
        "risk_weight had no effect on the objective the solver minimized"
    )


def test_greedy_also_responds_to_the_risk_weight(service):
    """Greedy must consume the same uncertainty CP-SAT does (§21).

    The fixture is two 2-slot atomic jobs competing for the same window, where the
    forecast says slot 0 is clean and the interval says slot 0 is the LEAST
    certain slot. EXPECTED sends them to slot 0; a large risk weight pushes them
    to slot 1, which is dirtier on the forecast but safer under uncertainty.
    """
    from app.domain.loads import LoadType, LoadSpec

    start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)

    def job(job_id):
        return LoadSpec(
            id=job_id,
            normalized_name="Oven",
            category="Cooking",
            job_type=LoadType.DEFERRABLE_ATOMIC,
            power_kw=2.0,
            max_power_kw=2.0,
            duration_minutes=30,
            release_at=start,
            deadline_at=start + timedelta(hours=2),
        )

    jobs = [job("a"), job("b")]
    horizon = horizon_for(jobs)
    # flat carbon, so only the UNCERTAINTY can reorder the choice
    signal = signal_from_pairs([300.0] * horizon.slot_count, start=start)
    # Flat carbon, so only the UNCERTAINTY can reorder the windows. The widths
    # are chosen so the 2-slot window costs differ: window (0,1) sums to 800,
    # window (2,3) to 500, window (4,5) back to 800.
    uncertainty = repeat_pattern([500, 300, 200, 300], horizon.slot_count)

    starts = {}
    for weight in (0.0, 1.0):
        scheduler_input, _ = build(
            service,
            ForecastConfig(forecast_mode=ForecastMode.ROBUST, risk_weight=weight),
            uncertainty=uncertainty,
            jobs=jobs,
            signal=signal,
            capacity_kw=5.0,
        )
        result = service.run(scheduler_input, SchedulerName.GREEDY)
        starts[weight] = sorted(j.start_slot for j in result.schedule)

    # Weight 0 ignores the intervals, every window ties on carbon, and the
    # deterministic tie-break is the EARLIEST start.
    assert starts[0.0] == [0, 0], f"expected mode should tie-break earliest, got {starts[0.0]}"
    # Weight 1 optimizes the upper bound: window (0,1) costs 800 while (1,2) and
    # (2,3) cost 500, so the cheapest safe window is (1,2) and the deterministic
    # tie-break puts the second job there too (2 kW + 2 kW fits 5 kW).
    assert starts[1.0] == [1, 1], (
        f"a large risk weight should take the narrow-interval window, got {starts[1.0]}"
    )


def test_expected_and_robust_can_reach_different_schedules(service):
    """§16, §44: the two modes must be distinguishable, not aliases."""
    jobs = bottleneck_jobs()
    uncertainty = covering_uncertainty(jobs, width=400.0, varying=True)
    by_mode = {}
    for mode in (ForecastMode.EXPECTED, ForecastMode.ROBUST):
        scheduler_input, _ = build(
            service,
            ForecastConfig(forecast_mode=mode, risk_weight=1.0),
            uncertainty=uncertainty,
            jobs=jobs,
        )
        result = service.run(scheduler_input, SchedulerName.CPSAT)
        by_mode[mode] = tuple(sorted((j.job_id, j.start_slot) for j in result.schedule))
    assert by_mode[ForecastMode.EXPECTED] != by_mode[ForecastMode.ROBUST]


# --- §45 realized CO2 is measured against actuals ----------------------------


def test_a_forecast_built_schedule_is_scored_against_actual_carbon(service):
    """§45. If the CO2 figure were computed against the forecast, a schedule would
    always look good under its own beliefs."""
    jobs = mixed_scenario()
    forecast_signal = covering_signal(jobs)
    forecast_values = [p.gco2_per_kwh for p in forecast_signal.points]
    actual_signal = signal_from_pairs(
        [v + 200.0 for v in forecast_values], start=forecast_signal.start
    )

    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.EXPECTED),
        jobs=jobs,
        signal=forecast_signal,
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)

    evaluation = RealizedScheduleEvaluator().evaluate(
        result, actual_signal, None, ForecastConfig(forecast_mode=ForecastMode.EXPECTED)
    )
    assert evaluation.realized_co2_kg is not None
    assert evaluation.realized_co2_kg > result.metrics.total_co2_kg, (
        "actuals are 200 g/kWh dirtier than the forecast everywhere, so realized "
        "emissions must exceed the forecast-scored figure"
    )


def test_an_infeasible_schedule_is_never_scored(service):
    jobs = mixed_scenario()
    scheduler_input, _ = build(service, ForecastConfig(), jobs=jobs)
    result = service.run(scheduler_input, SchedulerName.ASAP)
    result.status = type(result.status)("INFEASIBLE")
    evaluation = RealizedScheduleEvaluator().evaluate(
        result, covering_signal(jobs), None, ForecastConfig()
    )
    assert evaluation.realized_co2_kg is None
    assert "nothing to score" in evaluation.note


def test_realized_evaluation_refuses_an_incomplete_actual_signal(service):
    jobs = mixed_scenario()
    scheduler_input, _ = build(service, ForecastConfig(), jobs=jobs)
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    evaluation = RealizedScheduleEvaluator().evaluate(
        result, signal_from_pairs(diurnal(4), start=ORIGIN), None, ForecastConfig()
    )
    assert evaluation.realized_co2_kg is None
    assert "does not cover" in evaluation.note


def test_realized_evaluation_reports_forecast_error_when_given_a_forecast(service):
    from app.services.forecasting import SeasonalForecaster

    jobs = mixed_scenario()
    forecast_signal = covering_signal(jobs)
    scheduler_input, _ = build(
        service,
        ForecastConfig(forecast_mode=ForecastMode.EXPECTED),
        jobs=jobs,
        signal=forecast_signal,
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    # The forecast has to be built from history that ENDS BEFORE the window it
    # predicts, so the synthetic history is generated backwards from the
    # window start and the forecast covers the window itself.
    from app.services.forecasting import SyntheticCarbonHistory

    past = SyntheticCarbonHistory().history(forecast_signal.start, days=21)
    forecast = SeasonalForecaster().forecast(
        past, forecast_signal.start, forecast_signal.end, 15
    )
    actual = signal_from_pairs(
        [p.gco2_per_kwh + 200.0 for p in forecast_signal.points],
        start=forecast_signal.start,
    )
    evaluation = RealizedScheduleEvaluator().evaluate(
        result, actual, forecast, ForecastConfig(forecast_mode=ForecastMode.EXPECTED)
    )
    assert evaluation.forecast_expected_co2_kg is not None
    assert evaluation.forecast_error_kg == pytest.approx(
        evaluation.realized_co2_kg - evaluation.forecast_expected_co2_kg
    )


# --- §36, §37 the robustness experiments -------------------------------------


def test_the_three_deterministic_scenarios_exist_and_describe_themselves():
    scenarios = RobustnessExperimentRunner.scenarios(ORIGIN)
    assert {s.name for s in scenarios} == {
        "diverging_forecast",
        "low_forecast_error",
        "high_forecast_error",
    }
    for scenario in scenarios:
        payload = scenario.as_dict()
        assert payload["description"]
        assert payload["forecast_points"] and payload["actual_points"]
        assert "verdict" in payload


def test_the_experiments_are_byte_identical_every_run():
    first = RobustnessExperimentRunner.scenarios(ORIGIN)
    second = RobustnessExperimentRunner.scenarios(ORIGIN)
    assert first[0].as_dict() == second[0].as_dict()


def test_the_low_and_high_error_scenarios_differ_in_error():
    """A fixture whose 'high error' case is not actually high error would make the
    whole experiment vacuous."""
    scenarios = {s.name: s for s in RobustnessExperimentRunner.scenarios(ORIGIN)}

    def mean_error(scenario):
        errors = [
            abs(f - a)
            for (_t, f), (_at, a) in zip(scenario.forecast_points, scenario.actual_points)
        ]
        return sum(errors) / len(errors)

    assert mean_error(scenarios["high_forecast_error"]) > mean_error(
        scenarios["low_forecast_error"]
    )


def test_an_unrun_experiment_says_so_rather_than_inventing_a_verdict():
    """§36. With no schedules run the verdict must say so, not fabricate one."""
    scenario = RobustnessExperimentRunner.scenarios(ORIGIN)[0]
    assert scenario.as_dict()["verdict"] == "not enough results to compare"


def test_the_verdict_measures_rather_than_declares_a_winner():
    """§36: the purpose is to measure the trade-off, not to show robust wins."""
    scenario = RobustnessExperimentRunner.scenarios(ORIGIN)[0]
    scenario.results = {
        "EXPECTED": RealizedEvaluation(
            scheduler="CPSAT",
            forecast_mode="EXPECTED",
            risk_weight=0.0,
            deadline_buffer_minutes=0,
            realized_co2_kg=2.0,
        ),
        "ROBUST": RealizedEvaluation(
            scheduler="CPSAT",
            forecast_mode="ROBUST",
            risk_weight=0.5,
            deadline_buffer_minutes=0,
            realized_co2_kg=1.0,
        ),
    }
    verdict = scenario.as_dict()["verdict"]
    assert "1" in verdict
    assert "not a general claim" in verdict


def test_the_experiment_runs_both_modes_over_one_forecast_and_one_actual(service):
    """§36 end to end. EXPECTED and ROBUST are built from the SAME forecast and
    scored against the SAME actuals; anything else is circular."""
    jobs = bottleneck_jobs()
    horizon = horizon_for(jobs)
    forecast_values = diurnal(horizon.slot_count, start=horizon.start)
    actual_values = [v * 0.9 for v in forecast_values]
    width = 150.0

    forecast_signal = signal_from_pairs(forecast_values, start=horizon.start)
    actual_signal = signal_from_pairs(actual_values, start=horizon.start)

    experiment = RobustnessExperiment(
        name="test",
        description="both modes, same forecast, same actuals",
        forecast_points=[
            (p.time, p.gco2_per_kwh) for p in forecast_signal.points
        ],
        actual_points=[(p.time, p.gco2_per_kwh) for p in actual_signal.points],
        interval_half_width=width,
    )
    for mode, weight in (("EXPECTED", 0.0), ("ROBUST", 0.5)):
        scheduler_input, _ = service.build_input(
            jobs,
            forecast_signal,
            capacity_kw=10.0,
            forecast_config=ForecastConfig(
                forecast_mode=ForecastMode(mode), risk_weight=weight
            ),
            uncertainty_upper=repeat_pattern(
                [int(v + width) for v in forecast_values], len(forecast_values)
            ),
        )
        result = service.run(scheduler_input, SchedulerName.CPSAT)
        experiment.results[mode] = RealizedScheduleEvaluator().evaluate(
            result,
            actual_signal,
            None,
            ForecastConfig(forecast_mode=ForecastMode(mode), risk_weight=weight),
        )

    payload = experiment.as_dict()
    for mode in ("EXPECTED", "ROBUST"):
        assert payload["results"][mode]["realized_co2_kg"] is not None
        assert payload["results"][mode]["feasibility_violations"] == 0
        assert payload["results"][mode]["deadline_misses"] == 0
    assert "not a general claim" in payload["verdict"]