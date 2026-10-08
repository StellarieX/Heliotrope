"""CP-SAT: the real optimization engine (§8-§22).

The tests here are deliberately stronger than "the solver returned something":

  * OPTIMALITY IS PROVEN BY ENUMERATION (§40). For tiny problems we enumerate
    every feasible schedule, compute each objective by hand, and require
    CP-SAT's answer to equal the true minimum. Returning *a* feasible schedule
    proves nothing; returning the *best* one is the actual claim being made.
  * MINIMUM CHUNK IS TESTED AS A CONSTRAINT (§12), not as a field the solver is
    free to ignore.
  * THERMAL JOBS ARE CHECKED AGAINST INDEPENDENT ARITHMETIC (§13), because the
    model and the validator must not merely agree with each other.
  * NOTHING IS CLAIMED WITHOUT PROOF (§46). A `FEASIBLE` result must never be
    reported as `OPTIMAL`, and an unmet target must never be silently traded
    away for carbon.
"""

from datetime import timedelta

import pytest

from app.domain.carbon import CarbonPoint, CarbonSignal
from app.domain.loads import LoadSpec, LoadType
from app.domain.scaling import CO2_G_PER_KG, WMIN_PER_KWH
from app.domain.scheduling import ObjectiveWeights, SchedulerConfig
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SCHEDULERS, SchedulerName

from .fixtures import (
    DAY_START,
    at,
    bottleneck_jobs,
    ev_job,
    geyser_job,
    make_signal,
    mixed_scenario,
)


@pytest.fixture()
def service() -> SchedulerService:
    return SchedulerService()


def constant_signal(
    intensities: list[float], resolution_minutes: int = 15, pad_slots: int = 1
) -> CarbonSignal:
    """A signal whose intensities are chosen slot by slot, for tiny problems.

    `pad_slots` extends the signal past the last named intensity by repeating it.
    The normalizer derives its horizon from the job windows and deliberately pads
    it by one slot (`SchedulingHorizon.spanning(..., pad_slots=1)`), and it
    REFUSES to invent a carbon value for a slot the signal does not cover. So a
    signal that stops exactly at the last usable slot is genuinely too short; the
    pad exists so these tests stay about the solver instead of about grid
    coverage. Padded slots are never reachable by any job (the deadline excludes
    them), which the brute-force optima below rely on.
    """
    values = list(intensities) + [intensities[-1]] * pad_slots
    points = [
        CarbonPoint(
            time=DAY_START + timedelta(minutes=resolution_minutes * i),
            gco2_per_kwh=value,
            source="test_constant",
        )
        for i, value in enumerate(values)
    ]
    return CarbonSignal(
        start=DAY_START,
        end=DAY_START + timedelta(minutes=resolution_minutes * len(values)),
        resolution_minutes=resolution_minutes,
        points=points,
    )


# --- §40 optimality against exhaustive enumeration ---------------------------


def test_cpsat_matches_brute_force_on_one_atomic_job(service):
    """One 2-slot atomic job, three candidate start slots, known carbon.

    The optimum is arithmetic we can do by hand, so any deviation is a real
    modelling error rather than a solver quirk.
    """
    # A 2-slot job in a 4-slot window can only start on slots 0, 1 or 2, and the
    # three windows are given DIFFERENT costs (600, 150, 550 g) so the optimum is
    # unique. A tied fixture would pass just as happily if the objective were
    # ignored and the solver returned any legal window.
    signal = constant_signal([500.0, 100.0, 50.0, 500.0])
    job = LoadSpec(
        id="atomic-1",
        normalized_name="Oven",
        category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC,
        power_kw=2.0,
        max_power_kw=2.0,
        duration_minutes=30,
        release_at=DAY_START,
        deadline_at=DAY_START + timedelta(hours=1),
    )
    scheduler_input, _ = service.build_input([job], signal, capacity_kw=5.0)
    result = service.run(
        scheduler_input,
        SchedulerName.CPSAT,
        config=SchedulerConfig(time_limit_seconds=30.0),
    )
    assert result.status.value == "OPTIMAL"

    # Brute force: every feasible contiguous window, costed by hand.
    # grams = sum over the 2 occupied slots of intensity[g/kWh] * 2 kW * 0.25 h
    costs = {
        0: (500 + 100) * 2.0 * 0.25,
        1: (100 + 50) * 2.0 * 0.25,
        2: (50 + 500) * 2.0 * 0.25,
    }
    assert len(set(costs.values())) == 3, "the fixture must have a unique optimum"
    best_start = min(costs, key=lambda s: (costs[s], s))
    assert best_start == 1
    assert costs[best_start] == 75.0

    placed = result.schedule[0]
    assert placed.start_slot == best_start
    # The schedule's own accounting must match the hand computation.
    assert result.metrics.total_co2_kg == pytest.approx(
        costs[best_start] / CO2_G_PER_KG, rel=1e-6
    )


def test_cpsat_optimality_on_two_atomic_jobs_that_compete(service):
    """Two atomic jobs, capacity too small to overlap: the optimum requires
    actually resolving the conflict, not just picking the cheapest window."""
    signal = constant_signal([500.0, 500.0, 100.0, 100.0, 100.0, 100.0])
    job_a = LoadSpec(
        id="a", normalized_name="Oven", category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, max_power_kw=2.0,
        duration_minutes=30, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(minutes=90),
    )
    job_b = LoadSpec(
        id="b", normalized_name="Kettle", category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, max_power_kw=2.0,
        duration_minutes=30, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(minutes=90),
    )
    scheduler_input, _ = service.build_input([job_a, job_b], signal, capacity_kw=2.0)
    result = service.run(
        scheduler_input, SchedulerName.CPSAT,
        config=SchedulerConfig(time_limit_seconds=30.0),
    )
    assert result.status.value == "OPTIMAL"

    # Enumerate every pair of non-overlapping 2-slot windows within 6 slots.
    carbon = [500.0, 500.0, 100.0, 100.0, 100.0, 100.0]
    best = None
    for sa in range(5):
        for sb in range(5):
            a_slots = {sa, sa + 1}
            b_slots = {sb, sb + 1}
            if a_slots & b_slots:
                continue  # capacity is 2 kW and each job is 2 kW
            cost = sum(carbon[t] for t in a_slots) + sum(carbon[t] for t in b_slots)
            if best is None or cost < best:
                best = cost
    assert best == 400.0, "brute force should find four 100 g slots"
    # `best` sums g/kWh over 4 slots; each slot carries 2 kW * 0.25 h = 0.5 kWh,
    # so grams are best * 0.5 and kilograms are grams / CO2_G_PER_KG.
    assert result.metrics.total_co2_kg == pytest.approx(
        best * 2.0 * 0.25 / CO2_G_PER_KG, rel=1e-6
    )


def test_cpsat_finds_the_lowest_carbon_window_not_merely_a_feasible_one(service):
    """A solver that merely satisfies constraints would happily run the first
    legal window. The objective must actually move the job."""
    signal = constant_signal([500.0, 500.0, 500.0, 100.0, 100.0, 500.0])
    job = LoadSpec(
        id="atomic-1", normalized_name="Oven", category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, max_power_kw=2.0,
        duration_minutes=30, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(minutes=90),
    )
    scheduler_input, _ = service.build_input([job], signal, capacity_kw=5.0)
    result = service.run(
        scheduler_input, SchedulerName.CPSAT,
        config=SchedulerConfig(time_limit_seconds=30.0),
    )
    assert result.status.value == "OPTIMAL"
    assert result.schedule[0].start_slot == 3, "should take the 100 g slots at 3-4"


def test_cpsat_solves_with_a_peak_weight(service):
    """The peak term builds a constraint per slot; it must not crash on a live expression."""
    signal = constant_signal([500.0, 500.0, 500.0, 100.0, 100.0, 500.0])
    job = LoadSpec(
        id="atomic-1", normalized_name="Oven", category="Cooking",
        job_type=LoadType.DEFERRABLE_ATOMIC, power_kw=2.0, max_power_kw=2.0,
        duration_minutes=30, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(minutes=90),
    )
    scheduler_input, _ = service.build_input(
        [job], signal, capacity_kw=5.0, objective=ObjectiveWeights(carbon=1.0, peak=1.0)
    )
    result = service.run(
        scheduler_input, SchedulerName.CPSAT, config=SchedulerConfig(time_limit_seconds=30.0)
    )
    assert result.status.value in ("OPTIMAL", "FEASIBLE")
    assert result.schedule and result.schedule[0].start_slot == 3


# --- §10 atomic contiguity ---------------------------------------------------


def test_atomic_job_occupies_exactly_one_contiguous_window(service):
    scheduler_input, _ = service.build_input(
        bottleneck_jobs(), make_signal(), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    for placed in result.schedule:
        if placed.job_type is LoadType.DEFERRABLE_ATOMIC:
            slots = {a.slot for a in placed.allocations}
            assert slots == set(range(placed.start_slot, placed.end_slot))
            assert len(slots) == placed.end_slot - placed.start_slot


def test_atomic_job_never_runs_past_its_deadline(service):
    scheduler_input, _ = service.build_input(
        bottleneck_jobs(), make_signal(), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    by_id = {j.id: j for j in scheduler_input.jobs}
    for placed in result.schedule:
        assert placed.end_slot <= by_id[placed.job_id].deadline_slot


# --- §12 minimum chunk is a real constraint ---------------------------------


def test_min_chunk_forbids_an_isolated_one_slot_burst(service):
    """A 4-slot minimum must prevent the solver from buying one cheap slot.

    Carbon is 10x cheaper in the middle two slots than anywhere else. A solver
    that treats `min_chunk_slots` as decorative would take just that burst,
    under-deliver the energy, and look optimal. It must instead run a full
    4-slot chunk.
    """
    intensities = [500.0, 500.0, 10.0, 10.0, 500.0, 500.0, 500.0, 500.0]
    signal = constant_signal(intensities)
    job = LoadSpec(
        id="ev-1", normalized_name="EV charger", category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=2.0,
        max_power_kw=2.0, energy_required_kwh=1.0,
        min_chunk_minutes=60, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(hours=2),
    )
    scheduler_input, _ = service.build_input([job], signal, capacity_kw=4.0)
    normalized = {j.id: j for j in scheduler_input.jobs}["ev-1"]
    assert normalized.min_chunk_slots == 4, "fixture should carry a 4-slot chunk"

    result = service.run(
        scheduler_input, SchedulerName.CPSAT,
        config=SchedulerConfig(time_limit_seconds=30.0),
    )
    assert result.status.value in ("FEASIBLE", "OPTIMAL")

    # Every contiguous run of active slots must be at least 4 long.
    active = sorted({a.slot for a in result.schedule[0].allocations})
    runs = []
    for slot in active:
        if runs and slot == runs[-1][-1] + 1:
            runs[-1].append(slot)
        else:
            runs.append([slot])
    assert runs, "the job must actually be scheduled"
    for run in runs:
        assert len(run) >= 4, f"a {len(run)}-slot run violates min_chunk_slots=4"


def test_min_chunk_energy_target_is_still_met(service):
    """Batching must not cost the job its required energy."""
    intensities = [500.0, 500.0, 10.0, 10.0, 500.0, 500.0, 500.0, 500.0]
    job = LoadSpec(
        id="ev-1", normalized_name="EV charger", category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=2.0,
        max_power_kw=2.0, energy_required_kwh=1.0,
        min_chunk_minutes=60, release_at=DAY_START,
        deadline_at=DAY_START + timedelta(hours=2),
    )
    scheduler_input, _ = service.build_input(
        [job], constant_signal(intensities), capacity_kw=4.0
    )
    result = service.run(
        scheduler_input, SchedulerName.CPSAT,
        config=SchedulerConfig(time_limit_seconds=30.0),
    )
    delivered = sum(a.power_w for a in result.schedule[0].allocations) * 15 / WMIN_PER_KWH
    assert delivered >= 1.0 - 1e-6


def test_min_chunk_of_one_imposes_no_batching(service):
    """min_chunk 1 means any pattern is legal, so one-slot bursts must appear."""
    intensities = [500.0, 10.0, 500.0, 10.0, 500.0]
    job = LoadSpec(
        id="ev-1", normalized_name="EV charger", category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE, power_kw=2.0,
        max_power_kw=2.0, energy_required_kwh=0.1,
        release_at=DAY_START, deadline_at=DAY_START + timedelta(minutes=75),
    )
    scheduler_input, _ = service.build_input(
        [job], constant_signal(intensities), capacity_kw=4.0
    )
    result = service.run(
        scheduler_input, SchedulerName.CPSAT,
        config=SchedulerConfig(time_limit_seconds=30.0),
    )
    active = sorted({a.slot for a in result.schedule[0].allocations})
    runs = []
    for slot in active:
        if runs and slot == runs[-1][-1] + 1:
            runs[-1].append(slot)
        else:
            runs.append([slot])
    assert any(len(run) == 1 for run in runs), "min_chunk=1 must permit a 1-slot burst"


# --- §11 interruptible energy -----------------------------------------------


def test_interruptible_job_meets_its_energy_target(service):
    """§11. This must be a DEFERRABLE_INTERRUPTIBLE job: an atomic job has no
    energy target at all (`energy_required_wmin is None`), so the assertion would
    be vacuous on one."""
    scheduler_input, _ = service.build_input(
        [ev_job()], make_signal(), capacity_kw=10.0
    )
    job = {j.id: j for j in scheduler_input.jobs}["ev-1"]
    assert job.job_type is LoadType.DEFERRABLE_INTERRUPTIBLE
    assert job.energy_required_wmin is not None
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    delivered = sum(a.power_w for a in result.schedule[0].allocations) * 15 / WMIN_PER_KWH
    assert delivered >= job.energy_required_wmin / WMIN_PER_KWH - 1e-6


def test_interruptible_job_never_exceeds_max_power(service):
    scheduler_input, _ = service.build_input(
        [ev_job()], make_signal(), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    job = {j.id: j for j in scheduler_input.jobs}["ev-1"]
    for allocation in result.schedule[0].allocations:
        assert 0 <= allocation.power_w <= job.max_power_w


# --- §13 thermal -------------------------------------------------------------


def test_thermal_job_reaches_its_service_target(service):
    """§13: comfort is a hard constraint. The optimizer may not trade comfort
    for carbon, so the target must be met even though it costs emissions."""
    scheduler_input, _ = service.build_input(
        [geyser_job()], make_signal(hours=56), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    assert result.status.value in ("FEASIBLE", "OPTIMAL")

    # Re-simulate independently from the reported allocation profile.
    job = {j.id: j for j in scheduler_input.jobs}["geyser-1"]
    scale = job.thermal
    allocations = {a.slot: a.power_w for a in result.schedule[0].allocations}
    temperature = scale.initial_milli
    for slot in job.slots():
        temperature = scale.next_temperature_milli_from_watts(
            temperature, allocations.get(slot, 0)
        )
    assert scale.min_milli <= temperature <= scale.max_milli
    assert temperature >= scale.target_milli, "service target must be met"


def test_thermal_job_stays_inside_the_comfort_band_every_slot(service):
    scheduler_input, _ = service.build_input(
        [geyser_job()], make_signal(hours=56), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    job = {j.id: j for j in scheduler_input.jobs}["geyser-1"]
    scale = job.thermal
    allocations = {a.slot: a.power_w for a in result.schedule[0].allocations}
    temperature = scale.initial_milli
    for slot in job.slots():
        temperature = scale.next_temperature_milli_from_watts(
            temperature, allocations.get(slot, 0)
        )
        assert scale.min_milli <= temperature <= scale.max_milli, f"band broken at slot {slot}"


def test_thermal_trajectory_is_exposed_for_a_future_gantt_view(service):
    """§50: enough data to draw a temperature curve later."""
    scheduler_input, _ = service.build_input(
        [geyser_job()], make_signal(hours=56), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    placed = result.schedule[0]
    assert placed.temperature, "a thermal job must expose its trajectory"
    assert all(sample.temperature_c is not None for sample in placed.temperature)


def test_thermal_and_flexible_jobs_share_one_connection(service):
    """§14: capacity is a single constraint over every flexible job type."""
    scheduler_input, _ = service.build_input(
        mixed_scenario(), make_signal(hours=56), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    assert result.metrics.peak_kw <= 10.0 + 1e-9
    assert max(result.slot_load_w) <= 10_000


# --- §8/§46 honesty about proof ---------------------------------------------


def test_a_feasible_result_is_never_labelled_optimal(service):
    """If CP-SAT runs out of time it has NOT proved optimality."""
    scheduler_input, _ = service.build_input(
        mixed_scenario(), make_signal(hours=56), capacity_kw=10.0
    )
    result = service.run(
        scheduler_input, SchedulerName.CPSAT,
        config=SchedulerConfig(time_limit_seconds=0.4),
    )
    if result.solver.status.value != "OPTIMAL":
        assert result.status.value == "FEASIBLE"
        assert result.solver.is_optimal is False
    else:
        assert result.solver.is_optimal is True


def test_optimality_gap_is_reported_only_when_proven(service):
    scheduler_input, _ = service.build_input(
        bottleneck_jobs(), make_signal(), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    if result.solver.status.value == "OPTIMAL":
        assert result.solver.optimality_gap is not None
        assert result.solver.objective_value is not None
        assert result.solver.best_bound is not None


def test_heuristic_schedulers_never_claim_optimality(service):
    """§7: Greedy is explicitly not globally optimal and must not pretend."""
    scheduler_input, _ = service.build_input(
        mixed_scenario(), make_signal(hours=56), capacity_kw=10.0
    )
    for name in (SchedulerName.ASAP, SchedulerName.GREEDY):
        result = service.run(scheduler_input, name)
        assert result.status.value != "OPTIMAL"
        assert result.solver.is_optimal is False


# --- §21, §46 solver configuration ------------------------------------------


def test_time_limit_and_seed_are_reported_back(service):
    scheduler_input, _ = service.build_input(
        bottleneck_jobs(), make_signal(), capacity_kw=10.0
    )
    config = SchedulerConfig(
        time_limit_seconds=2.0, num_workers=1, random_seed=7
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT, config=config)
    assert result.solver.time_limit_seconds == 2.0
    assert result.solver.random_seed == 7
    assert result.solver.num_workers == 1


def test_a_fixed_seed_gives_a_repeatable_answer(service):
    """Determinism matters: a schedule that changes on every retry is not a
    schedule a household can plan around."""
    scheduler_input, _ = service.build_input(
        bottleneck_jobs(), make_signal(), capacity_kw=10.0
    )
    config = SchedulerConfig(time_limit_seconds=10.0, random_seed=42, num_workers=1)
    first = service.run(scheduler_input, SchedulerName.CPSAT, config=config)
    second = service.run(scheduler_input, SchedulerName.CPSAT, config=config)
    assert [j.start_slot for j in first.schedule] == [
        j.start_slot for j in second.schedule
    ]
    assert first.metrics.total_co2_kg == second.metrics.total_co2_kg


def test_scheduler_config_rejects_nonsense():
    with pytest.raises(ValueError):
        SchedulerConfig(time_limit_seconds=0)
    with pytest.raises(ValueError):
        SchedulerConfig(num_workers=0)
    with pytest.raises(ValueError):
        SchedulerConfig(relative_gap_limit=-0.1)


# --- §14 baseline must not be double-counted --------------------------------


def test_fixed_loads_are_charged_to_the_baseline_not_the_flexible_schedule(service):
    """§5: a FIXED load belongs in baseline demand. If it were also placed as a
    job, capacity would be charged twice for it."""
    from .fixtures import baseline_loads

    specs = baseline_loads()
    assert all(s.job_type is LoadType.FIXED for s in specs)
    scheduler_input, _ = service.build_input(specs, make_signal(), capacity_kw=10.0)

    result = service.run(scheduler_input, SchedulerName.ASAP)
    assert result.schedule == [], "fixed loads are not placed as jobs"
    # 0.1 kW fan + 0.2 kW fridge = 0.3 kW in every slot they declare. The
    # normalizer's horizon is padded one slot past the fixed loads' own window,
    # and the fixture deliberately says the loads are off outside that window, so
    # only slots INSIDE the declared window are charged. Checking the padded tail
    # would be asserting a fiction the normalizer warns about.
    inside = scheduler_input.horizon.slot_range_for(at(0), at(48))
    assert inside
    assert {result.baseline_slot_load_w[s] for s in inside} == {300}
    assert {result.slot_load_w[s] for s in inside} == {300}
    # Nothing outside the window is drawn, and nothing is ever placed as a job.
    for slot in range(scheduler_input.horizon.slot_count):
        if slot not in inside:
            assert result.baseline_slot_load_w[slot] == 0
            assert result.slot_load_w[slot] == 0


def test_capacity_applies_to_baseline_plus_flexible(service):
    """§5: 4 kW baseline on a 10 kW connection leaves 6 kW, not 10 kW."""
    from app.domain.loads import LoadType as LT

    big_fixed = LoadSpec(
        id="baseline-1", normalized_name="Heat pump", category="HVAC",
        job_type=LT.FIXED, power_kw=4.0, max_power_kw=4.0,
    )
    job = LoadSpec(
        id="ev-1", normalized_name="EV charger", category="EV charging",
        job_type=LT.DEFERRABLE_INTERRUPTIBLE, power_kw=7.0, max_power_kw=7.0,
        energy_required_kwh=18.0,
        release_at=at(18), deadline_at=at(31),
    )
    scheduler_input, _ = service.build_input(
        [big_fixed, job], make_signal(hours=56), capacity_kw=10.0
    )
    result = service.run(scheduler_input, SchedulerName.CPSAT)
    # 4 + 7 = 11 > 10, so the 7 kW job must never run at full power.
    assert max(result.slot_load_w) <= 10_000
    for allocation in result.schedule[0].allocations:
        assert allocation.power_w <= 6_000 + 1e-6, "flexible headroom was 6 kW, not 7"


# --- registry ---------------------------------------------------------------


def test_the_registry_exposes_exactly_the_three_engines():
    assert set(SCHEDULERS) == {
        SchedulerName.ASAP, SchedulerName.GREEDY, SchedulerName.CPSAT
    }


def test_every_engine_implements_the_same_interface():
    for engine in SCHEDULERS.values():
        assert engine.name in set(SchedulerName)
        assert callable(engine.schedule)
        assert callable(engine.preflight)