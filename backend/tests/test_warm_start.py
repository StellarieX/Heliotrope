"""Warm-starting, honest optimality reporting, and the interactive gap tolerance.

What these pin down:
  * Hints are ADVISORY: no hint, however wrong, can change the optimum or make an
    infeasible problem feasible. (A solver hint is not a constraint.)
  * Hints are keyed by timestamp, so they survive a shifted horizon; placements that
    fell out of the horizon, and unknown job ids, are ignored.
  * Replans hand the previous schedule to the solver as hints.
  * A result is OPTIMAL only when optimality was PROVED. A solve that stopped inside
    `relative_gap_limit` is FEASIBLE and reports the real gap.
"""

from datetime import timedelta

import pytest

from app.core import config as app_config
from app.domain.scheduling import SchedulerConfig
from app.services.receding import hints_from_previous
from app.services.scheduler_service import SchedulerService, with_default_gap
from app.services.schedulers import SchedulerName

from .fixtures import DAY_START, ev_job, geyser_job, make_signal, washing_machine_job


@pytest.fixture()
def service() -> SchedulerService:
    return SchedulerService()


@pytest.fixture()
def signal():
    return make_signal(hours=56)


def _solve(service, signal, specs, hints=None, config=None):
    inp, _ = service.build_input(specs, signal, capacity_kw=20.0, hints=hints)
    return inp, service.run(inp, SchedulerName.CPSAT, config=config, explain=False)


def _schedule(result):
    return {s.job_id: sorted((a.timestamp, a.power_w) for a in s.allocations) for s in result.schedule}


SMALL = lambda: [ev_job(), washing_machine_job()]  # noqa: E731  (proves optimal instantly)


# ---- hints are advisory ----------------------------------------------------------

def test_hints_from_an_optimal_schedule_reproduce_it(service, signal):
    _, cold = _solve(service, signal, SMALL())
    hints = service.extract_hints_from_result(cold)
    _, warm = _solve(service, signal, SMALL(), hints=hints)
    assert cold.status.value == warm.status.value == "OPTIMAL"
    assert warm.metrics.total_co2_kg == pytest.approx(cold.metrics.total_co2_kg)


def test_a_deliberately_terrible_hint_cannot_change_the_optimum(service, signal):
    _, cold = _solve(service, signal, SMALL())
    # Hint every job at the very end of the horizon: wrong, and likely infeasible.
    last = DAY_START + timedelta(hours=40)
    junk = {j.id: [(last + timedelta(minutes=15 * k), 7000) for k in range(6)] for j in SMALL()}
    _, warm = _solve(service, signal, SMALL(), hints=junk)
    assert warm.status.value == "OPTIMAL"
    assert warm.metrics.total_co2_kg == pytest.approx(cold.metrics.total_co2_kg)


def test_a_hint_cannot_make_an_infeasible_problem_feasible(service, signal):
    impossible = ev_job(energy_required_kwh=900.0)  # cannot fit in the window
    hints = {impossible.id: [(DAY_START + timedelta(hours=1), 7200)]}
    _, without = _solve(service, signal, [impossible])
    _, with_hint = _solve(service, signal, [impossible], hints=hints)
    assert without.status.value == "INFEASIBLE"
    assert with_hint.status.value == "INFEASIBLE"


def test_unknown_job_ids_and_out_of_horizon_hints_are_ignored(service, signal):
    _, cold = _solve(service, signal, SMALL())
    hints = service.extract_hints_from_result(cold)
    hints["ghost"] = [(DAY_START, 5000)]
    hints["ev-1"] = hints["ev-1"] + [(DAY_START - timedelta(days=3), 7200), (DAY_START + timedelta(days=30), 7200)]
    _, warm = _solve(service, signal, SMALL(), hints=hints)
    assert warm.status.value == "OPTIMAL"
    assert warm.metrics.total_co2_kg == pytest.approx(cold.metrics.total_co2_kg)


def test_hints_survive_a_shifted_horizon(service, signal):
    _, cold = _solve(service, signal, SMALL())
    hints = service.extract_hints_from_result(cold)
    later = [
        s.model_copy(update={"release_at": s.release_at + timedelta(hours=2)}) if s.release_at else s
        for s in SMALL()
    ]
    _, cold2 = _solve(service, signal, later)
    _, warm2 = _solve(service, signal, later, hints=hints)
    assert warm2.status.value == cold2.status.value == "OPTIMAL"
    assert warm2.metrics.total_co2_kg == pytest.approx(cold2.metrics.total_co2_kg)


def test_extracted_hints_are_keyed_by_timestamp(service, signal):
    _, cold = _solve(service, signal, SMALL())
    hints = service.extract_hints_from_result(cold)
    assert set(hints) == {s.job_id for s in cold.schedule}
    for job_id, placed in hints.items():
        assert placed and all(hasattr(ts, "year") for ts, _ in placed)


def test_hints_are_not_serialised_with_the_problem(service, signal):
    inp, _ = _solve(service, signal, SMALL(), hints={"ev-1": [(DAY_START, 7200)]})
    assert inp.hints
    assert "hints" not in inp.model_dump()
    assert "hints" not in inp.model_dump_json()


# ---- replans hand the previous schedule to the solver ---------------------------------

def test_hints_from_previous_maps_slots_to_timestamps_and_drops_the_past(service, signal):
    inp, _ = service.build_input(SMALL(), signal, capacity_kw=20.0)
    previous = {"ev-1": {2: 7200, 3: 7200, 4: 0, 9: 7200}, "wm-1": {}}
    hints = hints_from_previous(inp, previous, ["ev-1", "wm-1", "nope"], from_slot=3)
    assert set(hints) == {"ev-1"}  # nothing to hint for the others
    assert [p for _, p in hints["ev-1"]] == [7200, 7200]  # slot 2 is past, slot 4 has no power
    assert hints["ev-1"][0][0] == inp.horizon.slot_start(3)
    assert hints["ev-1"][1][0] == inp.horizon.slot_start(9)


# ---- honest optimality ---------------------------------------------------------------

def test_a_proven_optimum_is_optimal_with_zero_gap(service, signal):
    _, r = _solve(service, signal, SMALL())
    assert r.status.value == "OPTIMAL" and r.solver.is_optimal
    assert r.solver.relative_gap == pytest.approx(0.0, abs=1e-9)


def test_a_solve_that_stops_inside_the_tolerance_is_not_called_optimal(service, signal):
    specs = [ev_job(), washing_machine_job(), geyser_job()]
    _, r = _solve(service, signal, specs, config=SchedulerConfig(time_limit_seconds=20, relative_gap_limit=0.001))
    assert r.status.value == "FEASIBLE" and not r.solver.is_optimal
    assert 0 < r.solver.relative_gap <= 0.001 + 1e-6
    assert r.solver.optimality_gap > 0
    assert r.solver.solve_time_ms < 5000  # the tolerance is what makes this fast


def test_the_tolerance_costs_almost_nothing_in_carbon(service, signal):
    specs = [ev_job(), washing_machine_job(), geyser_job()]
    _, loose = _solve(service, signal, specs, config=SchedulerConfig(time_limit_seconds=20, relative_gap_limit=0.0001))
    # Within the tolerance of the proven bound, by construction.
    assert loose.solver.relative_gap <= 0.0001 + 1e-6


# ---- the deployment default ------------------------------------------------------------

def test_default_gap_applies_only_when_configured_and_never_overrides_a_request(monkeypatch):
    monkeypatch.setattr(app_config, "SOLVER_RELATIVE_GAP", 0.0)
    assert with_default_gap(None) is None
    explicit = SchedulerConfig(time_limit_seconds=3)
    assert with_default_gap(explicit) is explicit

    monkeypatch.setattr(app_config, "SOLVER_RELATIVE_GAP", 0.0002)
    assert with_default_gap(None).relative_gap_limit == 0.0002
    assert with_default_gap(SchedulerConfig(time_limit_seconds=3)).relative_gap_limit == 0.0002
    chosen = SchedulerConfig(relative_gap_limit=0.0)  # the caller explicitly asked for strict
    assert with_default_gap(chosen).relative_gap_limit == 0.0
