"""Canonical scheduling horizon (Phase 4, §3).

Heliotrope schedules on a fixed 15-minute grid. Every scheduler operates on the
same `SchedulingHorizon`, so there is exactly one interpretation of time in the
system:

    slot_minutes = 15
    slot_hours   = 0.25
    24 h horizon  = 96 slots

A scheduler that invented its own slot size would produce numbers that could not
be compared against another scheduler's, which would defeat the entire purpose
of having ASAP / Greedy / CP-SAT. `slot_hours` is therefore a constant of the
domain, not a parameter of a scheduler.

SLOT INDEXING IS HALF-OPEN AND ZERO-BASED. Slot `i` covers
`[start + i*slot, start + (i+1)*slot)`. A job occupying slots `a..b-1` runs for
`(b - a) * slot_hours` hours. There is no inclusive-end ambiguity anywhere.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from ..utils.time import to_utc

# The canonical grid. Not configurable, on purpose (§3).
SLOT_MINUTES = 15
SLOT_HOURS = SLOT_MINUTES / 60.0


class HorizonError(ValueError):
    """The requested horizon is not usable."""


class SchedulingHorizon(BaseModel):
    """A half-open [start, end) window discretized into equal slots.

    `end` is always aligned to a slot boundary relative to `start`, so
    `slot_count * slot_minutes` is exactly the horizon length and the last slot
    is never partial. A partial slot would silently misreport energy.
    """

    start: datetime
    end: datetime
    slot_minutes: int = SLOT_MINUTES
    slot_count: int = Field(gt=0)

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("horizon timestamps must be timezone-aware")
        return to_utc(v)

    @model_validator(mode="after")
    def _consistent(self) -> "SchedulingHorizon":
        if self.end <= self.start:
            raise ValueError("horizon end must be after start")
        span_minutes = (self.end - self.start).total_seconds() / 60.0
        if abs(span_minutes - self.slot_count * self.slot_minutes) > 1e-6:
            raise ValueError(
                f"horizon span of {span_minutes} min is not an exact multiple of "
                f"{self.slot_count} slots of {self.slot_minutes} min"
            )
        return self

    # --- construction -------------------------------------------------------

    @classmethod
    def create(
        cls,
        start: datetime,
        end: datetime,
        slot_minutes: int = SLOT_MINUTES,
    ) -> "SchedulingHorizon":
        """Build a horizon, truncating `end` down to a whole slot.

        Truncating rather than rounding up matters: rounding up would add a slot
        the carbon signal may not cover, and a slot with no carbon data would
        have to be guessed at.
        """
        if slot_minutes <= 0:
            raise HorizonError("slot_minutes must be > 0")
        s, e = to_utc(start), to_utc(end)
        if e <= s:
            raise HorizonError("horizon end must be after start")
        slot_count = int((e - s).total_seconds() // (slot_minutes * 60))
        if slot_count <= 0:
            raise HorizonError("horizon is shorter than a single slot")
        return cls(
            start=s,
            end=s + timedelta(minutes=slot_count * slot_minutes),
            slot_minutes=slot_minutes,
            slot_count=slot_count,
        )

    @classmethod
    def spanning(
        cls,
        moments: list[datetime],
        slot_minutes: int = SLOT_MINUTES,
        pad_slots: int = 0,
    ) -> "SchedulingHorizon":
        """A horizon that covers every moment, aligned to the slot grid.

        The grid is anchored to the floor of the earliest moment in UTC, so all
        schedulers see byte-identical slot boundaries for the same input.
        """
        if not moments:
            raise HorizonError("cannot build a horizon from no moments")
        s, e = to_utc(min(moments)), to_utc(max(moments))
        floored = s.replace(second=0, microsecond=0)
        floored -= timedelta(minutes=floored.minute % slot_minutes)
        end = floored + timedelta(minutes=slot_minutes * (pad_slots + 1))
        if end <= e:
            # Extend until every moment is strictly inside the horizon.
            while end <= e:
                end += timedelta(minutes=slot_minutes)
        return cls.create(floored, end, slot_minutes)

    @classmethod
    def for_hours(
        cls, hours: int, start: Optional[datetime] = None, slot_minutes: int = SLOT_MINUTES
    ) -> "SchedulingHorizon":
        start = to_utc(start or datetime.now(timezone.utc))
        start = start.replace(second=0, microsecond=0) - timedelta(minutes=start.minute % slot_minutes)
        return cls.create(start, start + timedelta(hours=hours), slot_minutes)

    # --- slot arithmetic ----------------------------------------------------

    @property
    def slot_hours(self) -> float:
        return self.slot_minutes / 60.0

    @property
    def slots(self) -> range:
        return range(self.slot_count)

    def __len__(self) -> int:
        return self.slot_count

    def slot_start(self, index: int) -> datetime:
        self._check_index(index)
        return self.start + timedelta(minutes=self.slot_minutes * index)

    def slot_end(self, index: int) -> datetime:
        self._check_index(index)
        return self.start + timedelta(minutes=self.slot_minutes * (index + 1))

    def index_of(self, moment: datetime) -> int:
        """The index of the slot CONTAINING `moment`.

        A moment exactly on a boundary belongs to the slot it opens, which is
        what makes release/deadline handling half-open and unambiguous. Returns
        -1 before the horizon and `slot_count` at or after the end, so callers
        can detect "outside the horizon" with one comparison.
        """
        m = to_utc(moment)
        if m < self.start:
            return -1
        if m >= self.end:
            return self.slot_count
        raw = (m - self.start).total_seconds() / (self.slot_minutes * 60.0)
        return min(int(raw), self.slot_count - 1)

    def first_slot_at_or_after(self, moment: datetime) -> int:
        """Earliest slot index whose START is at or after `moment`.

        Rounds UP, not down. A release at 18:05 cannot use the 18:00 slot,
        because that slot begins two minutes before the load is allowed to run.
        """
        m = to_utc(moment)
        if m <= self.start:
            return 0
        if m >= self.end:
            return self.slot_count
        raw = (m - self.start).total_seconds() / (self.slot_minutes * 60.0)
        return min(int(math.ceil(raw - 1e-9)), self.slot_count)

    def last_slot_before(self, moment: datetime) -> int:
        """Latest slot index whose END is <= `moment`."""
        idx = self.index_of(moment)
        # index_of returns the containing slot for interior moments, so a
        # moment inside slot i means slot i-1 is the last one fully before it.
        return max(idx - 1, -1)

    def slot_range_for(self, start_moment: datetime, end_moment: datetime) -> range:
        """Slots fully contained in [start_moment, end_moment)."""
        first = self.first_slot_at_or_after(start_moment)
        last = self.last_slot_before(end_moment)
        if last < first:
            return range(0)
        return range(first, last + 1)

    def slot_overlap_fractions(
        self, start_moment: datetime, end_moment: datetime
    ) -> list[tuple[int, float]]:
        """(slot index, the fraction of that slot covered by the window), in order.

        `slot_range_for` answers a different question: which slots are FULLY
        CONTAINED in the window. It rounds the front edge up and the back edge
        down, so a window whose ends are not on the grid loses its two boundary
        slots. Using it as a load's occupancy window therefore dropped part of
        the load's energy — 30 minutes of draw across a 15-minute grid was
        charged as 15 minutes, half of it vanishing from the baseline, with
        headroom overstated and no downstream check able to notice.

        Each slot's covered fraction is returned instead, so a caller can charge
        exactly the energy the window really spans. An aligned window yields 1.0
        for every slot it touches, so this is a no-op for everything currently
        grid-aligned.
        """
        window_start = to_utc(start_moment)
        window_end = to_utc(end_moment)
        if window_end <= window_start:
            return []
        span_s = self.slot_minutes * 60.0
        out: list[tuple[int, float]] = []
        for index in self.slots:
            slot_start = self.start + timedelta(minutes=self.slot_minutes * index)
            slot_end = slot_start + timedelta(seconds=span_s)
            lo = max(window_start, slot_start)
            hi = min(window_end, slot_end)
            if hi > lo:
                out.append((index, (hi - lo).total_seconds() / span_s))
        return out

    def minutes_to_slots(self, minutes: float) -> int:
        """Slots required to cover `minutes`, rounding UP.

        Rounding up is required for atomic durations: rounding down would let a
        job be scheduled into less time than it needs.
        """
        if minutes <= 0:
            return 0
        return int(-(-minutes // self.slot_minutes))

    def slots_to_minutes(self, count: int) -> float:
        return count * self.slot_minutes

    def clamp_index(self, index: int) -> int:
        return max(0, min(index, self.slot_count - 1))

    def _check_index(self, index: int) -> None:
        if not 0 <= index < self.slot_count:
            raise HorizonError(
                f"slot index {index} is outside horizon of {self.slot_count} slots"
            )

    def iter_slot_windows(self) -> Iterator[tuple[int, datetime, datetime]]:
        for i in self.slots:
            yield i, self.slot_start(i), self.slot_end(i)

    def contains_window(self, start_moment: datetime, end_moment: datetime) -> bool:
        return self.start <= start_moment and end_moment <= self.end

    def describe(self) -> str:
        return (
            f"{self.slot_count} x {self.slot_minutes}min "
            f"({self.start.isoformat()} -> {self.end.isoformat()})"
        )


def slot_count_for(
    start: datetime, end: datetime, slot_minutes: int = SLOT_MINUTES
) -> int:
    """Whole slots in a window, never negative."""
    s, e = to_utc(start), to_utc(end)
    if e <= s:
        return 0
    return int((e - s).total_seconds() // (slot_minutes * 60))
