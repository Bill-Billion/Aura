import pytest
from backend.engine.event_bus import SimEvent
from backend.engine.state import WorldState, DeviceState, DeviceStateValues, Location3D, RoomState, UserState
from backend.engine.state_manager import (
    InvariantViolation,
    StateManager,
    DeltaChange,
    verify_world_invariants,
)
from backend.execution.command import (
    LIFECYCLE_EVENT_TYPE,
    CommandSource,
    CommandStatus,
    DeviceCommand,
)
from backend.execution.executor import (
    ACTION_EVENT_TYPE,
    COMMAND_FAILED_EVENT_TYPE,
    FEEDBACK_EVENT_TYPE,
    INVARIANT_VIOLATION_ERROR_CODE,
    INVARIANT_VIOLATION_EVENT_TYPE,
    CommandExecutor,
)
from backend.models.schemas import WSMessage, SimCommand


def _make_world() -> WorldState:
    ws = WorldState()
    ws.rooms["living_room"] = RoomState(id="living_room", temperature=22.0)
    ws.users["user_01"] = UserState(
        id="user_01",
        name="User",
        location=Location3D(room="living_room"),
        activity="idle",
    )
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


def test_apply_path_update_supports_root_and_room_paths():
    mgr = StateManager(_make_world())

    deltas = mgr.apply_path_update(
        caused_by="simulator_timer",
        path="rooms[living_room].temperature",
        new_value=24.5,
        reason="environment refresh",
    )
    deltas += mgr.apply_path_update(
        caused_by="simulator_timer",
        path="simulation_tick",
        new_value=3,
        reason="timer tick",
    )

    assert mgr.world.rooms["living_room"].temperature == 24.5
    assert mgr.world.simulation_tick == 3
    assert deltas[0].path == "rooms[living_room].temperature"
    assert deltas[1].path == "simulation_tick"


def test_apply_updates_batches_multiple_paths():
    mgr = StateManager(_make_world())

    deltas = mgr.apply_updates(
        caused_by="user_behavior_sim",
        updates=[
            ("users[user_01].activity", "breakfast"),
            ("environment.time_of_day", "08:00"),
        ],
        reason="apply user event",
    )

    assert len(deltas) == 2
    assert mgr.world.users["user_01"].activity == "breakfast"
    assert mgr.world.environment.time_of_day == "08:00"


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


# -------------------------------------------------------------------
# S1-T6 §2.2 状态不变式
# -------------------------------------------------------------------


def _make_invariant_world() -> WorldState:
    """living_room 一致（persons↔user.location↔occupancy），另带离线风扇与只读传感器。"""

    world = WorldState()
    world.rooms = {"living_room": RoomState(id="living_room", light_level=300.0)}
    world.users = {
        "user_01": UserState(
            id="user_01", name="User", location=Location3D(room="living_room")
        )
    }
    world.rooms["living_room"].persons = ["user_01"]
    world.rooms["living_room"].occupancy = True
    world.devices = {
        "light_living": DeviceState(
            id="light_living",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(power=False, extra={"brightness": 60}),
        ),
        "fan_living": DeviceState(
            id="fan_living",
            type="fan",
            location=Location3D(room="living_room"),
            capabilities=["power", "speed"],
            state=DeviceStateValues(power=False, extra={"online": False, "speed": "low"}),
        ),
        "sensor_living": DeviceState(
            id="sensor_living",
            type="sensor",
            location=Location3D(room="living_room"),
            capabilities=["read"],
            state=DeviceStateValues(extra={"read": 21.5}),
        ),
    }
    return world


def _collector():
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    return events, publish


@pytest.mark.anyio
async def test_offline_device_writable_command_rejected_via_executor():
    """§2.2：online=false 的设备不接受可写命令——状态层守卫抛不变式，命令路径世界零变更。"""

    mgr = StateManager(_make_invariant_world())
    fan = mgr.world.devices["fan_living"]

    # 状态层守卫：即便调用方绕过入口校验，apply 前这一关也拦得住。
    with pytest.raises(InvariantViolation) as excinfo:
        mgr.check_command_invariants(fan, "power", caused_by="agent")
    assert excinfo.value.invariant == "offline_device_write"

    events, publish = _collector()
    executor = CommandExecutor(mgr, publish)
    record = await executor.submit(
        DeviceCommand(
            source=CommandSource.UI,
            device_id="fan_living",
            capability="power",
            value=True,
        )
    )

    assert record.status is CommandStatus.FAILED
    assert mgr.world.devices["fan_living"].state.power is False
    assert not [e for e in events if e.event_type == FEEDBACK_EVENT_TYPE]


def test_agent_write_to_sensor_read_rejected_but_simulator_write_allowed():
    """§2.2 第6条：只读能力只允许仿真源写入，agent/UI 一律拒。"""

    mgr = StateManager(_make_invariant_world())
    sensor = mgr.world.devices["sensor_living"]

    for caused_by in ("agent", "user_ui", "ui", "scenario"):
        with pytest.raises(InvariantViolation) as excinfo:
            mgr.check_command_invariants(sensor, "read", caused_by=caused_by)
        assert excinfo.value.invariant == "read_only_capability_write"

    # 仿真引擎写只读能力是合法的（传感器读数本就由仿真产生）。
    for caused_by in ("environment_sim", "user_behavior_sim", "simulator_timer", "system"):
        mgr.check_command_invariants(sensor, "read", caused_by=caused_by)


@pytest.mark.anyio
async def test_invariant_violation_emits_structured_failure_event_and_fails_command():
    """世界级不变式被破坏 → 回滚已落地 delta + system.invariant_violation + 命令 failed。"""

    mgr = StateManager(_make_invariant_world())

    def corrupting_effect(state_manager, command, deltas):
        # 模拟一个把世界改坏的派生 effect：占用位与 persons 脱钩（§2.2 occupancy 可推导）。
        return state_manager.apply_path_update(
            caused_by="broken_effect",
            path="rooms[living_room].persons",
            new_value=[],
            reason="corrupt world on purpose",
        )

    events, publish = _collector()
    executor = CommandExecutor(mgr, publish, effects=corrupting_effect)
    record = await executor.submit(
        DeviceCommand(
            source=CommandSource.UI,
            device_id="light_living",
            capability="power",
            value=True,
            correlation_id="corr-inv",
            causal_parent="root-inv",
        )
    )

    assert record.status is CommandStatus.FAILED
    assert record.failure_code == INVARIANT_VIOLATION_ERROR_CODE

    violations = [e for e in events if e.event_type == INVARIANT_VIOLATION_EVENT_TYPE]
    assert len(violations) == 1
    violation = violations[0]
    assert violation.data["invariant"] == "occupancy_derivable_from_persons"
    assert violation.data["device_id"] == "light_living"
    assert violation.data["command_id"] == record.command.command_id
    assert violation.correlation_id == "corr-inv"
    action_event = next(e for e in events if e.event_type == ACTION_EVENT_TYPE)
    assert violation.causal_parent == action_event.event_id
    # 纠正（回滚）必须留痕，不允许静默纠正。
    assert "rooms[living_room].persons" in violation.data["reverted_paths"]
    assert f"devices[light_living].state.power" in violation.data["reverted_paths"]

    failed = [e for e in events if e.event_type == COMMAND_FAILED_EVENT_TYPE]
    assert len(failed) == 1
    assert failed[0].data["error_code"] == INVARIANT_VIOLATION_ERROR_CODE

    # 世界回到命令之前：既不落地半成品，也不留下被改坏的状态。
    assert mgr.world.devices["light_living"].state.power is False
    assert mgr.world.rooms["living_room"].persons == ["user_01"]
    assert mgr.world.rooms["living_room"].occupancy is True
    verify_world_invariants(mgr.world)
    # 违规命令不产生反馈事件（评估器不会把它当成生效变更）。
    assert not [e for e in events if e.event_type == FEEDBACK_EVENT_TYPE]


@pytest.mark.anyio
async def test_delta_carries_caused_by_event_id():
    """§2.2 第7条：所有 mutation 可归因到 SimEvent——delta 带 action 事件 id。"""

    mgr = StateManager(_make_invariant_world())
    events, publish = _collector()
    executor = CommandExecutor(mgr, publish)

    record = await executor.submit(
        DeviceCommand(
            source=CommandSource.UI,
            device_id="light_living",
            capability="power",
            value=True,
        )
    )

    assert record.status is CommandStatus.SUCCEEDED
    action_event = next(e for e in events if e.event_type == ACTION_EVENT_TYPE)
    feedbacks = [e for e in events if e.event_type == FEEDBACK_EVENT_TYPE]
    assert feedbacks
    # 直控 delta 与派生 effect delta（房间光照）都必须可归因。
    assert {e.data["path"] for e in feedbacks} >= {
        "devices[light_living].state.power",
        "rooms[living_room].light_level",
    }
    for feedback in feedbacks:
        assert feedback.data["caused_by_event_id"] == action_event.event_id


def test_verify_world_invariants_detects_user_in_two_rooms():
    world = _make_invariant_world()
    world.rooms["kitchen"] = RoomState(id="kitchen", persons=["user_01"], occupancy=True)

    with pytest.raises(InvariantViolation) as excinfo:
        verify_world_invariants(world)
    assert excinfo.value.invariant == "user_single_location"


def test_verify_world_invariants_detects_person_location_mismatch():
    world = _make_invariant_world()
    world.users["user_01"].location = Location3D(room="kitchen")

    with pytest.raises(InvariantViolation) as excinfo:
        verify_world_invariants(world)
    assert excinfo.value.invariant == "person_location_matches_room"


def test_verify_world_invariants_detects_device_in_unregistered_room():
    world = _make_invariant_world()
    world.devices["light_living"].location = Location3D(room="ghost_room")

    with pytest.raises(InvariantViolation) as excinfo:
        verify_world_invariants(world)
    assert excinfo.value.invariant == "device_single_room"


def test_revert_restores_state_in_reverse_order():
    mgr = StateManager(_make_invariant_world())
    deltas = mgr.apply_action(
        agent_id="agent-1",
        device_id="light_living",
        property_path="power",
        new_value=True,
    )

    reverted = mgr.revert(deltas)

    assert mgr.world.devices["light_living"].state.power is False
    assert mgr.world.devices["light_living"].state.last_changed_by == "system"
    assert reverted == [delta.path for delta in deltas]
