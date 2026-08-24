"""ScenarioSpec 2.1 event-relative counterfactual contract."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.scenarios.counterfactual import (
    CounterfactualPairError,
    validate_counterfactual_pairs,
)
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import load_library
from backend.scenarios.spec_v2 import (
    ScenarioSpecV2,
    compile_perturbations,
    unsupported_perturbations,
)

PILOT_DIR = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"
)

_SHARED_GOAL = {
    "user_goal": "用户阅读期间客厅照明充足，且不控制无关房间。",
    "relevant_room_ids": ["living_room"],
    "forbidden_room_ids": ["bedroom"],
    "safety_constraints": [],
}

def _v2_payloads() -> tuple[dict, dict]:
    library = load_library([PILOT_DIR])
    static = library["read_then_leave_001_static"].model_dump(mode="json")
    dynamic = library["read_then_leave_001_dynamic"].model_dump(mode="json")
    for payload in (static, dynamic):
        payload["scenario_schema_version"] = "2.0"
        payload["shared_goal"] = None
        payload["intervention_response"] = None
    dynamic["perturbations"] = [
        {
            "type": "resident_state_change",
            "phase": "after_plan_before_execution",
            "at_sim_time_s": 10,
            "user_id": "user_01",
            "room_id": "outside",
            "activity": "away",
        }
    ]
    return static, dynamic


def _v2_1_pair() -> tuple[ScenarioSpecV2, ScenarioSpecV2]:
    library = load_library([PILOT_DIR])
    return (
        library["read_then_leave_001_static"],
        library["read_then_leave_001_dynamic"],
    )


def test_v2_0_pilot_remains_readable_and_fingerprint_stable() -> None:
    static, dynamic = _v2_payloads()
    assert ScenarioSpecV2.model_validate(static).scenario_schema_version == "2.0"
    assert ScenarioSpecV2.model_validate(dynamic).scenario_schema_version == "2.0"
    static_spec = ScenarioSpecV2.model_validate(static)
    dynamic_spec = ScenarioSpecV2.model_validate(dynamic)
    assert scenario_contract_fingerprint(
        static_spec
    ) == "5c233ac7bd45e7c72dbe22eaf8b33c5ffcee776a511f357d8bca2c73705267f6"
    assert scenario_contract_fingerprint(
        dynamic_spec
    ) == "f1b94f8e247525584439ef38ede8023c4bf2f3192bd6df999603a3c215211070"

    alternate_spelling = static | {"scenario_schema_version": " 2.0 "}
    alternate = ScenarioSpecV2.model_validate(alternate_spelling)
    assert alternate.model_dump(mode="json")["shared_goal"] is None
    assert scenario_contract_fingerprint(alternate) == (
        "f1b98c6322ff62acbdb5b26200550e3770f01d750471ac1729e13ba258440c46"
    )


def test_v2_1_pair_accepts_shared_goal_and_dynamic_response() -> None:
    static, dynamic = _v2_1_pair()
    pair = validate_counterfactual_pairs([dynamic, static])[0]
    assert pair.static.shared_goal == pair.dynamic.shared_goal
    assert dynamic.intervention_response is not None
    assert dynamic.intervention_response.time_origin == "trigger"
    assert dynamic.summary()["has_intervention_response"] is True


def test_v2_1_rejects_absolute_or_mislabeled_phase_timing() -> None:
    _, dynamic = _v2_1_pair()
    payload = dynamic.model_dump(mode="json")
    payload["perturbations"][0] = {
        **payload["perturbations"][0],
        "anchor": None,
        "offset_seconds": None,
        "must_precede": None,
        "at_sim_time_s": 10,
    }
    with pytest.raises(ValidationError, match="event-relative anchor"):
        ScenarioSpecV2.model_validate(payload)

    payload = dynamic.model_dump(mode="json")
    payload["perturbations"][0]["must_precede"] = {
        "event_type": "feedback.state_delta"
    }
    with pytest.raises(ValidationError, match="requires anchor"):
        ScenarioSpecV2.model_validate(payload)


def test_v2_1_requires_factor_bound_dynamic_response() -> None:
    _, dynamic = _v2_1_pair()
    payload = dynamic.model_dump(mode="json")
    payload["intervention_response"]["trigger"]["where"][0]["value"] = (
        "device_failure"
    )
    with pytest.raises(ValidationError, match="counterfactual.factor"):
        ScenarioSpecV2.model_validate(payload)


def test_v2_1_rejects_shared_goal_that_conflicts_with_ground_truth() -> None:
    static, _ = _v2_1_pair()
    payload = static.model_dump(mode="json")
    payload["shared_goal"]["user_goal"] = "a contradictory goal"
    with pytest.raises(ValidationError, match="must match ground_truth"):
        ScenarioSpecV2.model_validate(payload)

    payload["shared_goal"]["user_goal"] = "   "
    payload["ground_truth"]["user_goal"] = "   "
    with pytest.raises(ValidationError, match="cannot be blank"):
        ScenarioSpecV2.model_validate(payload)


@pytest.mark.parametrize(
    "selector",
    ["anchor", "must_precede", "response", "response_other_path", "source"],
)
def test_phase_critical_selectors_reject_contradictions(selector: str) -> None:
    _, dynamic = _v2_1_pair()
    payload = dynamic.model_dump(mode="json")
    if selector == "anchor":
        payload["perturbations"][0]["anchor"]["where"] = [
            {"path": "data.agent_id", "comparator": "eq", "value": "lighting"},
            {"path": "data.agent_id", "comparator": "ne", "value": "lighting"},
        ]
    elif selector == "must_precede":
        payload["perturbations"][0]["must_precede"]["where"] = [
            {"path": "event_type", "comparator": "eq", "value": "other.event"}
        ]
    elif selector == "response":
        payload["intervention_response"]["trigger"]["where"].append(
            {
                "path": "data.perturbation_type",
                "comparator": "contains",
                "value": "impossible_value",
            }
        )
    elif selector == "response_other_path":
        payload["intervention_response"]["trigger"]["where"].append(
            {"path": "data.guard", "comparator": "in", "value": ["never"]}
        )
    else:
        payload["intervention_response"]["trigger"]["source"] = "never-emitted"
    with pytest.raises(
        ValidationError, match="multiple constraints|does not allow|cannot constrain"
    ):
        ScenarioSpecV2.model_validate(payload)


def test_new_v2_1_collections_are_bounded_before_nested_validation() -> None:
    static, dynamic = _v2_1_pair()
    static_payload = static.model_dump(mode="json")
    static_payload["shared_goal"]["relevant_room_ids"] = [
        f"room_{index}" for index in range(65)
    ]
    with pytest.raises(ValidationError, match="cannot exceed 64"):
        ScenarioSpecV2.model_validate(static_payload)

    dynamic_payload = dynamic.model_dump(mode="json")
    one_effect = dynamic_payload["intervention_response"][
        "expected_device_effects"
    ][0]
    dynamic_payload["intervention_response"]["expected_device_effects"] = [
        one_effect
    ] * 129
    with pytest.raises(ValidationError, match="cannot exceed 128"):
        ScenarioSpecV2.model_validate(dynamic_payload)


def test_pair_allows_only_explicit_dynamic_response_to_differ() -> None:
    static, dynamic = _v2_1_pair()
    first = validate_counterfactual_pairs([static, dynamic])[0]

    payload = dynamic.model_dump(mode="json")
    payload["intervention_response"]["expected_device_effects"][0][
        "within_seconds"
    ] = 6
    changed_response = ScenarioSpecV2.model_validate(payload)
    second = validate_counterfactual_pairs([static, changed_response])[0]
    assert first.fingerprint != second.fingerprint

    changed_goal = dynamic.model_copy(
        update={
            "shared_goal": dynamic.shared_goal.model_copy(
                update={"user_goal": "a different task"}
            )
        }
    )
    with pytest.raises(CounterfactualPairError, match="differ outside"):
        validate_counterfactual_pairs([static, changed_goal])


def test_event_relative_contract_is_owned_by_phase_runtime() -> None:
    _, dynamic = _v2_1_pair()
    assert unsupported_perturbations(dynamic) == []
    assert compile_perturbations(dynamic) == ()


def test_v2_0_cannot_smuggle_v2_1_fields() -> None:
    static, _ = _v2_payloads()
    static["shared_goal"] = deepcopy(_SHARED_GOAL)
    with pytest.raises(ValidationError, match="require ScenarioSpec 2.1"):
        ScenarioSpecV2.model_validate(static)
