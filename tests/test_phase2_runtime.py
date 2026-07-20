from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.simulation import SimulationEngine
from backend.engine.state import (
    AgentRuntimeState,
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    UserState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.agents.llm import LLMProvider, LLMProviderError
from backend.agents.types import AgentLLMDecision, AgentCommandProposal, LLMDecisionRequest


class StubProvider(LLMProvider):
    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        if self.should_fail:
            raise LLMProviderError("timeout", "provider timed out")

        if request.agent_id == "lighting_agent":
            return AgentLLMDecision(
                intent="light occupied room",
                confidence=0.94,
                task_steps=["raise brightness", "warm color"],
                proposed_commands=[
                    AgentCommandProposal(
                        device_id="light_living_01",
                        property="extra.brightness",
                        value=70,
                        reason="occupied evening lighting",
                    )
                ],
                explanation="Occupancy increased in the living room during the evening",
                needs_coordination=False,
            )

        return AgentLLMDecision(
            intent="hold hvac steady",
            confidence=0.55,
            task_steps=["no change"],
            proposed_commands=[],
            explanation="No HVAC action needed",
            needs_coordination=False,
        )


class DelayedProvider(LLMProvider):
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        await asyncio.sleep(self.delay_s)
        if request.agent_id == "lighting_agent":
            return AgentLLMDecision(
                intent="light occupied room",
                confidence=0.9,
                task_steps=["raise brightness"],
                proposed_commands=[
                    AgentCommandProposal(
                        device_id="light_living_01",
                        property="extra.brightness",
                        value=70,
                        reason="occupied evening lighting",
                    )
                ],
                explanation="Lighting should react to occupancy",
                needs_coordination=False,
            )

        return AgentLLMDecision(
            intent="cool occupied room",
            confidence=0.88,
            task_steps=["adjust target temp"],
            proposed_commands=[
                AgentCommandProposal(
                    device_id="ac_living_01",
                    property="extra.target_temp",
                    value=25.0,
                    reason="occupied room is too warm",
                )
            ],
            explanation="HVAC should react to occupancy and room temperature",
            needs_coordination=False,
        )


def _make_world() -> WorldState:
    world = WorldState(scene_id="test")
    world.environment.time_of_day = "19:00"
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=28.0,
            occupancy=False,
            persons=[],
        )
    }
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness", "color_temp"],
            state=DeviceStateValues(
                power=True,
                extra={"brightness": 0, "color_temp": 4000},
            ),
        ),
        "ac_living_01": DeviceState(
            id="ac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            capabilities=["power", "target_temp", "mode", "speed"],
            state=DeviceStateValues(
                power=True,
                extra={"target_temp": 24.0, "mode": "cool"},
            ),
        ),
    }
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="entry"),
            activity="arriving",
        )
    }
    world.agents = {
        "lighting_agent": AgentRuntimeState(id="lighting_agent", name="Lighting Agent"),
        "hvac_agent": AgentRuntimeState(id="hvac_agent", name="HVAC Agent"),
    }
    return world


def _make_engine(provider: LLMProvider) -> SimulationEngine:
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(_make_world()),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


def _sim_events_from(engine: SimulationEngine) -> list[dict]:
    return [
        call.args[0].payload
        for call in engine.conn.broadcast.call_args_list
        if call.args[0].type == "SIM_EVENT"
    ]


@pytest.mark.anyio
async def test_user_activity_change_emits_reasoning_chain_and_action_feedback():
    engine = _make_engine(StubProvider())

    root_event = SimEvent(
        event_id="root-1",
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=5.0,
        wall_time=5.0,
        correlation_id="corr-1",
        priority=2,
        data={
            "user_id": "user_01",
            "from_room": "entry",
            "to_room": "living_room",
            "activity": "watching_tv",
        },
    )

    await engine._publish_sim_event(root_event)
    await asyncio.sleep(0.05)

    event_types = [event["event_type"] for event in _sim_events_from(engine)]
    assert "reasoning.perception_snapshot" in event_types
    assert "reasoning.intent_recognized" in event_types
    assert "reasoning.task_decomposition" in event_types
    assert "reasoning.coordination_decision" in event_types
    assert "reasoning.execution_plan" in event_types
    assert "action.device_control" in event_types
    assert "feedback.state_delta" in event_types

    light = engine.state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 70
    assert engine.state_manager.world.rooms["living_room"].light_level == 560.0


@pytest.mark.anyio
async def test_provider_failure_emits_fallback_and_uses_rule_based_actions():
    engine = _make_engine(StubProvider(should_fail=True))

    root_event = SimEvent(
        event_id="root-2",
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=6.0,
        wall_time=6.0,
        correlation_id="corr-2",
        priority=2,
        data={
            "user_id": "user_01",
            "from_room": "entry",
            "to_room": "living_room",
            "activity": "arrive_home",
        },
    )

    await engine._publish_sim_event(root_event)
    await asyncio.sleep(0.05)

    events = _sim_events_from(engine)
    event_types = [event["event_type"] for event in events]
    assert "reasoning.fallback_rule_based" in event_types
    assert "action.device_control" in event_types

    fallback_event = next(event for event in events if event["event_type"] == "reasoning.fallback_rule_based")
    assert fallback_event["data"]["reason"] == "timeout"

    light = engine.state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 70


@pytest.mark.anyio
async def test_runtime_evaluates_agents_concurrently():
    engine = _make_engine(DelayedProvider(delay_s=0.2))

    root_event = SimEvent(
        event_id="root-3",
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=7.0,
        wall_time=7.0,
        correlation_id="corr-3",
        priority=2,
        data={
            "user_id": "user_01",
            "from_room": "entry",
            "to_room": "living_room",
            "activity": "watching_tv",
        },
    )

    started_at = time.monotonic()
    await engine._publish_sim_event(root_event)

    while True:
        event_types = [event["event_type"] for event in _sim_events_from(engine)]
        if event_types.count("reasoning.task_decomposition") >= 2:
            break
        await asyncio.sleep(0.01)

    elapsed = time.monotonic() - started_at

    assert elapsed < 0.32


@pytest.mark.anyio
async def test_runtime_caps_slow_provider_and_falls_back_quickly():
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(_make_world()),
        connection_manager=ConnectionManager(),
        llm_provider=DelayedProvider(delay_s=0.4),
        agent_episode_timeout_ms=80,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

    root_event = SimEvent(
        event_id="root-4",
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=8.0,
        wall_time=8.0,
        correlation_id="corr-4",
        priority=2,
        data={
            "user_id": "user_01",
            "from_room": "entry",
            "to_room": "living_room",
            "activity": "arrive_home",
        },
    )

    started_at = time.monotonic()
    await engine._publish_sim_event(root_event)

    while True:
        events = _sim_events_from(engine)
        event_types = [event["event_type"] for event in events]
        if "reasoning.fallback_rule_based" in event_types:
            break
        await asyncio.sleep(0.01)

    elapsed = time.monotonic() - started_at
    fallback_event = next(event for event in events if event["event_type"] == "reasoning.fallback_rule_based")

    assert elapsed < 0.2
    assert fallback_event["data"]["reason"] == "timeout"
