"""Tests for EnvironmentSimulator."""

import pytest

from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    WorldState,
)
from backend.simulators.environment import EnvironmentSimulator


def _make_world(
    *,
    room_temp: float = 30.0,
    outdoor_temp: float = 35.0,
    ac_mode: str | None = "cool",
    ac_target: float = 24.0,
    ac_power: bool = True,
    light_brightness: float = 0.0,
    light_power: bool = False,
    curtain_open: float = 100.0,
    time_of_day: str = "12:00",
) -> WorldState:
    """Build a minimal WorldState for testing."""
    world = WorldState(environment=EnvironmentState(
        time_of_day=time_of_day,
        outdoor_temp=outdoor_temp,
    ))
    world.rooms = {
        "living_room": RoomState(id="living_room", temperature=room_temp),
    }

    devices = {}
    if ac_mode is not None:
        devices["ac_01"] = DeviceState(
            id="ac_01",
            type="hvac",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=ac_power,
                extra={"target_temp": ac_target, "mode": ac_mode},
            ),
        )
    if light_brightness > 0 or light_power:
        devices["light_01"] = DeviceState(
            id="light_01",
            type="light",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=light_power,
                extra={"brightness": light_brightness},
            ),
        )
    devices["curtain_01"] = DeviceState(
        id="curtain_01",
        type="curtain",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(
            power=True,
            extra={"open_percent": curtain_open},
        ),
    )
    world.devices = devices
    return world


class TestTemperature:
    def test_temperature_towards_target(self):
        """AC cooling should move room temperature toward the target."""
        world = _make_world(room_temp=30.0, ac_mode="cool", ac_target=24.0)
        sim = EnvironmentSimulator()

        for _ in range(200):
            sim.step(world, dt=1.0)

        # Temperature should have decreased significantly toward 24
        assert world.rooms["living_room"].temperature < 28.0
        # And should still be above target (asymptotic approach)
        assert world.rooms["living_room"].temperature > 24.0

    def test_outdoor_heat_diffusion(self):
        """With AC off, room temp should drift toward outdoor temp."""
        world = _make_world(
            room_temp=20.0,
            outdoor_temp=35.0,
            ac_mode=None,  # no AC
        )
        sim = EnvironmentSimulator()

        for _ in range(100):
            sim.step(world, dt=1.0)

        # Should have warmed toward outdoor temp
        assert world.rooms["living_room"].temperature > 20.0
        # But not reached it yet (slow diffusion)
        assert world.rooms["living_room"].temperature < 35.0


class TestLightLevel:
    def test_light_level_with_curtain(self):
        """Light level = artificial + natural * curtain_pct/100."""
        # Noon, brightness=50, curtain 50% open
        world = _make_world(
            light_brightness=50.0,
            light_power=True,
            curtain_open=50.0,
            time_of_day="12:00",
        )
        sim = EnvironmentSimulator()
        sim.step(world, dt=1.0)

        room = world.rooms["living_room"]
        # Artificial: 50 * 8 = 400 lux
        # Natural at noon (peak): 500 * 0.5 = 250 lux
        expected = 50.0 * 8.0 + 500.0 * 0.5
        assert abs(room.light_level - expected) < 1.0

    def test_no_light_at_night(self):
        """At night, no natural light contribution."""
        world = _make_world(
            light_brightness=0.0,
            light_power=False,
            curtain_open=100.0,
            time_of_day="22:00",
        )
        sim = EnvironmentSimulator()
        sim.step(world, dt=1.0)

        assert world.rooms["living_room"].light_level == 0.0
