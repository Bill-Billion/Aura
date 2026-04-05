"""AgentRuntime — orchestrates all registered agents and collects their actions."""

from __future__ import annotations

from backend.agents.base import BaseAgent
from backend.engine.state import WorldState


class AgentRuntime:
    """Manages a collection of agents and runs their decide() step."""

    def __init__(self) -> None:
        self.agents: list[BaseAgent] = []

    def register(self, agent: BaseAgent) -> None:
        """Add an agent to the runtime."""
        self.agents.append(agent)

    async def step(self, world_state: WorldState) -> list[dict]:
        """Call each agent's decide(), return a flat list of action dicts.

        Each returned action dict contains:
        ``device_id``, ``property``, ``value``, ``reason``,
        ``agent_id``, ``agent_name``.
        """
        all_actions: list[dict] = []
        for agent in self.agents:
            actions = agent.decide(world_state)
            if actions:
                for a in actions:
                    a["agent_id"] = agent.agent_id
                    a["agent_name"] = agent.name
                all_actions.extend(actions)
        return all_actions
