"""Finite-trace verifier for the typed AuraBench TraceSpec language.

All temporal bounds use ``sim_time_s``.  ``seq`` is the sole tie-breaker when
events share a simulated timestamp.  Bounds are closed on both ends.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
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


MAX_VERIFICATION_WORK = 1_000_000


class _WorkBudgetExceeded(RuntimeError):
    pass


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


@dataclass(frozen=True)
class _TriggeredTrace:
    """Canonical trace suffix whose simulated clock starts at its trigger."""

    events: tuple[dict[str, Any], ...]
    trigger_event_id: str
    trigger_seq: int
    trigger_sim_time_s: float


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


def _charge_comparison(
    spend: Callable[[int], None], left: Any, right: Any
) -> None:
    """Charge before comparing bounded JSON-like values."""

    stack = [left, right]
    seen: set[int] = set()
    while stack:
        value = stack.pop()
        spend(1)
        if isinstance(value, str):
            spend(len(value))
        elif isinstance(value, bytes):
            spend(len(value))
        elif isinstance(value, Mapping):
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, (list, tuple)):
            identity = id(value)
            if identity in seen:
                continue
            seen.add(identity)
            stack.extend(value)


def _condition_matches(
    condition: EventFieldCondition,
    actual: Any,
    *,
    spend: Callable[[int], None] | None = None,
) -> bool:
    if spend is not None:
        spend(1)
    comparator = condition.comparator
    expected = condition.value
    if comparator == "exists":
        return (actual is not MISSING) is expected
    if actual is MISSING:
        return False
    if comparator == "eq":
        if spend is not None:
            _charge_comparison(spend, actual, expected)
        return strict_equal(actual, expected)
    if comparator == "ne":
        if spend is not None:
            _charge_comparison(spend, actual, expected)
        return not strict_equal(actual, expected)
    if comparator in {"in", "not_in"}:
        assert isinstance(expected, list)
        if spend is not None:
            for item in expected:
                _charge_comparison(spend, actual, item)
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
            if spend is not None:
                expected_length = len(expected) if isinstance(expected, str) else 1
                spend(len(actual) + expected_length)
            return isinstance(expected, str) and expected in actual
        if isinstance(actual, (list, tuple)):
            if spend is not None:
                for item in actual:
                    _charge_comparison(spend, item, expected)
            return any(strict_equal(item, expected) for item in actual)
        return False
    raise AssertionError(f"unsupported comparator {comparator!r}")


def _selector_matches(
    selector: EventSelector,
    event: TraceEvent,
    *,
    spend: Callable[[int], None] | None = None,
) -> bool:
    if selector.event_type is not None:
        if spend is not None:
            _charge_comparison(spend, event.event_type, selector.event_type)
        if event.event_type != selector.event_type:
            return False
    if selector.source is not None:
        if spend is not None:
            _charge_comparison(spend, event.source, selector.source)
        if event.source != selector.source:
            return False
    for condition in selector.where:
        if spend is not None:
            spend(len(condition.path))
        if not _condition_matches(
            condition,
            event.field(condition.path),
            spend=spend,
        ):
            return False
    return True


def _trace_suffix_from_trigger(
    selector: EventSelector, events: list[Any]
) -> _TriggeredTrace:
    """Select one trigger and return its re-based, canonical trace suffix.

    Building the suffix from a validated ``TraceIndex`` keeps trigger matching
    identical to TraceSpec evaluation.  Sequence numbers and simulated time are
    re-based because the suffix is itself a finite canonical trace; causal
    parents before the trigger become external roots.
    """

    index = TraceIndex(events)
    matches = [
        event for event in index.events if _selector_matches(selector, event)
    ]
    if len(matches) != 1:
        raise TraceValidationError(
            "intervention response trigger must match exactly one event; "
            f"matched {len(matches)}"
        )

    trigger = matches[0]
    suffix = tuple(event for event in index.events if event.seq >= trigger.seq)
    suffix_ids = {event.event_id for event in suffix}
    normalized: list[dict[str, Any]] = []
    for seq, event in enumerate(suffix):
        payload = dict(event.payload)
        payload["seq"] = seq
        payload["sim_time_s"] = event.sim_time_s - trigger.sim_time_s
        if event.causal_parent not in suffix_ids:
            payload["causal_parent"] = None
        normalized.append(payload)

    return _TriggeredTrace(
        events=tuple(normalized),
        trigger_event_id=trigger.event_id,
        trigger_seq=trigger.seq,
        trigger_sim_time_s=trigger.sim_time_s,
    )


class _Verifier:
    def __init__(self, index: TraceIndex, *, work_limit: int) -> None:
        self.index = index
        self.work_limit = work_limit
        self._remaining_work = work_limit

    def _spend(self, amount: int) -> None:
        self._remaining_work -= amount
        if self._remaining_work < 0:
            raise _WorkBudgetExceeded(
                f"TraceSpec verification exceeded {self.work_limit} work units"
            )

    def evaluate_property(self, prop: TraceProperty) -> PropertyVerification:
        try:
            outcome = self._evaluate(
                prop.expression,
                self.index.events,
                anchor_time_s=0.0,
            )
            primary = outcome.counterexample or outcome.witness
            causal_chain = outcome.causal_chain
            if not causal_chain and primary:
                causal_chain = self._causal_chain(primary[0].event_id)
        except _WorkBudgetExceeded as exc:
            outcome = _Outcome(
                VerificationStatus.UNEVALUABLE,
                str(exc),
                details={"work_limit": self.work_limit},
            )
            causal_chain = ()
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
        self._spend(len(events))
        if isinstance(expression, EventSelector):
            matches = tuple(
                event
                for event in events
                if _selector_matches(expression, event, spend=self._spend)
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
                event
                for event in events
                if _selector_matches(expression.trigger, event, spend=self._spend)
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
                self._spend(len(events))
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
                event
                for event in scoped
                if _selector_matches(
                    expression.selector, event, spend=self._spend
                )
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
            self._spend(1)
            if (
                expression.relation == "same_correlation"
                and trigger.correlation_id is None
            ):
                continue
            if expression.relation == "same_correlation":
                _charge_comparison(
                    self._spend,
                    candidate.correlation_id,
                    trigger.correlation_id,
                )
                if candidate.correlation_id != trigger.correlation_id:
                    continue
            if expression.relation == "causal_descendant":
                parent_id = candidate.causal_parent
                matched_ancestor = False
                while parent_id is not None:
                    _charge_comparison(
                        self._spend, parent_id, trigger.event_id
                    )
                    if parent_id == trigger.event_id:
                        matched_ancestor = True
                        break
                    self._spend(1)
                    parent_id = self.index.by_id[parent_id].causal_parent
                if not matched_ancestor:
                    continue
            joined = True
            for join in expression.join_on:
                self._spend(len(join.trigger_path) + len(join.consequent_path))
                left = trigger.field(join.trigger_path)
                right = candidate.field(join.consequent_path)
                _charge_comparison(self._spend, left, right)
                if not strict_equal(left, right):
                    joined = False
                    break
            if not joined:
                continue
            related.append(candidate)
        return tuple(related)

    def _chain_from_ancestor(
        self, ancestor: TraceEvent, descendant: TraceEvent
    ) -> tuple[TraceEvent, ...]:
        chain = self._causal_chain(descendant.event_id)
        for index, event in enumerate(chain):
            if event.event_id == ancestor.event_id:
                return chain[index:]
        return ()

    def _causal_chain(self, event_id: str) -> tuple[TraceEvent, ...]:
        reverse_chain: list[TraceEvent] = []
        current_id: str | None = event_id
        while current_id is not None:
            self._spend(1 + len(current_id))
            event = self.index.by_id[current_id]
            reverse_chain.append(event)
            current_id = event.causal_parent
        reverse_chain.reverse()
        return tuple(reverse_chain)

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
        index = TraceIndex(events)
    except (TraceValidationError, RecursionError) as exc:
        return _unevaluable_spec(
            trace_spec, f"invalid canonical trace: {exc}"
        )

    work_per_property = max(
        1, MAX_VERIFICATION_WORK // len(trace_spec.properties)
    )
    properties = tuple(
        _Verifier(index, work_limit=work_per_property).evaluate_property(prop)
        for prop in trace_spec.properties
    )
    return TraceVerification(
        hard_status=_aggregate_hard_status(properties),
        properties=properties,
        language_version=trace_spec.language_version,
    )
