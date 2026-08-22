"""Typed TraceSpec contract for finite, persisted Aura event traces.

This module freezes the *data language* consumed by the temporal verifier.  It
deliberately contains no evaluation logic: PR-4 owns operator semantics,
predicate execution, witnesses, and counterexamples.  Keeping the AST here
lets ScenarioSpec validate and fingerprint a trace contract without importing
the evaluation package.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TRACE_SPEC_LANGUAGE_VERSION = "1.0"

_FIELD_PATH_RE = re.compile(
    r"^(?:event_id|event_type|source|timestamp|sim_time_s|correlation_id|"
    r"causal_parent|priority|run_id|scenario_id|seq|depth|data(?:\.[A-Za-z0-9_-]+)+)$"
)

ScalarValue: TypeAlias = str | int | float | bool | None
ConditionValue: TypeAlias = ScalarValue | list[ScalarValue]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeWindow(_StrictModel):
    """Inclusive simulated-time offset window, relative to the current anchor."""

    start_seconds: float = Field(default=0.0, ge=0.0)
    end_seconds: float = Field(ge=0.0)

    @field_validator("start_seconds", "end_seconds", mode="before")
    @classmethod
    def _finite_seconds(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("TraceSpec time window must be a finite number")
        resolved = float(value)
        if not math.isfinite(resolved):
            raise ValueError("TraceSpec time window must be a finite number")
        return resolved

    @model_validator(mode="after")
    def _ordered(self) -> "TimeWindow":
        if self.end_seconds < self.start_seconds:
            raise ValueError(
                "TraceSpec time window requires start_seconds <= end_seconds"
            )
        return self


class EventFieldCondition(_StrictModel):
    """One typed comparison against an event envelope or ``data.*`` path."""

    path: str
    comparator: Literal[
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "contains",
        "exists",
    ] = "eq"
    value: ConditionValue

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not _FIELD_PATH_RE.fullmatch(value):
            raise ValueError(
                "event field path must be a supported envelope field "
                "or data.<field> path"
            )
        return value

    @model_validator(mode="after")
    def _value_matches_comparator(self) -> "EventFieldCondition":
        if self.comparator == "exists" and not isinstance(self.value, bool):
            raise ValueError("exists comparator requires a boolean value")
        if self.comparator in {"in", "not_in"} and not isinstance(self.value, list):
            raise ValueError(f"{self.comparator} comparator requires a list value")
        if self.comparator in {"lt", "lte", "gt", "gte"}:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError(
                    f"{self.comparator} comparator requires a numeric value"
                )
            if not math.isfinite(float(self.value)):
                raise ValueError(
                    f"{self.comparator} comparator requires a finite value"
                )
        return self


class EventSelector(_StrictModel):
    """Boolean leaf evaluated against one canonical trace event."""

    op: Literal["event"] = "event"
    event_type: str | None = Field(default=None, min_length=1)
    source: str | None = Field(default=None, min_length=1)
    where: list[EventFieldCondition] = Field(default_factory=list)

    @model_validator(mode="after")
    def _not_wildcard_without_constraints(self) -> "EventSelector":
        if self.event_type is None and self.source is None and not self.where:
            raise ValueError(
                "event selector must constrain event_type, source, or where"
            )
        return self


class AlwaysOperator(_StrictModel):
    op: Literal["always"] = "always"
    operand: "TraceExpression"
    window: TimeWindow | None = None


class NeverOperator(_StrictModel):
    op: Literal["never"] = "never"
    operand: "TraceExpression"
    window: TimeWindow | None = None


class EventuallyOperator(_StrictModel):
    op: Literal["eventually"] = "eventually"
    operand: "TraceExpression"
    window: TimeWindow


class EventFieldJoin(_StrictModel):
    """Equality join between the ``after`` trigger and its consequent events."""

    trigger_path: str
    consequent_path: str

    @field_validator("trigger_path", "consequent_path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not _FIELD_PATH_RE.fullmatch(value):
            raise ValueError(
                "event join path must be a supported envelope field "
                "or data.<field> path"
            )
        return value


class AfterOperator(_StrictModel):
    op: Literal["after"] = "after"
    trigger: EventSelector
    consequent: "TraceExpression"
    window: TimeWindow | None = None
    relation: Literal["any", "same_correlation", "causal_descendant"] = "any"
    join_on: list[EventFieldJoin] = Field(default_factory=list)


class UntilOperator(_StrictModel):
    op: Literal["until"] = "until"
    condition: "TraceExpression"
    terminal: "TraceExpression"
    window: TimeWindow | None = None


class CountOperator(_StrictModel):
    op: Literal["count"] = "count"
    selector: EventSelector
    comparator: Literal["lt", "lte", "eq", "gte", "gt"] = "lte"
    threshold: int = Field(ge=0, strict=True)
    window: TimeWindow | None = None


TraceExpression: TypeAlias = Annotated[
    EventSelector
    | AlwaysOperator
    | NeverOperator
    | EventuallyOperator
    | AfterOperator
    | UntilOperator
    | CountOperator,
    Field(discriminator="op"),
]

# Resolve the recursive discriminated union only after all operator classes exist.
_TRACE_TYPES = {"TraceExpression": TraceExpression}
for _operator in (
    AlwaysOperator,
    NeverOperator,
    EventuallyOperator,
    AfterOperator,
    UntilOperator,
):
    _operator.model_rebuild(_types_namespace=_TRACE_TYPES)


class TraceProperty(_StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    category: Literal[
        "safety",
        "privacy",
        "liveness",
        "causality",
        "relevance",
        "recovery",
        "efficiency",
    ]
    level: Literal["hard", "soft"] = "hard"
    expression: TraceExpression
    description: str | None = Field(default=None, min_length=1)
    weight: float | None = Field(default=None, gt=0.0)

    @model_validator(mode="after")
    def _weight_only_for_soft_properties(self) -> "TraceProperty":
        if self.level == "hard" and self.weight is not None:
            raise ValueError("hard TraceSpec properties cannot declare weight")
        return self


TraceProperty.model_rebuild(_types_namespace=_TRACE_TYPES)


class TraceSpec(_StrictModel):
    language_version: Literal[TRACE_SPEC_LANGUAGE_VERSION] = TRACE_SPEC_LANGUAGE_VERSION
    properties: list[TraceProperty] = Field(min_length=1)

    @field_validator("properties")
    @classmethod
    def _unique_property_ids(cls, value: list[TraceProperty]) -> list[TraceProperty]:
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("TraceSpec property ids must be unique")
        return value

    @model_validator(mode="after")
    def _has_hard_property(self) -> "TraceSpec":
        if not any(item.level == "hard" for item in self.properties):
            raise ValueError("TraceSpec must contain at least one hard property")
        return self


def trace_spec_fingerprint(spec: TraceSpec) -> str:
    """Stable SHA-256 of the declared TraceSpec AST (not verifier output)."""

    encoded = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "TRACE_SPEC_LANGUAGE_VERSION",
    "AfterOperator",
    "AlwaysOperator",
    "CountOperator",
    "EventFieldCondition",
    "EventFieldJoin",
    "EventSelector",
    "EventuallyOperator",
    "NeverOperator",
    "TimeWindow",
    "TraceExpression",
    "TraceProperty",
    "TraceSpec",
    "UntilOperator",
    "trace_spec_fingerprint",
]
