from __future__ import annotations

from typing import Any

import pytest

from backend.engine.event_bus import SimEvent
from backend.evaluation.temporal import VerificationStatus, verify_trace
import backend.evaluation.trace_index as trace_index_module
from backend.evaluation.trace_index import TraceIndex, TraceValidationError
from backend.scenarios.trace_spec import TraceSpec


def _event(
    seq: int,
    event_type: str,
    *,
    sim_time_s: float | None = None,
    event_id: str | None = None,
    source: str = "test",
    correlation_id: str | None = "corr",
    causal_parent: str | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": event_id or f"e{seq}",
        "event_type": event_type,
        "source": source,
        "seq": seq,
        "sim_time_s": float(seq) if sim_time_s is None else sim_time_s,
        "correlation_id": correlation_id,
        "causal_parent": causal_parent,
        "data": data or {},
    }


def _spec(expression: dict[str, Any], *, level: str = "hard") -> TraceSpec:
    return TraceSpec.model_validate(
        {
            "properties": [
                {
                    "id": "trace_property",
                    "category": "safety",
                    "level": level,
                    "expression": expression,
                }
            ]
        }
    )


def _status(expression: dict[str, Any], events: list[dict[str, Any]]):
    result = verify_trace(_spec(expression), events)
    return result, result.properties[0]


def test_never_and_eventually_use_closed_simulated_time_windows() -> None:
    events = [
        _event(0, "start", sim_time_s=0),
        _event(1, "effect", sim_time_s=5),
    ]
    eventual = {
        "op": "eventually",
        "window": {"start_seconds": 5, "end_seconds": 5},
        "operand": {"op": "event", "event_type": "effect"},
    }
    result, prop = _status(eventual, events)
    assert result.hard_status == VerificationStatus.PASS
    assert prop.witness_event_ids == ("e1",)

    forbidden = {
        "op": "never",
        "window": {"start_seconds": 0, "end_seconds": 5},
        "operand": {"op": "event", "event_type": "effect"},
    }
    _, prop = _status(forbidden, events)
    assert prop.status == VerificationStatus.FAIL
    assert prop.counterexample_event_ids == ("e1",)


def test_vacuity_is_explicit_for_empty_and_triggerless_traces() -> None:
    never = {"op": "never", "operand": {"op": "event", "event_type": "bad"}}
    always = {"op": "always", "operand": {"op": "event", "event_type": "safe"}}
    eventual = {
        "op": "eventually",
        "window": {"end_seconds": 1},
        "operand": {"op": "event", "event_type": "done"},
    }
    after = {
        "op": "after",
        "trigger": {"op": "event", "event_type": "request"},
        "consequent": {"op": "event", "event_type": "done"},
    }
    assert _status(never, [])[1].status == VerificationStatus.PASS
    assert _status(always, [])[1].status == VerificationStatus.UNEVALUABLE
    assert _status(eventual, [])[1].status == VerificationStatus.FAIL
    assert _status(after, [])[1].status == VerificationStatus.UNEVALUABLE


def test_always_reports_the_earliest_counterexample() -> None:
    expression = {
        "op": "always",
        "operand": {
            "op": "event",
            "where": [{"path": "data.safe", "comparator": "eq", "value": True}],
        },
    }
    events = [
        _event(0, "sample", data={"safe": True}),
        _event(1, "sample", data={"safe": False}),
        _event(2, "sample", data={"safe": False}),
    ]
    _, prop = _status(expression, events)
    assert prop.status == VerificationStatus.FAIL
    assert prop.counterexample_event_ids == ("e1",)


def test_after_requires_a_consequent_for_every_trigger() -> None:
    expression = {
        "op": "after",
        "trigger": {"op": "event", "event_type": "request"},
        "relation": "same_correlation",
        "consequent": {
            "op": "eventually",
            "window": {"start_seconds": 0, "end_seconds": 2},
            "operand": {"op": "event", "event_type": "done"},
        },
    }
    events = [
        _event(0, "request", sim_time_s=0, correlation_id="c1"),
        _event(1, "done", sim_time_s=1, correlation_id="c1"),
        _event(2, "request", sim_time_s=3, correlation_id="c2"),
        _event(3, "done", sim_time_s=4, correlation_id="other"),
    ]
    _, prop = _status(expression, events)
    assert prop.status == VerificationStatus.FAIL
    assert prop.counterexample_event_ids == ("e2",)
    assert prop.details["failed_trigger_event_id"] == "e2"


def test_after_join_and_causal_descendant_return_minimal_causal_chain() -> None:
    expression = {
        "op": "after",
        "trigger": {"op": "event", "event_type": "request"},
        "relation": "causal_descendant",
        "join_on": [
            {
                "trigger_path": "data.device_id",
                "consequent_path": "data.device_id",
            }
        ],
        "consequent": {
            "op": "eventually",
            "window": {"start_seconds": 0, "end_seconds": 5},
            "operand": {"op": "event", "event_type": "feedback"},
        },
    }
    events = [
        _event(0, "request", event_id="root", data={"device_id": "light"}),
        _event(
            1,
            "plan",
            event_id="middle",
            causal_parent="root",
            data={"device_id": "light"},
        ),
        _event(
            2,
            "feedback",
            event_id="wrong",
            causal_parent="middle",
            data={"device_id": "hvac"},
        ),
        _event(
            3,
            "feedback",
            event_id="leaf",
            causal_parent="middle",
            data={"device_id": "light"},
        ),
    ]
    _, prop = _status(expression, events)
    assert prop.status == VerificationStatus.PASS
    assert prop.witness_event_ids == ("root", "leaf")
    assert tuple(item["event_id"] for item in prop.causal_chain) == (
        "root",
        "middle",
        "leaf",
    )


def test_strong_until_requires_terminal_and_condition_before_it() -> None:
    expression = {
        "op": "until",
        "condition": {"op": "event", "event_type": "executing"},
        "terminal": {"op": "event", "event_type": "complete"},
    }
    passing = [
        _event(0, "executing"),
        _event(1, "executing"),
        _event(2, "complete"),
    ]
    _, prop = _status(expression, passing)
    assert prop.status == VerificationStatus.PASS
    assert prop.witness_event_ids == ("e2",)
    assert prop.details["condition_checked_count"] == 2

    missing_terminal = [_event(0, "executing")]
    assert _status(expression, missing_terminal)[1].status == VerificationStatus.FAIL

    violated = [_event(0, "executing"), _event(1, "other"), _event(2, "complete")]
    _, prop = _status(expression, violated)
    assert prop.status == VerificationStatus.FAIL
    assert prop.counterexample_event_ids == ("e1", "e2")


@pytest.mark.parametrize(
    ("comparator", "threshold", "expected"),
    [
        ("lt", 3, VerificationStatus.PASS),
        ("lte", 2, VerificationStatus.PASS),
        ("eq", 2, VerificationStatus.PASS),
        ("gte", 2, VerificationStatus.PASS),
        ("gt", 1, VerificationStatus.PASS),
        ("lte", 1, VerificationStatus.FAIL),
    ],
)
def test_count_supports_all_comparators(
    comparator: str, threshold: int, expected: VerificationStatus
) -> None:
    expression = {
        "op": "count",
        "selector": {"op": "event", "event_type": "failure"},
        "comparator": comparator,
        "threshold": threshold,
    }
    events = [_event(0, "failure"), _event(1, "ok"), _event(2, "failure")]
    _, prop = _status(expression, events)
    assert prop.status == expected
    assert prop.details["actual_count"] == 2


def test_predicates_use_strict_types_and_nested_paths() -> None:
    events = [_event(0, "sample", data={"value": True, "nested": {"x": 3}})]
    equal_int = {
        "op": "eventually",
        "window": {"end_seconds": 1},
        "operand": {
            "op": "event",
            "where": [{"path": "data.value", "comparator": "eq", "value": 1}],
        },
    }
    not_equal_int = {
        **equal_int,
        "operand": {
            "op": "event",
            "where": [{"path": "data.value", "comparator": "ne", "value": 1}],
        },
    }
    nested = {
        **equal_int,
        "operand": {
            "op": "event",
            "where": [{"path": "data.nested.x", "comparator": "gte", "value": 3}],
        },
    }
    assert _status(equal_int, events)[1].status == VerificationStatus.FAIL
    assert _status(not_equal_int, events)[1].status == VerificationStatus.PASS
    assert _status(nested, events)[1].status == VerificationStatus.PASS


@pytest.mark.parametrize(
    ("comparator", "actual", "expected"),
    [
        ("lt", 2, 3),
        ("lte", 3, 3),
        ("gt", 4, 3),
        ("gte", 3, 3),
        ("in", "a", ["a", "b"]),
        ("not_in", "c", ["a", "b"]),
        ("contains", "smart home", "home"),
        ("contains", ["light", "hvac"], "hvac"),
        ("exists", None, True),
    ],
)
def test_all_field_comparators_have_typed_semantics(
    comparator: str, actual: Any, expected: Any
) -> None:
    expression = {
        "op": "eventually",
        "window": {"end_seconds": 1},
        "operand": {
            "op": "event",
            "where": [
                {
                    "path": "data.value",
                    "comparator": comparator,
                    "value": expected,
                }
            ],
        },
    }
    _, prop = _status(expression, [_event(0, "sample", data={"value": actual})])
    assert prop.status == VerificationStatus.PASS


def test_missing_field_only_matches_exists_false() -> None:
    base = {
        "op": "eventually",
        "window": {"end_seconds": 1},
        "operand": {"op": "event", "where": []},
    }
    exists_false = {
        **base,
        "operand": {
            "op": "event",
            "where": [
                {
                    "path": "data.missing",
                    "comparator": "exists",
                    "value": False,
                }
            ],
        },
    }
    missing_ne = {
        **base,
        "operand": {
            "op": "event",
            "where": [
                {"path": "data.missing", "comparator": "ne", "value": 1}
            ],
        },
    }
    events = [_event(0, "sample")]
    assert _status(exists_false, events)[1].status == VerificationStatus.PASS
    assert _status(missing_ne, events)[1].status == VerificationStatus.FAIL


def test_after_zero_window_uses_seq_to_order_same_time_events() -> None:
    expression = {
        "op": "after",
        "trigger": {"op": "event", "event_type": "request"},
        "window": {"start_seconds": 0, "end_seconds": 0},
        "consequent": {"op": "event", "event_type": "done"},
    }
    events = [
        _event(0, "request", sim_time_s=5),
        _event(1, "done", sim_time_s=5),
    ]
    assert _status(expression, events)[1].status == VerificationStatus.PASS


@pytest.mark.parametrize(
    "events",
    [
        [_event(0, "x"), _event(2, "y")],
        [_event(0, "x", event_id="same"), _event(1, "y", event_id="same")],
        [_event(0, "x", sim_time_s=2), _event(1, "y", sim_time_s=1)],
        [_event(0, "x", causal_parent="later"), _event(1, "y", event_id="later")],
    ],
)
def test_trace_index_rejects_noncanonical_evidence(
    events: list[dict[str, Any]],
) -> None:
    with pytest.raises(TraceValidationError):
        TraceIndex(events)
    result = verify_trace(
        _spec({"op": "never", "operand": {"op": "event", "event_type": "bad"}}),
        events,
    )
    assert result.hard_status == VerificationStatus.UNEVALUABLE
    assert result.validation_errors


def test_trace_index_rejects_nan_anywhere_in_event_payload() -> None:
    events = [_event(0, "sample", data={"value": float("nan")})]
    result = verify_trace(
        _spec({"op": "event", "event_type": "sample"}), events
    )
    assert result.hard_status == VerificationStatus.UNEVALUABLE
    assert "NaN" in result.validation_errors[0]


def test_trace_index_bounds_untrusted_payload_depth_and_event_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested: dict[str, Any] = {}
    cursor = nested
    for _ in range(70):
        child: dict[str, Any] = {}
        cursor["child"] = child
        cursor = child
    result = verify_trace(
        _spec({"op": "event", "event_type": "sample"}),
        [_event(0, "sample", data=nested)],
    )
    assert result.hard_status == VerificationStatus.UNEVALUABLE
    assert "nesting depth" in result.validation_errors[0]

    monkeypatch.setattr(trace_index_module, "MAX_TRACE_EVENTS", 1)
    with pytest.raises(TraceValidationError, match="maximum event count"):
        TraceIndex([_event(0, "sample"), _event(1, "sample")])


def test_verifier_rejects_nan_in_typed_comparison_value() -> None:
    spec = _spec(
        {
            "op": "event",
            "event_type": "sample",
            "where": [
                {"path": "data.value", "comparator": "eq", "value": float("nan")}
            ],
        }
    )
    result = verify_trace(spec, [_event(0, "sample", data={"value": 1.0})])
    assert result.hard_status == VerificationStatus.UNEVALUABLE
    assert "TraceSpec" in result.validation_errors[0]


def test_trace_index_accepts_sim_event_models_but_rejects_nonzero_first_seq() -> None:
    modeled = SimEvent(
        event_id="model-event",
        event_type="sample",
        source="test",
        timestamp=0,
        seq=0,
        sim_time_s=0,
        correlation_id="corr",
    )
    assert TraceIndex([modeled]).events[0].event_id == "model-event"
    with pytest.raises(TraceValidationError, match="start at 0"):
        TraceIndex([_event(1, "sample")])


def test_contains_does_not_treat_mapping_keys_as_collection_membership() -> None:
    expression = {
        "op": "eventually",
        "window": {"end_seconds": 1},
        "operand": {
            "op": "event",
            "where": [
                {"path": "data.mapping", "comparator": "contains", "value": "key"}
            ],
        },
    }
    _, prop = _status(expression, [_event(0, "sample", data={"mapping": {"key": 1}})])
    assert prop.status == VerificationStatus.FAIL


def test_soft_failure_does_not_change_hard_status_and_result_serializes() -> None:
    spec = TraceSpec.model_validate(
        {
            "properties": [
                {
                    "id": "hard_safe",
                    "category": "safety",
                    "level": "hard",
                    "expression": {
                        "op": "never",
                        "operand": {"op": "event", "event_type": "bad"},
                    },
                },
                {
                    "id": "soft_done",
                    "category": "liveness",
                    "level": "soft",
                    "weight": 1,
                    "expression": {
                        "op": "eventually",
                        "window": {"end_seconds": 2},
                        "operand": {"op": "event", "event_type": "done"},
                    },
                },
            ]
        }
    )
    result = verify_trace(spec, [_event(0, "ok")])
    assert result.hard_status == VerificationStatus.PASS
    assert result.properties[1].status == VerificationStatus.FAIL
    serialized = result.to_dict()
    assert serialized["hard_status"] == "pass"
    assert serialized["properties"][1]["status"] == "fail"
    assert set(
        (
            "operator",
            "witness_event_ids",
            "counterexample_event_ids",
            "checked_event_count",
        )
    ).issubset(serialized["properties"][0])
