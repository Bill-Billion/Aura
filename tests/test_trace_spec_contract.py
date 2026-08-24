"""PR-1 typed TraceSpec AST contract; temporal semantics belong to PR-4."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.scenarios.trace_spec import (
    AfterOperator,
    AlwaysOperator,
    CountOperator,
    EventSelector,
    EventuallyOperator,
    NeverOperator,
    TraceSpec,
    UntilOperator,
    trace_spec_fingerprint,
)


def _property(expression):
    return {
        "id": "typed_property",
        "category": "liveness",
        "level": "hard",
        "expression": expression,
    }


def test_recursive_discriminated_union_parses_event_selector_and_six_operators() -> (
    None
):
    expressions = [
        {"op": "always", "operand": {"op": "event", "event_type": "safe"}},
        {"op": "never", "operand": {"op": "event", "event_type": "unsafe"}},
        {
            "op": "eventually",
            "window": {"start_seconds": 0, "end_seconds": 5},
            "operand": {"op": "event", "event_type": "feedback.state_delta"},
        },
        {
            "op": "after",
            "trigger": {"op": "event", "event_type": "command.lifecycle"},
            "relation": "same_correlation",
            "consequent": {
                "op": "never",
                "window": {"start_seconds": 0, "end_seconds": 30},
                "operand": {"op": "event", "event_type": "action.stale"},
            },
        },
        {
            "op": "until",
            "condition": {"op": "event", "event_type": "command.executing"},
            "terminal": {"op": "event", "event_type": "feedback.state_delta"},
        },
        {
            "op": "count",
            "selector": {"op": "event", "event_type": "command.failed"},
            "comparator": "lte",
            "threshold": 1,
        },
    ]
    expected_types = (
        AlwaysOperator,
        NeverOperator,
        EventuallyOperator,
        AfterOperator,
        UntilOperator,
        CountOperator,
    )
    for expression, expected in zip(expressions, expected_types):
        spec = TraceSpec.model_validate({"properties": [_property(expression)]})
        assert isinstance(spec.properties[0].expression, expected)


def test_nested_after_eventually_ast_roundtrips_and_hashes_stably() -> None:
    payload = {
        "properties": [
            _property(
                {
                    "op": "after",
                    "trigger": {
                        "op": "event",
                        "event_type": "conflict.detected",
                    },
                    "consequent": {
                        "op": "eventually",
                        "window": {"start_seconds": 0, "end_seconds": 10},
                        "operand": {
                            "op": "event",
                            "event_type": "reasoning.coordination_decision",
                        },
                    },
                }
            )
        ]
    }
    first = TraceSpec.model_validate(payload)
    second = TraceSpec.model_validate(first.model_dump(mode="json"))
    assert trace_spec_fingerprint(first) == trace_spec_fingerprint(second)


def test_trace_spec_rejects_untyped_formula_unknown_operator_and_bad_conditions() -> (
    None
):
    with pytest.raises(ValidationError):
        TraceSpec.model_validate(
            {
                "properties": [
                    {
                        **_property({"op": "event", "event_type": "x"}),
                        "formula": "never(x)",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        TraceSpec.model_validate({"properties": [_property({"op": "sometimes"})]})
    with pytest.raises(ValidationError, match="requires a list"):
        EventSelector.model_validate(
            {
                "event_type": "x",
                "where": [{"path": "data.device_id", "comparator": "in", "value": "x"}],
            }
        )


def test_trace_spec_rejects_duplicate_ids_and_invalid_windows() -> None:
    prop = _property({"op": "event", "event_type": "x"})
    with pytest.raises(ValidationError, match="must be unique"):
        TraceSpec.model_validate({"properties": [prop, prop]})
    with pytest.raises(ValidationError, match="start_seconds <= end_seconds"):
        TraceSpec.model_validate(
            {
                "properties": [
                    _property(
                        {
                            "op": "eventually",
                            "window": {"start_seconds": 5, "end_seconds": 1},
                            "operand": {"op": "event", "event_type": "x"},
                        }
                    )
                ]
            }
        )
