"""Finite-trace verifier for the typed AuraBench TraceSpec language.

All temporal bounds use ``sim_time_s``.  ``seq`` is the sole tie-breaker when
events share a simulated timestamp.  Bounds are closed on both ends.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.evaluation.trace_index import (
    MISSING,
    TraceEvent,
    TraceIndex,
    TraceValidationError,
    strict_equal,
)
from backend.scenarios.trace_spec import (
    AfterOperator,
    AlwaysOperator,
    CountOperator,
    EventFieldCondition,
    EventSelector,
    EventuallyOperator,
    NeverOperator,
    TimeWindow,
    TraceExpression,
    TraceProperty,
    TraceSpec,
    UntilOperator,
)


class VerificationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNEVALUABLE = "unevaluable"


@dataclass(frozen=True)
class _Outcome:
    status: VerificationStatus
    message: str
    witness: tuple[TraceEvent, ...] = ()
    counterexample: tuple[TraceEvent, ...] = ()
    causal_chain: tuple[TraceEvent, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PropertyVerification:
    property_id: str
    category: str
    level: str
    operator: str
    status: VerificationStatus
    message: str
    checked_event_count: int
    witness: tuple[dict[str, Any], ...] = ()
    counterexample: tuple[dict[str, Any], ...] = ()
    causal_chain: tuple[dict[str, Any], ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def witness_event_ids(self) -> tuple[str, ...]:
        return tuple(item["event_id"] for item in self.witness)

    @property
    def counterexample_event_ids(self) -> tuple[str, ...]:
        return tuple(item["event_id"] for item in self.counterexample)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.property_id,
            "category": self.category,
            "level": self.level,
            "operator": self.operator,
            "status": self.status.value,
            "message": self.message,
            "checked_event_count": self.checked_event_count,
            "witness_event_ids": list(self.witness_event_ids),
            "counterexample_event_ids": list(self.counterexample_event_ids),
            "witness": [dict(item) for item in self.witness],
            "counterexample": [dict(item) for item in self.counterexample],
            "causal_chain": [dict(item) for item in self.causal_chain],
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class TraceVerification:
    hard_status: VerificationStatus
    properties: tuple[PropertyVerification, ...]
    validation_errors: tuple[str, ...] = ()
    language_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "language_version": self.language_version,
            "hard_status": self.hard_status.value,
            "properties": [item.to_dict() for item in self.properties],
            "validation_errors": list(self.validation_errors),
        }


def _unique(events: tuple[TraceEvent, ...] | list[TraceEvent]) -> tuple[TraceEvent, ...]:
    seen: set[str] = set()
    result: list[TraceEvent] = []
    for event in events:
        if event.event_id not in seen:
            result.append(event)
            seen.add(event.event_id)
    return tuple(result)


def _window_bounds(
    window: TimeWindow | None, anchor_time_s: float
) -> tuple[float | None, float | None]:
    if window is None:
        return None, None
    return (
        anchor_time_s + window.start_seconds,
        anchor_time_s + window.end_seconds,
    )


def _within_window(
    events: tuple[TraceEvent, ...],
    window: TimeWindow | None,
    anchor_time_s: float,
) -> tuple[TraceEvent, ...]:
    start, end = _window_bounds(window, anchor_time_s)
    return tuple(
        event
        for event in events
        if (start is None or event.sim_time_s >= start)
        and (end is None or event.sim_time_s <= end)
    )


def _condition_matches(condition: EventFieldCondition, actual: Any) -> bool:
    comparator = condition.comparator
    expected = condition.value
    if comparator == "exists":
        return (actual is not MISSING) is expected
    if actual is MISSING:
        return False
    if comparator == "eq":
        return strict_equal(actual, expected)
    if comparator == "ne":
        return not strict_equal(actual, expected)
    if comparator in {"in", "not_in"}:
        assert isinstance(expected, list)
        contained = any(strict_equal(actual, item) for item in expected)
        return contained if comparator == "in" else not contained
    if comparator in {"lt", "lte", "gt", "gte"}:
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isfinite(float(actual))
        ):
            return False
        assert isinstance(expected, (int, float)) and not isinstance(expected, bool)
        return {
            "lt": actual < expected,
            "lte": actual <= expected,
            "gt": actual > expected,
            "gte": actual >= expected,
        }[comparator]
    if comparator == "contains":
        if isinstance(actual, str):
            return isinstance(expected, str) and expected in actual
        if isinstance(actual, (list, tuple)):
            return any(strict_equal(item, expected) for item in actual)
        return False
    raise AssertionError(f"unsupported comparator {comparator!r}")


def _selector_matches(selector: EventSelector, event: TraceEvent) -> bool:
    if selector.event_type is not None and event.event_type != selector.event_type:
        return False
    if selector.source is not None and event.source != selector.source:
        return False
    return all(
        _condition_matches(condition, event.field(condition.path))
        for condition in selector.where
    )


class _Verifier:
    def __init__(self, index: TraceIndex) -> None:
        self.index = index

    def evaluate_property(self, prop: TraceProperty) -> PropertyVerification:
        outcome = self._evaluate(
            prop.expression,
            self.index.events,
            anchor_time_s=0.0,
        )
        primary = outcome.counterexample or outcome.witness
        causal_chain = outcome.causal_chain
        if not causal_chain and primary:
            causal_chain = self.index.causal_chain(primary[0].event_id)
        return PropertyVerification(
            property_id=prop.id,
            category=prop.category,
            level=prop.level,
            operator=prop.expression.op,
            status=outcome.status,
            message=outcome.message,
            checked_event_count=int(
                outcome.details.get("scoped_event_count", len(self.index.events))
            ),
            witness=tuple(event.reference() for event in _unique(list(outcome.witness))),
            counterexample=tuple(
                event.reference() for event in _unique(list(outcome.counterexample))
            ),
            causal_chain=tuple(
                event.reference() for event in _unique(list(causal_chain))
            ),
            details=outcome.details,
        )

    def _evaluate(
        self,
        expression: TraceExpression,
        events: tuple[TraceEvent, ...],
        *,
        anchor_time_s: float,
    ) -> _Outcome:
        if isinstance(expression, EventSelector):
            matches = tuple(
                event for event in events if _selector_matches(expression, event)
            )
            if matches:
                return _Outcome(
                    VerificationStatus.PASS,
                    "selector matched",
                    witness=(matches[0],),
                    details={"match_count": len(matches)},
                )
            return _Outcome(
                VerificationStatus.FAIL,
                "selector did not match any event",
                details={"match_count": 0},
            )

        if isinstance(expression, NeverOperator):
            scoped = _within_window(events, expression.window, anchor_time_s)
            child = self._evaluate(expression.operand, scoped, anchor_time_s=anchor_time_s)
            if child.status == VerificationStatus.PASS:
                counterexample = child.witness or child.counterexample
                return _Outcome(
                    VerificationStatus.FAIL,
                    "forbidden expression occurred",
                    counterexample=counterexample[:1],
                    causal_chain=child.causal_chain,
                    details={"scoped_event_count": len(scoped)},
                )
            if child.status == VerificationStatus.UNEVALUABLE:
                return child
            return _Outcome(
                VerificationStatus.PASS,
                "forbidden expression did not occur",
                details={"scoped_event_count": len(scoped)},
            )

        if isinstance(expression, EventuallyOperator):
            scoped = _within_window(events, expression.window, anchor_time_s)
            child = self._evaluate(expression.operand, scoped, anchor_time_s=anchor_time_s)
            if child.status == VerificationStatus.PASS:
                return _Outcome(
                    VerificationStatus.PASS,
                    "required expression occurred within the closed window",
                    witness=child.witness,
                    causal_chain=child.causal_chain,
                    details={"scoped_event_count": len(scoped)},
                )
            if child.status == VerificationStatus.UNEVALUABLE:
                return child
            return _Outcome(
                VerificationStatus.FAIL,
                "required expression did not occur within the closed window",
                counterexample=child.counterexample,
                details={"scoped_event_count": len(scoped)},
            )

        if isinstance(expression, AlwaysOperator):
            scoped = _within_window(events, expression.window, anchor_time_s)
            if not scoped:
                return _Outcome(
                    VerificationStatus.UNEVALUABLE,
                    "always has no event in its evaluation window",
                    details={"scoped_event_count": 0},
                )
            for event in scoped:
                child = self._evaluate(
                    expression.operand, (event,), anchor_time_s=event.sim_time_s
                )
                if child.status != VerificationStatus.PASS:
                    return _Outcome(
                        child.status,
                        "always operand failed at the earliest event"
                        if child.status == VerificationStatus.FAIL
                        else child.message,
                        counterexample=(event,),
                        causal_chain=child.causal_chain,
                        details={"scoped_event_count": len(scoped)},
                    )
            return _Outcome(
                VerificationStatus.PASS,
                "operand held at every event in the window",
                details={"scoped_event_count": len(scoped)},
            )

        if isinstance(expression, AfterOperator):
            triggers = tuple(
                event for event in events if _selector_matches(expression.trigger, event)
            )
            if not triggers:
                return _Outcome(
                    VerificationStatus.UNEVALUABLE,
                    "after has no trigger event (non-vacuous semantics)",
                    details={"trigger_count": 0},
                )
            all_witnesses: list[TraceEvent] = []
            all_chains: list[TraceEvent] = []
            unevaluable: _Outcome | None = None
            for trigger in triggers:
                candidates = tuple(event for event in events if event.seq > trigger.seq)
                candidates = _within_window(
                    candidates, expression.window, trigger.sim_time_s
                )
                candidates = self._related_candidates(expression, trigger, candidates)
                child = self._evaluate(
                    expression.consequent,
                    candidates,
                    anchor_time_s=trigger.sim_time_s,
                )
                if child.status == VerificationStatus.FAIL:
                    counterexample = _unique([trigger, *child.counterexample])
                    return _Outcome(
                        VerificationStatus.FAIL,
                        "a trigger lacks its required consequent",
                        counterexample=counterexample,
                        causal_chain=child.causal_chain,
                        details={
                            "trigger_count": len(triggers),
                            "failed_trigger_event_id": trigger.event_id,
                        },
                    )
                if child.status == VerificationStatus.UNEVALUABLE:
                    if unevaluable is None:
                        unevaluable = _Outcome(
                            VerificationStatus.UNEVALUABLE,
                            "a trigger consequent is unevaluable: " + child.message,
                            counterexample=(trigger,),
                            details={
                                "trigger_count": len(triggers),
                                "unevaluable_trigger_event_id": trigger.event_id,
                            },
                        )
                    continue
                all_witnesses.extend((trigger, *child.witness))
                if (
                    expression.relation == "causal_descendant"
                    and child.witness
                    and not all_chains
                ):
                    all_chains.extend(
                        self._chain_from_ancestor(trigger, child.witness[0])
                    )
            if unevaluable is not None:
                return unevaluable
            return _Outcome(
                VerificationStatus.PASS,
                "every trigger has a required consequent",
                witness=_unique(all_witnesses),
                causal_chain=_unique(all_chains),
                details={"trigger_count": len(triggers)},
            )

        if isinstance(expression, UntilOperator):
            scoped = _within_window(events, expression.window, anchor_time_s)
            terminal: TraceEvent | None = None
            for event in scoped:
                child = self._evaluate(
                    expression.terminal, (event,), anchor_time_s=event.sim_time_s
                )
                if child.status == VerificationStatus.PASS:
                    terminal = event
                    break
            if terminal is None:
                return _Outcome(
                    VerificationStatus.FAIL,
                    "strong until terminal never occurred",
                    details={"scoped_event_count": len(scoped)},
                )
            prefix = tuple(event for event in scoped if event.seq < terminal.seq)
            for event in prefix:
                child = self._evaluate(
                    expression.condition, (event,), anchor_time_s=event.sim_time_s
                )
                if child.status != VerificationStatus.PASS:
                    return _Outcome(
                        child.status,
                        "until condition failed before the terminal"
                        if child.status == VerificationStatus.FAIL
                        else child.message,
                        counterexample=(event, terminal),
                        details={"terminal_event_id": terminal.event_id},
                    )
            return _Outcome(
                VerificationStatus.PASS,
                "condition held until the required terminal occurred",
                witness=(terminal,),
                details={
                    "terminal_event_id": terminal.event_id,
                    "condition_checked_count": len(prefix),
                    "scoped_event_count": len(scoped),
                },
            )

        if isinstance(expression, CountOperator):
            scoped = _within_window(events, expression.window, anchor_time_s)
            matches = tuple(
                event for event in scoped if _selector_matches(expression.selector, event)
            )
            actual = len(matches)
            threshold = expression.threshold
            passed = {
                "lt": actual < threshold,
                "lte": actual <= threshold,
                "eq": actual == threshold,
                "gte": actual >= threshold,
                "gt": actual > threshold,
            }[expression.comparator]
            details = {
                "actual_count": actual,
                "comparator": expression.comparator,
                "threshold": threshold,
                "scoped_event_count": len(scoped),
            }
            if passed:
                return _Outcome(
                    VerificationStatus.PASS,
                    f"count {actual} satisfies {expression.comparator} {threshold}",
                    witness=self._minimal_count_witness(
                        matches, expression.comparator, threshold
                    ),
                    details=details,
                )
            return _Outcome(
                VerificationStatus.FAIL,
                f"count {actual} does not satisfy {expression.comparator} {threshold}",
                counterexample=self._minimal_count_counterexample(
                    matches, expression.comparator, threshold
                ),
                details=details,
            )

        raise AssertionError(f"unsupported TraceSpec expression {type(expression)!r}")

    def _related_candidates(
        self,
        expression: AfterOperator,
        trigger: TraceEvent,
        candidates: tuple[TraceEvent, ...],
    ) -> tuple[TraceEvent, ...]:
        related: list[TraceEvent] = []
        for candidate in candidates:
            if (
                expression.relation == "same_correlation"
                and (
                    trigger.correlation_id is None
                    or candidate.correlation_id != trigger.correlation_id
                )
            ):
                continue
            if expression.relation == "causal_descendant" and not self.index.is_descendant(
                candidate, trigger.event_id
            ):
                continue
            if not all(
                strict_equal(
                    trigger.field(join.trigger_path),
                    candidate.field(join.consequent_path),
                )
                for join in expression.join_on
            ):
                continue
            related.append(candidate)
        return tuple(related)

    def _chain_from_ancestor(
        self, ancestor: TraceEvent, descendant: TraceEvent
    ) -> tuple[TraceEvent, ...]:
        chain = self.index.causal_chain(descendant.event_id)
        for index, event in enumerate(chain):
            if event.event_id == ancestor.event_id:
                return chain[index:]
        return ()

    @staticmethod
    def _minimal_count_witness(
        matches: tuple[TraceEvent, ...], comparator: str, threshold: int
    ) -> tuple[TraceEvent, ...]:
        if comparator == "gte":
            return matches[:threshold]
        if comparator == "gt":
            return matches[: threshold + 1]
        if comparator == "eq":
            return matches[:threshold]
        # For finite upper bounds the final count in details is the proof; no
        # subset of matching events can prove that later matches are absent.
        return ()

    @staticmethod
    def _minimal_count_counterexample(
        matches: tuple[TraceEvent, ...], comparator: str, threshold: int
    ) -> tuple[TraceEvent, ...]:
        if comparator == "lt" and len(matches) >= threshold:
            return matches[:threshold] if threshold else matches[:1]
        if comparator in {"lte", "eq"} and len(matches) > threshold:
            return matches[: threshold + 1]
        return matches


def _aggregate_hard_status(
    properties: tuple[PropertyVerification, ...]
) -> VerificationStatus:
    hard = tuple(item for item in properties if item.level == "hard")
    if any(item.status == VerificationStatus.FAIL for item in hard):
        return VerificationStatus.FAIL
    if any(item.status == VerificationStatus.UNEVALUABLE for item in hard):
        return VerificationStatus.UNEVALUABLE
    return VerificationStatus.PASS


def _non_finite_path(value: Any, path: str = "trace_spec") -> str | None:
    if isinstance(value, float) and not math.isfinite(value):
        return path
    if isinstance(value, Mapping):
        for key, child in value.items():
            found = _non_finite_path(child, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _non_finite_path(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _unevaluable_spec(
    trace_spec: TraceSpec, message: str
) -> TraceVerification:
    properties = tuple(
        PropertyVerification(
            property_id=prop.id,
            category=prop.category,
            level=prop.level,
            operator=prop.expression.op,
            status=VerificationStatus.UNEVALUABLE,
            message=message,
            checked_event_count=0,
        )
        for prop in trace_spec.properties
    )
    return TraceVerification(
        hard_status=VerificationStatus.UNEVALUABLE,
        properties=properties,
        validation_errors=(message,),
        language_version=trace_spec.language_version,
    )


def verify_trace(trace_spec: TraceSpec, events: list[Any]) -> TraceVerification:
    """Verify a complete TraceSpec against one finite persisted event list.

    Invalid evidence is returned as ``unevaluable`` rather than raised, making
    the result safe to embed directly in evaluation reports.  ``TraceIndex``
    remains available to callers that want the validation exception itself.
    """

    non_finite = _non_finite_path(trace_spec.model_dump(mode="python"))
    if non_finite is not None:
        return _unevaluable_spec(
            trace_spec,
            f"invalid TraceSpec: {non_finite} must not contain NaN or infinity",
        )

    try:
        verifier = _Verifier(TraceIndex(events))
    except (TraceValidationError, RecursionError) as exc:
        return _unevaluable_spec(
            trace_spec, f"invalid canonical trace: {exc}"
        )

    properties = tuple(
        verifier.evaluate_property(prop) for prop in trace_spec.properties
    )
    return TraceVerification(
        hard_status=_aggregate_hard_status(properties),
        properties=properties,
        language_version=trace_spec.language_version,
    )
