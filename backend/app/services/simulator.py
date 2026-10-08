"""Deterministic execution simulator (Phase 7).

This is NOT device telemetry. It replays a scripted sequence of (time, action)
pairs against a schedule record: advancing the clock, applying starts/pauses/
completions/failures, recording per-slot actual draws, and marking missed
starts. Every number it produces is labeled simulation.

Script action shapes (each item: {"at": iso, "do": ..., ...}):

  {"do": "start", "job": id}                 begin drawing per plan from `at`
  {"do": "pause"/"resume", "job": id}
  {"do": "complete", "job": id}              requirement met at `at`
  {"do": "fail", "job": id, "reason": ...}
  {"do": "delay", "job": id, "until": iso}   start held until `until`
  {"do": "miss", "job": id}                  never starts (for missed-path tests)
  {"do": "capacity", "kw": float}            shared capacity changes at `at`
  {"do": "capacity_profile", "profile": [...]} per-slot shared capacity (kW)
                                             changes at `at`; length must equal
                                             the horizon slot count
  {"do": "carbon_actual", "points": [...]}   observed carbon replaces forecast

Capacity accounting is per-slot: draws are checked against the connection's
per-slot capacity (`capacity_at`), so a time-varying profile is honored rather
than averaged away. The check is reported, never silently repaired.
"""

from __future__ import annotations

import logging
from datetime import datetime

from ..domain.execution import (
    JobStatus,
    ScheduleEvent,
    ScheduleEventType,
    ScheduleRecord,
)
from .execution_events import apply_event, transition

log = logging.getLogger("heliotrope.simulation")


def _at(item: dict) -> datetime:
    return datetime.fromisoformat(item["at"])


class ExecutionSimulator:
    """Replays a script. Deterministic: same record + same script = same trace."""

    def __init__(self, slot_minutes: int = 15) -> None:
        self.slot_minutes = slot_minutes

    def run(
        self,
        record: ScheduleRecord,
        script: list[dict],
        carbon_actual: list[dict] | None = None,
        to_time=None,
    ) -> dict:
        applied: list[str] = []
        capacity_kw = None
        capacity_profile_kw: list[float] | None = None
        for item in sorted(script, key=_at):
            at = _at(item)
            action = item.get("do", "")
            if action == "capacity":
                capacity_kw = float(item["kw"])
                if record.scheduler_input is not None:
                    from ..domain.scaling import to_power_w

                    record.scheduler_input = record.scheduler_input.model_copy(
                        update={"capacity_w": to_power_w(capacity_kw)}
                    )
                record.events.append(ScheduleEvent(
                    event_type=ScheduleEventType.CAPACITY_CHANGED,
                    timestamp=at, payload={"capacity_kw": capacity_kw, "simulated": True},
                ))
                applied.append(f"capacity -> {capacity_kw} kW at {at.isoformat()}")
            elif action == "capacity_profile":
                from ..domain.scaling import to_power_w

                raw = item.get("profile", item.get("capacity_profile_kw", item.get("kw")))
                if record.scheduler_input is None or not isinstance(raw, list):
                    applied.append(f"capacity_profile ignored at {at.isoformat()}: no horizon or no list")
                elif len(raw) != record.scheduler_input.horizon.slot_count:
                    applied.append(
                        f"capacity_profile ignored at {at.isoformat()}: "
                        f"{len(raw)} entries but the horizon has "
                        f"{record.scheduler_input.horizon.slot_count} slots"
                    )
                else:
                    capacity_profile_kw = [float(c) for c in raw]
                    record.scheduler_input = record.scheduler_input.model_copy(
                        update={"capacity_profile_w": [to_power_w(c) for c in capacity_profile_kw]}
                    )
                    record.events.append(ScheduleEvent(
                        event_type=ScheduleEventType.CAPACITY_CHANGED,
                        timestamp=at,
                        payload={"capacity_profile_kw": capacity_profile_kw, "simulated": True},
                    ))
                    applied.append(f"capacity_profile -> {len(capacity_profile_kw)} slots at {at.isoformat()}")
            elif action == "miss":
                state = record.execution.get(item["job"])
                if state and state.status in (JobStatus.PENDING, JobStatus.READY):
                    transition(state, JobStatus.MISSED, "simulated: never started")
                    applied.append(f"{item['job']} missed")
            elif action == "delay":
                state = record.execution.get(item["job"])
                if state:
                    state.note = f"simulated: held until {item['until']}"
                    applied.append(f"{item['job']} delayed until {item['until']}")
            elif action in ("start", "pause", "resume", "complete", "fail"):
                mapping = {
                    "start": ScheduleEventType.JOB_STARTED,
                    "pause": ScheduleEventType.JOB_PAUSED,
                    "resume": ScheduleEventType.JOB_RESUMED,
                    "complete": ScheduleEventType.JOB_COMPLETED,
                    "fail": ScheduleEventType.JOB_FAILED,
                }
                event = ScheduleEvent(
                    event_type=mapping[action], timestamp=at,
                    job_id=item["job"], payload={**item, "simulated": True},
                )
                record.events.append(event)
                apply_event(record, event)
                if action == "start":
                    self._draw_from_plan(record, item["job"], at)
                elif action == "resume":
                    # Drawing restarts where the plan still has power: slots
                    # already delivered stay delivered, future plan refills.
                    self._draw_from_plan(record, item["job"], at)
                elif action in ("pause", "fail"):
                    # The device stops drawing at `at`. Draws the plan had
                    # scheduled for slots ending after the event never happen;
                    # without trimming, a later replan would see phantom energy
                    # and under-schedule the remainder.
                    self._trim_future_draws(record, item["job"], at)
                applied.append(f"{item['job']} {action}")
            # Every scripted moment also advances the clock (miss detection).
            clock = ScheduleEvent(event_type=ScheduleEventType.CLOCK_ADVANCED, timestamp=at)
            record.events.append(clock)
            apply_event(record, clock)

        if carbon_actual:
            for point in carbon_actual:
                record.actual_carbon[point["timestamp"]] = float(
                    point.get("gco2_per_kwh", point.get("carbon_intensity_gco2_per_kwh", 0))
                )
        # The advance itself moves the clock even with an empty script, so
        # overdue starts are always detected.
        if to_time is not None:
            final = ScheduleEvent(event_type=ScheduleEventType.CLOCK_ADVANCED, timestamp=to_time)
            record.events.append(final)
            apply_event(record, final)
        return {
            "applied": applied,
            "capacity_kw": capacity_kw,
            "capacity_profile_kw": capacity_profile_kw,
            "capacity_violations": self.capacity_violations(record),
            "simulated": True,
        }

    def capacity_violations(self, record: ScheduleRecord) -> int:
        """Slots where actual delivered draws exceed per-slot capacity.

        Profile-aware: each slot is checked against `capacity_at(slot)`, so a
        dip in a time-varying profile is enforced rather than averaged away.
        """
        if record.scheduler_input is None:
            return 0
        horizon = record.scheduler_input.horizon
        n = horizon.slot_count
        totals = [0] * n
        for state in record.execution.values():
            for slot, power in state.delivered_slots.items():
                if 0 <= slot < n and power > 0:
                    totals[slot] += power
        baseline = [record.scheduler_input.baseline.at(s) for s in range(n)]
        return sum(
            1
            for s in range(n)
            if totals[s] + baseline[s] > record.scheduler_input.capacity_at(s)
        )

    def _draw_from_plan(self, record: ScheduleRecord, job_id: str, since: datetime) -> None:
        """Record actual draws following the plan from `since` onward, until a
        later event changes the state. Slot-granular, deterministic."""
        if record.scheduler_input is None:
            return
        horizon = record.scheduler_input.horizon
        state = record.execution.get(job_id)
        if state is None:
            return
        plan: dict[int, int] = {}
        for version in reversed(record.versions):
            for scheduled in version.result.schedule:
                if scheduled.job_id == job_id:
                    plan = {a.slot: a.power_w for a in scheduled.allocations}
                    break
            if plan:
                break
        for slot in sorted(plan):
            if horizon.slot_start(slot) >= since and plan[slot] > 0:
                state.delivered_slots[slot] = plan[slot]
        self._refresh_energy(record, state)
        state.last_updated = since
        log.info("simulated draw job=%s from=%s", job_id, since.isoformat())

    def _trim_future_draws(
        self, record: ScheduleRecord, job_id: str, at: datetime
    ) -> None:
        """Drop recorded draws for slots ending after `at`.

        Slots fully completed before the event keep their draws (that energy
        really flowed); anything still in progress or in the future did not
        happen once the device paused or failed.
        """
        if record.scheduler_input is None:
            return
        horizon = record.scheduler_input.horizon
        state = record.execution.get(job_id)
        if state is None:
            return
        kept = {
            slot: power
            for slot, power in state.delivered_slots.items()
            if horizon.slot_end(slot) <= at
        }
        if len(kept) != len(state.delivered_slots):
            state.delivered_slots.clear()
            state.delivered_slots.update(kept)
            self._refresh_energy(record, state)
        state.last_updated = at
        log.info("simulated trim job=%s at=%s", job_id, at.isoformat())

    def _refresh_energy(self, record: ScheduleRecord, state) -> None:
        """Recompute delivered energy from every recorded draw.

        Always derived from the full `delivered_slots` mapping — never
        overwritten from just the latest draw — so a start followed by a
        resume cannot lose the energy drawn before the pause.
        """
        slot_minutes = record.scheduler_input.horizon.slot_minutes
        total_w = sum(p for p in state.delivered_slots.values() if p > 0)
        state.energy_delivered_kwh = round(total_w * slot_minutes / 60_000, 4)
