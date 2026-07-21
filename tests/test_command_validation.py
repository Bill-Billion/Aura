"""六级顺序校验 + §10.2 十类错误码（spec §3.3 / §2.2 / §10.2）。

这些用例是 S1 执行契约的校验层钉子：一条坏命令在哪一级、以哪个 §10.2 码被拒，
是研究者重建"命令为何未影响世界"的唯一依据，因此逐级、逐码断言。
"""

from __future__ import annotations

import pytest

from backend.execution.capability_matrix import (
    all_device_types,
    get_capability_specs,
    get_writable_capability_names,
)
from backend.config.device_registry import build_default_devices
from backend.execution.validation import (
    CommandErrorCode,
    CommandFailure,
    ForbiddenDevicePolicy,
    PermissivePolicy,
    validate_command,
)
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    WorldState,
)


# §10.2 十类失败码（前七类校验期产出，后三类由 executor 层执行期产出）。
SPEC_10_2_CODES = {
    "unknown_device",
    "device_offline",
    "capability_not_supported",
    "read_only_capability",
    "invalid_value_type",
    "invalid_value_range",
    "policy_denied",
    "execution_timeout",
    "state_feedback_missing",
    "superseded_by_newer_command",
}

# 每种可写能力的一个合法样例值（用于"全部合法命令通过"回归）。
VALID_SAMPLE_VALUES: dict[tuple[str, str], object] = {
    ("light", "power"): True,
    ("light", "brightness"): 50,
    ("light", "color_temp"): 4000,
    ("hvac", "power"): True,
    ("hvac", "target_temp"): 22.5,
    ("hvac", "mode"): "cool",
    ("hvac", "speed"): "low",
    ("curtain", "open_percent"): 50,
    ("fan", "power"): True,
    ("fan", "speed"): "low",
    ("fan", "shake"): True,
    ("fan", "timeout"): 30,
}


def _world() -> WorldState:
    """默认设备（多数在线） + 一个显式离线灯，覆盖全部六设备类型与 online 分支。"""

    world = WorldState(scene_id="validation-test")
    world.devices = build_default_devices()
    world.devices["light_offline_01"] = DeviceState(
        id="light_offline_01",
        type="light",
        location=Location3D(room="living_room"),
        capabilities=["power", "brightness", "color_temp"],
        state=DeviceStateValues(power=True, extra={"online": False, "brightness": 40}),
    )
    return world


def _device_id_of_type(device_type: str) -> str:
    return {
        "light": "light_living_01",
        "hvac": "ac_living_01",
        "curtain": "curtain_living_01",
        "fan": "fan_living_01",
        "camera": "camera_entry_01",  # extra.online=True
        "sensor": "sensor_living_temp_01",
    }[device_type]


# --------------------------------------------------------------------------- #
# 错误码枚举本身：§10.2 十码单一来源
# --------------------------------------------------------------------------- #


def test_error_code_enum_is_exactly_the_ten_spec_10_2_codes():
    assert {code.value for code in CommandErrorCode} == SPEC_10_2_CODES


def test_executor_tier_codes_exist_but_are_not_emitted_by_validation():
    # 后三类由 executor 执行期产出；校验层永不返回它们。
    executor_only = {
        CommandErrorCode.EXECUTION_TIMEOUT,
        CommandErrorCode.STATE_FEEDBACK_MISSING,
        CommandErrorCode.SUPERSEDED_BY_NEWER_COMMAND,
    }
    assert executor_only <= set(CommandErrorCode)


# --------------------------------------------------------------------------- #
# 逐级失败：七个校验期错误码逐一命中
# --------------------------------------------------------------------------- #


def test_unknown_device_returns_unknown_device():
    failure = validate_command(_world(), "ghost_device", "brightness", 50)
    assert isinstance(failure, CommandFailure)
    assert failure.code is CommandErrorCode.UNKNOWN_DEVICE
    assert failure.details["device_id"] == "ghost_device"


def test_offline_device_returns_device_offline():
    # 命令本身完全合法（brightness=50），仅因设备离线被拒。
    failure = validate_command(_world(), "light_offline_01", "brightness", 50)
    assert failure is not None
    assert failure.code is CommandErrorCode.DEVICE_OFFLINE


def test_nonexistent_capability_returns_capability_not_supported():
    failure = validate_command(_world(), "light_living_01", "no_such_cap", 1)
    assert failure is not None
    assert failure.code is CommandErrorCode.CAPABILITY_NOT_SUPPORTED


def test_writing_sensor_value_returns_read_only_capability():
    # 能力名必须是 value（effects 写的、前端读的都是 state.extra.value）：写真实
    # 注册表传感器要报"只读"，而不是"能力不存在"。
    failure = validate_command(_world(), "sensor_living_temp_01", "value", 18.0)
    assert failure is not None
    assert failure.code is CommandErrorCode.READ_ONLY_CAPABILITY


def test_sensor_capability_name_matches_effect_model_field():
    """矩阵里的 sensor 能力名 = 效果模型写入的 state.extra 字段名。

    §3.4 的 sensor 读数落在 ``state.extra.value``（backend/simulators/effects.py），
    前端 SensorPanel 读的也是它。矩阵若声明成别的名字，只读拦截就会落空——写 value
    变成 capability_not_supported，真正的只读语义永远触发不到。
    """

    names = {spec.name for spec in get_capability_specs("sensor")}
    assert names == {"value"}

    device = build_default_devices()["sensor_living_temp_01"]
    assert "value" in device.capabilities, "注册表能力位必须与矩阵同名"
    assert "value" in device.state.extra, "extra 镜像（effects/前端读的字段）不可删"


def test_writing_camera_view_returns_read_only_capability():
    failure = validate_command(_world(), "camera_entry_01", "view", "zoom")
    assert failure is not None
    assert failure.code is CommandErrorCode.READ_ONLY_CAPABILITY


def test_brightness_wrong_type_returns_invalid_value_type():
    failure = validate_command(_world(), "light_living_01", "brightness", "bright")
    assert failure is not None
    assert failure.code is CommandErrorCode.INVALID_VALUE_TYPE


def test_bool_capability_rejects_int_as_invalid_value_type():
    # power 是 bool；int 1 不是 bool（Python 里 True 才是 bool），应报类型错。
    failure = validate_command(_world(), "light_living_01", "power", 1)
    assert failure is not None
    assert failure.code is CommandErrorCode.INVALID_VALUE_TYPE


def test_brightness_over_max_returns_invalid_value_range():
    failure = validate_command(_world(), "light_living_01", "brightness", 101)
    assert failure is not None
    assert failure.code is CommandErrorCode.INVALID_VALUE_RANGE


def test_brightness_below_min_returns_invalid_value_range():
    failure = validate_command(_world(), "light_living_01", "brightness", -1)
    assert failure is not None
    assert failure.code is CommandErrorCode.INVALID_VALUE_RANGE


def test_hvac_mode_unknown_enum_returns_invalid_value_range():
    failure = validate_command(_world(), "ac_living_01", "mode", "blast")
    assert failure is not None
    assert failure.code is CommandErrorCode.INVALID_VALUE_RANGE


def test_fan_timeout_over_max_returns_invalid_value_range():
    failure = validate_command(_world(), "fan_living_01", "timeout", 999)
    assert failure is not None
    assert failure.code is CommandErrorCode.INVALID_VALUE_RANGE


# --------------------------------------------------------------------------- #
# 顺序稳定性：多个问题同时存在时，先报靠前的级
# --------------------------------------------------------------------------- #


def test_validation_order_offline_precedes_invalid_value():
    # 离线设备 + 越界值同时存在：online 是第 2 级，值域是第 6 级 → 先报 device_offline。
    failure = validate_command(_world(), "light_offline_01", "brightness", 999)
    assert failure is not None
    assert failure.code is CommandErrorCode.DEVICE_OFFLINE


def test_validation_order_capability_precedes_value_type():
    # 不存在能力 + 值类型也不对：capability 是第 3 级，类型是第 5 级 → 先报能力不存在。
    failure = validate_command(_world(), "light_living_01", "no_such_cap", object())
    assert failure is not None
    assert failure.code is CommandErrorCode.CAPABILITY_NOT_SUPPORTED


def test_validation_order_writable_precedes_value_range():
    # 只读能力 + 越界值：writable 是第 4 级，值域是第 6 级 → 先报只读。
    failure = validate_command(_world(), "sensor_living_temp_01", "value", 10**9)
    assert failure is not None
    assert failure.code is CommandErrorCode.READ_ONLY_CAPABILITY


# --------------------------------------------------------------------------- #
# 实例级能力位：类型矩阵是上界，设备自己声明的能力位才是实际契约（§3.2/§3.3）
# --------------------------------------------------------------------------- #


def _world_with_reduced_devices() -> WorldState:
    """默认世界 + 两台"能力位少于其类型声明"的设备（S2 场景会大量生成这种设备）。"""

    world = _world()
    world.devices["light_no_ct_01"] = DeviceState(
        id="light_no_ct_01",
        type="light",
        location=Location3D(room="bedroom"),
        capabilities=["power", "brightness"],  # 有意不含 color_temp
        state=DeviceStateValues(power=True, extra={"brightness": 40}),
    )
    world.devices["hvac_no_speed_01"] = DeviceState(
        id="hvac_no_speed_01",
        type="hvac",
        location=Location3D(room="bedroom"),
        capabilities=["power", "target_temp", "mode"],  # 有意不含 speed
        state=DeviceStateValues(power=True, extra={"target_temp": 24.0, "mode": "cool"}),
    )
    return world


def test_instance_without_capability_returns_capability_not_supported():
    world = _world_with_reduced_devices()

    failure = validate_command(world, "light_no_ct_01", "color_temp", 4000)
    assert failure is not None
    assert failure.code is CommandErrorCode.CAPABILITY_NOT_SUPPORTED
    assert failure.details["capability"] == "color_temp"

    failure = validate_command(world, "hvac_no_speed_01", "speed", "low")
    assert failure is not None
    assert failure.code is CommandErrorCode.CAPABILITY_NOT_SUPPORTED


def test_instance_declared_capabilities_still_pass():
    # 实例级收紧只影响没声明的能力位，声明了的照常放行。
    world = _world_with_reduced_devices()
    assert validate_command(world, "light_no_ct_01", "brightness", 60) is None
    assert validate_command(world, "light_no_ct_01", "power", True) is None
    assert validate_command(world, "hvac_no_speed_01", "mode", "heat") is None


def test_instance_capability_gate_precedes_value_type():
    # 实例无该能力 + 值类型也不对：能力级是第 3 级，类型是第 5 级 → 先报能力不支持。
    world = _world_with_reduced_devices()
    failure = validate_command(world, "light_no_ct_01", "color_temp", "暖光")
    assert failure is not None
    assert failure.code is CommandErrorCode.CAPABILITY_NOT_SUPPORTED


def test_empty_instance_capability_list_falls_back_to_type_level():
    """能力位为空的设备回退到类型级声明，不因"没声明"被整体拒绝。

    最小构造的设备（脚本/旧夹具）常不填 capabilities，若一律拒绝会把合法命令全打死。
    """

    world = _world()
    world.devices["light_bare_01"] = DeviceState(
        id="light_bare_01",
        type="light",
        location=Location3D(room="loft"),
        capabilities=[],
        state=DeviceStateValues(power=True, extra={"brightness": 30}),
    )
    assert validate_command(world, "light_bare_01", "brightness", 60) is None
    assert validate_command(world, "light_bare_01", "color_temp", 4000) is None
    # 类型级矩阵之外的能力仍然被拒
    failure = validate_command(world, "light_bare_01", "open_percent", 50)
    assert failure is not None
    assert failure.code is CommandErrorCode.CAPABILITY_NOT_SUPPORTED


# --------------------------------------------------------------------------- #
# 场景策略级（S2 ScenarioSpec §5.1 接入前的 stub 钩子）
# --------------------------------------------------------------------------- #


def test_policy_denied_via_stub_policy():
    policy = ForbiddenDevicePolicy(["light_living_01"])
    failure = validate_command(
        _world(), "light_living_01", "brightness", 50, policy=policy
    )
    assert failure is not None
    assert failure.code is CommandErrorCode.POLICY_DENIED
    assert failure.details["device_id"] == "light_living_01"


def test_permissive_policy_allows_valid_command():
    failure = validate_command(
        _world(), "light_living_01", "brightness", 50, policy=PermissivePolicy()
    )
    assert failure is None


def test_policy_only_consulted_after_earlier_levels_pass():
    # 越界值 + 被禁设备：值域是第 6 级，策略是第 7 级 → 先报值域，不到策略。
    policy = ForbiddenDevicePolicy(["light_living_01"])
    failure = validate_command(
        _world(), "light_living_01", "brightness", 101, policy=policy
    )
    assert failure is not None
    assert failure.code is CommandErrorCode.INVALID_VALUE_RANGE


# --------------------------------------------------------------------------- #
# 正向：全部可写能力的合法命令通过
# --------------------------------------------------------------------------- #


def test_valid_commands_for_all_writable_capabilities_pass():
    world = _world()
    checked: set[tuple[str, str]] = set()
    for device_type in all_device_types():
        writable = get_writable_capability_names(device_type)
        if not writable:
            continue
        device_id = _device_id_of_type(device_type)
        for spec in get_capability_specs(device_type):
            if not spec.writable:
                continue
            value = VALID_SAMPLE_VALUES[(device_type, spec.name)]
            failure = validate_command(world, device_id, spec.name, value)
            assert failure is None, (
                f"{device_type}.{spec.name}={value!r} 应通过，却被拒: {failure}"
            )
            checked.add((device_type, spec.name))
    # 保证确实覆盖了矩阵里全部可写能力，未静默漏项。
    expected = {
        (dt, name)
        for dt in all_device_types()
        for name in get_writable_capability_names(dt)
    }
    assert checked == expected


def test_integer_valued_float_accepted_for_int_capability():
    # 前端/JSON 常把整数发成 float；整数值的 float 视为合法 int，不误判类型。
    failure = validate_command(_world(), "light_living_01", "brightness", 70.0)
    assert failure is None


def test_float_capability_accepts_int():
    failure = validate_command(_world(), "ac_living_01", "target_temp", 22)
    assert failure is None


def test_boundary_values_inclusive():
    world = _world()
    assert validate_command(world, "light_living_01", "brightness", 0) is None
    assert validate_command(world, "light_living_01", "brightness", 100) is None
    assert validate_command(world, "fan_living_01", "timeout", 240) is None


@pytest.mark.parametrize("device_type", ["light", "hvac", "curtain", "fan"])
def test_every_writable_type_has_at_least_one_passing_command(device_type):
    world = _world()
    device_id = _device_id_of_type(device_type)
    passed = [
        name
        for name in get_writable_capability_names(device_type)
        if validate_command(
            world, device_id, name, VALID_SAMPLE_VALUES[(device_type, name)]
        )
        is None
    ]
    assert passed
