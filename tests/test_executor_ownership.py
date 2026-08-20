"""S1 review finding-8：CommandExecutor 必须由引擎持有一台，而不是每次用完即弃。

旧结构里 main.py 每条入站 WS 消息造一台、runtime.py 每个 agent 每条 episode 造一台，
executor 的 ``_pending`` 注册表因此没有生产寿命：``cancel_pending()`` 在生产代码里
永远无事可取消，取代（superseded）也只能发生在单次 ``submit_batch`` 内部——
S2「reset 取消在飞命令」与 S3「用户命令取代 agent 在飞命令」赖以扩展的接缝形同虚设。

本文件锁住三件事：
  1. 引擎持有唯一一台 executor，UI 腿与 agent 腿共用它（不再各造各的）；
  2. 该注册表有跨调用寿命：先前挂起的命令能被后来的命令按 (device, capability) 取代；
  3. reset 取消在飞命令、清空注册表，并把 executor 换绑到重置后的新世界。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend import main as main_module
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
from backend.execution.command import (
    LIFECYCLE_EVENT_TYPE,
    CommandSource,
    CommandStatus,
    DeviceCommand,
)
from backend.execution.executor import CommandExecutor
from backend.main import app


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class RecordingExecutor(CommandExecutor):
    """记录每条经过本实例的命令，用来证明"同一台 executor"而非每次新造一台。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[str] = []

    async def submit(self, command, **kwargs):  # type: ignore[override]
        self.seen.append(f"{command.device_id}.{command.capability}")
        return await super().submit(command, **kwargs)

    async def propose(self, command, **kwargs):  # type: ignore[override]
        # S3-T5 起 agent 腿不再走 submit()：仲裁门先 propose（开仲裁窗口），
        # 裁决之后再 execute_approved。要证明"用的是注入的那台"，这条入口也得记账。
        self.seen.append(f"{command.device_id}.{command.capability}")
        return await super().propose(command, **kwargs)

    async def submit_batch(self, commands, **kwargs):  # type: ignore[override]
        self.seen.extend(f"{c.device_id}.{c.capability}" for c in commands)
        return await super().submit_batch(commands, **kwargs)


class StubProvider(LLMProvider):
    """只让 lighting_agent 产出一条命令，用于跑通一次真实 agent episode。"""

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
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
                        reason="occupancy detected",
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
    world = WorldState(scene_id="executor_ownership")
    world.environment.time_of_day = "19:00"
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=28.0,
            light_level=100.0,
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
            state=DeviceStateValues(power=True, extra={"brightness": 0, "color_temp": 4000}),
        ),
        "ac_living_01": DeviceState(
            id="ac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            capabilities=["power", "target_temp", "mode", "speed"],
            state=DeviceStateValues(power=True, extra={"target_temp": 24.0, "mode": "cool"}),
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


def _make_engine(provider: LLMProvider | None = None) -> SimulationEngine:
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(_make_world()),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


def _collector():
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    return events, publish


def _cmd(**overrides) -> DeviceCommand:
    params = dict(
        source=CommandSource.SCENARIO,
        device_id="light_living_01",
        capability="power",
        value=False,
        reason="parked in-flight command",
    )
    params.update(overrides)
    return DeviceCommand(**params)


async def _drain_tasks(engine: SimulationEngine) -> None:
    for _ in range(300):
        pending = [task for task in engine.agent_runtime._background_tasks if not task.done()]
        if not pending:
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("agent tasks did not settle in time")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _receive_until_message_type(ws, expected_type: str, max_messages: int = 24) -> dict:
    last: dict = {}
    for _ in range(max_messages):
        last = ws.receive_json()
        if last["type"] == expected_type:
            return last
    return last


# ---------------------------------------------------------------------------
# 1. 引擎持有唯一一台 executor
# ---------------------------------------------------------------------------


def test_engine_owns_one_executor_shared_with_agent_runtime():
    engine = _make_engine()

    assert isinstance(engine.command_executor, CommandExecutor)
    # 绑定当前世界，且 agent 腿用的就是同一台（不再每条 episode 新造）。
    assert engine.command_executor.state_manager is engine.state_manager
    assert engine.agent_runtime.command_executor is engine.command_executor


def test_ui_commands_go_through_the_engine_owned_executor(client):
    """两条入站 WS 消息必须落在同一台 executor 上（旧实现每条消息 new 一台）。"""

    engine = main_module.simulation_engine
    assert engine is not None
    recorder = RecordingExecutor(engine.state_manager)
    engine.command_executor = recorder

    with client.websocket_connect("/ws/simulation") as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json(
            {
                "type": "CMD_DEVICE_CONTROL",
                "payload": {"device_id": "light_living_01", "action": "turn_off"},
            }
        )
        _receive_until_message_type(ws, "STATE_DELTA")
        ws.send_json(
            {
                "type": "CMD_DEVICE_CONTROL",
                "payload": {"device_id": "light_living_01", "action": "turn_on"},
            }
        )
        _receive_until_message_type(ws, "STATE_DELTA")

    assert recorder.seen == ["light_living_01.power", "light_living_01.power"]
    # UI 腿仍拿到自己的 delta 聚合（per-call publish 包装按次传入而非烧进构造函数）。
    assert recorder.state_manager.world.devices["light_living_01"].state.power is True


@pytest.mark.anyio
async def test_agent_episode_uses_the_injected_executor():
    """agent episode 不再自造 executor：注入的那台必须看到 agent 命令。"""

    engine = _make_engine(StubProvider())
    recorder = RecordingExecutor(engine.state_manager)
    engine.command_executor = recorder
    engine.agent_runtime.command_executor = recorder

    await engine._publish_sim_event(
        SimEvent(
            event_id="root-agent",
            event_type="user.activity_change",
            source="user_behavior_sim",
            timestamp=5.0,
            wall_time=5.0,
            correlation_id="corr-agent",
            priority=2,
            data={
                "user_id": "user_01",
                "from_room": "entry",
                "to_room": "living_room",
                "activity": "watching_tv",
            },
        )
    )
    await _drain_tasks(engine)

    assert "light_living_01.brightness" in recorder.seen
    assert engine.state_manager.world.devices["light_living_01"].state.extra["brightness"] == 70


# ---------------------------------------------------------------------------
# 2. 注册表有跨调用寿命：后来的命令取代先前挂起的同控制点命令
# ---------------------------------------------------------------------------


def test_ui_command_supersedes_earlier_pending_command_on_same_target(client):
    """先挂一条在飞命令，再经 WS 提交同 (device, capability) 命令 → 前者被取代。

    旧实现下 WS 消息自造 executor，看不见任何在飞命令，取代永不发生。
    """

    engine = main_module.simulation_engine
    assert engine is not None
    victim = asyncio.run(engine.command_executor._propose(_cmd(command_id="parked"), None))
    assert ("light_living_01", "power") in engine.command_executor.pending

    with client.websocket_connect("/ws/simulation") as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json(
            {
                "type": "CMD_DEVICE_CONTROL",
                "payload": {"device_id": "light_living_01", "action": "turn_off"},
            }
        )
        _receive_until_message_type(ws, "STATE_DELTA")

    assert victim.status is CommandStatus.SUPERSEDED
    assert victim.failure_code == "superseded_by_newer_command"
    # 取代后注册表不残留：新命令跑完即注销。
    assert engine.command_executor.pending == {}


# ---------------------------------------------------------------------------
# 3. reset 取消在飞命令 + 清空注册表 + 换绑新世界
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_reset_cancels_pending_commands_and_rebinds_executor():
    engine = _make_engine()
    events, publish = _collector()
    victim = await engine.command_executor._propose(
        _cmd(command_id="inflight"), None, publish=publish
    )
    assert engine.command_executor.pending

    new_state_manager = StateManager(_make_world())
    await engine.reset(new_state_manager=new_state_manager)

    assert victim.status is CommandStatus.CANCELLED
    assert engine.command_executor.pending == {}
    # 换绑新世界：旧 run 的 executor 绝不能继续写 reset 之前的那份世界。
    assert engine.command_executor.state_manager is new_state_manager
    assert engine.agent_runtime.command_executor is engine.command_executor

    cancelled_events = [
        event
        for event in events
        if event.event_type == LIFECYCLE_EVENT_TYPE and event.data["to_status"] == "cancelled"
    ]
    assert len(cancelled_events) == 1


@pytest.mark.anyio
async def test_in_place_reset_clears_pending_registry():
    """原地 reset（不换世界）同样必须清空注册表，否则旧命令会跨 run 存活。"""

    engine = _make_engine()
    await engine.command_executor._propose(_cmd(command_id="inflight-inplace"), None)
    assert engine.command_executor.pending

    await engine.reset()

    assert engine.command_executor.pending == {}
    assert engine.command_executor.state_manager is engine.state_manager
