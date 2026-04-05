import json
import pytest
from backend.engine.state import (
    Location3D,
    DeviceState,
    DeviceStateValues,
    RoomState,
    EnvironmentState,
    AgentRuntimeState,
    UserState,
    WorldState,
)


def test_device_state_creation():
    device = DeviceState(
        id="light-001",
        type="light",
        location=Location3D(room="living_room", x=1.0, y=2.0, z=0.5),
        state=DeviceStateValues(power=True, last_changed_by="agent-1"),
    )
    assert device.id == "light-001"
    assert device.type == "light"
    assert device.location.room == "living_room"
    assert device.location.x == 1.0
    assert device.state.power is True
    assert device.state.last_changed_by == "agent-1"


def test_world_state_defaults():
    ws = WorldState()
    assert ws.simulation_tick == 0
    assert ws.simulation_speed == 1.0
    assert ws.is_running is False
    assert ws.scene_id == ""
    assert isinstance(ws.environment, EnvironmentState)
    assert ws.devices == {}
    assert ws.rooms == {}
    assert ws.agents == {}
    assert ws.users == {}


def test_world_state_snapshot_isolation():
    ws = WorldState()
    ws.devices["light-001"] = DeviceState(
        id="light-001",
        type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=False),
    )
    ws.rooms["living_room"] = RoomState(id="living_room", temperature=22.0)

    snap = ws.snapshot()

    # Mutate snapshot — original should be unaffected
    snap.devices["light-001"].state.power = True
    snap.rooms["living_room"].temperature = 99.0
    snap.simulation_tick = 42

    assert ws.devices["light-001"].state.power is False
    assert ws.rooms["living_room"].temperature == 22.0
    assert ws.simulation_tick == 0


def test_world_state_serialization_roundtrip():
    ws = WorldState(
        simulation_tick=5,
        is_running=True,
        scene_id="scene-morning",
        environment=EnvironmentState(
            time_of_day="07:30",
            outdoor_temp=18.0,
            weather="cloudy",
        ),
        devices={
            "hvac-001": DeviceState(
                id="hvac-001",
                type="hvac",
                location=Location3D(room="bedroom"),
                state=DeviceStateValues(power=True, extra={"target_temp": 22}),
            ),
        },
        rooms={
            "bedroom": RoomState(id="bedroom", temperature=20.0, occupancy=True),
        },
        agents={
            "agent-1": AgentRuntimeState(id="agent-1", name="ClimateAgent", status="active"),
        },
        users={
            "user-1": UserState(
                id="user-1",
                name="Alice",
                location=Location3D(room="bedroom"),
                activity="sleeping",
            ),
        },
    )

    # Serialize to JSON and back
    json_str = ws.model_dump_json()
    restored = WorldState.model_validate_json(json_str)

    assert restored.simulation_tick == 5
    assert restored.is_running is True
    assert restored.scene_id == "scene-morning"
    assert restored.environment.time_of_day == "07:30"
    assert restored.environment.weather == "cloudy"
    assert "hvac-001" in restored.devices
    assert restored.devices["hvac-001"].state.extra["target_temp"] == 22
    assert restored.rooms["bedroom"].occupancy is True
    assert restored.agents["agent-1"].name == "ClimateAgent"
    assert restored.users["user-1"].activity == "sleeping"

    # Also verify dict roundtrip
    data = ws.model_dump()
    restored2 = WorldState.model_validate(data)
    assert restored2.simulation_tick == 5
