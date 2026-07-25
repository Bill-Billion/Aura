"""§12 评估器：七指标 + success_criteria 判定 + 报告。

从一次 run 的 events.jsonl + run.json 计算 EvalReport。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from backend.engine.event_bus import SimEvent
from backend.engine.event_log import RUN_METADATA_FILENAME, read_run_events, read_run_metadata, run_dir
from backend.evaluation.metrics import (
    MetricDatum,
    MetricsCollector,
    _get,
    compute_command_success_rate,
    compute_coordination_effectiveness,
    compute_device_effect_accuracy,
    compute_episode_completeness,
    compute_fallback_rate,
    compute_first_action_latency_ms,
    compute_safety_compliance,
)


class EvalOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


@dataclass
class EvalMetrics:
    """七指标的一次计算快照。"""

    episode_completeness: MetricDatum
    first_action_latency_ms: MetricDatum
    command_success_rate: MetricDatum
    fallback_rate: MetricDatum
    coordination_effectiveness: MetricDatum
    safety_compliance: MetricDatum
    device_effect_accuracy: MetricDatum

    # 便利方法：转成 dict（给报告序列化用）
    def to_dict(self) -> dict[str, Any]:
        def _datum(d: MetricDatum) -> dict[str, Any]:
            return {"name": d.name, "value": d.value, "unit": d.unit, "details": d.details}

        return {
            "episode_completeness": _datum(self.episode_completeness),
            "first_action_latency_ms": _datum(self.first_action_latency_ms),
            "command_success_rate": _datum(self.command_success_rate),
            "fallback_rate": _datum(self.fallback_rate),
            "coordination_effectiveness": _datum(self.coordination_effectiveness),
            "safety_compliance": _datum(self.safety_compliance),
            "device_effect_accuracy": _datum(self.device_effect_accuracy),
        }


@dataclass
class EvalReport:
    """一份 run 的完整评估报告——S5 对比视图直接消费的形状。"""

    run_id: str
    scenario_id: str | None
    seed: int | None
    outcome: EvalOutcome
    metrics: EvalMetrics
    criteria_checks: dict[str, bool] = field(default_factory=dict)
    failure_reasons: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "outcome": self.outcome.value,
            "metrics": self.metrics.to_dict(),
            "criteria_checks": self.criteria_checks,
            "failure_reasons": self.failure_reasons,
            "metadata": self.metadata,
        }


class ScenarioEvaluator:
    """§12.2 的场景级评估器：一组事件的七指标计算 + success_criteria 判定。"""

    def __init__(self, success_criteria: dict[str, Any] | None = None) -> None:
        self._criteria = success_criteria or {}

    def evaluate(
        self,
        events: list[SimEvent],
        *,
        run_id: str = "",
        scenario_id: str | None = None,
        seed: int | None = None,
        expected_failure_device_ids: set[str] | None = None,
        expected_failure_categories: set[str] | None = None,
        expected_device_effects: list[dict[str, Any]] | None = None,
    ) -> EvalReport:
        """计算七指标并按 success_criteria 判定 pass/fail。

        每个指标都是本模块独立产出（一条数据入口），不依赖外部副作用；
        S4-T4 suite runner 把这份报告原样序列化到 data/runs/{run_id}/report.json。
        """

        collector = MetricsCollector(
            events=events,
            scenario_id=scenario_id,
            seed=seed,
            run_id=run_id,
            expected_failure_device_ids=expected_failure_device_ids or set(),
            expected_failure_categories=expected_failure_categories or set(),
            expected_device_effects=expected_device_effects or [],
            success_criteria=self._criteria,
        )

        metrics = EvalMetrics(
            episode_completeness=compute_episode_completeness(collector),
            first_action_latency_ms=compute_first_action_latency_ms(collector),
            command_success_rate=compute_command_success_rate(collector),
            fallback_rate=compute_fallback_rate(collector),
            coordination_effectiveness=compute_coordination_effectiveness(collector),
            safety_compliance=compute_safety_compliance(collector),
            device_effect_accuracy=compute_device_effect_accuracy(collector),
        )

        criteria_checks, failure_reasons = self._check_criteria(metrics)

        return EvalReport(
            run_id=run_id,
            scenario_id=scenario_id,
            seed=seed,
            outcome=EvalOutcome.FAIL if failure_reasons else EvalOutcome.PASS,
            metrics=metrics,
            criteria_checks=criteria_checks,
            failure_reasons=failure_reasons,
            metadata={
                "total_events": len(events),
                "total_episodes": len(collector.correlation_ids),
                "total_commands": len(collector.command_lifecycle_events),
            },
        )

    def _check_criteria(self, metrics: EvalMetrics) -> tuple[dict[str, bool], list[str]]:
        """按 success_criteria 逐条判定。"""

        checks: dict[str, bool] = {}
        failures: list[str] = []

        # 1) require_complete_episode：至少一个 episode 达到 100% 六环完整
        if self._criteria.get("require_complete_episode", True):
            eps = metrics.episode_completeness.details.get("episodes", {})
            has_full = any(v.get("score", 0) >= 1.0 for v in eps.values() if isinstance(v, dict))
            checks["require_complete_episode"] = has_full
            if not has_full:
                failures.append(
                    f"require_complete_episode: no episode has all {len(metrics.episode_completeness.details.get('episodes', {}))} episodes with 100% completeness"
                )

        # 2) max_first_action_latency_ms
        max_latency = self._criteria.get("max_first_action_latency_ms")
        if max_latency is not None:
            ok = metrics.first_action_latency_ms.value <= max_latency
            checks["max_first_action_latency_ms"] = ok
            if not ok:
                failures.append(
                    f"max_first_action_latency_ms: {metrics.first_action_latency_ms.value:.1f}ms > {max_latency}ms"
                )

        # 3) max_command_failures（排除 expected_failures 后）
        max_failures = self._criteria.get("max_command_failures")
        if max_failures is not None:
            details = metrics.command_success_rate.details
            total = details.get("total", 0)
            succeeded = details.get("succeeded", 0)
            expected = details.get("expected_failures", 0)
            actual_failures = total - succeeded - expected
            ok = actual_failures <= max_failures
            checks["max_command_failures"] = ok
            if not ok:
                failures.append(
                    f"max_command_failures: {actual_failures} unexpected failures > {max_failures} allowed"
                )

        # 4) allow_fallback：如果不允许回退，检查 fallback_rate == 0
        if not self._criteria.get("allow_fallback", True):
            ok = metrics.fallback_rate.value == 0.0
            checks["allow_fallback"] = ok
            if not ok:
                failures.append(
                    f"allow_fallback=false but fallback_rate={metrics.fallback_rate.value:.2f}"
                )

        return checks, failures


def evaluate_run(
    run_id: str,
    *,
    scenario_id: str | None = None,
    seed: int | None = None,
    success_criteria: dict[str, Any] | None = None,
    expected_failure_device_ids: set[str] | None = None,
    expected_failure_categories: set[str] | None = None,
    expected_device_effects: list[dict[str, Any]] | None = None,
    data_root: Path | str | None = None,
) -> EvalReport:
    """从一次已完成的 run 的持久化工件计算评估报告。

    这是 S4-T5 的 ``GET /api/runs/{run_id}/report`` 与 S4-T4 suite runner 的统一入口。
    """

    try:
        all_events, _ = read_run_events(run_id)
    except Exception as exc:
        return EvalReport(
            run_id=run_id,
            scenario_id=None,
            seed=None,
            outcome=EvalOutcome.ERROR,
            metrics=EvalMetrics(
                episode_completeness=MetricDatum("episode_completeness", 0.0),
                first_action_latency_ms=MetricDatum("first_action_latency_ms", 0.0),
                command_success_rate=MetricDatum("command_success_rate", 0.0),
                fallback_rate=MetricDatum("fallback_rate", 0.0),
                coordination_effectiveness=MetricDatum("coordination_effectiveness", 0.0),
                safety_compliance=MetricDatum("safety_compliance", 0.0),
                device_effect_accuracy=MetricDatum("device_effect_accuracy", 0.0),
            ),
            failure_reasons=[f"cannot read events for run {run_id}: {exc}"],
        )

    try:
        metadata = read_run_metadata(run_id)
    except Exception:
        metadata = {}

    evaluator = ScenarioEvaluator(success_criteria=success_criteria)

    return evaluator.evaluate(
        list(all_events),
        run_id=run_id,
        scenario_id=scenario_id or metadata.get("scenario_id"),
        seed=seed or metadata.get("seed"),
        expected_failure_device_ids=expected_failure_device_ids or set(),
        expected_failure_categories=expected_failure_categories or set(),
        expected_device_effects=expected_device_effects or [],
    )
