"""Scheduler abstraction.

Phase 1 establishes the interface only. Implementations raise
SchedulerNotAvailable: the API surface exists, the engine does not.
Phase 4 will add real ASAP/Greedy/CP-SAT implementations behind this
protocol without changing API contracts.
"""

from enum import Enum
from typing import List, Protocol

from ..domain.carbon import CarbonSignal
from ..domain.jobs import Job
from ..domain.schedules import ScheduleResponse


class SchedulerNotAvailable(RuntimeError):
    """Raised when a scheduler is requested before its engine exists."""


class SchedulerName(str, Enum):
    ASAP = "ASAP"
    GREEDY = "GREEDY"
    CPSAT = "CPSAT"


class Scheduler(Protocol):
    name: SchedulerName

    def schedule(
        self,
        jobs: List[Job],
        carbon_signal: CarbonSignal,
        capacity_kw: float,
    ) -> ScheduleResponse: ...


class _UnimplementedScheduler:
    """Placeholder base: instantiable (for contract tests/DI) but honest."""

    name: SchedulerName = SchedulerName.ASAP

    def schedule(self, jobs: List[Job], carbon_signal: CarbonSignal, capacity_kw: float) -> ScheduleResponse:
        raise SchedulerNotAvailable(
            f"{self.name} engine is not implemented yet (Phase 4). No schedule was produced."
        )


class ASAPScheduler(_UnimplementedScheduler):
    name = SchedulerName.ASAP


class GreedyScheduler(_UnimplementedScheduler):
    name = SchedulerName.GREEDY


class CPSATScheduler(_UnimplementedScheduler):
    name = SchedulerName.CPSAT


SCHEDULERS: dict[SchedulerName, Scheduler] = {
    SchedulerName.ASAP: ASAPScheduler(),
    SchedulerName.GREEDY: GreedyScheduler(),
    SchedulerName.CPSAT: CPSATScheduler(),
}


def get_scheduler(name: SchedulerName) -> Scheduler:
    return SCHEDULERS[name]
