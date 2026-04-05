import pytest
from backend.engine.state import WorldState, DeviceState, DeviceStateValues, Location3D
from backend.engine.state_manager import StateManager, DeltaChange
from backend.models.schemas import WSMessage, SimCommand


def _make_world() -> WorldState:
    ws = WorldState()
    ws.devices["light-001"] = DeviceState(
        id="light-001",
        type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=False, extra={"brightness": 0}),
    )
    return ws


# -------------------------------------------------------------------
# apply_action
# -------------------------------------------------------------------


def test_apply_action_produces_delta():
    mgr = StateManager(_make_world())

    deltas = mgr.apply_action(
        agent_id="agent-1",
        device_id="light-001",
        property_path="power",
        new_value=True,
        reason="user requested light on",
    )

    assert len(deltas) >= 1
    main_delta = [d for d in deltas if d.path.endswith("power")][0]
    assert main_delta.old_value is False
    assert main_delta.new_value is True
    assert main_delta.caused_by == "agent-1"
    assert main_delta.reason == "user requested light on"

    # Verify state actually changed
    assert mgr.world.devices["light-001"].state.power is True


def test_apply_action_nested_path():
    mgr = StateManager(_make_world())

    deltas = mgr.apply_action(
        agent_id="agent-2",
        device_id="light-001",
        property_path="extra.brightness",
        new_value=75,
        reason="adjust brightness",
    )

    brightness_delta = [d for d in deltas if "brightness" in d.path][0]
    assert brightness_delta.old_value == 0
    assert brightness_delta.new_value == 75
    assert mgr.world.devices["light-001"].state.extra["brightness"] == 75


def test_apply_action_no_delta_when_same_value():
    mgr = StateManager(_make_world())

    deltas = mgr.apply_action(
        agent_id="agent-1",
        device_id="light-001",
        property_path="power",
        new_value=False,  # same as current
    )

    assert len(deltas) == 0


def test_apply_action_unknown_device_raises():
    mgr = StateManager(_make_world())
    with pytest.raises(KeyError, match="nonexistent"):
        mgr.apply_action("agent-1", "nonexistent", "power", True)


# -------------------------------------------------------------------
# get_full_snapshot
# -------------------------------------------------------------------


def test_get_full_snapshot():
    mgr = StateManager(_make_world())
    snap = mgr.get_full_snapshot()

    assert isinstance(snap, dict)
    assert snap["simulation_tick"] == 0
    assert "light-001" in snap["devices"]
    assert snap["devices"]["light-001"]["state"]["power"] is False

    # Mutating the snapshot dict should not affect the manager
    snap["simulation_tick"] = 999
    assert mgr.world.simulation_tick == 0


# -------------------------------------------------------------------
# DeltaChange serialization
# -------------------------------------------------------------------


def test_delta_serialization():
    delta = DeltaChange(
        path="devices.light-001.state.power",
        old_value=False,
        new_value=True,
        caused_by="agent-1",
        reason="user request",
    )

    data = delta.model_dump()
    assert data["path"] == "devices.light-001.state.power"
    assert data["old_value"] is False
    assert data["new_value"] is True

    # Round-trip
    restored = DeltaChange.model_validate(data)
    assert restored == delta


# -------------------------------------------------------------------
# Schema models
# -------------------------------------------------------------------


def test_ws_message_defaults():
    msg = WSMessage(type="state_update", payload={"tick": 5})
    assert msg.type == "state_update"
    assert msg.id  # auto-generated
    assert msg.timestamp > 0
    assert msg.payload["tick"] == 5


def test_sim_command_literal():
    cmd = SimCommand(command="start", params={})
    assert cmd.command == "start"

    cmd2 = SimCommand(command="set_speed", params={"speed": 2.0})
    assert cmd2.params["speed"] == 2.0
