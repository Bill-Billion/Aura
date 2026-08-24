from __future__ import annotations

from unittest.mock import Mock

import pytest

from backend.agents.contracts import PriorityLevel, ProposalOutcome
from backend.agents.energy import EnergyAgent
from backend.agents.generalist import SINGLE_DIRECT_AGENT_ID, SingleDirectAgent
from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.llm_modes import MockedLLMProvider, RuleBasedLLMProvider
from backend.agents.memory import AgentMemoryStore
from backend.agents.scene import SceneAgent
from backend.agents.security import SecurityAgent
from backend.agents.types import AgentCommandProposal, AgentLLMDecision
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    WorldState,
)


def _world() -> WorldState:
    return WorldState(
        environment=EnvironmentState(time_of_day="19:00"),
        rooms={
            "living_room": RoomState(
                id="living_room",
                occupancy=True,
                light_level=80.0,
            )
        },
        devices={
            "light_living_01": DeviceState(
                id="light_living_01",
                type="light",
                location=Location3D(room="living_room"),
                capabilities=["power", "brightness", "color_temp"],
                state=DeviceStateValues(
                    power=True,
                    extra={"brightness": 5, "color_temp": 6000},
                ),
            ),
            "hvac_living_01": DeviceState(
                id="hvac_living_01",
                type="hvac",
                location=Location3D(room="living_room"),
                capabilities=["power", "target_temp", "mode", "speed"],
                state=DeviceStateValues(
                    power=True,
                    extra={"target_temp": 24.0, "mode": "cool", "speed": "auto"},
                ),
            ),
        },
    )


def _root_event(*, event_id: str = "root-1") -> SimEvent:
    return SimEvent(
        event_id=event_id,
        event_type="user.enters_room",
        source="scenario",
        timestamp=10.0,
        correlation_id=f"corr-{event_id}",
        data={
            "user_id": "resident_01",
            "from_room": "outside",
            "to_room": "living_room",
            "activity": "relaxing",
        },
    )


@pytest.mark.asyncio
async def test_single_direct_rule_output_does_not_enter_domain_agent_pipelines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poison = Mock(side_effect=AssertionError("domain agent method must not be called"))
    for agent_type in (LightingAgent, HVACAgent, SecurityAgent, EnergyAgent, SceneAgent):
        monkeypatch.setattr(agent_type, "is_relevant", poison)
        monkeypatch.setattr(agent_type, "get_allowed_command_specs", poison)
        monkeypatch.setattr(agent_type, "decide_for_event", poison)
    monkeypatch.setattr(EnergyAgent, "review_peer_proposals", poison)
    agent = SingleDirectAgent()

    world = _world()
    root_event = _root_event()
    envelope = await agent.handle_event(
        root_event=root_event,
        world_state=world,
        memory_store=AgentMemoryStore(),
        llm_provider=RuleBasedLLMProvider(),
    )
    assert envelope is not None
    proposal = agent.build_proposal(
        envelope=envelope,
        world_state=world,
        root_event=root_event,
    )

    assert envelope.agent_id == SINGLE_DIRECT_AGENT_ID
    assert proposal.agent_id == SINGLE_DIRECT_AGENT_ID
    assert proposal.agent_role == "generalist"
    assert proposal.priority is PriorityLevel.COMFORT
    assert proposal.outcome is ProposalOutcome.ACTED
    assumption_paths = {item.path for item in proposal.assumptions}
    assert {
        "environment.time_of_day",
        "environment.weather",
        "rooms[living_room].occupancy",
        "rooms[living_room].temperature",
        "rooms[living_room].light_level",
    }.issubset(assumption_paths)
    assert {
        (command.device_id, command.property, command.value)
        for command in proposal.commands
    } == {
        ("light_living_01", "extra.brightness", 70),
        ("light_living_01", "extra.color_temp", 3000),
    }


@pytest.mark.asyncio
async def test_single_direct_makes_one_mocked_request_per_root_event() -> None:
    agent = SingleDirectAgent()
    fixture = AgentLLMDecision(
        intent="adjust living-room light",
        confidence=0.9,
        task_steps=["set brightness"],
        proposed_commands=[
            AgentCommandProposal(
                device_id="light_living_01",
                property="extra.brightness",
                value=70,
                reason="resident entered the room",
            )
        ],
        explanation="one generalist decision",
        needs_coordination=False,
    )
    provider = MockedLLMProvider(
        fixtures_by_agent={SINGLE_DIRECT_AGENT_ID: fixture},
        strict=True,
    )
    memory = AgentMemoryStore()

    envelopes = [
        await agent.handle_event(
            root_event=_root_event(event_id=f"root-{index}"),
            world_state=_world(),
            memory_store=memory,
            llm_provider=provider,
        )
        for index in (1, 2)
    ]
    proposals = [
        agent.build_proposal(
            envelope=envelope,
            world_state=_world(),
            root_event=_root_event(event_id=f"root-{index}"),
        )
        for index, envelope in zip((1, 2), envelopes, strict=True)
        if envelope is not None
    ]

    assert len(provider.calls) == 2
    assert all(envelope is not None for envelope in envelopes)
    assert all(
        envelope.agent_id == SINGLE_DIRECT_AGENT_ID
        for envelope in envelopes
        if envelope is not None
    )
    assert all(
        len(envelope.candidate_commands) == 1
        for envelope in envelopes
        if envelope is not None
    )
    assert len(proposals) == 2
    assert all(proposal.agent_id == SINGLE_DIRECT_AGENT_ID for proposal in proposals)


@pytest.mark.asyncio
async def test_content_addressed_coordination_memory_does_not_block_next_request() -> None:
    fixture = AgentLLMDecision(
        intent="adjust living-room light",
        confidence=0.9,
        task_steps=["set brightness"],
        proposed_commands=[
            AgentCommandProposal(
                device_id="light_living_01",
                property="extra.brightness",
                value=70,
                reason="resident entered the room",
            )
        ],
        explanation="one generalist decision",
        needs_coordination=False,
    )
    provider = MockedLLMProvider(
        fixtures_by_agent={SINGLE_DIRECT_AGENT_ID: fixture},
        strict=True,
    )
    memory = AgentMemoryStore()
    agent = SingleDirectAgent()
    first = _root_event(event_id="root-1")
    second = _root_event(event_id="root-2")
    second.correlation_id = first.correlation_id

    first_envelope = await agent.handle_event(
        root_event=first,
        world_state=_world(),
        memory_store=memory,
        llm_provider=provider,
    )
    memory.remember(
        SimEvent(
            event_type="reasoning.coordination_decision",
            source="proposal_passthrough",
            timestamp=11.0,
            correlation_id=first.correlation_id,
            data={
                "runtime_profile": "single_direct",
                "governance": "none",
                "per_agent": [
                    {"agent_id": SINGLE_DIRECT_AGENT_ID, "outcome": "approved"}
                ],
                "observable_snapshot": {"blob": "x" * 12_000},
                "proposal_set": [{"blob": "x" * 2_000}],
                "approved_command_set": [],
                "rejected_command_set": [],
                "observable_snapshot_hash": "a" * 64,
                "proposal_set_hash": "b" * 64,
                "approved_command_set_hash": "c" * 64,
                "rejected_command_set_hash": "d" * 64,
            },
        ),
        agent_id=SINGLE_DIRECT_AGENT_ID,
    )
    second_envelope = await agent.handle_event(
        root_event=second,
        world_state=_world(),
        memory_store=memory,
        llm_provider=provider,
    )

    assert first_envelope is not None and first_envelope.mode == "llm"
    assert second_envelope is not None and second_envelope.mode == "llm"
    assert len(provider.calls) == 2
    memory_line = memory.build_recent_event_lines(
        SINGLE_DIRECT_AGENT_ID,
        first.correlation_id,
    )[0]
    assert len(memory_line) < 2048
    assert "observable_snapshot" not in memory_line


def test_single_direct_has_a_visible_decision_surface_for_every_root_type() -> None:
    agent = SingleDirectAgent()
    world = _world()
    invisible: list[str] = []
    for event_type in sorted(ALL_ROOT_EVENT_TYPES):
        event = SimEvent(
            event_type=event_type,
            source="test",
            timestamp=1.0,
            data={
                "user_id": "resident_01",
                "room_id": "living_room",
                "from_room": "outside",
                "to_room": "living_room",
                "activity": "relaxing",
                "device_id": "light_living_01",
                "device_type": "light",
                "significant_change_reasons": ["test"],
            },
        )
        if not agent.is_relevant(world, event):
            invisible.append(event_type)
    assert invisible == []
