"""S3-T9：意图分类评测集 + scripts/eval_intent.py 的契约测试。

**先说清楚这道门是什么**（critic 的诚实性要求）：本文件断言的是"评测跑得通、报告
结构完整、规则路径与混合路径的准确率分开报"，**不断言任何准确率阈值**。16 条 case 是
冒烟规模，不是 benchmark；而且标签取自 spec §7 映射表，规则路径正是同一张表的实现——
它的准确率是一次**一致性检查**，不是质量指标。谁想把这个数字当成模型能力，请先把
case 数扩到有统计意义、并且划出与规则实现无关的 held-out split（§12.2）。

四条 plan_raw 点名的测试：
  1. 评测集能被 S2 loader 加载，且每条 case 都有 expected_intent 标签
  2. 已知 fixture 上的准确率计算（2 对 1 错 → 2/3）
  3. rule 路径准确率与 hybrid 路径准确率分开报（否则量不出 LLM 相对规则的增量）
  4. 报告记录 llm_mode / provider / model / 评测集版本
再加两条本任务自己认领的：标签绝不进 agent 可见的事件 data；CLI 能落一份报告工件。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from backend.agents.llm_modes import MockedLLMProvider
from backend.agents.orchestrator import HomeOrchestratorAgent
from backend.agents.types import AgentLLMDecision
from backend.scenarios.loader import load_library

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script_module():
    """按文件路径加载 scripts/eval_intent.py（scripts/ 不是包，刻意不加 __init__.py）。"""

    path = REPO_ROOT / "scripts" / "eval_intent.py"
    spec = importlib.util.spec_from_file_location("aura_eval_intent", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eval_intent = _load_script_module()


# ------------------------------------------------------------------ 测试用小评测集


def _case_entry(
    *,
    at: float,
    event_type: str,
    case_id: str,
    expected_intent: str,
    expected_domain: str | None = None,
    payload: dict[str, Any] | None = None,
    world: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"at": at, "type": event_type, **fields}
    case: dict[str, Any] = {"case_id": case_id, "expected_intent": expected_intent}
    if expected_domain is not None:
        case["expected_domain"] = expected_domain
    if world is not None:
        case["world"] = world
    entry["payload"] = {**(payload or {}), eval_intent.EVAL_CASE_KEY: case}
    return entry


def _write_eval_set(tmp_path: Path, entries: list[dict[str, Any]], set_id: str = "tmp_intent_eval_v1") -> Path:
    document = {
        "scenario_schema_version": "1.1",
        "id": set_id,
        "name": "临时评测集",
        "description": "单测用",
        "seed": 1,
        "duration_seconds": 600,
        "initial_state": {"time_of_day": "18:30", "weather": "clear"},
        "timeline": entries,
        "expected_device_effects": [],
        "involved_agents": [],
        "success_criteria": {},
    }
    path = tmp_path / "intent_eval_set.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


# ------------------------------------------------------- 1. 评测集加载 + 标签完整性


def test_eval_set_loads_and_every_case_has_expected_intent_label():
    # 评测集必须走 S2 的 load_library(dirs)：eval/ 与 library/ 共用同一个 loader，
    # 因此这个文件同时是一份合法 ScenarioSpec——目录里放一份 loader 读不懂的 YAML，
    # 以后任何"整库加载"都会炸在这里。
    library = load_library([eval_intent.EVAL_SET_DIR])
    assert library, "backend/scenarios/eval/ 下应当至少有评测集本身"

    eval_set = eval_intent.load_eval_set()
    assert eval_set.spec.id in library

    assert len(eval_set.cases) >= 16
    case_ids = [case.case_id for case in eval_set.cases]
    assert len(set(case_ids)) == len(case_ids), "case_id 必须唯一（报告按它对齐）"
    for case in eval_set.cases:
        assert case.expected_intent, f"{case.case_id} 缺 expected_intent 标签"

    # §7 的八条根事件行必须都被覆盖到（活动限定行按 type:activity 计）。
    covered = {
        f"{case.entry.type}:{case.entry.activity}" if case.entry.activity else case.entry.type
        for case in eval_set.cases
    }
    required = {
        "user.arrives_home",
        "user.leaves_home",
        "user.starts_activity:sleeping",
        "user.starts_activity:cooking",
        "environment.temperature_threshold",
        "environment.light_level_threshold",
        "security.presence_detected",
        "device.offline",
    }
    assert required <= covered, f"§7 未覆盖：{sorted(required - covered)}"


def test_ground_truth_labels_never_reach_the_orchestrator():
    """标签只走 ground_truth_labels（评估侧），绝不进 agent 可见的事件 data（§2.3）。"""

    eval_set = eval_intent.load_eval_set()
    case = eval_set.cases[0]
    context = eval_intent.build_case_context(eval_set, case)

    assert eval_intent.EVAL_CASE_KEY not in context.root_event.data
    assert "expected_intent" not in context.root_event.data
    assert context.expected_intent == case.expected_intent


# ------------------------------------------------------------ 2. 已知 fixture 上的准确率


@pytest.mark.anyio
async def test_runner_computes_accuracy_on_known_fixture(tmp_path):
    """两条标对、一条**故意标错** → 规则路径准确率恰好 2/3。"""

    entries = [
        _case_entry(
            at=0,
            event_type="user.arrives_home",
            case_id="right_arrival",
            expected_intent="arrival_comfort",
            user_id="user_01",
            world={"users": {"user_01": {"location": "living_room"}}},
        ),
        _case_entry(
            at=1,
            event_type="user.leaves_home",
            case_id="right_departure",
            expected_intent="departure_energy_saving",
            user_id="user_01",
            world={"users": {"user_01": {"location": "outside", "presence_state": "away"}}},
        ),
        _case_entry(
            at=2,
            event_type="security.presence_detected",
            case_id="wrong_on_purpose",
            # 故意错标：规则路径会给 security_presence_response
            expected_intent="arrival_comfort",
            room_id="living_room",
            payload={"device_id": "camera_living_02"},
            world={"users": {"user_01": {"location": "outside", "presence_state": "away"}}},
        ),
    ]
    eval_set = eval_intent.load_eval_set(_write_eval_set(tmp_path, entries))

    report = await eval_intent.run_eval(eval_set)

    assert report["rule_path"] == {
        "correct": 2,
        "total": 3,
        "accuracy": pytest.approx(2 / 3, abs=1e-3),
    }
    assert report["aggregate"]["correct"] == 2
    by_id = {case["case_id"]: case for case in report["cases"]}
    assert by_id["right_arrival"]["rule_correct"] is True
    assert by_id["wrong_on_purpose"]["rule_correct"] is False
    assert by_id["wrong_on_purpose"]["rule_intent"] == "security_presence_response"
    # 冒烟门：报告必须自己说明"没有阈值"，免得被当成质量条。
    assert report["smoke_test_only"] is True
    assert "SMOKE" in report["notice"].upper()


# --------------------------------------------------- 3. rule 与 hybrid 分开报


@pytest.mark.anyio
async def test_rule_path_accuracy_reported_separately_from_hybrid(tmp_path):
    entries = [
        _case_entry(
            at=0,
            event_type="user.arrives_home",
            case_id="arrival",
            expected_intent="arrival_comfort",
            user_id="user_01",
            world={"users": {"user_01": {"location": "living_room"}}},
        ),
        _case_entry(
            at=1,
            event_type="user.leaves_home",
            case_id="departure",
            expected_intent="departure_energy_saving",
            user_id="user_01",
            world={"users": {"user_01": {"location": "outside", "presence_state": "away"}}},
        ),
    ]
    eval_set = eval_intent.load_eval_set(_write_eval_set(tmp_path, entries))

    # mocked provider（永不打网）+ 显式打开 LLM 意图步：模型每次都给一个"错"的意图，
    # 于是 hybrid 准确率必然与 rule 准确率不同——这正是这道断言要证明的"两条路分开量"。
    provider = MockedLLMProvider(
        default_factory=lambda request: AgentLLMDecision(
            intent="ambience_mood_scene",
            confidence=0.9,
            explanation="ambience",
        )
    )
    report = await eval_intent.run_eval(
        eval_set,
        orchestrator=HomeOrchestratorAgent(llm_intent_enabled=True),
        llm_provider=provider,
    )

    assert report["rule_path"]["accuracy"] == pytest.approx(1.0)
    assert report["hybrid_path"]["accuracy"] == pytest.approx(0.0)
    assert report["rule_path"]["accuracy"] != report["hybrid_path"]["accuracy"]
    assert report["llm"]["llm_invoked"] is True
    # 聚合口径 = 端到端（hybrid）那条，不能悄悄用规则路径的数字冒充。
    assert report["aggregate"]["accuracy"] == pytest.approx(report["hybrid_path"]["accuracy"])
    for case in report["cases"]:
        assert case["rule_intent"] != case["hybrid_intent"]


@pytest.mark.anyio
async def test_mocked_mode_does_not_invoke_llm(tmp_path):
    """mocked 模式（单测默认）下编排器走纯规则：hybrid == rule，且明确标注未调用模型。"""

    entries = [
        _case_entry(
            at=0,
            event_type="user.arrives_home",
            case_id="arrival",
            expected_intent="arrival_comfort",
            user_id="user_01",
            world={"users": {"user_01": {"location": "living_room"}}},
        )
    ]
    eval_set = eval_intent.load_eval_set(_write_eval_set(tmp_path, entries))
    provider = MockedLLMProvider()

    report = await eval_intent.run_eval(eval_set, llm_provider=provider)

    assert report["llm"]["llm_invoked"] is False
    assert report["llm"]["mode"] == "mocked"
    assert provider.calls == []
    assert report["hybrid_path"] == report["rule_path"]


# ---------------------------------------------- 4. 报告记录模式/provider/评测集版本


@pytest.mark.anyio
async def test_report_records_mode_provider_and_eval_set_version(tmp_path):
    entries = [
        _case_entry(
            at=0,
            event_type="environment.temperature_threshold",
            case_id="temp_occupied",
            expected_intent="temperature_comfort",
            room_id="living_room",
            payload={"value": 29.5, "threshold": 27.0},
            world={"users": {"user_01": {"location": "living_room"}}},
        )
    ]
    path = _write_eval_set(tmp_path, entries, set_id="versioned_eval_v1")
    eval_set = eval_intent.load_eval_set(path)

    report = await eval_intent.run_eval(eval_set, llm_provider=MockedLLMProvider(model="mocked-x"))

    assert report["schema"] == eval_intent.REPORT_SCHEMA
    assert report["llm"]["mode"] == "mocked"
    assert report["llm"]["provider"] == "mocked"
    assert report["llm"]["model"] == "mocked-x"

    meta = report["eval_set"]
    assert meta["id"] == "versioned_eval_v1"
    assert meta["case_count"] == 1
    # §12.2：这是 dev split，不是 benchmark split——报告必须自己写明。
    assert meta["split"] == eval_intent.EVAL_SET_SPLIT == "dev"
    assert meta["scenario_schema_version"] == "1.1"
    assert len(meta["content_sha256"]) == 64
    assert meta["version"] == f"versioned_eval_v1@{meta['content_sha256'][:12]}"


def test_cli_writes_report_artifact_in_mocked_mode(tmp_path, capsys):
    report_path = tmp_path / "intent_eval_report.json"
    exit_code = eval_intent.main(
        [
            "--mode",
            "mocked",
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0, "冒烟门只在跑不通时失败，绝不因为准确率低而失败"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["schema"] == eval_intent.REPORT_SCHEMA
    assert report["eval_set"]["case_count"] == len(report["cases"]) >= 16
    assert report["llm"]["mode"] == "mocked"

    stdout = capsys.readouterr().out
    assert "SMOKE" in stdout.upper()
    assert "threshold" in stdout.lower()
