"""S1-T1：能力矩阵数据化测试（对照 spec §3.2 逐行断言）。

矩阵是 S1-T2 六级校验的唯一值契约来源，这里锁死：
- 六类设备全部有 spec；
- 每一行的 writable 位与值契约（range/enum/type）与 §3.2 表逐字一致；
- 只读能力（camera view/online、sensor value）被标记为不可写。
"""

from __future__ import annotations

import pytest

from backend.execution.capability_matrix import (
    CAPABILITY_MATRIX,
    CapabilitySpec,
    all_device_types,
    get_capability_spec,
    get_capability_specs,
    get_writable_capability_names,
    is_writable_capability,
    read_only_capability_names,
)
from backend.config.device_registry import get_default_device_registry
from backend.engine.state import DeviceCapability, DeviceType


# spec §3.2 Capability Matrix，逐行落成 (type, name, value_type, writable, min, max, enum_values)
SPEC_ROWS: list[tuple[str, str, str, bool, float | None, float | None, tuple[str, ...] | None]] = [
    ("light", "power", "bool", True, None, None, None),
    ("light", "brightness", "int", True, 0, 100, None),
    ("light", "color_temp", "int", True, 2200, 6500, None),
    ("hvac", "power", "bool", True, None, None, None),
    ("hvac", "target_temp", "float", True, 16, 30, None),
    ("hvac", "mode", "enum", True, None, None, ("cool", "heat", "fan", "dry", "auto")),
    ("hvac", "speed", "enum", True, None, None, ("low", "medium", "high", "auto")),
    ("curtain", "open_percent", "int", True, 0, 100, None),
    ("fan", "power", "bool", True, None, None, None),
    ("fan", "speed", "enum", True, None, None, ("low", "medium", "high")),
    ("fan", "shake", "bool", True, None, None, None),
    ("fan", "timeout", "int", True, 0, 240, None),
    ("camera", "view", "metadata", False, None, None, None),
    ("camera", "online", "bool", False, None, None, None),
    # 有意偏离 spec §3.2 表：表里这行叫 `read`，实现改名为 `value`——效果模型写入的、
    # 前端 SensorPanel 读的都是 state.extra.value；叫 read 时只读语义永远触发不到
    # （写真实传感器会被判成 capability_not_supported）。改名前先看 capability_matrix 的注释。
    ("sensor", "value", "float", False, None, None, None),
]

SIX_TYPES = ("light", "hvac", "curtain", "fan", "camera", "sensor")


def test_matrix_covers_all_six_types_and_all_spec_rows():
    # 六类设备全部有条目
    assert set(CAPABILITY_MATRIX) == set(SIX_TYPES)
    assert set(all_device_types()) == set(SIX_TYPES)
    for device_type in SIX_TYPES:
        assert get_capability_specs(device_type), f"{device_type} 缺少能力 spec"

    # §3.2 表的每一行都必须存在，且 value_type/writable/range/enum 逐字段一致
    for dtype, name, value_type, writable, vmin, vmax, enum_values in SPEC_ROWS:
        spec = get_capability_spec(dtype, name)  # type: ignore[arg-type]
        assert spec is not None, f"缺少能力 {dtype}.{name}"
        assert isinstance(spec, CapabilitySpec)
        assert spec.name == name
        assert spec.value_type == value_type, f"{dtype}.{name} value_type 不符"
        assert spec.writable is writable, f"{dtype}.{name} writable 位不符"
        assert spec.min == vmin, f"{dtype}.{name} min 不符"
        assert spec.max == vmax, f"{dtype}.{name} max 不符"
        assert spec.enum_values == enum_values, f"{dtype}.{name} enum 不符"

    # 矩阵不得引入 §3.2 表以外的能力
    declared = {(dtype, spec.name) for dtype, specs in CAPABILITY_MATRIX.items() for spec in specs}
    expected = {(row[0], row[1]) for row in SPEC_ROWS}
    assert declared == expected


def test_camera_view_and_sensor_value_are_read_only():
    view = get_capability_spec("camera", "view")
    online = get_capability_spec("camera", "online")
    reading = get_capability_spec("sensor", "value")
    assert view is not None and view.writable is False
    assert online is not None and online.writable is False
    assert reading is not None and reading.writable is False

    # 访问器语义：只读能力不出现在可写集合中
    assert not is_writable_capability("camera", "view")
    assert not is_writable_capability("camera", "online")
    assert not is_writable_capability("sensor", "value")
    assert "view" not in get_writable_capability_names("camera")
    assert "online" not in get_writable_capability_names("camera")
    assert get_writable_capability_names("sensor") == frozenset()

    # 只读能力名集合聚合三者
    assert read_only_capability_names() == frozenset({"view", "online", "value"})


def test_fan_timeout_range_0_240_and_hvac_mode_enum():
    timeout = get_capability_spec("fan", "timeout")
    assert timeout is not None
    assert timeout.value_type == "int"
    assert (timeout.min, timeout.max) == (0, 240)
    assert timeout.unit == "min"
    assert timeout.writable is True

    mode = get_capability_spec("hvac", "mode")
    assert mode is not None
    assert mode.value_type == "enum"
    assert mode.enum_values == ("cool", "heat", "fan", "dry", "auto")
    assert mode.min is None and mode.max is None

    speed = get_capability_spec("fan", "speed")
    assert speed is not None
    assert speed.enum_values == ("low", "medium", "high")  # fan 无 auto 档


def test_value_contracts_are_well_formed():
    """每条 spec 的值契约自洽：enum 必带 enum_values 且无 range；bool 无 range/enum。"""
    for dtype, specs in CAPABILITY_MATRIX.items():
        assert isinstance(dtype, str)
        names = [s.name for s in specs]
        assert len(names) == len(set(names)), f"{dtype} 存在重复能力名"
        for spec in specs:
            assert spec.effect_class in {
                "physical",
                "perceived_comfort",
                "security_coverage",
                "ui_observability",
            }
            if spec.value_type == "enum":
                assert spec.enum_values, f"{dtype}.{spec.name} enum 缺 enum_values"
                assert spec.min is None and spec.max is None
            elif spec.value_type in {"int", "float"}:
                assert spec.enum_values is None
                if spec.min is not None and spec.max is not None:
                    assert spec.min <= spec.max
            elif spec.value_type in {"bool", "metadata"}:
                assert spec.min is None and spec.max is None
                assert spec.enum_values is None


def test_get_capability_spec_unknown_returns_none():
    assert get_capability_spec("light", "nonexistent") is None
    assert not is_writable_capability("light", "nonexistent")


def test_capability_spec_is_immutable():
    spec = get_capability_spec("light", "brightness")
    assert spec is not None
    with pytest.raises((TypeError, ValueError)):
        spec.writable = False  # type: ignore[misc]


def test_matrix_device_type_keys_are_valid_device_types():
    valid: set[str] = set(DeviceType.__args__)  # type: ignore[attr-defined]
    assert set(CAPABILITY_MATRIX) <= valid


def test_device_capability_literal_covers_every_matrix_capability():
    """DeviceCapability Literal 必须能表达矩阵里的每一条能力名。

    Literal 缺一个名字，注册表就写不出对应的能力位——online 曾因此只活在
    default_extra 里，导致 read_only_capability_names() 声明了一个没有设备能持有的能力。
    """

    literal_names: set[str] = set(DeviceCapability.__args__)  # type: ignore[attr-defined]
    matrix_names = {spec.name for specs in CAPABILITY_MATRIX.values() for spec in specs}
    assert matrix_names <= literal_names, f"Literal 缺失能力名: {sorted(matrix_names - literal_names)}"


def test_registry_capabilities_are_all_declared_in_matrix():
    """注册表实例能力位不得越出其设备类型在 §3.2 矩阵中的声明。"""

    for entry in get_default_device_registry():
        matrix_names = {spec.name for spec in get_capability_specs(entry.type)}
        unknown = set(entry.capabilities) - matrix_names
        assert not unknown, f"{entry.id} 声明了矩阵外能力 {sorted(unknown)}"


def test_camera_entries_declare_online_capability_and_keep_extra_mirror():
    """camera 的 online 既进能力位（供矩阵校验），又保留 extra.online 镜像（前端预览在读）。"""

    cameras = [entry for entry in get_default_device_registry() if entry.type == "camera"]
    assert cameras, "默认注册表应有 camera 设备"
    for entry in cameras:
        assert "view" in entry.capabilities
        assert "online" in entry.capabilities, f"{entry.id} 缺少 online 能力位"
        # 镜像不可删：frontend/src 的 CameraPanel/deviceFloorMap 读的是 state.extra.online
        assert isinstance(entry.default_extra.get("online"), bool), f"{entry.id} 缺 extra.online 镜像"
        assert entry.to_device_state().state.extra["online"] == entry.default_extra["online"]
