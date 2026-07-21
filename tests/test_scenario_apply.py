"""S2-T5：ScenarioSpec.initial_state → 世界的确定性覆盖（§5.2 + §2.2 可归因）。

承重断言：
  - 每一条初始状态覆盖都产生一条 caused_by="scenario_loader" 的 DeltaChange
    （§2.2 第7条"所有变更可归因"；直接 setattr 世界=一个没有出处的起始状态）；
  - 应用顺序与声明顺序无关（同内容不同 YAML 键序 → 同一串 delta），
    否则 §11 的 initial_state_hash 会因为作者手滑换行而漂移；
  - 应用完成后世界仍满足 §2.2 不变式（occupancy↔persons、用户单一位置）。
"""

from __future__ import annotations

import pytest

from backend.config.device_registry import build_default_devices, build_default_rooms
from backend.engine.state import Location3D, UserState, WorldState
from backend.engine.state_manager import StateManager, verify_world_invariants
from backend.scenarios.apply import (
    SCENARIO_INITIAL_STATE_CAUSED_BY,
    STATIC_ROOM_FIELDS,
    InitialStateApplyError,
    InitialStateApplyErrorCode,
    apply_initial_state,
    apply_scenario_initial_state,
)
from backend.scenarios.spec import InitialState, ScenarioSpec


def _make_state_manager() -> StateManager:
    world = WorldState(scene_id="apartment_v1")
    world.rooms = build_default_rooms()
    world.devices = build_default_devices()
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="living_room"),
            activity="idle",
        )
    }
    world.rooms["living_room"].occupancy = True
    world.rooms["living_room"].persons = ["user_01"]
    return StateManager(world)


# --------------------------------------------------------------- 可归因 + 确定性


def test_initial_state_overrides_produce_attributable_deltas():
    manager = _make_state_manager()
    initial = InitialState(
        time_of_day="18:30",
        weather="rainy",
        outdoor_temp=12.0,
        rooms={"bedroom": {"temperature": 19.5, "light_level": 40.0}},
        devices={"light_living_01": {"state": {"power": False, "extra": {"brightness": 20}}}},
    )

    result = apply_initial_state(manager, initial)

    assert result.deltas, "initial_state 覆盖必须产生 delta，而不是静默 setattr"
    assert {delta.caused_by for delta in result.deltas} == {SCENARIO_INITIAL_STATE_CAUSED_BY}
    assert all(delta.reason for delta in result.deltas)

    world = manager.world
    assert world.environment.time_of_day == "18:30"
    assert world.environment.weather == "rainy"
    assert world.environment.outdoor_temp == 12.0
    assert world.rooms["bedroom"].temperature == 19.5
    assert world.rooms["bedroom"].light_level == 40.0
    assert world.devices["light_living_01"].state.power is False
    assert world.devices["light_living_01"].state.extra["brightness"] == 20

    paths = {delta.path for delta in result.deltas}
    assert "environment.time_of_day" in paths
    assert "rooms[bedroom].temperature" in paths
    assert "devices[light_living_01].state.extra.brightness" in paths


def test_apply_is_deterministic_regardless_of_declaration_order():
    """同内容、不同键序 → 同一串 delta（路径 + 新值都一致）。"""

    payload_a = {
        "rooms": {"bedroom": {"temperature": 19.0}, "kitchen": {"temperature": 21.0}},
        "devices": {
            "light_living_01": {"state": {"extra": {"brightness": 30, "color_temp": 3000}}},
            "light_kitchen_01": {"state": {"power": True}},
        },
    }
    payload_b = {
        "devices": {
            "light_kitchen_01": {"state": {"power": True}},
            "light_living_01": {"state": {"extra": {"color_temp": 3000, "brightness": 30}}},
        },
        "rooms": {"kitchen": {"temperature": 21.0}, "bedroom": {"temperature": 19.0}},
    }

    result_a = apply_initial_state(_make_state_manager(), InitialState.model_validate(payload_a))
    result_b = apply_initial_state(_make_state_manager(), InitialState.model_validate(payload_b))

    assert [(d.path, d.new_value) for d in result_a.deltas] == [
        (d.path, d.new_value) for d in result_b.deltas
    ]


def test_apply_twice_is_idempotent():
    manager = _make_state_manager()
    initial = InitialState(rooms={"bedroom": {"temperature": 19.5}})

    first = apply_initial_state(manager, initial)
    second = apply_initial_state(manager, initial)

    assert first.deltas
    assert second.deltas == ()


# ------------------------------------------------------------------- 用户与房间


def test_user_location_moves_persons_and_derives_occupancy():
    manager = _make_state_manager()
    initial = InitialState(users={"user_01": {"location": "bedroom", "activity": "sleeping"}})

    apply_initial_state(manager, initial)

    world = manager.world
    assert world.users["user_01"].location is not None
    assert world.users["user_01"].location.room == "bedroom"
    assert world.users["user_01"].activity == "sleeping"
    assert world.rooms["bedroom"].persons == ["user_01"]
    assert world.rooms["bedroom"].occupancy is True
    assert world.rooms["living_room"].persons == []
    assert world.rooms["living_room"].occupancy is False
    verify_world_invariants(world)


def test_user_outside_clears_location_and_persons():
    manager = _make_state_manager()

    apply_initial_state(manager, InitialState(users={"user_01": {"location": "outside"}}))

    world = manager.world
    assert world.users["user_01"].location is None
    assert world.rooms["living_room"].persons == []
    assert world.rooms["living_room"].occupancy is False
    verify_world_invariants(world)


def test_presence_state_away_evicts_user_from_rooms():
    manager = _make_state_manager()

    apply_initial_state(manager, InitialState(users={"user_01": {"presence_state": "away"}}))

    assert manager.world.users["user_01"].location is None
    assert manager.world.rooms["living_room"].persons == []
    verify_world_invariants(manager.world)


def test_presence_state_conflicting_with_room_location_raises():
    manager = _make_state_manager()
    initial = InitialState(users={"user_01": {"location": "bedroom", "presence_state": "away"}})

    with pytest.raises(InitialStateApplyError) as exc:
        apply_initial_state(manager, initial)

    assert exc.value.code is InitialStateApplyErrorCode.PRESENCE_LOCATION_CONFLICT


def test_unknown_user_is_created():
    manager = _make_state_manager()

    result = apply_initial_state(
        manager, InitialState(users={"user_02": {"location": "kitchen"}})
    )

    assert result.created_users == ("user_02",)
    assert manager.world.users["user_02"].location.room == "kitchen"
    assert manager.world.rooms["kitchen"].persons == ["user_02"]
    verify_world_invariants(manager.world)


def test_explicit_occupancy_contradicting_persons_raises():
    manager = _make_state_manager()
    initial = InitialState(rooms={"bedroom": {"occupancy": True}})

    with pytest.raises(InitialStateApplyError) as exc:
        apply_initial_state(manager, initial)

    assert exc.value.code is InitialStateApplyErrorCode.OCCUPANCY_CONFLICT
    assert "bedroom" in str(exc.value)


def test_explicit_occupancy_consistent_with_users_is_accepted():
    manager = _make_state_manager()
    initial = InitialState(
        users={"user_01": {"location": "bedroom"}},
        rooms={"bedroom": {"occupancy": True}, "living_room": {"occupancy": False}},
    )

    apply_initial_state(manager, initial)

    assert manager.world.rooms["bedroom"].occupancy is True
    verify_world_invariants(manager.world)


# --------------------------------------------------------------------- 未知引用


def test_unknown_room_id_raises():
    manager = _make_state_manager()

    with pytest.raises(InitialStateApplyError) as exc:
        apply_initial_state(manager, InitialState(rooms={"ghost_room": {"temperature": 20.0}}))

    assert exc.value.code is InitialStateApplyErrorCode.UNKNOWN_ROOM_ID


def test_unknown_device_id_raises():
    manager = _make_state_manager()

    with pytest.raises(InitialStateApplyError) as exc:
        apply_initial_state(
            manager, InitialState(devices={"ghost_device": {"state": {"power": True}}})
        )

    assert exc.value.code is InitialStateApplyErrorCode.UNKNOWN_DEVICE_ID
    assert exc.value.to_dict()["code"] == "unknown_device_id"


def test_unknown_user_location_raises():
    manager = _make_state_manager()

    with pytest.raises(InitialStateApplyError) as exc:
        apply_initial_state(manager, InitialState(users={"user_01": {"location": "atlantis"}}))

    assert exc.value.code is InitialStateApplyErrorCode.UNKNOWN_USER_LOCATION


# ----------------------------------------------------------- 设备 extra 新键 / 离线


def test_missing_extra_key_is_created_with_an_explicit_delta():
    """场景把一台灯标成离线：extra.online 在注册表默认里并不存在，不能静默跳过。"""

    manager = _make_state_manager()
    assert "online" not in manager.world.devices["light_living_01"].state.extra

    result = apply_initial_state(
        manager,
        InitialState(devices={"light_living_01": {"state": {"extra": {"online": False}}}}),
    )

    assert manager.world.devices["light_living_01"].state.extra["online"] is False
    delta = next(
        d for d in result.deltas if d.path == "devices[light_living_01].state.extra.online"
    )
    assert delta.old_value is None
    assert delta.new_value is False


# --------------------------------------- 审计发现①已修：room.humidity 不再是冻结值


def test_room_humidity_is_writable_and_now_evolves():
    """room.humidity 可由场景设定，且**已经不再是冻结值**（S2-T6 修审计发现①）。

    此前 EnvironmentSimulator 只写 environment.outdoor_humidity，房间湿度自
    device_registry 默认值起终生不变——§6.7 雨天场景"室外下雨→室内变潮"在数据上不成立。
    现在它随室外湿度扩散，因此从 STATIC_ROOM_FIELDS 里摘除；元组保留为空是刻意的
    （下一个"写得进却不演化"的字段仍要登记在那里）。
    """

    from backend.simulators.environment import EnvironmentSimulator

    assert "humidity" not in STATIC_ROOM_FIELDS

    manager = _make_state_manager()
    result = apply_initial_state(manager, InitialState(rooms={"bathroom": {"humidity": 0.92}}))

    assert manager.world.rooms["bathroom"].humidity == 0.92
    assert "rooms[bathroom].humidity" in {delta.path for delta in result.deltas}
    assert result.static_fields == ()

    # 仿真器确实在演化它：08:00 是 clear 天气（室外湿度 0.45），室内 0.92 应朝它移动
    manager.world.environment.time_of_day = "08:00"
    updates = EnvironmentSimulator().step(manager.world, dt=600.0)
    next_humidity = updates["rooms[bathroom].humidity"]
    assert 0.45 < next_humidity < 0.92
    assert updates["environment.outdoor_humidity"] == 0.45


# ------------------------------------------------------------- ScenarioSpec 入口


def _minimal_spec(**overrides) -> ScenarioSpec:
    payload = {
        "id": "apply_smoke",
        "name": "apply smoke",
        "description": "S2-T5 initial_state 应用冒烟",
        "seed": 7,
        "initial_state": {
            "time_of_day": "07:30",
            "users": {"user_01": {"location": "bedroom"}},
            "devices": {"light_bedroom_01": {"state": {"power": True}}},
        },
        "timeline": [],
        "expected_device_effects": [
            {"device_id": "light_bedroom_01", "expected": {"power": True}}
        ],
        "involved_agents": ["lighting_agent"],
        "success_criteria": {},
    }
    payload.update(overrides)
    return ScenarioSpec.model_validate(payload)


def test_apply_scenario_initial_state_uses_the_spec_initial_state():
    manager = _make_state_manager()

    result = apply_scenario_initial_state(manager, _minimal_spec())

    assert manager.world.environment.time_of_day == "07:30"
    assert manager.world.users["user_01"].location.room == "bedroom"
    assert manager.world.devices["light_bedroom_01"].state.power is True
    assert all(delta.caused_by == SCENARIO_INITIAL_STATE_CAUSED_BY for delta in result.deltas)
    verify_world_invariants(manager.world)


def test_init_default_state_applies_a_scenario_initial_state():
    """main._init_default_state 是"默认世界"的唯一构造点；场景 runner 要能一步拿到
    "默认世界 + 场景覆盖"，否则每个调用方都得自己记得补一次 apply。"""

    from backend.main import _init_default_state

    manager = _init_default_state(
        InitialState(
            time_of_day="06:15",
            users={"user_01": {"location": "bedroom"}},
            devices={"light_bedroom_01": {"state": {"power": True}}},
        )
    )

    assert manager.world.environment.time_of_day == "06:15"
    assert manager.world.users["user_01"].location.room == "bedroom"
    assert manager.world.rooms["bedroom"].occupancy is True
    assert manager.world.rooms["living_room"].occupancy is False
    assert manager.world.devices["light_bedroom_01"].state.power is True
    verify_world_invariants(manager.world)


def test_init_default_state_without_scenario_is_unchanged():
    from backend.main import _init_default_state

    # agents 由 main 额外登记，不属于 initial_state 的作用域，故不参与比较。
    fields = {"environment", "devices", "rooms", "users", "scene_id"}
    assert _init_default_state().world.model_dump(include=fields) == (
        _make_state_manager().world.model_dump(include=fields)
    )


def test_caused_by_event_id_is_threaded_into_every_delta():
    manager = _make_state_manager()

    result = apply_initial_state(
        manager,
        InitialState(rooms={"kitchen": {"temperature": 20.0}}),
        caused_by_event_id="evt-scenario-load",
    )

    assert result.deltas
    assert {d.caused_by_event_id for d in result.deltas} == {"evt-scenario-load"}
