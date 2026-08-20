"""S4 canonical metric contract and real-artifact regression tests."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.engine.event_log import EVENTS_FILENAME, RUN_METADATA_FILENAME, run_dir
from backend.engine.event_types import starts_agent_episode
from backend.evaluation.evaluator import (
    CANONICAL_METRIC_NAMES,
    REPORT_SCHEMA_VERSION,
    EvalOutcome,
    ScenarioEvaluator,
    evaluate_run,
)
from backend.evaluation.metrics import (
    MetricsCollector,
    compute_command_failure_count,
    compute_conflict_count,
    compute_device_state_match_rate,
    compute_episode_complete,
    compute_fallback_count,
    compute_first_action_latency_ms,
    compute_user_intent_satisfied,
)
from backend.main import app
from backend.scenarios.loader import load_library
from backend.scenarios.runner import run_scenario
from backend.scenarios.spec import ScenarioSpec


def event(
    event_type: str,
    *,
    seq: int,
    event_id: str,
    parent: str | None,
    wall_time: float | None = None,
    sim_time_s: float | None = None,
    data: dict[str, Any] | None = None,
    correlation_id: str = "corr-1",
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "event_id": event_id,
        "seq": seq,
        "correlation_id": correlation_id,
        "causal_parent": parent,
        "wall_time": 100.0 + seq / 1000 if wall_time is None else wall_time,
        "timestamp": float(seq),
        "sim_time_s": float(seq) if sim_time_s is None else sim_time_s,
        "source": "test",
        "data": data or {},
    }


def complete_episode() -> list[dict[str, Any]]:
    return [
        event("user.command", seq=0, event_id="root", parent=None, wall_time=100.0),
        event("reasoning.perception_snapshot", seq=1, event_id="perception", parent="root"),
        event("reasoning.intent_recognized", seq=2, event_id="intent", parent="perception"),
        event("reasoning.task_decomposition", seq=3, event_id="tasks", parent="intent"),
        event(
            "reasoning.coordination_decision",
            seq=4,
            event_id="coordination",
            parent="tasks",
            data={"conflicts": [{"kind": "a"}, {"kind": "b"}]},
        ),
        event("reasoning.execution_plan", seq=5, event_id="plan", parent="coordination"),
        event(
            "command.lifecycle",
            seq=6,
            event_id="approved",
            parent="plan",
            data={
                "command_id": "cmd-1",
                "device_id": "light_living_01",
                "to_status": "approved",
            },
        ),
        event(
            "action.device_control",
            seq=7,
            event_id="action",
            parent="plan",
            wall_time=100.125,
            sim_time_s=2.0,
            data={
                "command_id": "cmd-1",
                "device_id": "light_living_01",
                "property": "power",
                "value": True,
                "agent_id": "lighting_agent",
            },
        ),
        event(
            "feedback.state_delta",
            seq=8,
            event_id="feedback-power",
            parent="action",
            sim_time_s=2.0,
            data={
                "device_id": "light_living_01",
                "path": "devices[light_living_01].state.power",
                "old_value": False,
                "new_value": True,
            },
        ),
        event(
            "command.lifecycle",
            seq=9,
            event_id="succeeded",
            parent="plan",
            data={
                "command_id": "cmd-1",
                "device_id": "light_living_01",
                "to_status": "succeeded",
            },
        ),
    ]


def test_episode_contract_requires_order_and_connected_causality() -> None:
    events = complete_episode()
    result = compute_episode_complete(MetricsCollector(events=events))
    assert result.value is True

    disconnected = [dict(item) for item in events]
    disconnected[2] = {**disconnected[2], "causal_parent": "not-in-trace"}
    broken = compute_episode_complete(MetricsCollector(events=disconnected))
    assert broken.value is False
    assert "causal_connection" in broken.details["episodes"]["corr-1"]["missing"]

    out_of_order = [dict(item) for item in events]
    out_of_order[4] = {**out_of_order[4], "seq": 2}
    unordered = compute_episode_complete(MetricsCollector(events=out_of_order))
    assert unordered.value is False
    assert "causal_order" in unordered.details["episodes"]["corr-1"]["missing"]


def test_episode_contract_rejects_sibling_branches_that_individually_reach_root() -> None:
    """All rings must share the selected feedback's lineage, not merely its root."""

    events = [dict(item) for item in complete_episode()]
    for item in events:
        if (
            item["event_type"].startswith("reasoning.")
            or item["event_type"] == "action.device_control"
        ):
            item["causal_parent"] = "root"

    result = compute_episode_complete(MetricsCollector(events=events))

    assert result.value is False
    assert "causal_connection" in result.details["episodes"]["corr-1"]["missing"]


def test_expected_agent_root_without_reasoning_makes_the_run_incomplete() -> None:
    events = complete_episode() + [
        event(
            "user.arrives_home",
            seq=20,
            event_id="unhandled-root",
            parent=None,
            correlation_id="corr-unhandled",
        )
    ]
    result = compute_episode_complete(MetricsCollector(events=events))
    assert result.value is False
    assert result.details["episode_count"] == 2
    assert result.details["episodes"]["corr-unhandled"]["missing"] == [
        "reasoning.perception_snapshot",
        "reasoning.intent_recognized",
        "reasoning.task_decomposition",
        "reasoning.coordination_decision",
        "reasoning.execution_plan",
        "approved_command",
        "action.device_control",
        "feedback.state_delta",
    ]


def test_direct_device_control_root_is_not_an_expected_agent_episode() -> None:
    direct = event(
        "user.command",
        seq=20,
        event_id="direct-root",
        parent=None,
        correlation_id="corr-direct",
        data={
            "message_type": "CMD_DEVICE_CONTROL",
            "device_id": "light_living_01",
            "capability": "power",
            "value": True,
        },
    )
    result = compute_episode_complete(MetricsCollector(events=complete_episode() + [direct]))
    assert result.value is True
    assert result.details["episode_count"] == 1


def test_only_significant_environment_roots_open_agent_episodes() -> None:
    insignificant = event(
        "environment.state_refresh",
        seq=20,
        event_id="insignificant-refresh",
        parent=None,
        correlation_id="corr-insignificant",
        data={"significant_change_reasons": []},
    )
    ignored = compute_episode_complete(
        MetricsCollector(events=complete_episode() + [insignificant])
    )
    assert ignored.value is True
    assert ignored.details["episode_count"] == 1

    significant = {
        **insignificant,
        "event_id": "significant-refresh",
        "correlation_id": "corr-significant",
        "data": {"significant_change_reasons": ["temperature_threshold"]},
    }
    required = compute_episode_complete(
        MetricsCollector(events=complete_episode() + [significant])
    )
    assert required.value is False
    assert required.details["episode_count"] == 2
    assert required.details["episodes"]["corr-significant"]["root_event_types"] == [
        "environment.state_refresh"
    ]


def test_approved_lifecycle_must_precede_its_action() -> None:
    events = complete_episode()
    approved = next(item for item in events if item["event_id"] == "approved")
    approved["seq"] = 10
    result = compute_episode_complete(MetricsCollector(events=events))
    assert result.value is False
    assert "causal_order" in result.details["episodes"]["corr-1"]["missing"]


def test_episode_requires_exactly_one_agent_trigger() -> None:
    extra_root = event(
        "user.arrives_home",
        seq=10,
        event_id="second-root",
        parent=None,
    )
    result = compute_episode_complete(
        MetricsCollector(events=complete_episode() + [extra_root])
    )
    assert result.value is False
    assert result.details["episodes"]["corr-1"]["root_count"] == 2
    assert "root_event" in result.details["episodes"]["corr-1"]["missing"]

    invalid_root = [dict(item) for item in complete_episode()]
    invalid_root[0] = {**invalid_root[0], "event_type": "system.simulation_started"}
    wrong_root = compute_episode_complete(MetricsCollector(events=invalid_root))
    assert wrong_root.value is False
    assert "root_event" in wrong_root.details["episodes"]["corr-1"]["missing"]

    latency = compute_first_action_latency_ms(MetricsCollector(events=invalid_root))
    assert latency.value is None
    assert latency.details["missing_episode_ids"] == ["corr-1"]
    assert latency.details["by_episode"]["corr-1"]["reason"] == (
        "missing_unique_agent_root"
    )


def test_significant_environment_refresh_can_be_nested_under_timer_root() -> None:
    """Runtime's compatibility refresh is a timer child but still owns the episode."""

    timer = event(
        "system.timer_tick",
        seq=0,
        event_id="tick",
        parent=None,
        wall_time=99.9,
    )
    trigger = event(
        "environment.state_refresh",
        seq=1,
        event_id="environment-trigger",
        parent="tick",
        wall_time=100.0,
        data={"significant_change_reasons": ["temperature_threshold"]},
    )
    descendants: list[dict[str, Any]] = []
    for item in complete_episode()[1:]:
        clone = dict(item)
        clone["seq"] = item["seq"] + 1
        if clone["causal_parent"] == "root":
            clone["causal_parent"] = "environment-trigger"
        descendants.append(clone)

    collector = MetricsCollector(events=[timer, trigger, *descendants])
    complete = compute_episode_complete(collector)
    latency = compute_first_action_latency_ms(collector)

    assert complete.value is True
    episode = complete.details["episodes"]["corr-1"]
    assert episode["root_event_types"] == ["environment.state_refresh"]
    assert episode["causal_root_event_types"] == ["system.timer_tick"]
    assert latency.value == pytest.approx(125.0)


def test_first_action_latency_uses_wall_time_not_sim_timestamp() -> None:
    result = compute_first_action_latency_ms(MetricsCollector(events=complete_episode()))
    assert result.value == pytest.approx(125.0)
    assert result.details["max_latency_ms"] == pytest.approx(125.0)
    assert result.details["sample_count"] == result.details["episode_count"] == 1


def test_command_failures_are_unique_terminal_commands_and_exact_expected_matches() -> None:
    events = [
        event("command.lifecycle", seq=0, event_id="a0", parent=None, data={"command_id": "a", "device_id": "ac_living_01", "to_status": "proposed"}),
        event("command.lifecycle", seq=1, event_id="a1", parent=None, data={"command_id": "a", "device_id": "ac_living_01", "to_status": "failed", "failure_code": "device_offline"}),
        event("command.lifecycle", seq=2, event_id="b0", parent=None, data={"command_id": "b", "device_id": "fan_living_01", "to_status": "proposed"}),
        event("command.lifecycle", seq=3, event_id="b1", parent=None, data={"command_id": "b", "device_id": "fan_living_01", "to_status": "timed_out", "failure_code": "execution_timeout"}),
        event("command.lifecycle", seq=4, event_id="c0", parent=None, data={"command_id": "c", "device_id": "light_living_01", "to_status": "succeeded"}),
    ]
    collector = MetricsCollector(
        events=events,
        expected_failures=[
            {
                "category": "device_offline_before_command",
                "device_id": "ac_living_01",
                "error_code": "device_offline",
            }
        ],
    )
    result = compute_command_failure_count(collector)
    assert result.value == 1
    assert result.details["unique_command_count"] == 3
    assert result.details["expected_failure_count"] == 1
    assert result.details["unexpected_failures"] == [
        {
            "command_id": "b",
            "device_id": "fan_living_01",
            "status": "timed_out",
            "failure_code": "execution_timeout",
        }
    ]
    assert result.details["unobserved_expected_failures"] == []


def test_declared_expected_failure_is_not_silently_satisfied_when_absent() -> None:
    result = compute_command_failure_count(
        MetricsCollector(
            events=[],
            expected_failures=[
                {
                    "category": "device_offline_before_command",
                    "device_id": "ac_living_01",
                    "error_code": "device_offline",
                }
            ],
        )
    )
    assert result.value == 0
    assert result.details["unobserved_expected_failures"] == [
        {
            "category": "device_offline_before_command",
            "device_id": "ac_living_01",
            "error_code": "device_offline",
        }
    ]


def test_fallback_and_conflict_counts_read_their_wire_events() -> None:
    events = complete_episode() + [
        event("reasoning.fallback_rule_based", seq=10, event_id="fallback-1", parent="intent", data={"reason": "timeout"}),
        event("reasoning.fallback_rule_based", seq=11, event_id="fallback-2", parent="intent", data={"reason": "invalid_output"}),
    ]
    collector = MetricsCollector(events=events)
    assert compute_fallback_count(collector).value == 2
    assert compute_conflict_count(collector).value == 2


def test_device_match_rate_counts_fields_from_flat_feedback_and_deadlines() -> None:
    events = complete_episode() + [
        event(
            "feedback.state_delta",
            seq=10,
            event_id="feedback-level",
            parent="action",
            sim_time_s=3.0,
            data={
                "device_id": "light_living_01",
                "path": "devices[light_living_01].state.extra.brightness",
                "old_value": 0,
                "new_value": 70,
            },
        )
    ]
    effects = [
        {
            "device_id": "light_living_01",
            "within_seconds": 5,
            "expected": {
                "power": {"equals": True},
                "extra.brightness": {"min": 50},
                "extra.color_temp": {"one_of": [3000, 3500]},
            },
        }
    ]
    collector = MetricsCollector(
        events=events,
        initial_device_states={
            "light_living_01": {
                "power": False,
                "extra": {"brightness": 0, "color_temp": 3000},
            }
        },
        expected_device_effects=effects,
    )
    result = compute_device_state_match_rate(collector)
    assert result.value == 1.0
    assert result.details["matched_field_count"] == 3
    assert result.details["expected_field_count"] == 3

    late_events = [dict(item) for item in events]
    late_events[-1] = {**late_events[-1], "sim_time_s": 7.0}
    late = compute_device_state_match_rate(
        MetricsCollector(
            events=late_events,
            initial_device_states=collector.initial_device_states,
            expected_device_effects=effects,
        )
    )
    assert late.value == pytest.approx(2 / 3)
    assert late.details["fields"][1]["deadline_matched"] is False


def test_empty_effects_and_unknown_safety_constraint_do_not_default_pass() -> None:
    empty = MetricsCollector(
        events=[],
        ground_truth={"acceptable_noop": False, "safety_constraints": []},
    )
    assert compute_device_state_match_rate(empty).value is None
    assert compute_user_intent_satisfied(empty).value is False

    unknown = MetricsCollector(
        events=complete_episode(),
        expected_device_effects=[
            {
                "device_id": "light_living_01",
                "expected": {"power": {"equals": True}},
            }
        ],
        initial_device_states={"light_living_01": {"power": False}},
        ground_truth={
            "acceptable_noop": False,
            "safety_constraints": ["future_constraint_without_predicate"],
        },
    )
    intent = compute_user_intent_satisfied(unknown)
    assert intent.value is False
    assert intent.details["safety_checks"][0]["evaluable"] is False


def _events_with_ground_truth_wire(
    *,
    normalized_intent: str | None = "arrival_comfort",
    agent_roles: list[str] | None = None,
) -> list[dict[str, Any]]:
    events = [
        {**item, "data": dict(item["data"])} for item in complete_episode()
    ]
    for item in events:
        if item["event_type"] == "reasoning.intent_recognized":
            item["data"] = {
                # Deliberately equal to ground truth: only normalized_intent may count.
                "intent": "arrival_comfort",
                **(
                    {}
                    if normalized_intent is None
                    else {"normalized_intent": normalized_intent}
                ),
            }
        elif item["event_type"] == "reasoning.task_decomposition":
            item["data"] = {
                "agent_roles": list(
                    ["lighting", "hvac"] if agent_roles is None else agent_roles
                )
            }
    return events


def _ground_truth_collector(events: list[dict[str, Any]]) -> MetricsCollector:
    return MetricsCollector(
        events=events,
        expected_device_effects=[
            {
                "device_id": "light_living_01",
                "expected": {"power": {"equals": True}},
            }
        ],
        initial_device_states={"light_living_01": {"power": False}},
        ground_truth={
            "acceptable_noop": False,
            "forbidden_device_ids": ["camera_bedroom_02"],
            "expected_intent": "arrival_comfort",
            "required_agent_roles": ["lighting", "hvac"],
            "safety_constraints": [],
        },
    )


def test_user_intent_rejects_an_action_against_a_forbidden_device() -> None:
    clean_events = _events_with_ground_truth_wire()
    clean = compute_user_intent_satisfied(_ground_truth_collector(clean_events))
    assert clean.value is True

    forbidden_action = event(
        "action.device_control",
        seq=10,
        event_id="forbidden-camera-action",
        parent="plan",
        data={
            "command_id": "cmd-forbidden",
            "device_id": "camera_bedroom_02",
            "property": "power",
            "value": False,
            "agent_id": "security_agent",
        },
    )
    rejected = compute_user_intent_satisfied(
        _ground_truth_collector([*clean_events, forbidden_action])
    )

    assert rejected.value is False
    check = rejected.details["ground_truth_checks"]["forbidden_devices"]
    assert check["satisfied"] is False
    assert check["violating_actions"] == [
        {
            "event_id": "forbidden-camera-action",
            "device_id": "camera_bedroom_02",
        }
    ]


@pytest.mark.parametrize(
    ("normalized_intent", "evaluable"),
    [("wrong_intent", True), (None, False)],
)
def test_user_intent_requires_matching_complete_intent_wire_evidence(
    normalized_intent: str | None,
    evaluable: bool,
) -> None:
    result = compute_user_intent_satisfied(
        _ground_truth_collector(
            _events_with_ground_truth_wire(normalized_intent=normalized_intent)
        )
    )

    assert result.value is False
    check = result.details["ground_truth_checks"]["expected_intent"]
    assert check["evaluable"] is evaluable
    assert check["satisfied"] is False
    assert check["expected"] == "arrival_comfort"


def test_user_intent_requires_every_declared_agent_role_on_the_wire() -> None:
    result = compute_user_intent_satisfied(
        _ground_truth_collector(
            _events_with_ground_truth_wire(agent_roles=["lighting"])
        )
    )

    assert result.value is False
    check = result.details["ground_truth_checks"]["required_agent_roles"]
    assert check["evaluable"] is True
    assert check["satisfied"] is False
    assert check["observed"] == ["lighting"]
    assert check["missing"] == ["hvac"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("scenario_id", "expected_intent"),
    [
        ("user_leaves_home_morning", "departure_energy_saving"),
        ("night_sleep_bedtime", "sleep_comfort"),
        ("hot_weather_afternoon", "temperature_comfort"),
        ("device_offline_fallback", "device_offline_failover"),
        ("morning_wake_up", "activity_comfort:waking_up"),
        ("multi_user_conflict", "sleep_comfort"),
    ],
)
async def test_canonical_expected_intent_is_present_in_real_wire_evidence(
    scenario_id: str,
    expected_intent: str,
) -> None:
    scenario = load_library()[scenario_id]
    assert scenario.ground_truth is not None
    assert scenario.ground_truth.expected_intent == expected_intent

    result = await run_scenario(scenario_id)
    observed = {
        event.data.get("normalized_intent")
        for event in result.events
        if event.event_type == "reasoning.intent_recognized"
        and isinstance(event.data.get("normalized_intent"), str)
    }

    assert expected_intent in observed
    report = evaluate_run(result.run_id)
    check = report.metrics.user_intent_satisfied.details["ground_truth_checks"][
        "expected_intent"
    ]
    assert check["evaluable"] is True
    assert check["satisfied"] is True


@pytest.mark.anyio
async def test_device_offline_rule_trace_is_truthful_about_missing_fallback() -> None:
    scenario = load_library()["device_offline_fallback"]
    assert scenario.ground_truth is not None
    assert scenario.expected_failures == []
    assert "ac_living_01" in scenario.ground_truth.relevant_device_ids
    assert "ac_living_01" not in scenario.ground_truth.forbidden_device_ids
    assert {
        effect.device_id for effect in scenario.expected_device_effects
    } == {"ac_living_02", "fan_living_01"}

    result = await run_scenario(scenario.id)
    report = evaluate_run(result.run_id)
    offline = next(
        event
        for event in result.events
        if event.event_type == "device.offline"
        and event.data.get("device_id") == "ac_living_01"
    )
    actions = [
        event
        for event in result.events
        if event.event_type == "action.device_control"
    ]
    assert all(event.data.get("device_id") != "ac_living_01" for event in actions)
    alternative_actions = [
        event
        for event in actions
        if event.seq > offline.seq
        and event.data.get("device_id") in {"ac_living_02", "fan_living_01"}
    ]
    assert alternative_actions == []
    assert report.metrics.device_state_match_rate.value != 1.0
    assert report.metrics.user_intent_satisfied.value is False

    details = report.metrics.user_intent_satisfied.details
    forbidden = details["ground_truth_checks"]["forbidden_devices"]
    assert forbidden["satisfied"] is True
    retry_check = next(
        item
        for item in details["safety_checks"]
        if item["constraint"]
        == "do_not_retry_commands_to_a_device_known_offline"
    )
    assert retry_check["evaluable"] is True
    assert retry_check["satisfied"] is True


def test_every_canonical_safety_constraint_has_an_executable_predicate() -> None:
    checked: set[str] = set()
    for scenario in load_library().values():
        if scenario.ground_truth is None or not scenario.ground_truth.safety_constraints:
            continue
        report = ScenarioEvaluator.from_scenario(scenario).evaluate(
            [], scenario=scenario, scenario_id=scenario.id, seed=scenario.seed
        )
        safety_checks = report.metrics.user_intent_satisfied.details["safety_checks"]
        by_name = {item["constraint"]: item for item in safety_checks}
        for constraint in scenario.ground_truth.safety_constraints:
            checked.add(constraint)
            assert by_name[constraint]["evaluable"] is True, constraint
            assert by_name[constraint]["evidence"] != ["no_registered_evaluator"]
    assert len(checked) == 15


def test_unknown_required_metric_returns_explicit_error() -> None:
    scenario = minimal_scenario().model_copy(
        update={"metrics": ["future_metric_not_implemented"]}
    )
    report = ScenarioEvaluator.from_scenario(scenario).evaluate(
        complete_episode(), scenario=scenario
    )
    assert report.outcome is EvalOutcome.ERROR
    assert report.failed_metrics == list(CANONICAL_METRIC_NAMES)
    assert "unsupported required metric" in report.failure_reasons[0]
    assert "future_metric_not_implemented" in report.failure_reasons[0]
    assert report.provenance["required_metrics"] == [
        "future_metric_not_implemented"
    ]


def test_library_required_metrics_are_canonical_and_reported_in_criteria() -> None:
    canonical = set(CANONICAL_METRIC_NAMES)
    for scenario in load_library().values():
        assert set(scenario.metrics) <= canonical, scenario.id
        report = ScenarioEvaluator.from_scenario(scenario).evaluate(
            [], scenario=scenario, scenario_id=scenario.id, seed=scenario.seed
        )
        assert report.outcome is not EvalOutcome.ERROR, scenario.id
        metric_payload = report.metrics.to_dict()
        for metric_name in scenario.metrics:
            assert metric_name in metric_payload
            assert f"required_metric:{metric_name}" in report.criteria_checks
        assert report.provenance["required_metrics"] == scenario.metrics


def minimal_scenario() -> ScenarioSpec:
    return ScenarioSpec.model_validate(
        {
            "scenario_schema_version": "1.0",
            "id": "eval_contract_fixture",
            "name": "Evaluation contract fixture",
            "description": "One complete action that turns on the living room light.",
            "seed": 7,
            "initial_state": {
                "devices": {
                    "light_living_01": {
                        "state": {"power": False, "extra": {"brightness": 0}}
                    }
                }
            },
            "timeline": [],
            "expected_device_effects": [
                {
                    "device_id": "light_living_01",
                    "within_seconds": 5,
                    "expected": {"power": True},
                }
            ],
            "involved_agents": ["lighting_agent"],
            "success_criteria": {
                "require_complete_episode": True,
                "max_first_action_latency_ms": 200,
                "max_command_failures": 0,
                "allow_fallback": True,
            },
            "metrics": list(CANONICAL_METRIC_NAMES),
            "ground_truth": {
                "user_goal": "turn on the light",
                "relevant_device_ids": ["light_living_01"],
                "acceptable_noop": False,
                "safety_constraints": [],
            },
        }
    )


def test_evaluator_report_is_versioned_and_s5_consumable() -> None:
    report = ScenarioEvaluator.from_scenario(minimal_scenario()).evaluate(
        complete_episode(),
        run_id="run-fixture",
        scenario_id="eval_contract_fixture",
        seed=7,
        run_metadata={"sim_version": "test", "llm_mode": "mocked"},
    )
    body = report.to_dict()
    assert report.outcome is EvalOutcome.PASS
    assert body["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert tuple(body["metrics"]) == CANONICAL_METRIC_NAMES
    assert body["failed_metrics"] == []
    assert all(body["criteria_checks"].values())
    assert body["provenance"]["scenario_schema_version"] == "1.0"
    assert body["provenance"]["llm_mode"] == "mocked"


def test_report_distinguishes_run_source_from_current_evaluator_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.evaluation.evaluator.read_source_revision",
        lambda: "sha256:evaluator-current",
    )

    report = ScenarioEvaluator.from_scenario(minimal_scenario()).evaluate(
        complete_episode(),
        run_id="run-historical",
        scenario_id="eval_contract_fixture",
        seed=7,
        run_metadata={"source_revision": "sha256:run-historical"},
    )

    assert report.provenance["source_revision"] == "sha256:run-historical"
    assert (
        report.provenance["evaluator_source_revision"]
        == "sha256:evaluator-current"
    )


def test_multi_user_conflict_contract_requires_at_least_one_real_conflict() -> None:
    scenario = load_library()["multi_user_conflict"]
    assert scenario.success_criteria.min_conflict_count == 1

    fixture = minimal_scenario()
    fixture = fixture.model_copy(
        update={
            "success_criteria": fixture.success_criteria.model_copy(
                update={"min_conflict_count": 1}
            )
        }
    )
    zero_conflict_events = [
        {**item, "data": dict(item["data"])} for item in complete_episode()
    ]
    coordination = next(
        item
        for item in zero_conflict_events
        if item["event_type"] == "reasoning.coordination_decision"
    )
    coordination["data"]["conflicts"] = []

    report = ScenarioEvaluator.from_scenario(fixture).evaluate(
        zero_conflict_events, scenario=fixture
    )

    assert report.metrics.conflict_count.value == 0
    assert report.criteria_checks["min_conflict_count"] is False
    assert "conflict_count" in report.failed_metrics
    assert report.outcome is EvalOutcome.FAIL


@pytest.mark.anyio
async def test_real_multi_user_trace_satisfies_the_conflict_threshold() -> None:
    result = await run_scenario("multi_user_conflict")
    report = evaluate_run(result.run_id)

    assert report.metrics.conflict_count.value >= 1
    assert report.criteria_checks["min_conflict_count"] is True


def test_latency_threshold_uses_worst_episode_not_mean() -> None:
    events = complete_episode()
    second = []
    for item in complete_episode():
        clone = dict(item)
        clone["correlation_id"] = "corr-2"
        clone["event_id"] = f"second-{item['event_id']}"
        clone["causal_parent"] = (
            None if item["causal_parent"] is None else f"second-{item['causal_parent']}"
        )
        clone["seq"] = item["seq"] + 20
        if item["event_type"] == "action.device_control":
            clone["wall_time"] = 100.350
        second.append(clone)
    report = ScenarioEvaluator.from_scenario(minimal_scenario()).evaluate(
        events + second, scenario=minimal_scenario()
    )
    assert report.metrics.first_action_latency_ms.value == pytest.approx(237.5)
    assert report.metrics.first_action_latency_ms.details["max_latency_ms"] == pytest.approx(350.0)
    assert report.criteria_checks["max_first_action_latency_ms"] is False
    assert "first_action_latency_ms" in report.failed_metrics


def test_unterminated_command_ledger_cannot_pass_with_complete_effects() -> None:
    events = [
        item for item in complete_episode() if item["event_id"] != "succeeded"
    ]
    report = ScenarioEvaluator.from_scenario(minimal_scenario()).evaluate(
        events, scenario=minimal_scenario()
    )
    assert report.metrics.episode_complete.value is True
    assert report.metrics.device_state_match_rate.value == 1.0
    assert report.metrics.command_failure_count.value == 0
    assert report.metrics.command_failure_count.details["unterminated_command_ids"] == [
        "cmd-1"
    ]
    assert report.criteria_checks["command_ledger_complete"] is False
    assert report.outcome is EvalOutcome.FAIL
    assert "command_failure_count" in report.failed_metrics


@pytest.mark.anyio
async def test_real_canonical_run_is_evaluated_from_metadata_and_exact_trace_values() -> None:
    result = await run_scenario("user_arrives_home_evening")
    report = evaluate_run(result.run_id)

    reasoning_ids = {
        event.correlation_id
        for event in result.events
        if event.event_type.startswith("reasoning.")
    }
    independently_computed_latencies = []
    for correlation_id in reasoning_ids:
        scoped = [event for event in result.events if event.correlation_id == correlation_id]
        roots = [
            event
            for event in scoped
            if starts_agent_episode(event.event_type, event.data)
        ]
        actions = [event for event in scoped if event.event_type == "action.device_control"]
        if len(roots) == 1 and actions:
            independently_computed_latencies.append(
                (min(action.wall_time for action in actions) - roots[0].wall_time) * 1000
            )

    assert report.outcome is EvalOutcome.FAIL
    assert report.scenario_id == "user_arrives_home_evening"
    assert report.seed == 1001
    assert report.metrics.episode_complete.value is False
    assert report.metrics.episode_complete.details["episode_count"] == 3
    assert report.metrics.episode_complete.details["complete_count"] == 1
    assert report.metrics.first_action_latency_ms.value == pytest.approx(
        sum(independently_computed_latencies) / len(independently_computed_latencies)
    )
    assert report.metrics.command_failure_count.value == 0
    assert report.metrics.fallback_count.value == sum(
        event.event_type == "reasoning.fallback_rule_based" for event in result.events
    )
    assert report.metrics.conflict_count.value == sum(
        len(event.data.get("conflicts", []))
        for event in result.events
        if event.event_type == "reasoning.coordination_decision"
    )
    # The real trace changes brightness but never powers the light or HVAC on.
    assert report.metrics.device_state_match_rate.value == 0.5
    assert report.metrics.user_intent_satisfied.value is False
    assert report.failed_metrics == [
        "episode_complete",
        "first_action_latency_ms",
        "user_intent_satisfied",
        "device_state_match_rate",
    ]
    assert report.provenance["scenario_schema_version"] == "1.0"
    assert report.provenance["sim_version"]


@pytest.mark.anyio
async def test_run_metadata_cannot_be_overridden_by_offline_caller() -> None:
    result = await run_scenario("user_arrives_home_evening")
    report = evaluate_run(result.run_id, scenario_id="different-scenario")
    assert report.outcome is EvalOutcome.ERROR
    assert "disagrees with run metadata" in report.failure_reasons[0]


@pytest.mark.anyio
async def test_public_evaluate_run_rejects_an_active_artifact() -> None:
    result = await run_scenario("user_arrives_home_evening")
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["ended_at"] = None
    metadata["end_reason"] = None
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = evaluate_run(result.run_id)

    assert report.outcome is EvalOutcome.ERROR
    assert report.failure_reasons == [
        f"run {result.run_id} is not finalized: ended_at is null"
    ]

    response = TestClient(app).get(f"/api/runs/{result.run_id}/report")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_not_finalized"


@pytest.mark.parametrize("end_reason", ["engine_error", "closed", "superseded"])
@pytest.mark.anyio
async def test_non_completed_finalized_run_cannot_produce_a_report(end_reason: str) -> None:
    result = await run_scenario("user_arrives_home_evening")
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["end_reason"] = end_reason
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = evaluate_run(result.run_id)

    assert report.outcome is EvalOutcome.ERROR
    assert f"end_reason={end_reason!r}" in report.failure_reasons[0]


@pytest.mark.parametrize("mutation", ["missing", "mismatch"])
@pytest.mark.anyio
async def test_scenario_contract_fingerprint_prevents_silent_reevaluation_drift(
    mutation: str,
) -> None:
    result = await run_scenario("user_arrives_home_evening")
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        metadata.pop("scenario_contract_hash")
    else:
        metadata["scenario_contract_hash"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = evaluate_run(result.run_id)

    assert report.outcome is EvalOutcome.ERROR
    assert (
        "scenario_contract_hash" in report.failure_reasons[0]
        if mutation == "missing"
        else "evaluation contract drift" in report.failure_reasons[0]
    )


@pytest.mark.parametrize(
    "field_name",
    [
        "scenario_schema_version",
        "event_schema_version",
        "command_schema_version",
        "device_registry_version",
    ],
)
@pytest.mark.anyio
async def test_legacy_run_without_required_schema_version_is_not_evaluable(
    field_name: str,
) -> None:
    result = await run_scenario("user_arrives_home_evening")
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop(field_name)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert f"missing required {field_name}" in report.failure_reasons[0]


@pytest.mark.parametrize(
    "field_name",
    [
        "scenario_schema_version",
        "event_schema_version",
        "command_schema_version",
        "device_registry_version",
    ],
)
@pytest.mark.anyio
async def test_incompatible_run_metadata_schema_version_is_not_evaluable(
    field_name: str,
) -> None:
    result = await run_scenario("user_arrives_home_evening")
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata[field_name] = "2.0"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert field_name in report.failure_reasons[0]
    assert "explicit migration is required" in report.failure_reasons[0]


@pytest.mark.parametrize("record_kind", ["event", "command"])
@pytest.mark.anyio
async def test_persisted_record_schema_versions_are_required(
    record_kind: str,
) -> None:
    result = await run_scenario("user_arrives_home_evening")
    events_path = run_dir(result.run_id) / EVENTS_FILENAME
    lines = events_path.read_text(encoding="utf-8").splitlines()
    if record_kind == "event":
        item = json.loads(lines[0])
        item.pop("event_schema_version")
        lines[0] = json.dumps(item)
        expected_field = "event_schema_version"
    else:
        for index, raw in enumerate(lines):
            item = json.loads(raw)
            if item["event_type"] == "command.lifecycle":
                item["data"].pop("command_schema_version")
                lines[index] = json.dumps(item)
                break
        else:  # pragma: no cover - canonical run contract guarantees lifecycle events
            pytest.fail("canonical run emitted no command.lifecycle")
        expected_field = "command_schema_version"
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert f"missing required {expected_field}" in report.failure_reasons[0]


@pytest.mark.parametrize("record_kind", ["event", "command"])
@pytest.mark.anyio
async def test_incompatible_persisted_record_schema_versions_are_rejected(
    record_kind: str,
) -> None:
    result = await run_scenario("user_arrives_home_evening")
    events_path = run_dir(result.run_id) / EVENTS_FILENAME
    lines = events_path.read_text(encoding="utf-8").splitlines()
    if record_kind == "event":
        item = json.loads(lines[0])
        item["event_schema_version"] = "2.0"
        lines[0] = json.dumps(item)
        expected_field = "event_schema_version"
    else:
        for index, raw in enumerate(lines):
            item = json.loads(raw)
            if item["event_type"] == "command.lifecycle":
                item["data"]["command_schema_version"] = "2.0"
                lines[index] = json.dumps(item)
                break
        else:  # pragma: no cover - canonical run contract guarantees lifecycle events
            pytest.fail("canonical run emitted no command.lifecycle")
        expected_field = "command_schema_version"
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert expected_field in report.failure_reasons[0]
    assert "explicit migration is required" in report.failure_reasons[0]


@pytest.mark.anyio
async def test_event_schema_version_must_equal_run_metadata_declaration() -> None:
    result = await run_scenario("user_arrives_home_evening")
    events_path = run_dir(result.run_id) / EVENTS_FILENAME
    lines = events_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["event_schema_version"] = "1.1"
    lines[0] = json.dumps(first)
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert "event_schema_version" in report.failure_reasons[0]
    assert "disagrees with run metadata" in report.failure_reasons[0]


@pytest.mark.anyio
async def test_persisted_event_sequence_must_be_contiguous_from_zero() -> None:
    result = await run_scenario("user_arrives_home_evening")
    events_path = run_dir(result.run_id) / EVENTS_FILENAME
    lines = events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 2
    del lines[1]
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert "leaves a gap" in report.failure_reasons[0]


@pytest.mark.anyio
async def test_evaluate_run_rejects_complete_event_suffix_truncation() -> None:
    result = await run_scenario("user_arrives_home_evening")
    events_path = run_dir(result.run_id) / EVENTS_FILENAME
    lines = events_path.read_bytes().splitlines(keepends=True)
    assert len(lines) > 2
    events_path.write_bytes(b"".join(lines[:-1]))

    report = evaluate_run(result.run_id)

    assert report.outcome is EvalOutcome.ERROR
    assert "[corrupt_event_log]" in report.failure_reasons[0]
    assert "integrity check failed" in report.failure_reasons[0]


@pytest.mark.anyio
async def test_evaluate_run_rejects_legacy_artifact_without_event_seal() -> None:
    result = await run_scenario("user_arrives_home_evening")
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("events_integrity")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = evaluate_run(result.run_id)

    assert report.outcome is EvalOutcome.ERROR
    assert "[unsupported_run_artifact]" in report.failure_reasons[0]
    assert "events_integrity" in report.failure_reasons[0]


@pytest.mark.anyio
async def test_command_schema_version_must_equal_run_metadata_declaration() -> None:
    result = await run_scenario("user_arrives_home_evening")
    events_path = run_dir(result.run_id) / EVENTS_FILENAME
    lines = events_path.read_text(encoding="utf-8").splitlines()
    for index, raw in enumerate(lines):
        item = json.loads(raw)
        if item["event_type"] == "command.lifecycle":
            item["data"]["command_schema_version"] = "1.1"
            lines[index] = json.dumps(item)
            break
    else:  # pragma: no cover - canonical run contract guarantees lifecycle events
        pytest.fail("canonical run emitted no command.lifecycle")
    events_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert "command_schema_version" in report.failure_reasons[0]
    assert "disagrees with run metadata" in report.failure_reasons[0]


@pytest.mark.anyio
async def test_non_object_or_missing_required_event_field_is_explicit_error() -> None:
    scalar_result = await run_scenario("user_arrives_home_evening")
    scalar_path = run_dir(scalar_result.run_id) / EVENTS_FILENAME
    scalar_lines = scalar_path.read_text(encoding="utf-8").splitlines()
    scalar_lines[0] = "[]"
    scalar_path.write_text("\n".join(scalar_lines) + "\n", encoding="utf-8")
    scalar_report = evaluate_run(scalar_result.run_id)
    assert scalar_report.outcome is EvalOutcome.ERROR
    assert "must be a JSON object" in scalar_report.failure_reasons[0]

    missing_result = await run_scenario("user_arrives_home_evening")
    missing_path = run_dir(missing_result.run_id) / EVENTS_FILENAME
    missing_lines = missing_path.read_text(encoding="utf-8").splitlines()
    first = json.loads(missing_lines[0])
    first.pop("source")
    missing_lines[0] = json.dumps(first)
    missing_path.write_text("\n".join(missing_lines) + "\n", encoding="utf-8")
    missing_report = evaluate_run(missing_result.run_id)
    assert missing_report.outcome is EvalOutcome.ERROR
    assert "missing required source" in missing_report.failure_reasons[0]


@pytest.mark.anyio
async def test_report_api_and_offline_entry_return_the_same_canonical_contract() -> None:
    result = await run_scenario("user_arrives_home_evening")
    offline = evaluate_run(result.run_id).to_dict()
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{result.run_id}/report")
    assert response.status_code == 200
    body = response.json()
    assert body["report_schema_version"] == REPORT_SCHEMA_VERSION
    assert tuple(body["metrics"]) == CANONICAL_METRIC_NAMES
    for key in (
        "outcome",
        "metrics",
        "criteria_checks",
        "failed_metrics",
        "failure_reasons",
        "provenance",
    ):
        assert body[key] == offline[key]
