"""Solver benchmark (development script, not part of the test suite).

Reproduces the numbers quoted in CHANGELOG / ARCHITECTURE: how long CP-SAT takes on
a household with a water heater, with and without warm-start hints and extra
workers, and what a small optimality-gap tolerance buys.

Run from the backend directory:  python bench_solver.py
Everything printed is MEASURED at run time; nothing is written down by hand.
"""

import sys
import time

sys.path.insert(0, ".")

from app.domain.scheduling import SchedulerConfig  # noqa: E402
from app.services.scheduler_service import SchedulerService  # noqa: E402
from app.services.schedulers import SchedulerName  # noqa: E402
from tests.fixtures import ev_job, geyser_job, make_signal, washing_machine_job  # noqa: E402

LIMIT_S = 12
service = SchedulerService()
signal = make_signal(hours=56)
specs = [ev_job(), washing_machine_job(), geyser_job()]


def solve(config, hints=None):
    inp, _ = service.build_input(specs, signal, capacity_kw=20.0, hints=hints)
    t = time.perf_counter()
    result = service.run(inp, SchedulerName.CPSAT, config=config, explain=False)
    return result, time.perf_counter() - t


print(f"household: EV + washer + water heater, 56h horizon, time limit {LIMIT_S}s\n")
print("1) hints and workers (strict, proves optimality):")
base, _ = solve(SchedulerConfig(time_limit_seconds=LIMIT_S))
hints = service.extract_hints_from_result(base)
for workers in (1, 4):
    for use in (False, True):
        r, s = solve(SchedulerConfig(time_limit_seconds=LIMIT_S, num_workers=workers), hints if use else None)
        print(f"   workers={workers} hints={str(use):5} -> {r.status.value:8} gap={r.solver.relative_gap:.5%}  {s:5.1f}s")

print("\n2) optimality-gap tolerance (relative_gap_limit):")
for gap in (0.0, 0.0001, 0.001):
    r, s = solve(SchedulerConfig(time_limit_seconds=LIMIT_S, relative_gap_limit=gap))
    print(f"   limit={gap:<7} -> {r.status.value:8} co2={r.metrics.total_co2_kg:.4f} kg  gap={r.solver.relative_gap:.5%}  {s:5.2f}s")
