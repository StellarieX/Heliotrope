"""Greedy interruptible block choice and the pairwise improvement pass."""

from datetime import timedelta

from app.domain.carbon import CarbonPoint, CarbonSignal
from app.domain.loads import LoadSpec, LoadType
from app.services.scheduler_service import SchedulerService
from app.services.schedulers import SchedulerName

from .fixtures import DAY_START


def signal(values: list[float]) -> CarbonSignal:
    values = list(values) + [values[-1]]
    points = [
        CarbonPoint(time=DAY_START + timedelta(minutes=15 * i), gco2_per_kwh=v, source="t")
        for i, v in enumerate(values)
    ]
    return CarbonSignal(
        start=DAY_START,
        end=DAY_START + timedelta(minutes=15 * len(values)),
        resolution_minutes=15,
        points=points,
    )


def slot_time(slot: int):
    return DAY_START + timedelta(minutes=15 * slot)


def test_greedy_picks_the_cleaner_sub_block_of_a_long_run():
    # One 8-slot run. The first four slots are dirty, the last four are clean.
    # A 1 kW job needing 1 kWh (4 slots) used to be charged from the run start.
    values = [500, 500, 500, 500, 50, 50, 50, 50]
    job = LoadSpec(
        id="ev", normalized_name="EV", category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=1.0, max_power_kw=1.0, energy_required_kwh=1.0, min_chunk_minutes=15,
        release_at=slot_time(0), deadline_at=slot_time(8),
    )
    service = SchedulerService()
    scheduler_input, _ = service.build_input([job], signal(values), capacity_kw=5.0)
    result = service.run(scheduler_input, SchedulerName.GREEDY)
    assert result.status.value == "FEASIBLE"
    slots = [a.slot for a in result.schedule[0].allocations]
    assert slots == [4, 5, 6, 7]
    asap = service.run(scheduler_input, SchedulerName.ASAP)
    assert result.metrics.total_co2_kg < asap.metrics.total_co2_kg


def test_greedy_block_respects_min_chunk_and_trims_dirty_edges():
    # Clean pocket of 2 slots surrounded by dirt; min chunk is 3 slots, so the
    # block must be 3 slots long and still sit on the pocket.
    values = [400, 400, 20, 20, 400, 400, 400, 400]
    job = LoadSpec(
        id="ev", normalized_name="EV", category="EV charging",
        job_type=LoadType.DEFERRABLE_INTERRUPTIBLE,
        power_kw=1.0, max_power_kw=1.0, energy_required_kwh=0.5, min_chunk_minutes=45,
        release_at=slot_time(0), deadline_at=slot_time(8),
    )
    service = SchedulerService()
    scheduler_input, _ = service.build_input([job], signal(values), capacity_kw=5.0)
    result = service.run(scheduler_input, SchedulerName.GREEDY)
    assert result.status.value == "FEASIBLE"
    slots = [a.slot for a in result.schedule[0].allocations]
    assert len(slots) >= 3 and slots == list(range(slots[0], slots[0] + len(slots)))
    assert {2, 3} <= set(slots)
