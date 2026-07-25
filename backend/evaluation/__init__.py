"""S4 评估器：七指标 + success_criteria 判定 + suite 运行器。

消费 S2 的 events.jsonl + run.json，产出 S5 对比视图直接消费的
``GET /api/runs/{run_id}/report``。
"""

from backend.evaluation.evaluator import (
    EvalMetrics,
    EvalOutcome,
    EvalReport,
    ScenarioEvaluator,
    evaluate_run,
)
from backend.evaluation.metrics import (
    MetricDatum,
    MetricsCollector,
    compute_episode_completeness,
    compute_first_action_latency_ms,
    compute_command_success_rate,
    compute_fallback_rate,
    compute_coordination_effectiveness,
    compute_safety_compliance,
    compute_device_effect_accuracy,
)
from backend.evaluation.suite import (
    SeedSet,
    SuiteReport,
    SuiteRunner,
    run_suite,
)

__all__ = [
    "EvalMetrics",
    "EvalOutcome",
    "EvalReport",
    "MetricDatum",
    "MetricsCollector",
    "ScenarioEvaluator",
    "SeedSet",
    "SuiteReport",
    "SuiteRunner",
    "compute_episode_completeness",
    "compute_first_action_latency_ms",
    "compute_command_success_rate",
    "compute_fallback_rate",
    "compute_coordination_effectiveness",
    "compute_safety_compliance",
    "compute_device_effect_accuracy",
    "evaluate_run",
    "run_suite",
]
