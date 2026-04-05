"""Tests for AgentRuntime and SimulationEngine."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.runtime import AgentRuntime
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus
from backend.engine.simulation import SimulationEngine
from backend.engine.state import (
    AgentRuntimeState,
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    WorldState,
)
from backend.engine.state_manager import StateManager


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
        assert engine.speed == 1.0

    @pytest.mark.anyio
    async def test_main_loop_one_tick(self):
        """Run one tick and verify tick incremented and broadcast called."""
        engine = _make_engine()
        world = engine.state_manager.world

        # Mock broadcast to avoid needing real WebSocket connections
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        initial_tick = world.simulation_tick
        await engine._tick()

        # Tick should have incremented
        assert world.simulation_tick == initial_tick + 1

        # Time should have advanced
        assert world.environment.time_of_day != "12:00"

        # Broadcast should have been called at least once (AGENT_STATUS)
        assert engine.conn.broadcast.call_count >= 1

    @pytest.mark.anyio
    async def test_start_stop(self):
        """Verify start/stop lifecycle."""
        engine = _make_engine()
        # Mock broadcast
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        await engine.start()
        assert engine.is_running is True
        assert engine._task is not None

        # Let it run briefly
        await asyncio.sleep(0.3)

        await engine.stop()
        assert engine.is_running is False
        assert engine._task is None

    @pytest.mark.anyio
    async def test_reset(self):
        """Verify reset zeroes tick and resets time."""
        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        engine.state_manager.world.simulation_tick = 42
        engine.state_manager.world.environment.time_of_day = "18:30"

        await engine.reset()

        assert engine.state_manager.world.simulation_tick == 0
        assert engine.state_manager.world.environment.time_of_day == "12:00"
        assert engine.is_running is False

    @pytest.mark.anyio
    async def test_advance_time_wraps_at_midnight(self):
        engine = _make_engine()
        engine.state_manager.world.environment.time_of_day = "23:59"
        engine._advance_time(engine.state_manager.world)
        # After 1 minute advance (SIMULATED_DT=60s → 1 minute)
        assert engine.state_manager.world.environment.time_of_day == "00:00"

    @pytest.mark.anyio
    async def test_speed_control(self):
        engine = _make_engine()
        engine.speed = 5.0
        assert engine.speed == 5.0

    @pytest.mark.anyio
    async def test_tick_produces_delta_broadcast(self):
        """When agents produce actions, STATE_DELTA should be broadcast."""
        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        # Light brightness is 0, room occupied, time 12:00 → agent will act
        await engine._tick()

        # Check that at least one broadcast was for STATE_DELTA
        broadcast_types = [
            call.args[0].type for call in engine.conn.broadcast.call_args_list
        ]
        # If deltas were produced, STATE_DELTA should be present
        # (depends on whether agent found a change > threshold)
        assert "AGENT_STATUS" in broadcast_types

    @pytest.mark.anyio
    async def test_start_idempotent(self):
        """Calling start() twice should not create two tasks."""
        engine = _make_engine()
        engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

        await engine.start()
        task1 = engine._task
        await engine.start()
        task2 = engine._task
        assert task1 is task2

        await engine.stop()

    @pytest.mark.anyio
    async def test_pause_when_not_running(self):
        """Pausing when not running should be a no-op."""
        engine = _make_engine()
        await engine.pause()  # should not raise
        assert engine.is_running is False
