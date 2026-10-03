"""Scheduler registry (Phase 4, §47).

    Scheduler
       |
       +-- ASAPScheduler
       +-- GreedyScheduler
       +-- CPSATScheduler

All three implement the same `schedule(SchedulerInput) -> SchedulerResult`
interface. The requested scheduler is never silently substituted (§34): if a
caller asks for CPSAT they get CP-SAT, or an explicit error.
"""

from .asap import ASAPScheduler
from .base import BaseScheduler, PlacementFailure, PreflightResult, SchedulerName
from .cpsat import CPSATScheduler
from .greedy import GreedyScheduler

SCHEDULERS: dict[SchedulerName, BaseScheduler] = {
    SchedulerName.ASAP: ASAPScheduler(),
    SchedulerName.GREEDY: GreedyScheduler(),
    SchedulerName.CPSAT: CPSATScheduler(),
}

__all__ = [
    "ASAPScheduler",
    "BaseScheduler",
    "CPSATScheduler",
    "GreedyScheduler",
    "PlacementFailure",
    "PreflightResult",
    "SCHEDULERS",
    "SchedulerName",
]
