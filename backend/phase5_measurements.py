"""Phase 5 measurements (development script, not part of the test suite).

Runs the forecast, backtest, scheduling and robustness experiments and prints the
numbers that go into the Phase 5 report. Everything printed here is MEASURED at
runtime; nothing is written down by hand.

Run from the backend directory:  python phase5_measurements.py
"""

from datetime import datetime, timedelta, timezone

import statistics
import time

from app.domain.forecasting import ForecastConfig, ForecastMode
from app.domain.scheduling import SchedulerConfig
from app.services.forecast_backtest import (
    BacktestConfig,
    ForecastComparisonService,
    ForecastBacktester,
)
from app.services.forecast_service import ForecastService
from app.services.forecasting import PersistenceForecaster, SeasonalForecaster, SyntheticCarbonHistory
from app.services.schedule_realization import RealizedScheduleEvaluator, RobustnessExperiment
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SchedulerName

ORIGIN = datetime(2026, 10, 5, tzinfo=timezone.utc)


def rule(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# --- 1. forecast generation time ---------------------------------------------


def measure_forecasting():
    rule("1. FORECAST GENERATION TIME (synthetic history, 15-minute slots)")
    history = SyntheticCarbonHistory().history(ORIGIN, days=30)
    print(f"history: {len(history)} points, {history.start} .. {history.end}")

    for name, model in (
        ("persistence", PersistenceForecaster()),
        ("seasonal   ", SeasonalForecaster()),
    ):
        for days in (1, 3, 7):
            end = ORIGIN + timedelta(days=days)
            samples = []
            for _ in range(5):
                start = time.perf_counter()
                forecast = model.forecast(history, ORIGIN, end, 15)
                samples.append((time.perf_counter() - start) * 1000)
            print(
                f"  {name}  {days}d horizon ({len(forecast):3d} slots): "
                f"median {statistics.median(samples):7.1f} ms  "
                f"min {min(samples):7.1f} ms  max {max(samples):7.1f} ms"
            )


# --- 2. backtest results -----------------------------------------------------


def measure_backtests():
    rule("2. BACKTEST RESULTS (SYNTHETIC 30-day history, rolling daily 24h horizon)")
    history = SyntheticCarbonHistory().history(ORIGIN, days=30)
    config = BacktestConfig(
        horizon_hours=24.0, step_hours=24.0, max_steps=24, max_history_days=60
    )
    header = (
        f"{'model':<12}{'steps':>6}{'points':>8}{'MAE':>9}{'RMSE':>9}"
        f"{'sMAPE%':>9}{'bias':>9}{'coverage%':>11}{'width':>9}{'ms':>7}"
    )
    print(header)
    for model in ("persistence", "seasonal"):
        result = ForecastBacktester().run(history, BacktestConfig(**{**config.__dict__, "model": model}))
        m = result.metrics
        print(
            f"{model:<12}{result.steps:>6}{m.evaluated_points:>8}"
            f"{m.mae:>9.2f}{m.rmse:>9.2f}{m.smape_percent:>9.2f}{m.bias:>9.2f}"
            f"{m.interval_coverage_percent:>11.1f}{m.interval_width:>9.1f}{result.runtime_ms:>7}"
        )
    print(
        "\n  nominal interval coverage requested: "
        f"{result.metrics.nominal_coverage_percent:.0f}%"
    )

    # shorter horizons, where persistence should be relatively stronger
    print("\n  horizon sensitivity (same history):")
    print(f"    {'horizon':>10}  {'persistence MAE':>16}  {'seasonal MAE':>14}")
    for hours in (6, 12, 24, 48):
        row = {}
        for model in ("persistence", "seasonal"):
            r = ForecastBacktester().run(
                history,
                BacktestConfig(
                    model=model,
                    horizon_hours=float(hours),
                    step_hours=24.0,
                    max_steps=24,
                    max_history_days=60,
                ),
            )
            row[model] = r.metrics.mae
        print(f"    {hours:>8}h  {row['persistence']:>16.2f}  {row['seasonal']:>14.2f}")

    rule("2b. MODEL COMPARISON OVER ONE DATASET (no winner is named)")
    comparison = ForecastComparisonService().compare(
        history, config=BacktestConfig(max_steps=8, max_history_days=60)
    )
    print(f"  dataset: {comparison.dataset['history_points']} points, "
          f"synthetic={comparison.dataset['is_synthetic']}, "
          f"total {comparison.runtime_ms} ms")
    for name, result in comparison.results.items():
        m = result.metrics
        print(
            f"    {name:<12} MAE {m.mae:7.2f}  RMSE {m.rmse:7.2f}  "
            f"coverage {m.interval_coverage_percent:5.1f}%  width {m.interval_width:6.1f}  "
            f"{result.runtime_ms:5d} ms"
        )


# --- 3. forecast intervals ----------------------------------------------------


def measure_intervals():
    rule("3. PREDICTION INTERVALS (empirical, by time of day)")
    history = SyntheticCarbonHistory().history(ORIGIN, days=21)
    for name, model in (
        ("persistence", PersistenceForecaster()),
        ("seasonal   ", SeasonalForecaster()),
    ):
        forecast = model.forecast(history, ORIGIN, ORIGIN + timedelta(days=1), 15)
        widths = [p.uncertainty_gco2_per_kwh for p in forecast.points]
        by_hour = {}
        for point in forecast.points:
            by_hour.setdefault(point.timestamp.hour, []).append(
                point.uncertainty_gco2_per_kwh
            )
        night = statistics.mean(by_hour[3])
        midday = statistics.mean(by_hour[13])
        evening = statistics.mean(by_hour[19])
        print(
            f"  {name}: half-width min {min(widths):6.1f}  max {max(widths):6.1f}  "
            f"| night 03h {night:6.1f}  midday 13h {midday:6.1f}  evening 19h {evening:6.1f}"
        )


# --- 4. expected vs robust scheduling + realized CO2 -------------------------


def build_signal(values, start=ORIGIN):
    from app.domain.carbon import CarbonPoint, CarbonSignal, Quality, SignalType

    step = timedelta(minutes=15)
    points = [
        CarbonPoint(
            time=start + step * i,
            gco2_per_kwh=float(v),
            signal_type=SignalType.SYNTHETIC,
            quality=Quality.SYNTHETIC,
            source="synthetic_forecast",
        )
        for i, v in enumerate(values)
    ]
    return CarbonSignal(
        start=start,
        end=start + step * len(values),
        resolution_minutes=15,
        source="synthetic_forecast",
        points=points,
    )


def measure_scheduling():
    rule("4. EXPECTED vs ROBUST SCHEDULING, SCORED AGAINST ACTUAL CARBON")
    from tests.fixtures import mixed_scenario
    from app.domain.horizon import SchedulingHorizon

    jobs = mixed_scenario()
    moments = [m for job in jobs for m in (job.release_at, job.deadline_at) if m]
    horizon = SchedulingHorizon.spanning(moments, slot_minutes=15, pad_slots=1)
    n = horizon.slot_count

    # A forecast built from real history, and "actuals" that differ from it.
    history = SyntheticCarbonHistory().history(horizon.start, days=21)
    forecast = SeasonalForecaster().forecast(history, horizon.start, horizon.end, 15)
    forecast_values = [p.predicted_gco2_per_kwh for p in forecast.points]
    upper = [p.upper_gco2_per_kwh for p in forecast.points]

    # "Actual" is the forecast shifted by a smooth bias, which is what a real
    # forecast error looks like rather than white noise.
    actual_values = [
        v + 60.0 + 40.0 * (i % 8) / 8.0 for i, v in enumerate(forecast_values)
    ]

    forecast_signal = build_signal(forecast_values)
    actual_signal = build_signal(actual_values)

    service = SchedulerService()
    evaluator = RealizedScheduleEvaluator()

    print(
        f"  {len(forecast.points)} slots ({horizon.start} .. {horizon.end}), "
        f"{len(mixed_scenario())} loads"
    )
    print()
    header = (
        f"  {'engine':<8}{'mode':<10}{'lambda':>7}{'fcst CO2':>10}{'realized':>10}"
        f"{'err':>8}{'viol':>6}{'miss':>6}{'solve_ms':>10}"
    )
    print(header)
    rows = {}
    for engine in (SchedulerName.CPSAT, SchedulerName.GREEDY):
        for mode, weight in (
            (ForecastMode.EXPECTED, 0.0),
            (ForecastMode.ROBUST, 0.25),
            (ForecastMode.ROBUST, 0.5),
            (ForecastMode.ROBUST, 1.0),
        ):
            scheduler_input, _ = service.build_input(
                jobs,
                forecast_signal,
                capacity_kw=10.0,
                forecast_config=ForecastConfig(
                    forecast_mode=mode, risk_weight=weight
                ),
                uncertainty_upper=[int(u) for u in upper],
            )
            result = service.run(
                scheduler_input, engine, config=SchedulerConfig(time_limit_seconds=5.0)
            )
            evaluation = evaluator.evaluate(
                result,
                actual_signal,
                None,
                ForecastConfig(forecast_mode=mode, risk_weight=weight),
            )
            key = (engine.value, mode.value, weight)
            rows[key] = (result, evaluation)
            realized = evaluation.realized_co2_kg
            print(
                f"  {engine.value:<8}{mode.value:<10}{weight:>7}"
                f"{result.metrics.total_co2_kg:>10.4f}"
                f"{(realized if realized is not None else float('nan')):>10.4f}"
                f"{(evaluation.forecast_error_kg if evaluation.forecast_error_kg else 0.0):>8.4f}"
                f"{result.metrics.feasibility_violations:>6}"
                f"{result.metrics.deadline_misses:>6}"
                f"{result.solver.solve_time_ms:>10}"
            )

    print()
    print("  EXPECTED vs ROBUST on REALIZED emissions (CP-SAT):")
    base = rows[("CPSAT", "EXPECTED", 0.0)][1].realized_co2_kg
    for weight in (0.25, 0.5, 1.0):
        robust = rows[("CPSAT", "ROBUST", weight)][1].realized_co2_kg
        delta = robust - base
        pct = 100.0 * delta / base if base else 0.0
        print(
            f"    lambda={weight:<5} realized {robust:.4f} kg  "
            f"({delta:+.4f} kg, {pct:+.2f}% vs EXPECTED)"
        )


# --- 5. robustness experiments ------------------------------------------------


def measure_experiments():
    rule("5. DETERMINISTIC ROBUSTNESS EXPERIMENTS (§36, §37)")
    from app.domain.horizon import SchedulingHorizon
    from app.domain.loads import LoadSpec, LoadType
    from app.services.schedule_realization import RobustnessExperimentRunner

    # Two atomic jobs with a WIDE window, deliberately. `bottleneck_jobs` cannot
    # show this: its window runs 18:00-07:00 with every job released at 18:00, so
    # both modes are forced into the same few slots and every scenario reports a
    # tie that says nothing about robustness. The point of §36 is to give the two
    # modes room to choose differently.
    start = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)

    def atomic(job_id):
        return LoadSpec(
            id=job_id,
            normalized_name="Oven",
            category="Cooking",
            job_type=LoadType.DEFERRABLE_ATOMIC,
            power_kw=2.0,
            max_power_kw=2.0,
            duration_minutes=30,
            release_at=start,
            deadline_at=start + timedelta(hours=4),
        )

    jobs = [atomic("a"), atomic("b")]
    moments = [m for job in jobs for m in (job.release_at, job.deadline_at) if m]
    horizon = SchedulingHorizon.spanning(moments, slot_minutes=15, pad_slots=1)

    service = SchedulerService()
    evaluator = RealizedScheduleEvaluator()

    for scenario in RobustnessExperimentRunner.scenarios(ORIGIN):
        half = horizon.slot_count // 2
        forecast_values = [
            (scenario.forecast_points[0][1] if i < half else scenario.forecast_points[-1][1])
            for i in range(horizon.slot_count)
        ]
        actual_values = [
            (scenario.actual_points[0][1] if i < half else scenario.actual_points[-1][1])
            for i in range(horizon.slot_count)
        ]
        upper = [
            v + scenario.width_for(i, len(forecast_values))
            for i, v in enumerate(forecast_values)
        ]

        forecast_signal = build_signal(forecast_values, start=horizon.start)
        actual_signal = build_signal(actual_values, start=horizon.start)

        experiment = RobustnessExperiment(
            name=scenario.name,
            description=scenario.description,
            forecast_points=[
                (p.time, p.gco2_per_kwh) for p in forecast_signal.points
            ],
            actual_points=[(p.time, p.gco2_per_kwh) for p in actual_signal.points],
            interval_half_width=scenario.interval_half_width,
            interval_half_widths=scenario.interval_half_widths,
        )
        picked = {}
        for mode, weight in (
            ("EXPECTED", 0.0),
            # lambda = 1.0 is the pure upper-bound mode of §16: the scheduler
            # optimizes `forecast + 1.0 * (upper - forecast)`, i.e. the upper
            # prediction bound. A smaller weight does not flip these fixtures,
            # and reporting a tie there would say more about the weight than
            # about robustness.
            ("ROBUST", 1.0),
        ):
            scheduler_input, _ = service.build_input(
                jobs,
                forecast_signal,
                capacity_kw=5.0,
                forecast_config=ForecastConfig(
                    forecast_mode=ForecastMode(mode), risk_weight=weight
                ),
                uncertainty_upper=[int(u) for u in upper],
            )
            result = service.run(scheduler_input, SchedulerName.CPSAT)
            picked[mode] = result
            experiment.results[mode] = evaluator.evaluate(
                result,
                actual_signal,
                None,
                ForecastConfig(forecast_mode=ForecastMode(mode), risk_weight=weight),
            )

        expected = experiment.results["EXPECTED"].realized_co2_kg
        robust = experiment.results["ROBUST"].realized_co2_kg
        print()
        print(
            f"  {scenario.name}  (interval half-widths "
            f"{scenario.interval_half_widths} g/kWh)"
        )
        for mode in ("EXPECTED", "ROBUST"):
            starts = sorted(j.start_slot for j in picked[mode].schedule)
            print(
                f"    {mode:<9} starts {str(starts):<12} "
                f"realized {experiment.results[mode].realized_co2_kg:.4f} kg"
            )
        print(f"    delta ROBUST - EXPECTED: {robust - expected:+.4f} kg")
        print(f"    {experiment.as_dict()['verdict']}")


# --- 6. deadline buffer -------------------------------------------------------


def measure_deadline_buffer():
    rule("6. DEADLINE BUFFER EFFECT")
    from app.domain.horizon import SchedulingHorizon
    from app.domain.loads import LoadSpec, LoadType

    # One atomic job over 8 h with the cheapest window at the very END. Only such
    # a fixture can show the buffer doing anything: if the cheapest window already
    # sits inside the slack, every buffer yields the same schedule.
    start = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)
    jobs = [
        LoadSpec(
            id="late-job",
            normalized_name="Oven",
            category="Cooking",
            job_type=LoadType.DEFERRABLE_ATOMIC,
            power_kw=2.0,
            max_power_kw=2.0,
            duration_minutes=30,
            release_at=start,
            deadline_at=start + timedelta(hours=8),
        )
    ]
    moments = [m for job in jobs for m in (job.release_at, job.deadline_at) if m]
    horizon = SchedulingHorizon.spanning(moments, slot_minutes=15, pad_slots=1)
    # dirtiest for most of the day, cleanest in the final hour
    values = [500.0] * (horizon.slot_count - 4) + [10.0] * 4
    signal = build_signal(values, start=horizon.start)

    service = SchedulerService()
    print(f"  {'buffer':>8}{'CO2 kg':>12}{'misses':>8}{'viol':>6}   starts")
    for buffer in (0, 15, 30, 60, 120):
        scheduler_input, _ = service.build_input(
            jobs,
            signal,
            capacity_kw=5.0,
            forecast_config=ForecastConfig(deadline_buffer_minutes=buffer),
        )
        result = service.run(scheduler_input, SchedulerName.GREEDY)
        starts = ",".join(f"{j.job_id}:{j.start_slot}" for j in result.schedule)
        print(
            f"  {buffer:>6}m{result.metrics.total_co2_kg:>12.4f}"
            f"{result.metrics.deadline_misses:>8}{result.metrics.feasibility_violations:>6}   {starts}"
        )


# --- 7. solver scaling -------------------------------------------------------


def measure_solver_scaling():
    rule("7. CP-SAT SOLVE TIME BY PROBLEM SIZE (5 / 20 / 50 / 100 jobs)")
    from app.domain.loads import LoadSpec, LoadType
    from app.domain.horizon import SchedulingHorizon

    service = SchedulerService()
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    print(f"  {'jobs':>6}{'slots':>7}{'status':>12}{'solve_ms':>11}{'CO2 kg':>12}{'viol':>6}")
    for count in (5, 20, 50, 100):
        jobs = []
        for i in range(count):
            release = start + timedelta(minutes=15 * (i * 4))
            jobs.append(
                LoadSpec(
                    id=f"job-{i}",
                    normalized_name=f"Load {i}",
                    category="Test",
                    job_type=(
                        LoadType.DEFERRABLE_INTERRUPTIBLE if i % 2 else LoadType.DEFERRABLE_ATOMIC
                    ),
                    power_kw=1.5,
                    max_power_kw=1.5,
                    duration_minutes=60,
                    energy_required_kwh=1.5,
                    release_at=release,
                    deadline_at=release + timedelta(hours=12),
                )
            )
        moments = [m for job in jobs for m in (job.release_at, job.deadline_at) if m]
        horizon = SchedulingHorizon.spanning(moments, slot_minutes=15, pad_slots=1)
        signal = build_signal(
            [
                500.0
                if ((start + timedelta(minutes=15 * step_i)).hour % 24 < 6)
                else 150.0
                for step_i in range(horizon.slot_count)
            ],
            start=start,
        )
        try:
            scheduler_input, _ = service.build_input(jobs, signal, capacity_kw=10.0)
            result = service.run(
                scheduler_input, SchedulerName.CPSAT,
                config=SchedulerConfig(time_limit_seconds=5.0),
            )
            print(
                f"  {count:>6}{horizon.slot_count:>7}{result.status.value:>12}"
                f"{result.solver.solve_time_ms:>11}"
                f"{result.metrics.total_co2_kg:>12.4f}"
                f"{result.metrics.feasibility_violations:>6}"
            )
        except Exception as exc:  # pragma: no cover - measurement script
            print(f"  {count:>6}  error: {exc}")


def main():
    measure_forecasting()
    measure_intervals()
    measure_backtests()
    measure_scheduling()
    measure_experiments()
    measure_deadline_buffer()
    measure_solver_scaling()
    print()


if __name__ == "__main__":
    main()