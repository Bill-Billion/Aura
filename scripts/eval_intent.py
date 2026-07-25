#!/usr/bin/env python3
"""S3-T9：意图分类评测跑法（§8.3 意图识别 / §7 事件→设备映射 / §12.2 标注规则）。

用法::

    python scripts/eval_intent.py --mode mocked            # 纯规则路径，绝不打网
    python scripts/eval_intent.py --mode recorded --run-id my_run
    python scripts/eval_intent.py --mode live --llm-intent  # 只用于产品验证，不用于声明

============================ 这道门是什么，不是什么 ============================

**是**：一条端到端跑得通的意图分类基线——评测集能加载、编排器能对每条 case 给出意图、
报告把"规则路径准确率"与"混合（LLM 精炼）路径准确率"**分开**记下来，于是"LLM 相对
规则有没有增量"这个问题第一次可以被量出来。

**不是**：质量门。本脚本**不断言任何准确率阈值**，跑不通才返回非零；准确率低不会失败。
两条理由，报告里也原样写着：

  1. 17 条 case、单一 ``dev`` split，规模上没有统计意义（§12.2 要求 benchmark split 上
     不得调参——这里压根还没有 benchmark split）；
  2. ``expected_intent`` 标签取自 spec §7 映射表，而规则路径正是同一张表的实现，
     它的分数是**一致性检查**，不是模型能力。

S4 的评估器会把这里吸收进套件跑法，因此本脚本刻意**只算意图准确率**，不掺 S4 的
first_action_latency / command_failure / device_state_match 等指标。

**绝不打网**：``--mode mocked`` 下 provider 是 :class:`MockedLLMProvider`，而且编排器在
mocked 模式下按 §11.1 自动走纯规则路径（``HomeOrchestratorAgent._should_call_llm``）。
测试一律用这条路径。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

# scripts/ 不是包（不加 __init__.py，免得 pytest 把它当测试根）；直接跑脚本时
# sys.path[0] 是 scripts/ 自己，必须把仓库根塞进来才 import 得到 backend.*。
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from backend.agents.contracts import OrchestrationPolicy, RootEventContext  # noqa: E402
from backend.agents.llm import LLMProvider  # noqa: E402
from backend.agents.llm_modes import (  # noqa: E402
    LLMMode,
    build_provider_for_mode,
    recordings_path,
    resolve_mode_for_provider,
)
from backend.agents.orchestrator import (  # noqa: E402
    DomainAgentBinding,
    HomeOrchestratorAgent,
    OrchestrationDecision,
)
from backend.agents.runtime import AgentRuntime, build_default_agents  # noqa: E402
from backend.engine.event_bus import SimEvent  # noqa: E402
from backend.engine.run_manager import new_run_id  # noqa: E402
from backend.engine.state import WorldState  # noqa: E402
from backend.engine.state_manager import StateManager  # noqa: E402
from backend.scenarios.apply import apply_initial_state  # noqa: E402
from backend.scenarios.generator import GenerationContext, ScriptedEventSource  # noqa: E402
from backend.scenarios.loader import ScenarioLoadError, load_scenario_file  # noqa: E402
from backend.scenarios.spec import InitialState, ScenarioSpec, TimelineEvent  # noqa: E402

__all__ = [
    "EVAL_CASE_KEY",
    "EVAL_SET_DIR",
    "EVAL_SET_SPLIT",
    "DEFAULT_EVAL_SET_PATH",
    "REPORT_FILENAME",
    "REPORT_SCHEMA",
    "SMOKE_ONLY_NOTICE",
    "IntentEvalError",
    "IntentEvalCase",
    "IntentEvalSet",
    "CaseResult",
    "load_eval_set",
    "build_case_world",
    "build_case_event",
    "build_case_context",
    "default_bindings",
    "evaluate_case",
    "run_eval",
    "write_report",
    "main",
]


# ---------------------------------------------------------------------------
# 常量（跨阶段契约）
# ---------------------------------------------------------------------------

EVAL_SET_DIR = _REPO_ROOT / "backend" / "scenarios" / "eval"
"""评测集目录。critic 定死：eval/ 与 library/ 共用 S2 的 ``load_library(dirs)``，
**不是**仓库根的 scenarios/。"""

DEFAULT_EVAL_SET_PATH = EVAL_SET_DIR / "intent_eval_set.yaml"

EVAL_CASE_KEY = "eval_case"
"""timeline 条目里承载评测元数据的 payload 键。

它是**评估侧**的东西：:func:`build_case_event` 在构造根事件之前就把它整个摘掉，
标签只经 ``RootEventContext.ground_truth_labels`` 交给评估侧（§2.3 可观测/真值分离）。
"""

EVAL_SET_SPLIT = "dev"
"""§12.2：这份集合是 dev split。还没有 benchmark split，因此也谈不上"在 benchmark 上调参"——
但报告必须把这件事写出来，免得下一个人默认它是 held-out。"""

REPORT_SCHEMA = "aura.intent_eval_report.v1"
REPORT_FILENAME = "intent_eval_report.json"
"""报告工件名。与 S2 的 events.jsonl / llm_recordings.jsonl 同住 data/runs/{run_id}/——
"这次评测用的哪份录制"因此不需要第二套簿记，看目录就知道。"""

SMOKE_ONLY_NOTICE = (
    "SMOKE TEST ONLY: no accuracy threshold is asserted anywhere in this report. "
    "冒烟门——本报告不断言任何准确率阈值：case 数是冒烟规模（单一 dev split），"
    "且 expected_intent 标签与规则路径同源于 spec §7 映射表，"
    "规则路径的分数是一致性检查而非模型能力。"
)


class IntentEvalError(Exception):
    """评测集本身有问题（缺标签、case_id 重复、文件读不了）。

    刻意不吞：一条没有标签的 case 会静默把分母变小，让准确率凭空变好看。
    """


# ---------------------------------------------------------------------------
# 评测集模型
# ---------------------------------------------------------------------------


class IntentEvalCase(BaseModel):
    """一条带标签的意图 case。

    ``world`` 是本 case 的**可观测**世界覆盖（叠在集合级 initial_state 之上），
    不是真值；真值只有 ``expected_intent`` / ``expected_domain`` 两项。
    """

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    expected_intent: str = Field(min_length=1)
    expected_domain: str | None = None
    note: str = ""
    world: InitialState = Field(default_factory=InitialState)
    # payload 里的 EVAL_CASE_KEY 已在装载时剥离——这个 entry 就是 agent 将会看到的那条。
    entry: TimelineEvent


class IntentEvalSet(BaseModel):
    """一份评测集：合法 ScenarioSpec + 从 timeline 抽出来的 case 列表。"""

    model_config = ConfigDict(extra="forbid")

    spec: ScenarioSpec
    cases: list[IntentEvalCase]
    path: Path | None = None
    content_sha256: str = ""
    split: str = EVAL_SET_SPLIT

    @property
    def set_id(self) -> str:
        return self.spec.id

    @property
    def version(self) -> str:
        """内容锚定的版本串：``{id}@{content_sha12}``。

        比"手写一个 version 字段"可信：改了任意一条 case，版本自动变；忘了改版本号
        这件事在结构上不可能发生。
        """

        return f"{self.spec.id}@{self.content_sha256[:12]}"

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.spec.id,
            "name": self.spec.name,
            "version": self.version,
            "scenario_schema_version": self.spec.scenario_schema_version,
            "content_sha256": self.content_sha256,
            "split": self.split,
            "case_count": len(self.cases),
            "seed": self.spec.seed,
            "path": str(self.path) if self.path is not None else None,
        }


def load_eval_set(path: Path | str = DEFAULT_EVAL_SET_PATH) -> IntentEvalSet:
    """走 S2 loader 加载评测集，并把每条 timeline 项拆成一条带标签 case。

    用 :func:`load_scenario_file` 而不是自己 ``yaml.safe_load``：评测集与场景库共用同一套
    §14 版本分支、注册表引用校验与错误码，评测集里写错一个设备 id 应该在这里就炸。
    """

    path = Path(path)
    try:
        spec = load_scenario_file(path)
    except ScenarioLoadError as exc:
        raise IntentEvalError(f"评测集加载失败：{exc}") from exc

    cases: list[IntentEvalCase] = []
    seen: set[str] = set()
    for index, entry in enumerate(spec.timeline):
        payload = dict(entry.payload or {})
        raw_case = payload.pop(EVAL_CASE_KEY, None)
        if not isinstance(raw_case, dict):
            raise IntentEvalError(
                f"{path}: timeline 第 {index} 条（type={entry.type}）缺少 "
                f"payload.{EVAL_CASE_KEY}——评测集里每条根事件都必须带标签"
            )
        stripped_entry = entry.model_copy(update={"payload": payload})
        try:
            case = IntentEvalCase(**{**raw_case, "entry": stripped_entry})
        except Exception as exc:  # pydantic ValidationError 等
            raise IntentEvalError(f"{path}: timeline 第 {index} 条的 {EVAL_CASE_KEY} 非法：{exc}") from exc
        if case.case_id in seen:
            raise IntentEvalError(f"{path}: case_id {case.case_id!r} 重复（报告按它对齐）")
        seen.add(case.case_id)
        cases.append(case)

    if not cases:
        raise IntentEvalError(f"{path}: 评测集为空")

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return IntentEvalSet(spec=spec, cases=cases, path=path, content_sha256=digest)


# ---------------------------------------------------------------------------
# 单条 case 的世界 / 根事件 / 上下文
# ---------------------------------------------------------------------------


def build_case_world(eval_set: IntentEvalSet, case: IntentEvalCase) -> StateManager:
    """默认世界 + 集合级 initial_state + 本 case 的覆盖。

    延迟 import ``backend.main``（FastAPI 应用模块）与 S2 runner 同口径：世界只有
    ``_init_default_state()`` 一个来源，覆盖一律走 :func:`apply_initial_state`
    （每条都是一条 caused_by="scenario_loader" 的 delta，可归因）。
    """

    from backend.main import _init_default_state

    state_manager = _init_default_state(eval_set.spec.initial_state)
    apply_initial_state(
        state_manager,
        case.world,
        reason=f"intent_eval_case:{case.case_id}",
    )
    return state_manager


def build_case_event(
    eval_set: IntentEvalSet,
    case: IntentEvalCase,
    world: WorldState,
    *,
    run_id: str | None = None,
) -> SimEvent:
    """用 S2 的 scripted 产线造根事件（不自己拼一套事件 data 规则）。

    走 :class:`ScriptedEventSource` 的收益是保真：``from_room``/``to_room``/
    ``event_generation_mode='scripted'``/``scenario_id`` 这些键与真跑一次场景时**一模一样**，
    评测因此量的是编排器在真实事件形状上的表现，而不是在一份评测专用的假事件上的表现。
    """

    case_spec = eval_set.spec.model_copy(update={"timeline": [case.entry]})
    source = ScriptedEventSource(
        case_spec,
        context=GenerationContext(run_id=run_id, scenario_id=eval_set.spec.id),
    )
    generated = source.emit(world, trigger=None, sim_time_s=case.entry.at)
    if not generated:
        raise IntentEvalError(f"case {case.case_id} 没有产出根事件（at={case.entry.at}）")
    return generated[0].event


def build_case_context(
    eval_set: IntentEvalSet,
    case: IntentEvalCase,
    *,
    policy: OrchestrationPolicy | None = None,
    run_id: str | None = None,
) -> RootEventContext:
    """§8.3 输入上下文。标签只进 ``ground_truth_labels``，绝不进事件 data。"""

    state_manager = build_case_world(eval_set, case)
    event = build_case_event(eval_set, case, state_manager.world, run_id=run_id)
    return RootEventContext.from_observable_world(
        root_event=event,
        observable_world=state_manager.world,
        run_id=run_id,
        scenario_id=eval_set.spec.id,
        ground_truth_labels={
            "case_id": case.case_id,
            "expected_intent": case.expected_intent,
            "expected_domain": case.expected_domain,
        },
        policy=policy,
    )


def default_bindings() -> list[DomainAgentBinding]:
    """§8.2 五个域 agent 的绑定，**顺序 = 注册顺序**（= TaskPlan.domain_tasks 顺序）。"""

    return [DomainAgentBinding.from_agent(agent) for agent in build_default_agents()]


# ---------------------------------------------------------------------------
# 逐条评测
# ---------------------------------------------------------------------------


class CaseResult(BaseModel):
    """一条 case 的两条路径结果。rule 与 hybrid 并列存在，正是为了能相减。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    event_type: str
    activity: str | None = None
    expected_intent: str
    expected_domain: str | None = None
    occupied_room_ids: list[str] = Field(default_factory=list)
    time_of_day: str = ""

    rule_intent: str = ""
    rule_domain: str = ""
    rule_correct: bool = False
    rule_domain_correct: bool = False
    rule_confidence: float = 0.0

    hybrid_intent: str = ""
    hybrid_domain: str = ""
    hybrid_correct: bool = False
    hybrid_domain_correct: bool = False
    hybrid_confidence: float = 0.0
    confidence_source: str = ""
    outcome: str = ""
    fallback_reason: str | None = None
    agent_roles: list[str] = Field(default_factory=list)
    llm_invoked: bool = False


def _domain_matches(expected: str | None, actual: str) -> bool:
    # 没标 domain 的 case 不参与 domain 统计（按"未标注"处理，见 run_eval 的分母）。
    return expected is not None and expected == actual


async def evaluate_case(
    eval_set: IntentEvalSet,
    case: IntentEvalCase,
    *,
    orchestrator: HomeOrchestratorAgent,
    bindings: Sequence[DomainAgentBinding],
    llm_provider: LLMProvider | None = None,
    policy: OrchestrationPolicy | None = None,
    run_id: str | None = None,
) -> CaseResult:
    """同一条 case 上跑两次编排：纯规则一次、混合一次。

    两次用**同一份上下文**：世界只造一次，两条路径看到的输入逐位相同，差异因此只可能
    来自 LLM 那一步。
    """

    context = build_case_context(eval_set, case, policy=policy, run_id=run_id)

    rule_decision: OrchestrationDecision = orchestrator.plan_rule_based(context, bindings)
    hybrid_decision: OrchestrationDecision = await orchestrator.plan(
        context, bindings, llm_provider=llm_provider
    )

    return CaseResult(
        case_id=case.case_id,
        event_type=case.entry.type,
        activity=case.entry.activity,
        expected_intent=case.expected_intent,
        expected_domain=case.expected_domain,
        occupied_room_ids=list(context.observable_state.occupied_room_ids),
        time_of_day=context.observable_state.time_of_day,
        rule_intent=rule_decision.plan.intent,
        rule_domain=rule_decision.rule_intent.domain,
        rule_correct=rule_decision.plan.intent == case.expected_intent,
        rule_domain_correct=_domain_matches(case.expected_domain, rule_decision.rule_intent.domain),
        rule_confidence=rule_decision.plan.confidence,
        hybrid_intent=hybrid_decision.plan.intent,
        hybrid_domain=hybrid_decision.rule_intent.domain,
        hybrid_correct=hybrid_decision.plan.intent == case.expected_intent,
        hybrid_domain_correct=_domain_matches(
            case.expected_domain, hybrid_decision.rule_intent.domain
        ),
        hybrid_confidence=hybrid_decision.plan.confidence,
        confidence_source=hybrid_decision.plan.confidence_source.value,
        outcome=hybrid_decision.outcome.value,
        fallback_reason=hybrid_decision.plan.fallback_reason,
        agent_roles=list(hybrid_decision.plan.agent_roles),
        # provider 仍是 "rule_based" ⇒ 这一轮根本没调模型（§11.1 mocked 或没给 provider）。
        llm_invoked=hybrid_decision.provider != "rule_based",
    )


def _score(correct: int, total: int) -> dict[str, Any]:
    return {
        "correct": correct,
        "total": total,
        "accuracy": round(correct / total, 4) if total else 0.0,
    }


async def run_eval(
    eval_set: IntentEvalSet,
    *,
    orchestrator: HomeOrchestratorAgent | None = None,
    bindings: Sequence[DomainAgentBinding] | None = None,
    llm_provider: LLMProvider | None = None,
    policy: OrchestrationPolicy | None = None,
    run_id: str | None = None,
    requested_mode: str | None = None,
) -> dict[str, Any]:
    """跑完整份评测集，返回报告 dict（不落盘；落盘是 :func:`write_report` 的事）。"""

    orchestrator = orchestrator or HomeOrchestratorAgent()
    bindings = list(bindings) if bindings is not None else default_bindings()

    results: list[CaseResult] = []
    for case in eval_set.cases:
        results.append(
            await evaluate_case(
                eval_set,
                case,
                orchestrator=orchestrator,
                bindings=bindings,
                llm_provider=llm_provider,
                policy=policy,
                run_id=run_id,
            )
        )

    total = len(results)
    rule_path = _score(sum(1 for r in results if r.rule_correct), total)
    hybrid_path = _score(sum(1 for r in results if r.hybrid_correct), total)
    labeled_domains = sum(1 for r in results if r.expected_domain is not None)

    mode = resolve_mode_for_provider(llm_provider) if llm_provider is not None else LLMMode.MOCKED
    llm_invoked = any(r.llm_invoked for r in results)

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # 这两个键是给"三个月后翻到这份 json 的人"看的，不是装饰：没有它们，
        # 一个 1.000 的准确率看起来就像一条质量结论。
        "smoke_test_only": True,
        "notice": SMOKE_ONLY_NOTICE,
        "eval_set": eval_set.metadata(),
        "llm": {
            "mode": mode.value,
            "requested_mode": requested_mode,
            "provider": str(getattr(llm_provider, "provider_name", "rule_based") or "rule_based"),
            "model": str(getattr(llm_provider, "model", "") or "rule_based"),
            "llm_invoked": llm_invoked,
            "note": (
                "编排器调用了 LLM 精炼意图"
                if llm_invoked
                else "编排器走纯规则路径（§11.1 mocked / 未提供 provider），hybrid 等于 rule"
            ),
        },
        "orchestrator": {
            "orchestrator_id": orchestrator.orchestrator_id,
            "llm_intent_enabled": orchestrator.llm_intent_enabled,
            "agent_ids": [binding.agent_id for binding in bindings],
        },
        # 三个口径分开报：rule 是"§7 表实现得对不对"，hybrid 是"端到端产出什么"，
        # aggregate 取 hybrid——聚合数不能偷偷用规则路径的分数冒充端到端表现。
        "rule_path": rule_path,
        "hybrid_path": hybrid_path,
        "aggregate": dict(hybrid_path),
        "domain_accuracy": {
            "rule": _score(sum(1 for r in results if r.rule_domain_correct), labeled_domains),
            "hybrid": _score(sum(1 for r in results if r.hybrid_domain_correct), labeled_domains),
        },
        "cases": [result.model_dump(mode="json") for result in results],
    }
    return report


def write_report(report: dict[str, Any], path: Path | str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="意图分类冒烟评测（不断言任何准确率阈值）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--eval-set", default=str(DEFAULT_EVAL_SET_PATH), help="评测集 YAML")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in LLMMode],
        default=LLMMode.RECORDED.value,
        # 决策 #7：benchmark 必须 recorded（live 依赖"当天那个模型"，第三方复现不了）。
        help="§11.1 LLM 模式；benchmark 声明只认 recorded",
    )
    # run_id 必须是 §11 的合法形状（run-YYYYmmddTHHMMSS-xxxxxxxx）；不给就新开一个 run 目录。
    # 回放既有录制时，把当初那次的 --run-id 传回来（或直接 --recordings 指到文件）。
    parser.add_argument("--run-id", default=None, help="录制/报告落在 data/runs/{run_id}/（默认新开一个）")
    parser.add_argument("--recordings", default=None, help="录制文件路径（默认 data/runs/{run_id}/llm_recordings.jsonl）")
    parser.add_argument("--report", default=None, help="报告输出路径（默认 data/runs/{run_id}/intent_eval_report.json）")
    parser.add_argument(
        "--strict-mock",
        action="store_true",
        help="mocked 模式下 fixture 未命中即失败（默认落到确定性默认决策）",
    )
    intent_group = parser.add_mutually_exclusive_group()
    intent_group.add_argument(
        "--llm-intent",
        dest="llm_intent",
        action="store_true",
        default=None,
        help="强制打开 LLM 意图步（默认按模式自动：mocked 不调）",
    )
    intent_group.add_argument(
        "--no-llm-intent",
        dest="llm_intent",
        action="store_false",
        help="强制关闭 LLM 意图步（纯规则）",
    )
    parser.add_argument("--quiet", action="store_true", help="只打印汇总，不逐条打印")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)

    try:
        eval_set = load_eval_set(args.eval_set)
    except IntentEvalError as exc:
        print(f"[intent-eval] {exc}", file=sys.stderr)
        return 2

    mode = LLMMode(args.mode)
    run_id = args.run_id or new_run_id()
    try:
        recordings = Path(args.recordings) if args.recordings else recordings_path(run_id)
        provider = build_provider_for_mode(
            mode,
            live_provider_factory=AgentRuntime._build_default_provider,
            recordings_path=recordings,
            strict_mock=args.strict_mock,
        )
    except Exception as exc:  # provider/路径装配失败要说清楚是装配失败，不是评测失败
        print(f"[intent-eval] provider 装配失败（mode={mode.value}）：{exc}", file=sys.stderr)
        return 2

    # recorded 的读写分岔靠"录制文件在不在"（见 build_provider_for_mode）。第一次跑会
    # 真的调用配置好的 provider——这件事必须在跑之前说出来，别让人以为 recorded 天生离线。
    if mode is LLMMode.RECORDED and not recordings.exists():
        print(f"[intent-eval] recorded 首跑：录制文件不存在，将调用真实 provider 并写入 {recordings}")

    orchestrator = HomeOrchestratorAgent(llm_intent_enabled=args.llm_intent)
    report = asyncio.run(
        run_eval(
            eval_set,
            orchestrator=orchestrator,
            llm_provider=provider,
            run_id=run_id,
            requested_mode=mode.value,
        )
    )

    report_path = Path(args.report) if args.report else recordings.parent / REPORT_FILENAME
    write_report(report, report_path)

    meta = report["eval_set"]
    llm = report["llm"]
    print(
        f"[intent-eval] 评测集 {meta['version']}（split={meta['split']}，{meta['case_count']} cases）"
        f" run_id={run_id} mode={llm['mode']} provider={llm['provider']}/{llm['model']}"
        f" llm_invoked={llm['llm_invoked']}"
    )
    if not args.quiet:
        for case in report["cases"]:
            flag = "OK  " if case["hybrid_correct"] else "MISS"
            print(
                f"[intent-eval] {flag} {case['case_id']:38s} expected={case['expected_intent']:32s}"
                f" rule={case['rule_intent']:32s} hybrid={case['hybrid_intent']}"
            )
    rule, hybrid = report["rule_path"], report["hybrid_path"]
    print(
        f"[intent-eval] rule   {rule['correct']}/{rule['total']} = {rule['accuracy']:.3f}\n"
        f"[intent-eval] hybrid {hybrid['correct']}/{hybrid['total']} = {hybrid['accuracy']:.3f}"
    )
    print(f"[intent-eval] 报告已写入 {report_path}")
    print(f"[intent-eval] {SMOKE_ONLY_NOTICE}")
    # 退出码只反映"跑没跑通"：准确率低**不是**失败（本脚本没有质量阈值）。
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    raise SystemExit(main())
