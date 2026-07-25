"""S3-T2：§7 事件→设备映射作为**数据**（不是 agent 里的分支）。

审计发现（2026-07-16 codex 复核 §六）：§7 那张表现在散在 lighting.py / hvac.py 的
``_focus_rooms`` / ``get_relevant_devices`` 分支里，改一行"到家该不该动窗帘"要改代码。
本测试锁住三件事：

1. §7 八行原文逐行落在 YAML 里（primary / secondary / default_policy 三列齐全）；
2. 键支持活动限定（``user.starts_activity:sleeping``），未知活动回落基础事件行；
3. 表是**数据**——只改 YAML、零改代码，搜索空间就跟着变（数据驱动的证据）。

以及一条硬边界：表里出现设备注册表不认识的设备类型 / 能力 / 房间，必须是**加载期报错**，
不是运行期静默漏配（"没人响应"比"多响应一次"难查得多）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from backend.config.event_mapping import (
    DEFAULT_MAPPING_PATH,
    DeviceSearchSpace,
    EventDeviceMapping,
    EventMappingError,
    EventMappingErrorCode,
    get_device_search_space,
    get_event_device_mapping,
    load_event_device_mapping,
)
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES


def make_event(event_type: str, **data) -> SimEvent:
    return SimEvent(event_type=event_type, source="test", timestamp=0.0, data=dict(data))


def write_mapping(tmp_path: Path, raw: dict) -> Path:
    path = tmp_path / "event_device_mapping.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def base_raw() -> dict:
    """默认表的可变副本——变异测试在它上面动一行。"""

    return yaml.safe_load(DEFAULT_MAPPING_PATH.read_text(encoding="utf-8"))


# —— 1. §7 八行原文 ————————————————————————————————————————————————

SPEC_7_ROWS = {
    "user.arrives_home": (
        ("light", "hvac"),
        ("curtain", "camera"),
        "comfort and presence transition",
    ),
    "user.leaves_home": (
        ("light", "hvac", "fan", "camera"),
        ("curtain",),
        "energy saving and security",
    ),
    "user.starts_activity:sleeping": (
        ("light", "hvac", "curtain"),
        ("camera",),
        "sleep comfort and quiet security",
    ),
    "user.starts_activity:cooking": (
        ("light", "sensor"),
        ("fan",),
        "task lighting and air monitoring",
    ),
    "environment.temperature_threshold": (
        ("hvac", "fan"),
        ("curtain", "sensor"),
        "comfort if occupied, energy if empty",
    ),
    "environment.light_level_threshold": (
        ("light", "curtain"),
        ("sensor",),
        "preserve target light level",
    ),
    "security.presence_detected": (
        ("camera", "light"),
        ("sensor",),
        "security first",
    ),
}


def test_all_eight_spec_7_rows_present_with_primary_secondary_policy():
    mapping = get_event_device_mapping()

    # §7 共八行；其中 device.offline 那行的设备面由事件现场决定（"affected device"），
    # 因此它在表里以 dynamic 行登记，不写死 primary/secondary。
    assert set(mapping.spec_row_keys()) == set(SPEC_7_ROWS) | {"device.offline"}
    assert len(mapping.spec_row_keys()) == 8

    for key, (primary, secondary, policy) in SPEC_7_ROWS.items():
        row = mapping.rows[key]
        assert row.primary_device_types == primary, key
        assert row.secondary_device_types == secondary, key
        assert row.default_policy == policy, key

    offline = mapping.rows["device.offline"]
    assert offline.dynamic == "affected_device"
    assert offline.default_policy == "fail closed and explain"


def test_room_scoped_spec_rows_keep_their_room_qualifier():
    # §7 原文写的是 "bedroom lights" / "kitchen lights, sensors"——房间限定属于表里的一列，
    # 不是 agent 里的 _focus_rooms 分支。
    mapping = get_event_device_mapping()
    assert mapping.rows["user.starts_activity:sleeping"].room_scope == ("bedroom",)
    assert mapping.rows["user.starts_activity:cooking"].room_scope == ("kitchen",)


def test_every_4_1_root_event_type_has_a_row():
    mapping = get_event_device_mapping()
    missing = sorted(t for t in ALL_ROOT_EVENT_TYPES if t not in mapping.rows)
    assert missing == []


# —— 2. 键解析 ————————————————————————————————————————————————————

def test_activity_qualified_key_resolution():
    space = get_device_search_space(make_event("user.starts_activity", activity="sleeping"))
    assert space.event_key == "user.starts_activity:sleeping"
    assert space.resolved_from == "activity"
    assert space.primary == ("light", "hvac", "curtain")
    assert space.room_scope == ("bedroom",)

    # 未知活动 → 回落基础事件行，不报错、不空。
    unknown = get_device_search_space(make_event("user.starts_activity", activity="juggling"))
    assert unknown.event_key == "user.starts_activity"
    assert unknown.resolved_from == "base"
    assert unknown.device_types


def test_explicit_activity_argument_overrides_event_payload():
    event = make_event("user.starts_activity", activity="sleeping")
    space = get_device_search_space(event, activity="cooking")
    assert space.event_key == "user.starts_activity:cooking"
    assert space.room_scope == ("kitchen",)


def test_event_type_string_and_sim_event_resolve_identically():
    assert get_device_search_space("user.arrives_home") == get_device_search_space(
        make_event("user.arrives_home")
    )


def test_unknown_root_event_returns_empty_search_space_not_keyerror():
    space = get_device_search_space(make_event("totally.unknown_event"))
    assert isinstance(space, DeviceSearchSpace)
    assert space.resolved_from == "unmapped"
    assert space.is_empty
    assert space.primary == () and space.secondary == () and space.device_types == ()
    assert space.default_policy == ""


def test_device_offline_resolves_affected_device_and_alternatives():
    space = get_device_search_space(
        make_event("device.offline", device_id="light_living_01", device_type="light")
    )
    assert space.resolved_from == "dynamic"
    assert space.primary == ("light",)
    # §7「alternatives」：灯掉线时能补位的是别的灯 / 拉开窗帘补自然光。
    assert "curtain" in space.secondary
    assert space.default_policy == "fail closed and explain"

    # 说不出是哪台设备 → 谁也定位不了替代品，搜索面为空（而不是"全集"）。
    unresolved = get_device_search_space(make_event("device.offline"))
    assert unresolved.resolved_from == "dynamic_unresolved"
    assert unresolved.is_empty
    assert unresolved.default_policy == "fail closed and explain"


def test_search_space_helpers_are_deterministic_and_sorted():
    space = get_device_search_space(make_event("user.leaves_home"))
    assert space.device_types == tuple(sorted(space.device_types))
    assert set(space.device_types) == set(space.primary) | set(space.secondary)
    assert space.includes_device_type("camera")
    assert not space.includes_device_type("nonsense")
    # 同一输入两次调用完全相等（确定性门：不能有集合迭代序泄漏）。
    assert space == get_device_search_space(make_event("user.leaves_home"))


# —— 3. 数据驱动的证据：改 YAML、零改代码 ————————————————————————————

def test_mutated_yaml_changes_search_space_with_zero_code_change(tmp_path):
    """研究者改一行 YAML 就能改变"到家动哪些设备"，不用碰 agent 代码。

    （agent 侧的同一条断言由 S3-T3/T4 在改写 agent 后补，本任务不改 agent。）
    """

    raw = base_raw()
    assert "camera" in raw["events"]["user.arrives_home"]["secondary_device_types"]
    raw["events"]["user.arrives_home"]["secondary_device_types"] = ["sensor"]
    raw["events"]["user.arrives_home"]["default_policy"] = "researcher override"

    mapping = load_event_device_mapping(write_mapping(tmp_path, raw))
    space = get_device_search_space(make_event("user.arrives_home"), mapping=mapping)

    assert space.secondary == ("sensor",)
    assert space.default_policy == "researcher override"
    assert not space.includes_device_type("camera")

    # 默认表没被污染（loader 不共享可变状态）。
    assert get_device_search_space(make_event("user.arrives_home")).includes_device_type("camera")


# —— 4. 加载期校验：未知类型 / 能力 / 房间 / 事件 一律报错 ————————————————

def test_unknown_device_type_is_a_load_error(tmp_path):
    raw = base_raw()
    raw["events"]["user.arrives_home"]["primary_device_types"] = ["light", "toaster"]
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.UNKNOWN_DEVICE_TYPE
    assert "toaster" in str(exc.value)


def test_unknown_capability_is_a_load_error(tmp_path):
    raw = base_raw()
    raw["events"]["environment.light_level_threshold"]["capabilities"] = ["brightness", "warp_core"]
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.UNKNOWN_CAPABILITY


def test_capability_not_offered_by_any_declared_type_is_a_load_error(tmp_path):
    """能力名合法但那些设备类型压根没有它 —— 同样是静默漏配，必须拦。"""

    raw = base_raw()
    # shake 只有 fan 有；这一行声明的是 light/curtain/sensor。
    raw["events"]["environment.light_level_threshold"]["capabilities"] = ["shake"]
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.CAPABILITY_TYPE_MISMATCH


def test_unknown_room_is_a_load_error(tmp_path):
    raw = base_raw()
    raw["events"]["user.starts_activity:sleeping"]["room_scope"] = ["dungeon"]
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.UNKNOWN_ROOM_ID


def test_unknown_event_key_is_a_load_error(tmp_path):
    raw = base_raw()
    raw["events"]["user.arives_home"] = {  # 拼写错误
        "primary_device_types": ["light"],
        "default_policy": "typo",
    }
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.UNKNOWN_EVENT_TYPE


def test_missing_root_event_coverage_is_a_load_error(tmp_path):
    raw = base_raw()
    del raw["events"]["safety.smoke_detected"]
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.MISSING_EVENT_COVERAGE
    assert "safety.smoke_detected" in str(exc.value)


def test_unsupported_schema_version_is_a_load_error(tmp_path):
    raw = base_raw()
    raw["schema_version"] = 99
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.UNSUPPORTED_SCHEMA_VERSION


def test_unknown_row_field_is_a_load_error(tmp_path):
    raw = base_raw()
    raw["events"]["user.arrives_home"]["primary_devices"] = ["light"]  # 字段名写错
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(write_mapping(tmp_path, raw))
    assert exc.value.code is EventMappingErrorCode.INVALID_ROW


def test_missing_file_is_a_load_error(tmp_path):
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(tmp_path / "nope.yaml")
    assert exc.value.code is EventMappingErrorCode.FILE_NOT_FOUND


def test_non_mapping_document_is_a_load_error(tmp_path):
    path = tmp_path / "event_device_mapping.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(EventMappingError) as exc:
        load_event_device_mapping(path)
    assert exc.value.code is EventMappingErrorCode.NOT_A_MAPPING


# —— 5. 加载器可被 agent 直接消费 ————————————————————————————————————

def test_default_mapping_is_cached_and_immutable():
    first = get_event_device_mapping()
    second = get_event_device_mapping()
    assert first is second  # agent 每条事件都会查表，不能每次读盘
    assert isinstance(first, EventDeviceMapping)
    with pytest.raises(Exception):
        first.rows["user.arrives_home"].primary_device_types = ("nonsense",)


def test_lookup_is_cheap_enough_for_per_event_use():
    started = time.monotonic()
    for _ in range(2000):
        get_device_search_space(make_event("user.arrives_home"))
    assert time.monotonic() - started < 2.0
