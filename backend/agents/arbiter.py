from __future__ import annotations

from backend.engine.event_bus import SimEvent

from backend.agents.types import AgentCommandProposal, AgentDecisionEnvelope, ArbiterResult


PRIORITY_RANK = {
    "background_optimization": 1,
    "energy_efficiency": 2,
    "convenience": 3,
    "user_comfort": 4,
    "safety": 5,
    "direct_user_command": 5,
}


class Arbiter:
    """按优先级和稳定顺序裁决冲突命令。"""

    def resolve(
        self,
        proposals: list[AgentDecisionEnvelope],
        root_event: SimEvent,
    ) -> ArbiterResult:
        result = ArbiterResult()
        command_owner: dict[tuple[str, str], AgentDecisionEnvelope] = {}
        command_value: dict[tuple[str, str], AgentCommandProposal] = {}

        for proposal in sorted(
            proposals,
            key=lambda item: (
                -PRIORITY_RANK.get(item.priority, 0),
                -item.root_event_timestamp,
                item.agent_id,
            ),
        ):
            accepted: list[AgentCommandProposal] = []
            for command in proposal.candidate_commands:
                key = (command.device_id, command.property)
                previous_owner = command_owner.get(key)
                if previous_owner is None:
                    command_owner[key] = proposal
                    command_value[key] = command
                    accepted.append(command)
                    continue

                previous_command = command_value[key]
                if previous_command.value == command.value:
                    accepted.append(command)
                    continue

                result.conflicts.append(
                    {
                        "device_id": command.device_id,
                        "property": command.property,
                        "winner_agent_id": previous_owner.agent_id,
                        "loser_agent_id": proposal.agent_id,
                        "winner_priority": previous_owner.priority,
                        "loser_priority": proposal.priority,
                    }
                )

            result.winning_commands_by_agent[proposal.agent_id] = accepted

        for commands in result.winning_commands_by_agent.values():
            result.winning_commands.extend(commands)

        return result
