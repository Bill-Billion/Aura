import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any, Callable, Coroutine

from pydantic import BaseModel, Field


class WorldEvent(BaseModel):
    event_type: str
    source: str
    timestamp: float
    data: dict[str, Any] = Field(default_factory=dict)


class SimEvent(WorldEvent):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    wall_time: float = Field(default_factory=time.time)
    correlation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    causal_parent: str | None = None
    priority: int = Field(default=1, ge=0, le=3)

    @classmethod
    def from_world_event(cls, event: WorldEvent, **overrides: Any) -> 'SimEvent':
        payload = event.model_dump()
        payload.update(overrides)
        return cls(**payload)


Handler = Callable[[SimEvent], Coroutine | None]


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._history: list[SimEvent] = []
        self._max_history: int = 1000

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register *handler* for events matching *event_type*."""
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """Remove *handler* from *event_type* subscribers."""
        subs = self._subscribers.get(event_type, [])
        try:
            subs.remove(handler)
        except ValueError:
            pass

    def coerce_event(self, event: WorldEvent | SimEvent) -> SimEvent:
        if isinstance(event, SimEvent):
            return event
        return SimEvent.from_world_event(event)

    async def publish(self, event: WorldEvent | SimEvent) -> int:
        """Publish *event* to all matching subscribers (exact + wildcard)."""
        sim_event = self.coerce_event(event)

        handlers: list[Handler] = []
        handlers.extend(self._subscribers.get(sim_event.event_type, []))
        if sim_event.event_type != '*':
            handlers.extend(self._subscribers.get('*', []))

        self._history.append(sim_event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        for handler in handlers:
            result = handler(sim_event)
            if asyncio.iscoroutine(result):
                await result

        return len(handlers)

    def get_history(
        self,
        event_type: str | None = None,
        since: float | None = None,
        correlation_id: str | None = None,
        source: str | None = None,
        min_priority: int | None = None,
        causal_parent: str | None = None,
    ) -> list[SimEvent]:
        """Return historical events filtered by common query dimensions."""
        results = self._history
        if event_type is not None:
            results = [event for event in results if event.event_type == event_type]
        if since is not None:
            results = [event for event in results if event.timestamp >= since]
        if correlation_id is not None:
            results = [event for event in results if event.correlation_id == correlation_id]
        if source is not None:
            results = [event for event in results if event.source == source]
        if min_priority is not None:
            results = [event for event in results if event.priority >= min_priority]
        if causal_parent is not None:
            results = [event for event in results if event.causal_parent == causal_parent]
        return results

    def get_correlation_history(self, correlation_id: str) -> list[SimEvent]:
        return self.get_history(correlation_id=correlation_id)

    def get_causal_chain(self, root_event_id: str) -> list[SimEvent]:
        indexed = {event.event_id: event for event in self._history}
        root = indexed.get(root_event_id)
        if root is None:
            return []

        grouped_children: dict[str, list[SimEvent]] = defaultdict(list)
        for event in self._history:
            if event.causal_parent:
                grouped_children[event.causal_parent].append(event)

        for children in grouped_children.values():
            children.sort(key=lambda event: (event.timestamp, event.wall_time))

        chain: list[SimEvent] = []
        stack = [root]
        while stack:
            current = stack.pop()
            chain.append(current)
            children = grouped_children.get(current.event_id, [])
            stack.extend(reversed(children))

        chain.sort(key=lambda event: (event.timestamp, event.wall_time))
        return chain
