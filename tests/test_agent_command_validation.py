"""Agent execution path must validate device commands exactly like the UI path.

审计必修①：runtime 胜出命令循环曾绕过 validate_device_command 并静默吞掉
KeyError。这里的奇偶校验（agent 路径与 UI 路径对同一坏命令返回同一错误码）
是承重断言——S1 换错误码词表时两条路径必须一起换。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.agents.lighting import LightingAgent
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


class ScriptedProvider(LLMProvider):
    """Always propose the given commands, regardless of the request."""

    def __init__(self, proposals: list[AgentCommandProposal]) -> None:
        self.proposals = proposals

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        return AgentLLMDecision(
            intent="scripted intent",
            confidence=0.9,
            task_steps=["scripted step"],
            proposed_commands=list(self.proposals),
            explanation="scripted decision",
            needs_coordination=False,
        )


class PermissiveLightingAgent(LightingAgent):
    """模拟 allowed-spec 与世界脱节，让坏命令穿过 agent 层过滤抵达 runtime 循环。"""

    def __init__(self, allowed_specs: list[dict[str, str]]) -> None:
        super().__init__()
        self._allowed_specs = allowed_specs

    def get_allowed_command_specs(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict[str, Any]]:
        return [dict(spec) for spec in self._allowed_specs]


def _make_world() -> WorldState:
    world = WorldState(scene_id="test")
    world.environment.time_of_day = "19:00"
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=28.0,
            occupancy=True,
            persons=["user_01"],
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
        "sensor_living_temp_01": DeviceState(
            id="sensor_living_temp_01",
            type="sensor",
            location=Location3D(room="living_room"),
            capabilities=["read"],
            state=DeviceStateValues(
                power=True,
                extra={"sensor_type": "temperature", "value": 26.5, "unit": "°C"},
            ),
        ),
    }
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="living_room"),
            activity="watching_tv",
        )
    }
    world.agents = {
        "lighting_agent": AgentRuntimeState(id="lighting_agent", name="Lighting Agent"),
    }
    return world


def _make_engine(
    proposals: list[AgentCommandProposal],
    allowed_specs: list[dict[str, str]],
) -> SimulationEngine:
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(_make_world()),
        connection_manager=ConnectionManager(),
        llm_provider=ScriptedProvider(proposals),
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    engine.agent_runtime.agents.clear()
    engine.agent_runtime.register(PermissiveLightingAgent(allowed_specs))
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


async def _run_episode_to_completion(engine: SimulationEngine, root_event: SimEvent) -> None:
    await engine._publish_sim_event(root_event)
    for _ in range(300):
        pending = [
            task
            for task in engine.agent_runtime._background_tasks
            if not task.done()
        ]
        if not pending:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("agent episode did not finish in time")


def _mutation_snapshot(engine: SimulationEngine) -> dict:
    return engine.state_manager.world.model_dump(include={"devices", "rooms"})


@pytest.mark.anyio
async def test_unknown_device_command_emits_failure_not_silence():
    engine = _make_engine(
        proposals=[
            AgentCommandProposal(
                device_id="ghost_device",
                property="extra.brightness",
                value=70,
                reason="haunt the living room",
            )
        ],
        allowed_specs=[{"device_id": "ghost_device", "property": "extra.brightness"}],
    )
    before = _mutation_snapshot(engine)
    root_event = _root_event("unknown-device")

    await _run_episode_to_completion(engine, root_event)

    events = _sim_events_from(engine)
    event_types = [event["event_type"] for event in events]
    assert "action.device_control" not in event_types
    assert "feedback.state_delta" not in event_types

    failures = [event for event in events if event["event_type"] == "device.command_failed"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["correlation_id"] == root_event.correlation_id
    # executor 的 device.command_failed 词表（S1-T4）：source/capability 取代旧 agent_id/property
    assert failure["data"]["source"] == "agent"
    assert failure["data"]["device_id"] == "ghost_device"
    assert failure["data"]["capability"] == "brightness"
    assert failure["data"]["error_code"] == "unknown_device"  # §10.2
    assert failure["data"]["reason"]

    execution_plan = next(
        event for event in events if event["event_type"] == "reasoning.execution_plan"
    )
    assert failure["causal_parent"] == execution_plan["event_id"]

    assert _mutation_snapshot(engine) == before


@pytest.mark.anyio
async def test_read_only_capability_rejected():
    engine = _make_engine(
        proposals=[
            AgentCommandProposal(
                device_id="sensor_living_temp_01",
                property="extra.value",
                value=18,
                reason="rewrite ground truth",
            )
        ],
        allowed_specs=[{"device_id": "sensor_living_temp_01", "property": "extra.value"}],
    )
    before = _mutation_snapshot(engine)
    root_event = _root_event("read-only")

    await _run_episode_to_completion(engine, root_event)

    events = _sim_events_from(engine)
    event_types = [event["event_type"] for event in events]
    assert "action.device_control" not in event_types
    assert "feedback.state_delta" not in event_types

    failures = [event for event in events if event["event_type"] == "device.command_failed"]
    assert len(failures) == 1
    failure = failures[0]

    sensor = engine.state_manager.world.devices["sensor_living_temp_01"]
    # §10.2 词表：sensor 未声明 value 能力（只有 read），经 executor 六级校验 →
    # capability_not_supported。命令被拒、传感器 ground truth 不被改写。
    assert failure["data"]["error_code"] == "capability_not_supported"

    assert sensor.state.extra["value"] == 26.5
    assert _mutation_snapshot(engine) == before


@pytest.mark.anyio
async def test_valid_command_regression():
    engine = _make_engine(
        proposals=[
            AgentCommandProposal(
                device_id="light_living_01",
                property="extra.brightness",
                value=70,
                reason="occupied evening lighting",
            )
        ],
        allowed_specs=[{"device_id": "light_living_01", "property": "extra.brightness"}],
    )
    root_event = _root_event("valid")

    await _run_episode_to_completion(engine, root_event)

    events = _sim_events_from(engine)
    event_types = [event["event_type"] for event in events]
    assert "device.command_failed" not in event_types
    assert "action.device_control" in event_types
    assert "feedback.state_delta" in event_types
    assert event_types.index("action.device_control") < event_types.index("feedback.state_delta")

    delta_paths = {
        event["data"]["path"]
        for event in events
        if event["event_type"] == "feedback.state_delta"
    }
    assert "devices[light_living_01].state.extra.brightness" in delta_paths
    assert "rooms[living_room].light_level" in delta_paths

    light = engine.state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 70
    assert engine.state_manager.world.rooms["living_room"].light_level == 560.0


@pytest.mark.anyio
async def test_invalid_command_does_not_block_subsequent_commands():
    engine = _make_engine(
        proposals=[
            AgentCommandProposal(
                device_id="ghost_device",
                property="extra.brightness",
                value=70,
                reason="haunt first",
            ),
            AgentCommandProposal(
                device_id="light_living_01",
                property="extra.brightness",
                value=70,
                reason="then light the room",
            ),
        ],
        allowed_specs=[
            {"device_id": "ghost_device", "property": "extra.brightness"},
            {"device_id": "light_living_01", "property": "extra.brightness"},
        ],
    )
    root_event = _root_event("mixed-batch")

    await _run_episode_to_completion(engine, root_event)

    events = _sim_events_from(engine)
    failures = [event for event in events if event["event_type"] == "device.command_failed"]
    assert len(failures) == 1
    assert failures[0]["data"]["device_id"] == "ghost_device"

    light = engine.state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 70


async def _agent_failure_code(device_id: str, property_path: str, value: Any) -> str:
    engine = _make_engine(
        proposals=[
            AgentCommandProposal(
                device_id=device_id,
                property=property_path,
                value=value,
                reason="parity probe",
            )
        ],
        allowed_specs=[{"device_id": device_id, "property": property_path}],
    )
    await _run_episode_to_completion(engine, _root_event(f"parity-{device_id}"))
    failures = [
        event
        for event in _sim_events_from(engine)
        if event["event_type"] == "device.command_failed"
    ]
    assert len(failures) == 1
    return failures[0]["data"]["error_code"]


def _ui_error_code(ws, payload: dict) -> str:
    ws.send_json({"type": "CMD_DEVICE_CONTROL", "payload": payload})
    for _ in range(24):
        data = ws.receive_json()
        if data["type"] == "ERROR":
            return data["payload"]["code"]
    raise AssertionError("UI path did not reject the command with an ERROR message")


def test_agent_and_ui_paths_reject_identically():
    # 承重断言：同一坏命令在两条路径上必须拿到完全相同的错误码。
    # 不固化具体词表字面量——S1 换十码枚举时本测试仍然成立。
    from backend.main import app

    with TestClient(app) as client:
        with client.websocket_connect("/ws/simulation") as ws:
            ws.receive_json()  # initial STATE_FULL
            ui_unknown_code = _ui_error_code(
                ws,
                {"device_id": "ghost_device", "action": "turn_on"},
            )
            ui_read_only_code = _ui_error_code(
                ws,
                {
                    "device_id": "sensor_living_temp_01",
                    "action": "set_state",
                    "params": {"value": 18},
                },
            )

    agent_unknown_code = asyncio.run(
        _agent_failure_code("ghost_device", "extra.brightness", 70)
    )
    agent_read_only_code = asyncio.run(
        _agent_failure_code("sensor_living_temp_01", "extra.value", 18)
    )

    # 承重断言：两条路径对同一坏命令产出完全相同的错误码。
    assert agent_unknown_code == ui_unknown_code
    assert agent_read_only_code == ui_read_only_code
    # 并且已迁移到 §10.2 十码词表（S1-T2）。
    assert ui_unknown_code == "unknown_device"
    assert ui_read_only_code == "capability_not_supported"
