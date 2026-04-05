import asyncio
from collections import defaultdict
from typing import Callable, Coroutine
from pydantic import BaseModel


class WorldEvent(BaseModel):
    event_type: str
    source: str
    timestamp: float
    data: dict


# Type alias for handler callables (sync or async)
Handler = Callable[[WorldEvent], Coroutine | None]


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._history: list[WorldEvent] = []
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

    async def publish(self, event: WorldEvent) -> int:
        """Publish *event* to all matching subscribers (exact + wildcard).

        Returns the number of handlers notified.
        """
        handlers: list[Handler] = []
        # Exact match subscribers
        handlers.extend(self._subscribers.get(event.event_type, []))
        # Wildcard subscribers ("*")
        if event.event_type != "*":
            handlers.extend(self._subscribers.get("*", []))

        # Record in history
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        # Notify all handlers
        for handler in handlers:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result

        return len(handlers)

    def get_history(
        self,
        event_type: str | None = None,
        since: float | None = None,
    ) -> list[WorldEvent]:
        """Return historical events, optionally filtered by type and/or timestamp."""
        results = self._history
        if event_type is not None:
            results = [e for e in results if e.event_type == event_type]
        if since is not None:
            results = [e for e in results if e.timestamp >= since]
        return results
