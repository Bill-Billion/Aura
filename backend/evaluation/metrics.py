"""§12.1 七指标的计算函数。

每个指标接收同一个 ``MetricsCollector``（已经过一遍事件流并建好索引），
返回一个 ``MetricDatum``——S4 评估器汇总成 EvalMetrics，然后按 success_criteria 判 pass/fail。

指标定义（规格 §12.1）：
  1. episode_completeness      — 六环事件完整性
  2. first_action_latency_ms   — 第一条命令从根事件到 proposed 的延迟
  3. command_success_rate      — 成功命令占比（排除 expected_failures）
  4. fallback_rate             — LLM 回退占比
  5. coordination_effectiveness — 仲裁决策中 approved / total 命令比
  6. safety_compliance         — safety 档事件是否被正确响应
  7. device_effect_accuracy    — expected_device_effects 达标率
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Union

# 兼容两种事件形态：内存中的 SimEvent 与 events.jsonl 读出来的 dict
event_like = Union["SimEvent", Mapping[str, Any]]

# 六环事件类型（§4.3）：按推理链顺序。S5 因果树与 S4 评估器共用这一份词表。
SIX_RING_EVENT_TYPES: tuple[str, ...] = (
    "reasoning.perception_snapshot",
    "reasoning.intent_recognized",
    "reasoning.task_decomposition",
    "reasoning.coordination_decision",
    "command.lifecycle",
    "feedback.state_delta",
)

# 被认为"成功"的生命周期终态（§10.1）。
_SUCCESS_LIFECYCLE_STATUSES: frozenset[str] = frozenset({"succeeded"})


def _get(event: event_like, key: str, default: Any = None) -> Any:
    """统一从 dict 或 SimEvent 取字段。"""
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _event_data(event: Any, key: str, default: Any = None) -> Any:
    """从事件的 data 字段取嵌套值，兼容 dict 和 SimEvent。"""
    data = _get(event, "data", {})
    if isinstance(data, Mapping):
        return data.get(key, default)
    return default


@dataclass
class MetricDatum:
    """一条指标的计算结果。"""

    name: str
    value: float
    unit: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricsCollector:
    """一次 run 的事件索引，所有指标函数从这里取数据。

    事件可以是 SimEvent 对象（内存）或 dict（events.jsonl 读取），
    通过 ``_get()`` 统一访问。
    """

    events: list[Any]  # SimEvent | dict
    scenario_id: str | None = None
    seed: int | None = None
    run_id: str = ""
    expected_failure_device_ids: set[str] = field(default_factory=set)
    expected_failure_categories: set[str] = field(default_factory=set)
    expected_device_effects: list[dict[str, Any]] = field(default_factory=list)
    success_criteria: dict[str, Any] = field(default_factory=dict)

    # 懒索引
    _by_type: dict[str, list[Any]] = field(default_factory=dict)
    _by_correlation: dict[str, list[Any]] = field(default_factory=dict)

    def events_of_type(self, event_type: str) -> list[Any]:
        if event_type not in self._by_type:
            self._by_type[event_type] = [e for e in self.events if _get(e, "event_type") == event_type]
        return self._by_type[event_type]

    def events_by_correlation(self, correlation_id: str) -> list[Any]:
        if correlation_id not in self._by_correlation:
            self._by_correlation[correlation_id] = [
                e for e in self.events if _get(e, "correlation_id") == correlation_id
            ]
        return self._by_correlation[correlation_id]

    @property
    def root_events(self) -> list[Any]:
        """所有根事件（causal_parent 为 None）。"""
        return [e for e in self.events if _get(e, "causal_parent") is None]

    @property
    def correlation_ids(self) -> set[str]:
        cids: set[str] = set()
        for e in self.events:
            cid = _get(e, "correlation_id")
            if cid:
                cids.add(cid)
        return cids

    @property
    def command_lifecycle_events(self) -> list[Any]:
        return self.events_of_type("command.lifecycle")

    @property
    def reasoning_events(self) -> list[Any]:
        return [
            e
            for e in self.events
            if str(_get(e, "event_type", "")).startswith("reasoning.")
        ]


# ------------------------------------------------------------------ 指标函数


def compute_episode_completeness(collector: MetricsCollector) -> MetricDatum:
    """指标 1：六环事件完整性。

    对每个 episode（correlation_id），检查是否出现了全部六环事件类型。
    返回所有 episode 的完整性均值。
    """

    cids = collector.correlation_ids
    if not cids:
        return MetricDatum(name="episode_completeness", value=0.0, unit="ratio")

    scores: list[float] = []
    details: dict[str, Any] = {"episodes": {}}

    for cid in sorted(cids):
        events = collector.events_by_correlation(cid)
        present = {_get(e, "event_type") for e in events}
        found = sum(1 for ring in SIX_RING_EVENT_TYPES if ring in present)
        score = found / len(SIX_RING_EVENT_TYPES)
        scores.append(score)
        details["episodes"][cid] = {
            "score": score,
            "found": found,
            "total": len(SIX_RING_EVENT_TYPES),
            "missing": [ring for ring in SIX_RING_EVENT_TYPES if ring not in present],
        }

    return MetricDatum(
        name="episode_completeness",
        value=sum(scores) / len(scores) if scores else 0.0,
        unit="ratio",
        details=details,
    )


def compute_first_action_latency_ms(collector: MetricsCollector) -> MetricDatum:
    """指标 2：第一条命令从根事件到 proposed 态的延迟（毫秒）。

    按 correlation_id 分组，找到每组中第一条 command.lifecycle（proposed），
    计算其与根事件的 sim_time 差值。
    """

    cids = collector.correlation_ids
    latencies: list[float] = []
    details: dict[str, Any] = {"by_episode": {}}

    for cid in sorted(cids):
        corr_events = collector.events_by_correlation(cid)
        root = next((e for e in corr_events if _get(e, "causal_parent") is None), None)
        if root is None:
            continue
        first_lifecycle = next(
            (e for e in corr_events if _get(e, "event_type") == "command.lifecycle"),
            None,
        )
        if first_lifecycle is None:
            continue
        latency = (_get(first_lifecycle, "timestamp", 0) - _get(root, "timestamp", 0)) * 1000
        latencies.append(latency)
        details["by_episode"][cid] = {
            "latency_ms": latency,
            "root_type": _get(root, "event_type"),
        }

    return MetricDatum(
        name="first_action_latency_ms",
        value=sum(latencies) / len(latencies) if latencies else 0.0,
        unit="ms",
        details=details,
    )


def compute_command_success_rate(collector: MetricsCollector) -> MetricDatum:
    """指标 3：命令成功率。

    排除 expected_failures 中声明的设备和类别后，计算 succeeded / total 的比例。
    """

    lifecycle = collector.command_lifecycle_events
    if not lifecycle:
        return MetricDatum(name="command_success_rate", value=1.0, unit="ratio")

    total = 0
    succeeded = 0
    expected_fails = 0
    details: dict[str, Any] = {"command_outcomes": []}

    for event in lifecycle:
        status = _event_data(event, "status", "")
        device_id = _event_data(event, "device_id", "")
        error_code = _event_data(event, "error_code", "")
        is_expected = (
            device_id in collector.expected_failure_device_ids
            or error_code in collector.expected_failure_categories
        )

        total += 1
        if is_expected:
            expected_fails += 1
        elif status in _SUCCESS_LIFECYCLE_STATUSES:
            succeeded += 1

        details["command_outcomes"].append(
            {
                "device_id": device_id,
                "status": status,
                "expected_failure": is_expected,
            }
        )

    evaluable = total - expected_fails
    rate = succeeded / evaluable if evaluable > 0 else 1.0

    return MetricDatum(
        name="command_success_rate",
        value=rate,
        unit="ratio",
        details={
            **details,
            "total": total,
            "succeeded": succeeded,
            "expected_failures": expected_fails,
            "evaluable": evaluable,
        },
    )


def compute_fallback_rate(collector: MetricsCollector) -> MetricDatum:
    """指标 4：LLM 回退占比。

    在 reasoning.execution_plan 事件中，统计 execution_mode == "fallback_rule_based" 的比例。
    """

    plans = collector.events_of_type("reasoning.execution_plan")
    if not plans:
        return MetricDatum(name="fallback_rate", value=0.0, unit="ratio")

    fallback_count = sum(
        1 for p in plans if _event_data(p, "execution_mode") == "fallback_rule_based"
    )

    return MetricDatum(
        name="fallback_rate",
        value=fallback_count / len(plans),
        unit="ratio",
        details={
            "total_execution_plans": len(plans),
            "fallback_count": fallback_count,
        },
    )


def compute_coordination_effectiveness(collector: MetricsCollector) -> MetricDatum:
    """指标 5：仲裁有效性。

    在所有 coordination_decision 事件中，统计 approved_commands / total_proposed 的比例。
    """

    decisions = collector.events_of_type("reasoning.coordination_decision")
    if not decisions:
        return MetricDatum(name="coordination_effectiveness", value=1.0, unit="ratio")

    total_proposed = 0
    total_approved = 0

    for d in decisions:
        per_agent = _event_data(d, "per_agent", [])
        approved = _event_data(d, "approved_commands", [])
        total_proposed += sum(len(entry.get("commands", [])) for entry in per_agent)
        total_approved += len(approved)

    rate = total_approved / total_proposed if total_proposed > 0 else 1.0

    return MetricDatum(
        name="coordination_effectiveness",
        value=rate,
        unit="ratio",
        details={
            "total_decisions": len(decisions),
            "total_proposed": total_proposed,
            "total_approved": total_approved,
        },
    )


def compute_safety_compliance(collector: MetricsCollector) -> MetricDatum:
    """指标 6：安全合规。

    检查 safety.smoke_detected / safety.* 类事件是否触发了 SecurityAgent 的响应
    （security_agent 出现在 coordination_decision 的 per_agent 中且安全档优先）。
    """

    safety_roots = [
        e for e in collector.root_events if str(_get(e, "event_type", "")).startswith("safety.")
    ]
    if not safety_roots:
        return MetricDatum(
            name="safety_compliance",
            value=1.0,
            unit="ratio",
            details={"note": "no safety events in this run"},
        )

    responded = 0
    details: dict[str, Any] = {"safety_events": []}

    for root in safety_roots:
        corr_events = collector.events_by_correlation(_get(root, "correlation_id", ""))
        decisions = [e for e in corr_events if _get(e, "event_type") == "reasoning.coordination_decision"]
        has_security = any(
            "security_agent" in str(_event_data(d, "per_agent", [])) for d in decisions
        )
        has_safety_priority = any(
            _event_data(d, "winning_priority") == "safety" for d in decisions
        )
        is_compliant = has_security or has_safety_priority
        if is_compliant:
            responded += 1
        details["safety_events"].append(
            {
                "event_id": _get(root, "event_id"),
                "event_type": _get(root, "event_type"),
                "compliant": is_compliant,
                "has_security_agent": has_security,
                "has_safety_priority": has_safety_priority,
            }
        )

    return MetricDatum(
        name="safety_compliance",
        value=responded / len(safety_roots),
        unit="ratio",
        details=details,
    )


def compute_device_effect_accuracy(collector: MetricsCollector) -> MetricDatum:
    """指标 7：设备效果准确率。

    对每个 expected_device_effect，找到最后一个 feedback.state_delta 或最终的设备状态，
    检查是否满足 expected 约束。这里简化为检查 state_delta 中的最终值是否匹配 expected。
    """

    expected_effects = collector.expected_device_effects
    if not expected_effects:
        return MetricDatum(
            name="device_effect_accuracy",
            value=1.0,
            unit="ratio",
            details={"note": "no expected_device_effects declared"},
        )

    # 收集所有 state_delta，按 device_id 取最终值
    deltas = collector.events_of_type("feedback.state_delta")
    final_state: dict[str, dict[str, Any]] = {}
    for delta in deltas:
        device_id = _event_data(delta, "device_id", "")
        changes = _event_data(delta, "changes", {})
        if device_id not in final_state:
            final_state[device_id] = {}
        final_state[device_id].update(changes)

    matched = 0
    details: dict[str, Any] = {"effects": []}

    for effect in expected_effects:
        device_id = effect.get("device_id", "")
        expected = effect.get("expected", {})
        state = final_state.get(device_id, {})
        effect_matched = True
        mismatches: list[str] = []

        for key, constraint in expected.items():
            actual = state.get(key)
            if isinstance(constraint, dict):
                # ExpectedValue shape: {equals, min, max, one_of}
                if "equals" in constraint:
                    if actual != constraint["equals"]:
                        effect_matched = False
                        mismatches.append(f"{key}: expected={constraint['equals']}, got={actual}")
                if "min" in constraint and (not isinstance(actual, (int, float)) or actual < constraint["min"]):
                    effect_matched = False
                    mismatches.append(f"{key}: min={constraint['min']}, got={actual}")
                if "max" in constraint and (not isinstance(actual, (int, float)) or actual > constraint["max"]):
                    effect_matched = False
                    mismatches.append(f"{key}: max={constraint['max']}, got={actual}")
            elif actual != constraint:
                effect_matched = False
                mismatches.append(f"{key}: expected={constraint}, got={actual}")

        if effect_matched:
            matched += 1
        details["effects"].append(
            {
                "device_id": device_id,
                "matched": effect_matched,
                "mismatches": mismatches,
            }
        )

    return MetricDatum(
        name="device_effect_accuracy",
        value=matched / len(expected_effects),
        unit="ratio",
        details=details,
    )
