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
    compute_episode_complete,
    compute_first_action_latency_ms,
    compute_command_failure_count,
    compute_fallback_count,
    compute_conflict_count,
    compute_user_intent_satisfied,
    compute_device_state_match_rate,
)
from backend.evaluation.temporal import (
    PropertyVerification,
    TraceVerification,
    VerificationStatus,
    verify_trace,
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
    "PropertyVerification",
    "TraceVerification",
    "VerificationStatus",
    "compute_episode_complete",
    "compute_first_action_latency_ms",
    "compute_command_failure_count",
    "compute_fallback_count",
    "compute_conflict_count",
    "compute_user_intent_satisfied",
    "compute_device_state_match_rate",
    "evaluate_run",
    "run_suite",
    "verify_trace",
]
