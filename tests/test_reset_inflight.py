"""Reset must cancel in-flight agent episodes before swapping the world.

审计必修②：SimulationEngine.reset() 曾只清 memory_store，挂在 LLM 上的
在飞 episode 恢复后会把旧世界的命令写进重置后的新世界（spec §15
"Reset cancels or invalidates old in-flight agent episodes"，§11 run
hygiene）。取消必须发生在世界替换之前（cancel-before-swap），并对每条
被取消的 episode 发布 system.episode_cancelled，前端因果链才不会凭空
消失；受影响 agent 不得停留在 thinking。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.agents.llm import LLMProvider
from backend.agents.types import (
    AgentCommandProposal,
    AgentLLMDecision,
    LLMDecisionRequest,
)
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


class GatedProvider(LLMProvider):
    """Park every decision on an asyncio gate so the episode stays in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.entered.set()
        await self.gate.wait()
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
                        reason="stale command from the pre-reset world",
                    )
                ],
                explanation="Lighting reacts to occupancy",
                needs_coordination=False,
            )
        return AgentLLMDecision(
            intent="hold hvac steady",
            confidence=0.5,
            task_steps=["no change"],
            proposed_commands=[],
            explanation="No HVAC action needed",
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


def _root_event(suffix: str) -> SimEvent:
    return SimEvent(
        event_id=f"root-{suffix}",
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=5.0,
        wall_time=5.0,
        correlation_id=f"corr-{suffix}",
        priority=2,
        data={
            "user_id": "user_01",
            "from_room": "entry",
            "to_room": "living_room",
            "activity": "watching_tv",
        },
    )


def _sim_events_from(engine: SimulationEngine) -> list[dict]:
    return [
        call.args[0].payload
        for call in engine.conn.broadcast.call_args_list
        if call.args[0].type == "SIM_EVENT"
    ]


async def _park_episode(
    engine: SimulationEngine, provider: GatedProvider, root_event: SimEvent
) -> asyncio.Task:
    """Publish the root event and wait until the episode is parked on the LLM gate."""
    await engine._publish_sim_event(root_event)
    await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
    # 给并发的第二个 agent 评估任务一个调度间隙，让它同样挂上 gate。
    await asyncio.sleep(0.02)
    tasks = [task for task in engine.agent_runtime._background_tasks if not task.done()]
    assert len(tasks) == 1, "expected exactly one in-flight episode task"
    return tasks[0]


async def _drain_tasks(engine: SimulationEngine) -> None:
    for _ in range(300):
        pending = [
            task
            for task in engine.agent_runtime._background_tasks
            if not task.done()
        ]
        if not pending:
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("agent tasks did not settle in time")


def _mutation_snapshot(state_manager: StateManager) -> dict:
    return state_manager.world.model_dump(include={"devices", "rooms"})


@pytest.mark.anyio
async def test_reset_cancels_inflight_episode():
    provider = GatedProvider()
    engine = _make_engine(provider)
    stale_task = await _park_episode(engine, provider, _root_event("stale"))

    new_state_manager = StateManager(_make_world())
    await engine.reset(new_state_manager=new_state_manager)
    before = _mutation_snapshot(new_state_manager)

    provider.gate.set()
    await _drain_tasks(engine)

    # 旧 episode 的命令绝不能落进重置后的新世界。
    assert _mutation_snapshot(new_state_manager) == before
    light = new_state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 0
    assert light.state.last_changed_by == "system"  # 模型默认值，未被任何 agent 改写

    assert stale_task.cancelled()
    assert engine.agent_runtime._active_tasks == {}
    assert engine.agent_runtime._background_tasks == set()


@pytest.mark.anyio
async def test_reset_emits_episode_cancelled_event():
    provider = GatedProvider()
    engine = _make_engine(provider)
    root_event = _root_event("cancelled")
    await _park_episode(engine, provider, root_event)

    await engine.reset(new_state_manager=StateManager(_make_world()))

    cancelled = [
        event
        for event in _sim_events_from(engine)
        if event["event_type"] == "system.episode_cancelled"
    ]
    assert len(cancelled) == 1
    event = cancelled[0]
    assert event["correlation_id"] == root_event.correlation_id
    assert event["data"]["correlation_id"] == root_event.correlation_id
    assert event["data"]["reason"] == "simulation_reset"
    assert set(event["data"]["agent_ids"]) == {"lighting_agent", "hvac_agent"}


@pytest.mark.anyio
async def test_reset_leaves_agents_idle():
    provider = GatedProvider()
    engine = _make_engine(provider)
    await _park_episode(engine, provider, _root_event("thinking"))

    # 原地 reset：同一个世界继续用，卡在 thinking 的 agent 必须回到 idle。
    await engine.reset()

    for agent in engine.state_manager.world.agents.values():
        assert agent.status == "idle"
        assert agent.active_correlation_id is None

    # reset 后新的根事件要能开启一条干净的 episode。
    provider.gate.set()
    fresh_event = _root_event("fresh")
    await engine._publish_sim_event(fresh_event)
    await _drain_tasks(engine)

    light = engine.state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 70

    events = _sim_events_from(engine)
    fresh_actions = [
        event
        for event in events
        if event["event_type"] == "action.device_control"
        and event["correlation_id"] == fresh_event.correlation_id
    ]
    assert len(fresh_actions) == 1
    for agent in engine.state_manager.world.agents.values():
        assert agent.status == "idle"


@pytest.mark.anyio
async def test_reset_without_inflight_is_noop():
    provider = GatedProvider()
    engine = _make_engine(provider)
    engine.state_manager.world.simulation_tick = 42
    engine.state_manager.world.environment.time_of_day = "18:30"
    engine.timer.current_tick = 42

    await engine.reset()

    assert engine.state_manager.world.simulation_tick == 0
    assert engine.state_manager.world.environment.time_of_day == "12:00"
    assert engine.timer.current_tick == 0

    event_types = [event["event_type"] for event in _sim_events_from(engine)]
    assert "system.episode_cancelled" not in event_types
    assert "system.simulation_reset" in event_types
