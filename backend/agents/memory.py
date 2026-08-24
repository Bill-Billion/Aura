from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from typing import Any

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

        if event.event_type == "reasoning.coordination_decision":
            per_agent = event.data.get("per_agent")
            outcomes = []
            if isinstance(per_agent, list):
                outcomes = [
                    (
                        f"{str(item.get('agent_id') or '-')[:64]}:"
                        f"{str(item.get('outcome') or '-')[:64]}"
                    )
                    for item in per_agent[:16]
                    if isinstance(item, Mapping)
                ]
            return (
                "reasoning.coordination_decision:"
                f" profile={str(event.data.get('runtime_profile') or '-')[:64]}"
                f" governance={str(event.data.get('governance') or '-')[:64]}"
                f" outcomes={','.join(outcomes) or '-'}"
                f" observable_hash={str(event.data.get('observable_snapshot_hash') or '-')[:64]}"
                f" proposal_hash={str(event.data.get('proposal_set_hash') or '-')[:64]}"
                f" approved_hash={str(event.data.get('approved_command_set_hash') or '-')[:64]}"
                f" rejected_hash={str(event.data.get('rejected_command_set_hash') or '-')[:64]}"
            )

        return f"{event.event_type}: {AgentMemoryStore._stable_event_data(event.data)}"

    @staticmethod
    def _stable_event_data(value: Any) -> Any:
        """Remove transport identities before event memory enters an LLM prompt."""

        if isinstance(value, Mapping):
            return {
                str(key): AgentMemoryStore._stable_event_data(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
                if key
                not in {
                    "event_id",
                    "trigger_event_id",
                    "caused_by_event_id",
                    "correlation_id",
                    "causal_parent",
                    "run_id",
                }
            }
        if isinstance(value, (list, tuple)):
            return [AgentMemoryStore._stable_event_data(item) for item in value]
        return value
