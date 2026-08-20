"""§6 八个典型场景的库自检（S2-T8，同时是 S2 阶段门的一条自动化门）。

门条款原文：*GET /api/scenarios 恰好枚举 8 个合法场景，每个可 headless 运行完成并发出其
timeline 声明的根事件*。

这条测试刻意只断三件事，不多断一件：

1. **形状**：八个文件都能通过 S2-T4 的 ScenarioSpec + 注册表引用校验，且带齐 §5.3 八标签
   ground_truth、success_criteria 与 §12 metrics 声明（S4 评估器直接消费这些字段，缺一项
   就是给评估器喂空）。
2. **枚举**：REST 层看到的就是这八个（研究者从 UI 选场景的入口）。
3. **可运行**：每个场景真能 headless 跑完，且它 timeline 里声明的根事件真的进了事件流。

**不断**「agent 是否做对了」——那是 S4 评估器的职责。expected_device_effects 在 S2 只作
数据存在性校验（plan_raw S2-T8 风险条原文），提前在这里断言设备终态，等于把评估器的判定
散进场景库测试里，S4 落地时必然要拆。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api import routes as routes_module
from backend.main import app
from backend.scenarios.generator import scenario_timeline_device_entries
from backend.scenarios.loader import load_library
from backend.scenarios.runner import run_scenario

# §6.1-6.10 的九个典型场景。前八个是 S2-T8 落地的 §6.1-6.9；
# ``multi_user_conflict``（§6.10）由 S3-T10 补上——S2-T8 当时刻意留给 S3，因为它是
# S3 阶段门的头号场景（仲裁必须真的选边并解释，见 tests/test_multi_user_conflict.py）。
# ``safety_smoke_kitchen`` 不属于 §6，它是 §13「safety event interrupts comfort or
# energy-saving behavior」的库内落点：S3 复审实测九个 §6 场景一次都没产出过 safety 档提案，
# §9.1 的最高档因此只被合成夹具走过（见 tests/test_episode_completeness.py 的档位生产者门）。
CANONICAL_SCENARIO_IDS = (
    "cooking_dinner",              # §6.5
    "device_offline_fallback",     # §6.9
    "hot_weather_afternoon",       # §6.6
    "morning_wake_up",             # §6.4
    "multi_user_conflict",         # §6.10（S3-T10）
    "night_sleep_bedtime",         # §6.3
    "safety_smoke_kitchen",        # §13 安全打断（S3 复审 minor）
    "security_presence_night",     # §6.8
    "user_arrives_home_evening",   # §6.1
    "user_leaves_home_morning",    # §6.2
)

# 每个场景必须真正发出的 §6 根事件（"这个场景是关于什么的"）。
# 与 timeline 全量声明分开列：timeline 可以有铺垫事件，但这一条是场景的立身之本。
SCENARIO_SIGNATURE_ROOT_EVENT = {
    "cooking_dinner": "user.starts_activity",
    "device_offline_fallback": "device.offline",
    "hot_weather_afternoon": "environment.temperature_threshold",
    "morning_wake_up": "user.starts_activity",
    "multi_user_conflict": "user.starts_activity",
    "night_sleep_bedtime": "user.starts_activity",
    "safety_smoke_kitchen": "safety.smoke_detected",
    "security_presence_night": "security.presence_detected",
    "user_arrives_home_evening": "user.arrives_home",
    "user_leaves_home_morning": "user.leaves_home",
}


@pytest.fixture
def library_client():
    """REST 客户端，且场景目录明确指回默认库目录。

    显式 configure_scenario_dirs(None) 不是多余：test_routes.py 的 scenario_dirs fixture
    会把目录指向 tmp_path，测试顺序一旦变化，这里就会枚举到别人的临时场景。
    """

    routes_module.configure_scenario_dirs(None)
    with TestClient(app) as client:
        yield client


# ----------------------------------------------------------------- 1. 形状


def test_library_contains_exactly_the_canonical_scenarios():
    library = load_library()
    assert tuple(library) == CANONICAL_SCENARIO_IDS


@pytest.mark.parametrize("scenario_id", CANONICAL_SCENARIO_IDS)
def test_each_scenario_declares_ground_truth_success_criteria_and_metrics(scenario_id):
    """§5.3 八标签 + success_criteria + §12 metrics：S4 评估器的输入面必须齐。"""

    spec = load_library()[scenario_id]

    assert spec.timeline, "场景必须有 timeline，否则没有根事件可触发"
    assert spec.expected_device_effects, "expected_device_effects 是 S4 判 user_intent_satisfied 的依据"
    assert spec.involved_agents, "involved_agents 声明这个场景考察哪几个 agent"
    assert spec.metrics, "§12.999：每个 ScenarioSpec 必须声明它要求哪些指标"

    ground_truth = spec.ground_truth
    assert ground_truth is not None, "§5.3 要求场景带 ground truth 标签"
    # 八标签里四条"没有默认值就等于没标注"的必须实打实写出来
    assert ground_truth.user_goal
    assert ground_truth.expected_intent
    assert ground_truth.primary_room_ids
    assert ground_truth.relevant_device_ids

    criteria = spec.success_criteria
    assert criteria.max_first_action_latency_ms is not None, "§12 首动作时延阈值必须显式给出"
    assert criteria.max_command_failures is not None, "§12 命令失败上限必须显式给出"


def test_only_the_arrival_scenario_anchors_the_s15_2_command_acceptance():
    """§15-2 的锚点场景是"按 id 排序第一个带 timeline 直控项"的库场景。

    tests/test_s1_acceptance.py::_library_scenario_with_device_command 按 id 升序取第一个
    带 ``payload.capability/value`` 的场景，并断言它在整个 run 里**只有一条**场景来源命令
    且必须成功。往任何 id 排在 ``user_arrives_home_evening`` 之前的场景里加直控项，都会
    悄悄改掉那条验收的被测对象——这条测试把这个跨文件耦合钉在明面上。
    """

    library = load_library()
    with_commands = [
        scenario_id
        for scenario_id, spec in library.items()
        if scenario_timeline_device_entries(spec)
    ]
    assert with_commands, "库里至少要有一个带脚本直控项的场景，否则 §15-2 退化为替身验收"
    assert with_commands[0] == "user_arrives_home_evening"
    assert len(scenario_timeline_device_entries(library[with_commands[0]])) == 1


def test_device_offline_scenario_models_preemptive_avoidance_not_a_fake_failure():
    """Canonical 掉线场景先发布环境事实，再由 agent 预判避让并选择替代设备。"""

    spec = load_library()["device_offline_fallback"]
    assert spec.scenario_schema_version == "1.0"
    assert spec.expected_failures == []
    assert spec.initial_state.devices["ac_living_01"].state.extra["online"] is False
    assert any(
        item.type == "device.offline" and item.device_id == "ac_living_01"
        for item in spec.timeline
    )
    assert spec.ground_truth is not None
    assert "ac_living_01" in spec.ground_truth.relevant_device_ids
    assert "ac_living_01" not in spec.ground_truth.forbidden_device_ids
    assert (
        "do_not_retry_commands_to_a_device_known_offline"
        in spec.ground_truth.safety_constraints
    )


def test_full_library_load_fires_no_forward_compat_warning():
    """整库加载不得触发 §14 高 MINOR 丢弃日志。

    库里任何一个文件声明了本后端读不懂的字段，都意味着那段数据对 REST / 评估器不可见；
    每次 /api/scenarios 请求都刷一条 warning 只会把这条日志训练成噪声。真正的向前兼容
    路径由 tests/test_scenario_spec.py 的合成高 MINOR fixture 保活，不靠库文件来演示。
    """

    import structlog.testing

    with structlog.testing.capture_logs() as logs:
        library = load_library()

    assert len(library) == len(CANONICAL_SCENARIO_IDS)
    dropped = [
        entry for entry in logs
        if entry.get("event") == "scenario.schema_forward_compat_fields_dropped"
    ]
    assert not dropped, f"库场景不应触发 §14 字段丢弃：{dropped}"


def test_expected_failure_device_ids_are_registry_checked():
    """expected_failures.device_id 参与注册表引用完整性校验（写错设备名必须加载期就炸）。"""

    from backend.scenarios.loader import ScenarioLoadError, parse_scenario_mapping

    spec = load_library()["device_offline_fallback"]
    assert "ac_living_01" in spec.referenced_device_ids()

    data = spec.model_dump()
    data["expected_failures"] = [
        {
            "category": "device_offline_before_command",
            "device_id": "ac_living_99",
            "error_code": "device_offline",
            "description": "synthetic registry-integrity fixture",
            "expected_recovery": "fallback_to_alternative_device",
        }
    ]
    with pytest.raises(ScenarioLoadError) as exc:
        parse_scenario_mapping(data)
    assert exc.value.code == "unknown_device_id"
    assert "ac_living_99" in str(exc.value)


# ----------------------------------------------------------------- 2. 枚举


def test_api_scenarios_enumerates_exactly_the_canonical_library(library_client):
    resp = library_client.get("/api/scenarios")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] == len(CANONICAL_SCENARIO_IDS) == 10
    assert [item["id"] for item in payload["scenarios"]] == list(CANONICAL_SCENARIO_IDS)
    # §2.3：枚举投影不得泄露 ground truth 本体
    assert all("ground_truth" not in item for item in payload["scenarios"])
    assert all(item["has_ground_truth"] is True for item in payload["scenarios"])


@pytest.mark.parametrize("scenario_id", CANONICAL_SCENARIO_IDS)
def test_api_scenario_detail_carries_ground_truth(library_client, scenario_id):
    resp = library_client.get(f"/api/scenarios/{scenario_id}")
    assert resp.status_code == 200
    scenario = resp.json()["scenario"]
    assert scenario["id"] == scenario_id
    assert scenario["ground_truth"]["expected_intent"]


# ----------------------------------------------------------------- 3. 可运行


@pytest.mark.anyio
@pytest.mark.parametrize("scenario_id", CANONICAL_SCENARIO_IDS)
async def test_each_scenario_runs_headless_emitting_its_declared_root_events(scenario_id):
    """门条款：每个场景 headless 跑完，且 timeline 声明的根事件真的进了事件流。

    "声明了却没发出来" 是场景库最隐蔽的坏法：文件校验全过、run 也"成功"，但那条根事件
    因为 at 超出 duration、或事件类型没有任何生成路径而从未出现，于是整个 run 里没有一条
    agent episode——一份空 trace 会被 S4 评成"agent 什么都没做"。
    """

    spec = load_library()[scenario_id]
    result = await run_scenario(scenario_id)

    assert result.scenario_id == scenario_id
    assert result.seed == spec.seed
    assert result.completed is True, "timeline 必须在 duration 内全部触发"
    assert result.fired_timeline_event_types == tuple(entry.type for entry in spec.timeline)

    emitted = {event.event_type for event in result.events}
    declared = {entry.type for entry in spec.timeline}
    assert declared <= emitted, f"声明但未发出的根事件：{sorted(declared - emitted)}"

    signature = SCENARIO_SIGNATURE_ROOT_EVENT[scenario_id]
    assert signature in emitted

    # 每条脚本事件都带齐 §4.5 的出处元数据（"这次行为是谁安排的"）
    scripted = [event for event in result.events if event.event_generation_mode == "scripted"]
    assert len(scripted) == len(spec.timeline)
    assert all(event.run_id == result.run_id for event in scripted)
    assert all(event.scenario_id == scenario_id for event in scripted)
