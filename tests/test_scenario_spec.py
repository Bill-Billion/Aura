"""S2-T4：ScenarioSpec 数据模型 + YAML loader（spec §5.1 / §5.2 / §5.3 / §14）。

测试分四组：
  1. §5.2 原文示例逐字段解析（场景 YAML 是对外契约，规格自带的例子必须能被加载）
  2. 必填/形状/引用完整性拒绝（缺字段、未知 device/room、timeline at 倒序、expected 路径非法）
  3. §14 版本兼容三分支（低/等 MINOR=严格 forbid，高 MINOR=容忍未知可选字段并记日志，未知 MAJOR=结构化拒绝）
  4. load_library(dirs) 多目录契约（S3 加 eval/、S4 加 failures/ 与 suites/ 走同一个 loader）
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError

from backend.engine.rng import MAX_JSON_SAFE_SEED
from backend.models.versioning import (
    SUPPORTED_SCENARIO_SCHEMA_VERSION,
    SchemaVersionError,
    check_schema_compatibility,
    parse_schema_version,
)
from backend.scenarios.loader import (
    DEFAULT_LIBRARY_DIRS,
    ScenarioLoadError,
    load_library,
    load_scenario_file,
)
from backend.scenarios.spec import (
    EXPECTED_FAILURE_CATEGORIES,
    ExpectedValue,
    ScenarioSpec,
    SuccessCriteria,
)


# —— spec §5.2 原文示例（逐字复制，仅补 §5.1 要求但示例省略的 description）——
SPEC_5_2_EXAMPLE = """
id: user_arrives_home_evening
name: User arrives home in the evening
description: 傍晚回家，客厅灯与空调应在数秒内进入舒适状态
seed: 1001
duration_seconds: 180
initial_state:
  time_of_day: "18:30"
  weather: cloudy
  users:
    user_01:
      location: outside
      activity: commuting
      presence_state: away
  rooms:
    living_room:
      occupancy: false
      light_level: 80
      temperature: 27.5
  devices:
    light_living_01:
      state:
        power: false
        extra:
          brightness: 0
    ac_living_01:
      state:
        power: false
        extra:
          target_temp: 24
          mode: cool
timeline:
  - at: 0
    type: user.arrives_home
    user_id: user_01
    room_id: living_room
expected_device_effects:
  - device_id: light_living_01
    within_seconds: 5
    expected:
      power: true
      extra.brightness:
        min: 50
  - device_id: ac_living_01
    within_seconds: 10
    expected:
      power: true
      extra.mode: cool
involved_agents:
  - home_orchestrator
  - lighting_agent
  - hvac_agent
success_criteria:
  require_complete_episode: true
  max_first_action_latency_ms: 5000
  max_command_failures: 0
  allow_fallback: true
"""

# §5.3 八个 ground truth 标签（规格原文示例）
SPEC_5_3_GROUND_TRUTH = """
ground_truth:
  user_goal: "comfortable arrival lighting and cooling"
  primary_room_ids: ["living_room"]
  relevant_device_ids: ["light_living_01", "ac_living_01"]
  forbidden_device_ids: ["camera_bedroom_02"]
  required_agent_roles: ["orchestrator", "lighting", "hvac"]
  acceptable_noop: false
  expected_intent: "arrival_comfort"
  safety_constraints:
    - "do_not_disable_security_when_user_is_away"
"""


def write_scenario(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("probability", "nope"),
        ("probability", 1.01),
        ("recovery_probability", -0.01),
    ],
)
def test_failure_injection_rejects_invalid_nested_probability(tmp_path, field, value):
    base = load_scenario_file(write_scenario(tmp_path, "valid-base", SPEC_5_2_EXAMPLE))
    payload = base.model_dump(mode="json")
    payload["failure_injection"] = {"device_offline": {field: value}}
    with pytest.raises(ValidationError, match=field):
        ScenarioSpec.model_validate(payload)


def test_noise_model_rejects_unknown_or_inverted_contract(tmp_path):
    base = load_scenario_file(write_scenario(tmp_path, "valid-base", SPEC_5_2_EXAMPLE))
    payload = base.model_dump(mode="json")
    payload["noise_model"] = {"temperature_comfort": [30, 20]}
    with pytest.raises(ValidationError, match="min < max"):
        ScenarioSpec.model_validate(payload)

    payload["noise_model"] = {"future_untyped_knob": {"value": "nope"}}
    with pytest.raises(ValidationError, match="Extra inputs"):
        ScenarioSpec.model_validate(payload)


# ---------------------------------------------------------------- 1. §5.2 契约


def test_spec_5_2_example_yaml_roundtrips(tmp_path):
    """规格 §5.2 原文示例必须逐字段解析成功（场景 YAML 是对外契约）。"""
    path = write_scenario(tmp_path, "user_arrives_home_evening", SPEC_5_2_EXAMPLE)
    spec = load_scenario_file(path)

    assert spec.id == "user_arrives_home_evening"
    assert spec.name == "User arrives home in the evening"
    assert spec.seed == 1001
    assert spec.duration_seconds == 180
    assert spec.mode == "observe"  # 可选字段缺省
    assert spec.scenario_schema_version == SUPPORTED_SCENARIO_SCHEMA_VERSION

    assert spec.initial_state.time_of_day == "18:30"
    assert spec.initial_state.weather == "cloudy"
    assert spec.initial_state.users["user_01"].location == "outside"
    assert spec.initial_state.users["user_01"].activity == "commuting"
    assert spec.initial_state.users["user_01"].presence_state == "away"
    assert spec.initial_state.rooms["living_room"].occupancy is False
    assert spec.initial_state.rooms["living_room"].light_level == 80
    assert spec.initial_state.rooms["living_room"].temperature == 27.5
    assert spec.initial_state.devices["light_living_01"].state.power is False
    assert spec.initial_state.devices["light_living_01"].state.extra == {"brightness": 0}
    assert spec.initial_state.devices["ac_living_01"].state.extra["mode"] == "cool"

    assert len(spec.timeline) == 1
    ev = spec.timeline[0]
    assert ev.at == 0
    assert ev.type == "user.arrives_home"
    assert ev.user_id == "user_01"
    assert ev.room_id == "living_room"

    # §5.2 的 expected 同时含精确值与区间——两种形状必须都被同一个模型容纳
    light_effect = spec.expected_device_effects[0]
    assert light_effect.device_id == "light_living_01"
    assert light_effect.within_seconds == 5
    assert light_effect.expected["power"].equals is True
    assert light_effect.expected["extra.brightness"].min == 50
    assert light_effect.expected["extra.brightness"].equals is None

    assert spec.involved_agents == ["home_orchestrator", "lighting_agent", "hvac_agent"]
    assert spec.success_criteria.require_complete_episode is True
    assert spec.success_criteria.max_first_action_latency_ms == 5000
    assert spec.success_criteria.max_command_failures == 0
    assert spec.success_criteria.allow_fallback is True

    assert spec.ground_truth is None  # §5.3 是可选块


def test_spec_5_3_ground_truth_labels_parse(tmp_path):
    """§5.3 八个标签全部落地，且不对 agent 可见（仅评估消费）。"""
    path = write_scenario(
        tmp_path, "with_gt", SPEC_5_2_EXAMPLE + SPEC_5_3_GROUND_TRUTH
    )
    spec = load_scenario_file(path)
    gt = spec.ground_truth
    assert gt is not None
    assert gt.user_goal == "comfortable arrival lighting and cooling"
    assert gt.primary_room_ids == ["living_room"]
    assert gt.relevant_device_ids == ["light_living_01", "ac_living_01"]
    assert gt.forbidden_device_ids == ["camera_bedroom_02"]
    assert gt.required_agent_roles == ["orchestrator", "lighting", "hvac"]
    assert gt.acceptable_noop is False
    assert gt.expected_intent == "arrival_comfort"
    assert gt.safety_constraints == ["do_not_disable_security_when_user_is_away"]


def test_expected_value_matches_exact_range_and_one_of():
    """S4 评估器直接消费 matches()：精确值 / 区间 / 枚举三种判定在此钉死。"""
    assert ExpectedValue(equals=True).matches(True) is True
    assert ExpectedValue(equals=True).matches(False) is False
    assert ExpectedValue(equals="cool").matches("cool") is True

    rng = ExpectedValue(min=50)
    assert rng.matches(50) is True
    assert rng.matches(49) is False
    assert rng.matches(None) is False
    assert rng.matches("bright") is False  # 非数值不进区间比较

    assert ExpectedValue(min=20, max=26).matches(24) is True
    assert ExpectedValue(min=20, max=26).matches(27) is False
    assert ExpectedValue(one_of=["cool", "dry"]).matches("dry") is True
    assert ExpectedValue(one_of=["cool", "dry"]).matches("heat") is False

    # bool 不得被当成 0/1 参与数值比较（power=True 不该匹配 min:1）
    assert ExpectedValue(min=1).matches(True) is False


@pytest.mark.parametrize("bound", ["min", "max"])
@pytest.mark.parametrize("token", [".nan", ".inf", "-.inf"])
def test_expected_value_bounds_must_be_finite_yaml_numbers(tmp_path, bound, token):
    body = SPEC_5_2_EXAMPLE.replace("min: 50", f"{bound}: {token}", 1)
    path = write_scenario(tmp_path, f"non_finite_expected_{bound}_{token}", body)

    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)

    assert bound in str(exc.value)
    assert "有限数值" in str(exc.value)


@pytest.mark.parametrize("bound", ["min", "max"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_expected_value_bounds_must_be_finite_model_inputs(bound, value):
    with pytest.raises(ValidationError, match="有限数值"):
        ExpectedValue.model_validate({bound: value})


def test_expected_value_rejects_inverted_range_and_allows_equal_bounds():
    with pytest.raises(ValidationError, match="min <= max"):
        ExpectedValue(min=51, max=50)

    exact_range = ExpectedValue(min=50, max=50)
    assert exact_range.matches(50) is True
    assert exact_range.matches(49) is False


def test_min_conflict_count_is_a_non_negative_typed_criterion():
    assert SuccessCriteria(min_conflict_count=1).min_conflict_count == 1
    for invalid in (-1, True, "1"):
        with pytest.raises(ValueError):
            SuccessCriteria(min_conflict_count=invalid)


def test_empty_expected_constraint_rejected():
    with pytest.raises(ValueError):
        ExpectedValue()


# ------------------------------------------------- 2. 必填 / 形状 / 引用完整性


def test_missing_required_field_rejected(tmp_path):
    body = SPEC_5_2_EXAMPLE.replace("seed: 1001\n", "")
    path = write_scenario(tmp_path, "no_seed", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "seed" in str(exc.value)
    assert str(path) in str(exc.value)


@pytest.mark.parametrize("seed", [True, MAX_JSON_SAFE_SEED + 1])
def test_seed_must_round_trip_exactly_through_json(tmp_path, seed):
    body = SPEC_5_2_EXAMPLE.replace("seed: 1001", f"seed: {str(seed).lower()}")
    path = write_scenario(tmp_path, "unsafe_seed", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "seed" in str(exc.value)


def test_unknown_device_id_rejected_against_registry(tmp_path):
    body = SPEC_5_2_EXAMPLE.replace("light_living_01", "light_nowhere_99")
    path = write_scenario(tmp_path, "bad_device", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "light_nowhere_99" in str(exc.value)


def test_unknown_room_id_rejected_against_registry(tmp_path):
    body = SPEC_5_2_EXAMPLE.replace("living_room", "ballroom")
    path = write_scenario(tmp_path, "bad_room", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "ballroom" in str(exc.value)


def test_registry_check_can_be_disabled_for_synthetic_specs(tmp_path):
    """S4 失败注入/合成场景可能引用不在默认注册表里的设备——校验须可显式关闭。"""
    body = SPEC_5_2_EXAMPLE.replace("light_living_01", "light_nowhere_99")
    path = write_scenario(tmp_path, "bad_device_ok", body)
    spec = load_scenario_file(path, check_registry=False)
    assert "light_nowhere_99" in spec.initial_state.devices


def test_timeline_at_must_be_non_decreasing(tmp_path):
    body = SPEC_5_2_EXAMPLE.replace(
        "timeline:\n  - at: 0\n    type: user.arrives_home\n    user_id: user_01\n    room_id: living_room\n",
        "timeline:\n"
        "  - at: 10\n    type: user.arrives_home\n    user_id: user_01\n    room_id: living_room\n"
        "  - at: 5\n    type: user.enters_room\n    user_id: user_01\n    room_id: living_room\n",
    )
    path = write_scenario(tmp_path, "bad_order", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "timeline" in str(exc.value)


@pytest.mark.parametrize(
    ("field", "needle", "replacement"),
    [
        ("timeline.at", "- at: 0", "- at: {token}"),
        ("within_seconds", "within_seconds: 5", "within_seconds: {token}"),
        ("duration_seconds", "duration_seconds: 180", "duration_seconds: {token}"),
    ],
)
@pytest.mark.parametrize("token", [".nan", ".inf", "-.inf"])
def test_scenario_times_must_be_finite_yaml_numbers(
    tmp_path, field, needle, replacement, token
):
    body = SPEC_5_2_EXAMPLE.replace(needle, replacement.format(token=token), 1)
    path = write_scenario(tmp_path, f"non_finite_{field}_{token}", body)

    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)

    assert field.split(".")[-1] in str(exc.value)
    assert "有限数值" in str(exc.value)


def test_unknown_root_event_type_rejected(tmp_path):
    body = SPEC_5_2_EXAMPLE.replace("type: user.arrives_home", "type: user.arives_home")
    path = write_scenario(tmp_path, "typo_event", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "user.arives_home" in str(exc.value)


def test_expected_path_must_be_power_or_extra_dotted(tmp_path):
    """expected 的键是设备状态路径；漏写 extra. 前缀是最常见的场景写错法。"""
    body = SPEC_5_2_EXAMPLE.replace("      extra.brightness:", "      brightness:")
    path = write_scenario(tmp_path, "bad_path", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "brightness" in str(exc.value)


def test_malformed_yaml_reports_file_and_reason(tmp_path):
    path = write_scenario(tmp_path, "broken", "id: x\n  name: [unclosed\n")
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert str(path) in str(exc.value)


def test_yaml_that_is_not_a_mapping_rejected(tmp_path):
    path = write_scenario(tmp_path, "list_doc", "- a\n- b\n")
    with pytest.raises(ScenarioLoadError):
        load_scenario_file(path)


def test_strict_extra_field_rejected_at_supported_version(tmp_path):
    """MINOR <= 支持版本时 extra='forbid' 生效（§14 严格分支）。"""
    body = SPEC_5_2_EXAMPLE + "unknown_future_field: 42\n"
    path = write_scenario(tmp_path, "extra_strict", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "unknown_future_field" in str(exc.value)


# ------------------------------------------------------------ 3. §14 版本兼容


def test_parse_schema_version_forms():
    assert parse_schema_version("1.0") == (1, 0)
    # YAML 里未加引号的 1.0 会被解析成 float，helper 必须容忍
    assert parse_schema_version(1.0) == (1, 0)
    for malformed in ("2", "1.3.7", 2, "abc"):
        with pytest.raises(SchemaVersionError):
            parse_schema_version(malformed)


def test_check_schema_compatibility_three_branches():
    lower = check_schema_compatibility("1.0", supported="1.2")
    assert lower.strict is True and lower.tolerated is False

    equal = check_schema_compatibility("1.2", supported="1.2")
    assert equal.strict is True and equal.tolerated is False

    higher = check_schema_compatibility("1.9", supported="1.2")
    assert higher.strict is False and higher.tolerated is True

    with pytest.raises(SchemaVersionError) as exc:
        check_schema_compatibility("2.0", supported="1.2")
    err = exc.value
    assert err.declared == "2.0"
    assert err.supported == "1.2"
    assert "major" in err.to_dict()["reason"].lower()


def test_higher_minor_with_extra_optional_field_accepted_and_logged(tmp_path):
    """高 MINOR 已知兼容：未知可选字段被接受并记日志（S4-T1 §14 兼容测试依赖此行为）。"""
    major, minor = parse_schema_version(SUPPORTED_SCENARIO_SCHEMA_VERSION)
    future = f"{major}.{minor + 5}"
    body = (
        SPEC_5_2_EXAMPLE
        + f"scenario_schema_version: '{future}'\n"
        + "future_optional_field: {a: 1}\n"
    )
    path = write_scenario(tmp_path, "future_minor", body)

    with structlog.testing.capture_logs() as logs:
        spec = load_scenario_file(path)

    assert spec.id == "user_arrives_home_evening"
    assert spec.scenario_schema_version == future
    dropped = [entry for entry in logs if "future_optional_field" in str(entry)]
    assert dropped, f"未知可选字段应被记日志，实际日志={logs}"


def test_unknown_major_rejected_with_structured_error(tmp_path):
    body = SPEC_5_2_EXAMPLE + "scenario_schema_version: '9.0'\n"
    path = write_scenario(tmp_path, "future_major", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    err = exc.value
    assert err.code == "unsupported_schema_version"
    assert err.details["declared"] == "9.0"
    assert err.details["supported"] == SUPPORTED_SCENARIO_SCHEMA_VERSION


# ------------------------------------------- 3b. schema 1.1 的 expected_failures


EXPECTED_FAILURE_BLOCK = """
expected_failures:
  - category: device_offline_before_command
    device_id: ac_living_01
    error_code: device_offline
    description: 主空调在命令下发前已离线
    expected_recovery: fallback_to_alternative_device
"""


def test_expected_failures_is_a_first_class_field_at_supported_version(tmp_path):
    """§13/§14：expected_failures 是 1.1 的可选字段，在**当前支持版本**下严格校验通过。

    它不是"高 MINOR 容忍分支放行的未知字段"——那条路径会把它丢掉。这条测试用不带任何
    版本声明的 §5.2 示例（默认 = SUPPORTED），走的就是 extra='forbid' 的严格分支。
    """
    path = write_scenario(tmp_path, "expected_failures", SPEC_5_2_EXAMPLE + EXPECTED_FAILURE_BLOCK)
    spec = load_scenario_file(path)

    assert len(spec.expected_failures) == 1
    entry = spec.expected_failures[0]
    assert entry.category == "device_offline_before_command"
    assert entry.error_code == "device_offline"
    assert entry.device_id == "ac_living_01"
    assert entry.expected_recovery == "fallback_to_alternative_device"
    # 未声明时是空列表而非 None：S4 评估器可以无条件迭代
    plain = load_scenario_file(write_scenario(tmp_path, "plain", SPEC_5_2_EXAMPLE))
    assert plain.expected_failures == []


def test_expected_failure_category_must_be_a_section_13_category(tmp_path):
    """category 取 §13 十类词表；拼错必须在加载期炸，而不是让 S4 静默少统计一类。"""
    body = SPEC_5_2_EXAMPLE + EXPECTED_FAILURE_BLOCK.replace(
        "device_offline_before_command", "device_offline_befor_command"
    )
    path = write_scenario(tmp_path, "bad_category", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "device_offline_befor_command" in str(exc.value)


def test_expected_failure_error_code_must_be_a_section_10_2_code(tmp_path):
    """error_code 复用 §10.2 十类失败码的**唯一词表**，不另立一份。"""
    body = SPEC_5_2_EXAMPLE + EXPECTED_FAILURE_BLOCK.replace(
        "error_code: device_offline", "error_code: device_is_offline"
    )
    path = write_scenario(tmp_path, "bad_code", body)
    with pytest.raises(ScenarioLoadError) as exc:
        load_scenario_file(path)
    assert "device_is_offline" in str(exc.value)


def test_expected_failure_categories_cover_all_ten_section_13_scenarios():
    """§13 "Required failure scenarios" 有十条，词表必须一条不少（S4-T3 逐条认领属主）。"""
    assert len(EXPECTED_FAILURE_CATEGORIES) == 10
    assert "device_offline_before_command" in EXPECTED_FAILURE_CATEGORIES
    assert "device_offline_during_execution" in EXPECTED_FAILURE_CATEGORIES
    assert "safety_event_interrupts_comfort" in EXPECTED_FAILURE_CATEGORIES


# ---------------------------------------------------- 4. load_library(dirs) 契约


def test_load_library_defaults_to_backend_scenarios_library():
    assert len(DEFAULT_LIBRARY_DIRS) == 1
    default_dir = DEFAULT_LIBRARY_DIRS[0]
    assert default_dir.name == "library"
    assert default_dir.parent.name == "scenarios"
    # 默认库目录必须真实存在（S2-T8 往里填 YAML；缺目录时 loader 不得炸）
    assert default_dir.is_dir()
    library = load_library()
    assert isinstance(library, dict)


def test_load_library_accepts_multiple_dirs(tmp_path):
    """S3 的 eval/ 与 S4 的 failures/、suites/ 都走这个签名，不许再造第二个 loader。"""
    dir_a = tmp_path / "library"
    dir_b = tmp_path / "failures"
    dir_a.mkdir()
    dir_b.mkdir()
    write_scenario(dir_a, "a", SPEC_5_2_EXAMPLE)
    write_scenario(dir_b, "b", SPEC_5_2_EXAMPLE.replace("user_arrives_home_evening", "b_scn"))

    library = load_library([dir_a, dir_b])
    assert set(library) == {"user_arrives_home_evening", "b_scn"}
    assert isinstance(library["b_scn"], ScenarioSpec)
    # 枚举顺序稳定（按 id 排序），便于 REST 输出与 diff 可复现
    assert list(library) == sorted(library)


def test_duplicate_scenario_id_across_dirs_rejected(tmp_path):
    dir_a = tmp_path / "library"
    dir_b = tmp_path / "failures"
    dir_a.mkdir()
    dir_b.mkdir()
    write_scenario(dir_a, "a", SPEC_5_2_EXAMPLE)
    write_scenario(dir_b, "b", SPEC_5_2_EXAMPLE)
    with pytest.raises(ScenarioLoadError) as exc:
        load_library([dir_a, dir_b])
    assert "user_arrives_home_evening" in str(exc.value)


def test_missing_directory_is_tolerated(tmp_path):
    """S3/S4 目录尚未创建时，loader 不得因缺目录而中断整库加载。"""
    dir_a = tmp_path / "library"
    dir_a.mkdir()
    write_scenario(dir_a, "a", SPEC_5_2_EXAMPLE)
    library = load_library([dir_a, tmp_path / "not_yet"])
    assert set(library) == {"user_arrives_home_evening"}
