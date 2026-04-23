from __future__ import annotations

from collections import defaultdict, deque

from backend.engine.event_bus import SimEvent


class AgentMemoryStore:
    """进程内短期记忆，只保留最近的相关事件。"""

    def __init__(
        self,
        *,
        max_events_per_correlation: int = 50,
        max_agent_events: int = 200,
    ) -> None:
        self.max_events_per_correlation = max_events_per_correlation
        self.max_agent_events = max_agent_events
        self._by_correlation: dict[str, deque[SimEvent]] = defaultdict(
            lambda: deque(maxlen=self.max_events_per_correlation)
        )
        self._by_agent: dict[str, deque[SimEvent]] = defaultdict(
            lambda: deque(maxlen=self.max_agent_events)
        )

    def clear(self) -> None:
        self._by_correlation.clear()
        self._by_agent.clear()

    def remember(self, event: SimEvent, agent_id: str | None = None) -> None:
        self._by_correlation[event.correlation_id].append(event)

        inferred_agent_id = agent_id
        if inferred_agent_id is None and event.source.endswith("_agent"):
            inferred_agent_id = event.source

        if inferred_agent_id:
            self._by_agent[inferred_agent_id].append(event)

    def get_correlation_history(self, correlation_id: str) -> list[SimEvent]:
        return list(self._by_correlation.get(correlation_id, ()))

    def get_agent_recent_events(self, agent_id: str) -> list[SimEvent]:
        return list(self._by_agent.get(agent_id, ()))

    def build_recent_event_lines(self, agent_id: str, correlation_id: str, limit: int = 6) -> list[str]:
        recent_events = self.get_correlation_history(correlation_id)[-limit:]
        if not recent_events:
            recent_events = self.get_agent_recent_events(agent_id)[-limit:]

        return [self._format_event_line(event) for event in recent_events]

    @staticmethod
    def _format_event_line(event: SimEvent) -> str:
        if event.event_type == "user.activity_change":
            return (
                "user.activity_change:"
                f" from={event.data.get('from_room') or '-'}"
                f" to={event.data.get('to_room') or '-'}"
                f" activity={event.data.get('activity') or '-'}"
            )

        if event.event_type == "environment.state_refresh":
            reasons = event.data.get("significant_change_reasons") or []
            return (
                "environment.state_refresh:"
                f" time={event.data.get('time_of_day') or '-'}"
                f" weather={event.data.get('weather') or '-'}"
                f" reasons={','.join(str(reason) for reason in reasons) or '-'}"
            )

        if event.event_type == "action.device_control":
            return (
                "action.device_control:"
                f" device={event.data.get('device_id') or '-'}"
                f" property={event.data.get('property') or '-'}"
                f" value={event.data.get('value')!r}"
            )

        if event.event_type == "feedback.state_delta":
            return (
                "feedback.state_delta:"
                f" device={event.data.get('device_id') or '-'}"
                f" property={event.data.get('property') or '-'}"
            )

        return f"{event.event_type}: {event.data}"
