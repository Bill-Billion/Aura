"""Tests for AgentRuntime and SimulationEngine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.runtime import AgentRuntime
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
from backend.engine.state_manager import (
    INVARIANT_VIOLATION_EVENT_TYPE,
    StateManager,
    verify_world_invariants,
)
from backend.execution.command import CommandSource, CommandStatus, DeviceCommand


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_world() -> WorldState:
    """Create a minimal world state for testing."""
    world = WorldState(scene_id="test")
    world.rooms = {
        "living_room": RoomState(
            id="living_room", temperature=28.0, occupancy=True, persons=["user_01"]
        ),
    }
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True,
                extra={"brightness": 0, "color_temp": 4000},
            ),
        ),
        "ac_living_01": DeviceState(
            id="ac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True,
                extra={"target_temp": 24.0, "mode": "cool"},
            ),
        ),
    }
    world.agents = {
        "lighting_agent": AgentRuntimeState(
            id="lighting_agent", name="Lighting Agent", status="idle"
        ),
        "hvac_agent": AgentRuntimeState(
            id="hvac_agent", name="HVAC Agent", status="idle"
        ),
    }
    # §2.2：persons 里的人必须在 users 里且 location 对得上，夹具世界自身必须是合法世界，
    # 否则仿真写入后的不变式探测会一直报一条与被测行为无关的违规。
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="living_room"),
            activity="idle",
        )
    }
    return world


def _make_engine() -> SimulationEngine:
    """Create a SimulationEngine with mocked connection manager."""
    world = _make_world()
    sm = StateManager(world)
    eb = EventBus()
    cm = ConnectionManager()
    engine = SimulationEngine(
        event_bus=eb,
        state_manager=sm,
        connection_manager=cm,
    )
    engine.timer.tick_interval = 0.01
    return engine


# ---------------------------------------------------------------------------
# AgentRuntime tests
# ---------------------------------------------------------------------------


class TestAgentRuntime:
    def test_init_empty(self):
        runtime = AgentRuntime()
        assert runtime.agents == []

    def test_register_agents(self):
        runtime = AgentRuntime()
        runtime.register(LightingAgent())
        runtime.register(HVACAgent())
        assert len(runtime.agents) == 2
        assert isinstance(runtime.agents[0], LightingAgent)
        assert isinstance(runtime.agents[1], HVACAgent)

    @pytest.mark.anyio
    async def test_step_returns_actions(self):
        runtime = AgentRuntime()
        runtime.register(LightingAgent())
        world = _make_world()
        # At time 12:00, light in occupied room should target brightness 40
        actions = await runtime.step(world)
        # LightingAgent should produce actions because current brightness=0,
        # target is 40 for daytime occupied room
        light_actions = [a for a in actions if a["agent_id"] == "lighting_agent"]
        assert len(light_actions) > 0
        # Each action should have the required keys
        for a in light_actions:
            assert "device_id" in a
            assert "property" in a
            assert "value" in a
            assert "reason" in a
            assert "agent_id" in a
            assert "agent_name" in a
            assert a["agent_id"] == "lighting_agent"
            assert a["agent_name"] == "Lighting Agent"

    @pytest.mark.anyio
    async def test_step_no_actions(self):
        runtime = AgentRuntime()
        runtime.register(LightingAgent())
        world = _make_world()
        # Set brightness to match target so no action is needed
        world.environment.time_of_day = "12:00"
        world.devices["light_living_01"].state.extra["brightness"] = 40
        world.devices["light_living_01"].state.extra["color_temp"] = 4500
        actions = await runtime.step(world)
        light_actions = [a for a in actions if a["agent_id"] == "lighting_agent"]
        assert len(light_actions) == 0


# ---------------------------------------------------------------------------
# SimulationEngine tests
# ---------------------------------------------------------------------------


class TestSimulationEngine:
    def test_engine_init(self):
        """Verify the engine initializes with 2 agents registered."""
        engine = _make_engine()
        assert len(engine.agent_runtime.agents) == 2
        assert isinstance(engine.agent_runtime.agents[0], LightingAgent)
        assert isinstance(engine.agent_runtime.agents[1], HVACAgent)
        assert engine.is_running is False

    def test_engine_init_subsystems(self):
        engine = _make_engine()
        assert engine.env_sim is not None
        assert engine.user_sim is not None
        assert engine.timer is not None
        assert engine.speed == 1.0

    @pytest.mark.anyio
    async def test_start_runs_timer_and_updates_world(self):
        """事件驱动模式下，start 会启动定时器并推进世界状态。"""
        engine = _make_engine()
        world = engine.state_manager.world

        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        initial_tick = world.simulation_tick
        await engine.start()
        await asyncio.sleep(0.12)
        await engine.stop()

        assert world.simulation_tick >= initial_tick + 1
        assert world.wall_tick_ms == 10
        assert world.simulated_dt_seconds == 10.0
        assert world.environment.weather == "cloudy"
        assert engine.conn.broadcast.call_count >= 1

    @pytest.mark.anyio
    async def test_start_stop(self):
        """Verify start/stop lifecycle."""
        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        await engine.start()
        assert engine.is_running is True
        assert engine.timer.is_running is True

        await asyncio.sleep(0.05)

        await engine.stop()
        assert engine.is_running is False
        assert engine.timer.is_running is False

    @pytest.mark.anyio
    async def test_reset(self):
        """Verify reset zeroes tick and resets time."""
        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        engine.state_manager.world.simulation_tick = 42
        engine.state_manager.world.environment.time_of_day = "18:30"
        engine.timer.current_tick = 42

        await engine.reset()

        assert engine.state_manager.world.simulation_tick == 0
        assert engine.state_manager.world.environment.time_of_day == "12:00"
        assert engine.timer.current_tick == 0
        assert engine.is_running is False

        # 没有在飞 episode 时，reset 不应发出取消事件。
        sim_event_types = [
            call.args[0].payload["event_type"]
            for call in engine.conn.broadcast.call_args_list
            if call.args[0].type == "SIM_EVENT"
        ]
        assert "system.episode_cancelled" not in sim_event_types

    @pytest.mark.anyio
    async def test_next_time_of_day_wraps_at_midnight(self):
        engine = _make_engine()
        assert engine._next_time_of_day("23:59", 60.0) == "00:00"

    @pytest.mark.anyio
    async def test_speed_control(self):
        engine = _make_engine()
        engine.speed = 5.0
        assert engine.speed == 5.0
        assert engine.timer.speed == 5.0
        assert engine.mode == "demo"

    @pytest.mark.anyio
    async def test_timer_tick_produces_delta_and_status_broadcasts(self):
        """每次 timer tick 后都要有事件流、状态增量和 agent 状态广播。"""
        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        await engine.start()
        await asyncio.sleep(0.12)
        await engine.stop()

        broadcast_types = [
            call.args[0].type for call in engine.conn.broadcast.call_args_list
        ]
        assert "SIM_EVENT" in broadcast_types
        assert "STATE_DELTA" in broadcast_types
        assert "AGENT_STATUS" in broadcast_types

    @pytest.mark.anyio
    async def test_start_idempotent(self):
        """Calling start() twice should not create two timer tasks."""
        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        await engine.start()
        task1 = engine.timer._task
        await engine.start()
        task2 = engine.timer._task
        assert task1 is task2

        await engine.stop()

    @pytest.mark.anyio
    async def test_pause_when_not_running(self):
        """Pausing when not running should be a no-op."""
        engine = _make_engine()
        await engine.pause()  # should not raise
        assert engine.is_running is False


class TestSimulatorWriteInvariants:
    """审计 finding 3：仿真写入路径必须自己跑不变式探测并自己背锅。"""

    @staticmethod
    def _sim_events(engine: SimulationEngine) -> list[dict]:
        return [
            call.args[0].payload
            for call in engine.conn.broadcast.call_args_list
            if call.args[0].type == "SIM_EVENT"
        ]

    @pytest.mark.anyio
    async def test_environment_refresh_write_reports_violation_attributed_to_simulator(self):
        """仿真把 occupancy 与 persons 写脱钩 → 系统级违规事件归因到仿真源，且不静默纠正。"""

        engine = _make_engine()
        verify_world_invariants(engine.state_manager.world)
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        await engine._handle_environment_refresh(
            SimEvent(
                event_type="environment.state_refresh",
                source="environment_sim",
                timestamp=0.0,
                data={
                    "simulated_dt": 10.0,
                    "updates": {"rooms[living_room].occupancy": False},
                },
            )
        )

        violations = [
            payload
            for payload in self._sim_events(engine)
            if payload["event_type"] == INVARIANT_VIOLATION_EVENT_TYPE
        ]
        assert len(violations) == 1
        data = violations[0]["data"]
        assert data["invariant"] == "occupancy_derivable_from_persons"
        assert data["attributed_to"] == "environment_sim"
        assert data["phase"] == "environment_refresh"
        assert data["pre_existing"] is False
        assert data["command_id"] is None
        assert violations[0]["priority"] == 3
        # 只检测不纠正：仿真世界不回滚，纠正与否留给上层（§2.2 禁止静默纠正）。
        assert data["reverted_paths"] == []
        assert engine.state_manager.world.rooms["living_room"].occupancy is False

    @pytest.mark.anyio
    async def test_same_unrepaired_violation_is_not_reported_twice(self):
        """同一条未修复的违规不重复刷屏；世界恢复一致后再坏才重新上报。"""

        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        async def refresh(updates: dict) -> None:
            await engine._handle_environment_refresh(
                SimEvent(
                    event_type="environment.state_refresh",
                    source="environment_sim",
                    timestamp=0.0,
                    data={"simulated_dt": 10.0, "updates": updates},
                )
            )

        await refresh({"rooms[living_room].occupancy": False})
        await refresh({"rooms[living_room].temperature": 25.0})

        def violation_count() -> int:
            return len(
                [
                    payload
                    for payload in self._sim_events(engine)
                    if payload["event_type"] == INVARIANT_VIOLATION_EVENT_TYPE
                ]
            )

        assert violation_count() == 1

        # 修好之后再次写坏 → 重新上报（不是永久静音）。
        await refresh({"rooms[living_room].occupancy": True})
        assert violation_count() == 1
        await refresh({"rooms[living_room].occupancy": False})
        assert violation_count() == 2

    @pytest.mark.anyio
    async def test_command_path_does_not_re_report_what_simulation_already_reported(self):
        """review2 finding-3：仿真侧与命令侧共用同一份去抖签名。

        否则仿真写坏世界之后，仿真侧报一条，此后每条 UI/agent 命令还各补一条
        pre_existing 事件——一次损坏在演示里会变成一路刷屏的事件洪水。
        """

        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        await engine._handle_environment_refresh(
            SimEvent(
                event_type="environment.state_refresh",
                source="environment_sim",
                timestamp=0.0,
                data={
                    "simulated_dt": 10.0,
                    "updates": {"rooms[living_room].occupancy": False},
                },
            )
        )

        def violation_count() -> int:
            return len(
                [
                    payload
                    for payload in self._sim_events(engine)
                    if payload["event_type"] == INVARIANT_VIOLATION_EVENT_TYPE
                ]
            )

        assert violation_count() == 1

        for value in (40, 60):
            record = await engine.command_executor.submit(
                DeviceCommand(
                    source=CommandSource.UI,
                    device_id="light_living_01",
                    capability="brightness",
                    value=value,
                ),
                publish=engine._publish_sim_event,
            )
            assert record.status is CommandStatus.SUCCEEDED

        assert violation_count() == 1
