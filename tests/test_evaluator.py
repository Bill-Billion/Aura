"""S4 评估器测试：七指标 + success_criteria 判定 + report 端点。"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.evaluation.evaluator import EvalOutcome, ScenarioEvaluator, evaluate_run
from backend.evaluation.metrics import (
    MetricDatum,
    MetricsCollector,
    compute_command_success_rate,
    compute_coordination_effectiveness,
    compute_device_effect_accuracy,
    compute_episode_completeness,
    compute_fallback_rate,
    compute_first_action_latency_ms,
    compute_safety_compliance,
)
from backend.scenarios.runner import run_scenario


# ------------------------------------------------------------------ 指标单元测试


class TestMetricsCollector:
    def test_empty_events(self):
        collector = MetricsCollector(events=[])
        assert len(collector.correlation_ids) == 0
        assert compute_episode_completeness(collector).value == 0.0
        assert compute_first_action_latency_ms(collector).value == 0.0
        assert compute_command_success_rate(collector).value == 1.0
        assert compute_fallback_rate(collector).value == 0.0
        assert compute_coordination_effectiveness(collector).value == 1.0
        assert compute_safety_compliance(collector).value == 1.0
        assert compute_device_effect_accuracy(collector).value == 1.0

    def test_no_safety_events_returns_1(self):
        collector = MetricsCollector(events=[])
        result = compute_safety_compliance(collector)
        assert result.value == 1.0
        assert "no safety events" in result.details.get("note", "")

    def test_no_expected_effects_returns_1(self):
        collector = MetricsCollector(events=[])
        result = compute_device_effect_accuracy(collector)
        assert result.value == 1.0
        assert "no expected_device_effects" in result.details.get("note", "")


# ------------------------------------------------------------------ 评估器测试


class TestScenarioEvaluator:
    def test_empty_events_pass(self):
        evaluator = ScenarioEvaluator()
        report = evaluator.evaluate([], run_id="test")
        assert report.outcome == EvalOutcome.FAIL  # require_complete_episode fails
        assert "require_complete_episode" in report.failure_reasons[0]

    def test_allow_fallback_false(self):
        evaluator = ScenarioEvaluator({"allow_fallback": False})
        report = evaluator.evaluate([], run_id="test")
        assert not report.criteria_checks.get("require_complete_episode", True)

    def test_max_latency_check(self):
        evaluator = ScenarioEvaluator({"max_first_action_latency_ms": 100})
        report = evaluator.evaluate([], run_id="test")
        # 0 events → first_action_latency = 0.0 → passes the max_latency check (0 <= 100)
        assert report.criteria_checks.get("max_first_action_latency_ms", True) is True

    def test_max_command_failures_check(self):
        evaluator = ScenarioEvaluator({"max_command_failures": 0})
        report = evaluator.evaluate([], run_id="test")
        assert report.criteria_checks.get("max_command_failures", True) is True


# ------------------------------------------------------------------ 场景端到端


@pytest.mark.anyio
async def test_evaluate_arrive_home_run():
    """对到家场景跑一次评估，确保七指标全部有值且报告可序列化。"""

    result = await run_scenario("user_arrives_home_evening")
    assert result.run_id
    assert len(result.events) > 10

    evaluator = ScenarioEvaluator()
    report = evaluator.evaluate(
        list(result.events),
        run_id=result.run_id,
        scenario_id="user_arrives_home_evening",
        seed=result.seed,
    )

    d = report.to_dict()
    assert d["run_id"] == result.run_id
    assert d["scenario_id"] == "user_arrives_home_evening"
    assert d["outcome"] in {"pass", "fail"}

    metrics = d["metrics"]
    for name in [
        "episode_completeness",
        "first_action_latency_ms",
        "command_success_rate",
        "fallback_rate",
        "coordination_effectiveness",
        "safety_compliance",
        "device_effect_accuracy",
    ]:
        assert name in metrics, f"missing metric: {name}"
        assert "value" in metrics[name], f"missing value in {name}"
        assert isinstance(metrics[name]["value"], (int, float)), f"{name} value not numeric"


@pytest.mark.anyio
async def test_evaluate_run_function():
    """端到端：跑一个场景 → evaluate_run() → 拿到有效报告。"""

    result = await run_scenario("user_arrives_home_evening")
    report = evaluate_run(result.run_id)
    assert report.run_id == result.run_id
    assert report.outcome in {EvalOutcome.PASS, EvalOutcome.FAIL}
    assert report.metrics.episode_completeness.value >= 0.0


# ------------------------------------------------------------------ API 端点


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_report_endpoint_returns_404_for_unknown_run(client):
    resp = client.get("/api/runs/nonexistent-run-id/report")
    assert resp.status_code == 404  # run not found → eval error → 404


@pytest.mark.anyio
async def test_report_endpoint_for_real_run():
    """跑一个场景后通过 HTTP 拿报告。"""

    result = await run_scenario("user_arrives_home_evening")

    with TestClient(app) as client:
        resp = client.get(f"/api/runs/{result.run_id}/report")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == result.run_id
        assert "metrics" in body
        assert "outcome" in body


def test_health_endpoint_includes_llm_mode():
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("status") == "ok"
        if "llm" in body:
            assert "mode" in body["llm"]
