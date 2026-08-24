"""Stable simulated-time priority queue (no wall-clock sleeps)."""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Any


@dataclass(order=True, frozen=True)
class ScheduledItem:
    due_at_s: float
    sequence: int
    event: str = field(compare=False)
    operation_id: str = field(compare=False)
    data: Any = field(default=None, compare=False)


class SimTimeScheduler:
    def __init__(self) -> None:
        self._queue: list[ScheduledItem] = []
        self._sequence = itertools.count()

    def schedule(
        self, due_at_s: float, event: str, operation_id: str, data: Any = None
    ) -> ScheduledItem:
        item = ScheduledItem(
            due_at_s=float(due_at_s),
            sequence=next(self._sequence),
            event=event,
            operation_id=operation_id,
            data=data,
        )
        heapq.heappush(self._queue, item)
        return item

    def pop_due(self, sim_time_s: float) -> list[ScheduledItem]:
        due: list[ScheduledItem] = []
        while self._queue and self._queue[0].due_at_s <= sim_time_s:
            due.append(heapq.heappop(self._queue))
        return due

    def pop_next_due(self, sim_time_s: float) -> ScheduledItem | None:
        """Pop one item so handlers can insert earlier work before the next item."""

        if not self._queue or self._queue[0].due_at_s > sim_time_s:
            return None
        return heapq.heappop(self._queue)

    def clear(self) -> None:
        self._queue.clear()
        self._sequence = itertools.count()

    @property
    def pending_count(self) -> int:
        return len(self._queue)
