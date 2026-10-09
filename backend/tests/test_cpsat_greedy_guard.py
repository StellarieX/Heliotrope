"""CP-SAT warm start from Greedy, and the anytime guard that falls back to it."""

import pytest

from app.domain.scheduling import (
    ScheduleStatus,
    SchedulerConfig,
    SchedulerResult,
    SolverInfo,
    SolverStatus,
)
from app.services.scheduler_normalizer import fingerprint_input
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SCHEDULERS, SchedulerName
from app.services.schedulers.cpsat import CPSATScheduler

from .fixtures import ev_job, make_signal, washing_machine_job


@pytest.fixture()
def service() -> SchedulerService:
    return SchedulerService()


def _input(service, specs=None):
    inp, _ = service.build_input(
        specs or [ev_job(), washing_machine_job()], make_signal(hours=56), capacity_kw=20.0
    )
    return inp


def test_greedy_warm_start_keeps_the_optimum(service):
    inp = _input(service)
    cold = SCHEDULERS[SchedulerName.CPSAT].schedule(inp)  # no hints at all
    warm = service.run(inp, SchedulerName.CPSAT, explain=False)  # hinted from Greedy
    assert cold.status is ScheduleStatus.OPTIMAL and warm.status is ScheduleStatus.OPTIMAL
    assert warm.solver.name == "CPSAT"
    assert warm.metrics.total_co2_kg == pytest.approx(cold.metrics.total_co2_kg)
    assert warm.solver.objective_value == pytest.approx(cold.solver.objective_value)


def test_warm_start_does_not_touch_the_callers_input_or_fingerprint(service):
    inp = _input(service)
    before = fingerprint_input(inp)
    service.run(inp, SchedulerName.CPSAT, explain=False)
    assert inp.hints is None
    assert fingerprint_input(inp) == before
    assert "hints" not in inp.model_dump()


def _patch_cpsat(monkeypatch, make_result):
    monkeypatch.setattr(CPSATScheduler, "schedule", lambda self, scheduler_input: make_result(scheduler_input))


def test_guard_returns_greedy_when_a_feasible_cpsat_is_worse(service, monkeypatch):
    inp = _input(service)

    def worse(scheduler_input):
        # ASAP ignores carbon, so it is a valid but worse schedule than Greedy here.
        asap = SCHEDULERS[SchedulerName.ASAP].schedule(scheduler_input)
        asap.scheduler = "CPSAT"
        asap.solver = SolverInfo(
            name="CPSAT", status=SolverStatus.FEASIBLE, best_bound=1.0, time_limit_seconds=0.1
        )
        return asap

    _patch_cpsat(monkeypatch, worse)
    greedy = service.run(inp, SchedulerName.GREEDY, explain=False)
    result = service.run(inp, SchedulerName.CPSAT, explain=False)
    assert result.status is ScheduleStatus.FEASIBLE
    assert not result.solver.is_optimal and result.solver.status is SolverStatus.FEASIBLE
    assert result.solver.name == "GREEDY"
    assert "Greedy" in result.reason and "not proven optimal" in result.reason
    assert result.metrics.total_co2_kg == pytest.approx(greedy.metrics.total_co2_kg)


def test_guard_keeps_a_feasible_cpsat_that_is_better(service, monkeypatch):
    inp = _input(service)
    real = SCHEDULERS[SchedulerName.CPSAT].schedule(inp)

    def better_but_unproven(scheduler_input):
        r = real.model_copy(deep=True)
        r.status = ScheduleStatus.FEASIBLE
        r.solver.status = SolverStatus.FEASIBLE
        r.solver.is_optimal = False
        return r

    _patch_cpsat(monkeypatch, better_but_unproven)
    result = service.run(inp, SchedulerName.CPSAT, explain=False)
    assert result.solver.name == "CPSAT" and result.status is ScheduleStatus.FEASIBLE


def test_guard_returns_greedy_when_cpsat_finds_nothing(service, monkeypatch):
    inp = _input(service)

    def nothing(scheduler_input):
        return SchedulerResult(
            status=ScheduleStatus.UNKNOWN,
            scheduler="CPSAT",
            solver=SolverInfo(name="CPSAT", status=SolverStatus.UNKNOWN),
            reason="timed out",
        )

    _patch_cpsat(monkeypatch, nothing)
    result = service.run(inp, SchedulerName.CPSAT, explain=False)
    assert result.status is ScheduleStatus.FEASIBLE and result.schedule
    assert result.solver.name == "GREEDY" and not result.solver.is_optimal
    assert "no schedule within its time limit" in result.reason


def test_guard_never_masks_a_proved_infeasible(service):
    impossible = ev_job(energy_required_kwh=900.0)
    inp = _input(service, [impossible])
    result = service.run(inp, SchedulerName.CPSAT, explain=False)
    assert result.status is ScheduleStatus.INFEASIBLE


def test_compare_reports_each_engine_separately_and_shows_the_guard(service, monkeypatch):
    inp = _input(service)

    def nothing(scheduler_input):
        return SchedulerResult(
            status=ScheduleStatus.UNKNOWN,
            scheduler="CPSAT",
            solver=SolverInfo(name="CPSAT", status=SolverStatus.UNKNOWN),
        )

    _patch_cpsat(monkeypatch, nothing)
    comparison = service.compare(inp)
    assert set(comparison.results) == {"ASAP", "GREEDY", "CPSAT"}
    assert comparison.results["GREEDY"].solver.name == "GREEDY"
    assert comparison.results["CPSAT"].solver.name == "GREEDY"  # visibly the fallback
    assert comparison.results["CPSAT"].reason
    assert comparison.results["ASAP"].solver.name == "ASAP"


def test_a_real_run_is_still_proved_optimal(service):
    result = service.run(_input(service), SchedulerName.CPSAT, config=SchedulerConfig(time_limit_seconds=30.0))
    assert result.status is ScheduleStatus.OPTIMAL and result.solver.is_optimal


@pytest.mark.parametrize("weights", [{}, {"peak": 0.5}, {"delay": 0.3}, {"peak": 0.2, "delay": 0.2}])
def test_placement_objective_matches_the_solver_objective(service, weights):
    from app.domain.scheduling import ObjectiveWeights
    from app.services.scheduler_service import _powers_of
    from app.services.schedulers.cpsat import placement_objective

    inp, _ = service.build_input(
        [ev_job(), washing_machine_job()], make_signal(hours=56), capacity_kw=20.0,
        objective=ObjectiveWeights(**weights),
    )
    result = SCHEDULERS[SchedulerName.CPSAT].schedule(inp)
    assert result.status is ScheduleStatus.OPTIMAL
    assert placement_objective(inp, _powers_of(result)) == pytest.approx(result.solver.objective_value, abs=1)
