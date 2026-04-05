"""Tests for LightingAgent and HVACAgent."""

from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    WorldState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_light_world(
    *,
    hour: int = 12,
    brightness: int = 100,
    color_temp: int = 4000,
    occupied: bool = True,
) -> WorldState:
    world = WorldState(environment=EnvironmentState(
        time_of_day=f"{hour:02d}:00",
    ))
    world.rooms = {
        "living_room": RoomState(id="living_room", occupancy=occupied, persons=["user_01"] if occupied else []),
    }
    world.devices = {
        "light_01": DeviceState(
            id="light_01",
            type="light",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True,
                extra={"brightness": brightness, "color_temp": color_temp},
            ),
        ),
    }
    return world


def _make_hvac_world(
    *,
    room_temp: float = 30.0,
    mode: str = "cool",
    target_temp: float = 24.0,
    occupied: bool = True,
) -> WorldState:
    world = WorldState(environment=EnvironmentState(time_of_day="12:00"))
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=room_temp,
            occupancy=occupied,
            persons=["user_01"] if occupied else [],
        ),
    }
    world.devices = {
        "ac_01": DeviceState(
            id="ac_01",
            type="hvac",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True,
                extra={"target_temp": target_temp, "mode": mode},
            ),
        ),
    }
    return world


# ---------------------------------------------------------------------------
# LightingAgent tests
# ---------------------------------------------------------------------------


class TestLightingAgent:
    def test_lighting_agent_dim_at_night(self):
        """At 19:00 (evening), brightness should be < 100 for occupied room."""
        # Start with full brightness
        world = _make_light_world(hour=19, brightness=100, color_temp=5000, occupied=True)
        agent = LightingAgent()
        actions = agent.decide(world)

        brightness_actions = [a for a in actions if a["property"] == "extra.brightness"]
        assert len(brightness_actions) == 1
        assert brightness_actions[0]["value"] < 100
        assert brightness_actions[0]["value"] == 70  # evening occupied

    def test_lighting_agent_night_mode(self):
        """After 23:00, brightness should drop to 5."""
        world = _make_light_world(hour=23, brightness=50, color_temp=4000, occupied=True)
        agent = LightingAgent()
        actions = agent.decide(world)

        brightness_actions = [a for a in actions if a["property"] == "extra.brightness"]
        assert len(brightness_actions) == 1
        assert brightness_actions[0]["value"] == 5

    def test_lighting_agent_no_action_when_close(self):
        """No action when current values are within threshold."""
        world = _make_light_world(hour=14, brightness=40, color_temp=4500, occupied=True)
        agent = LightingAgent()
        actions = agent.decide(world)
        assert len(actions) == 0

    def test_lighting_agent_empty_room_daytime(self):
        """Empty room during daytime should dim to 10."""
        world = _make_light_world(hour=14, brightness=50, color_temp=4000, occupied=False)
        agent = LightingAgent()
        actions = agent.decide(world)

        brightness_actions = [a for a in actions if a["property"] == "extra.brightness"]
        assert len(brightness_actions) == 1
        assert brightness_actions[0]["value"] == 10


# ---------------------------------------------------------------------------
# HVACAgent tests
# ---------------------------------------------------------------------------


class TestHVACAgent:
    def test_hvac_agent_cools_when_hot(self):
        """Room at 30°C with AC in cool mode should lower target."""
        world = _make_hvac_world(room_temp=30.0, mode="cool", target_temp=24.0)
        agent = HVACAgent()
        actions = agent.decide(world)

        assert len(actions) == 1
        action = actions[0]
        assert action["property"] == "extra.target_temp"
        assert action["value"] < 28.0
        assert action["value"] >= 22.0  # comfort low bound

    def test_hvac_agent_heats_when_cold(self):
        """Room at 18°C with AC in heat mode should raise target."""
        world = _make_hvac_world(room_temp=18.0, mode="heat", target_temp=24.0)
        agent = HVACAgent()
        actions = agent.decide(world)

        assert len(actions) == 1
        action = actions[0]
        assert action["property"] == "extra.target_temp"
        assert action["value"] > 18.0
        assert action["value"] <= 28.0

    def test_hvac_agent_no_action_when_empty(self):
        """No HVAC action when room is unoccupied."""
        world = _make_hvac_world(room_temp=30.0, mode="cool", target_temp=24.0, occupied=False)
        agent = HVACAgent()
        actions = agent.decide(world)
        assert len(actions) == 0

    def test_hvac_agent_no_action_in_comfort_zone(self):
        """No action when temperature is within comfort zone."""
        world = _make_hvac_world(room_temp=24.0, mode="cool", target_temp=24.0)
        agent = HVACAgent()
        actions = agent.decide(world)
        assert len(actions) == 0
