"""Validated, deterministic index over one finite canonical event trace.

The verifier deliberately does not repair a trace.  A duplicate id, a sequence
gap, a backwards simulated timestamp, or a forward causal edge makes the trace
unevaluable evidence instead of something we silently sort into a plausible
history.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class TraceValidationError(ValueError):
    """The supplied events are not a valid finite canonical trace."""


_MISSING = object()
MAX_TRACE_EVENTS = 100_000
MAX_EVENT_PAYLOAD_NODES = 100_000
MAX_EVENT_PAYLOAD_DEPTH = 64
MAX_EVENT_SCALAR_BYTES = 16 * 1024 * 1024


def _strict_equal(left: Any, right: Any) -> bool:
    """JSON-scalar equality without Python's ``True == 1`` coercion."""

    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if type(left) is not type(right):
        return False
    return left == right


def _as_mapping(event: Any, index: int) -> dict[str, Any]:
    if isinstance(event, Mapping):
        return dict(event)
    model_dump = getattr(event, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return dict(dumped)
    raise TraceValidationError(f"event at index {index} must be an object")


def _validate_payload(value: Any, path: str) -> None:
    """Bound traversal of untrusted artifact payloads without Python recursion."""

    stack: list[tuple[Any, str, int]] = [(value, path, 0)]
    node_count = 0
    scalar_bytes = 0
    seen_containers: set[int] = set()
    while stack:
        current, current_path, depth = stack.pop()
        node_count += 1
        if node_count > MAX_EVENT_PAYLOAD_NODES:
            raise TraceValidationError(
                f"{path} exceeds {MAX_EVENT_PAYLOAD_NODES} payload nodes"
            )
        if depth > MAX_EVENT_PAYLOAD_DEPTH:
            raise TraceValidationError(
                f"{current_path} exceeds nesting depth {MAX_EVENT_PAYLOAD_DEPTH}"
            )
        if isinstance(current, float) and not math.isfinite(current):
            raise TraceValidationError(
                f"{current_path} must not contain NaN or infinity"
            )
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen_containers:
                raise TraceValidationError(f"{current_path} contains a cycle")
            seen_containers.add(identity)
            for key, child in current.items():
                scalar_bytes += len(str(key).encode("utf-8", errors="replace"))
                stack.append((child, f"{current_path}.{key}", depth + 1))
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen_containers:
                raise TraceValidationError(f"{current_path} contains a cycle")
            seen_containers.add(identity)
            for index, child in enumerate(current):
                stack.append((child, f"{current_path}[{index}]", depth + 1))
        elif isinstance(current, (str, bytes)):
            scalar_bytes += len(
                current.encode("utf-8", errors="replace")
                if isinstance(current, str)
                else current
            )
        if scalar_bytes > MAX_EVENT_SCALAR_BYTES:
            raise TraceValidationError(
                f"{path} exceeds {MAX_EVENT_SCALAR_BYTES} scalar bytes"
            )


@dataclass(frozen=True)
class TraceEvent:
    """Normalized verifier view; ``payload`` retains all selector fields."""

    event_id: str
    event_type: str
    source: str
    seq: int
    sim_time_s: float
    correlation_id: str | None
    causal_parent: str | None
    data: Mapping[str, Any]
    payload: Mapping[str, Any]

    def field(self, path: str, default: Any = _MISSING) -> Any:
        if path.startswith("data."):
            current: Any = self.data
            parts = path.split(".")[1:]
        else:
            current = self.payload
            parts = path.split(".")
        for part in parts:
            if not isinstance(current, Mapping) or part not in current:
                return default
            current = current[part]
        return current

    def reference(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "seq": self.seq,
            "sim_time_s": self.sim_time_s,
        }


class TraceIndex:
    """Validate and index a finite trace in its persisted sequence order."""

    def __init__(self, events: Sequence[Any]) -> None:
        if len(events) > MAX_TRACE_EVENTS:
            raise TraceValidationError(
                f"trace exceeds maximum event count {MAX_TRACE_EVENTS}"
            )
        normalized: list[TraceEvent] = []
        by_id: dict[str, TraceEvent] = {}
        previous_seq: int | None = None
        previous_time: float | None = None

        for index, raw_event in enumerate(events):
            payload = _as_mapping(raw_event, index)
            event_id = payload.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise TraceValidationError(
                    f"event at index {index} has no non-empty event_id"
                )
            if event_id in by_id:
                raise TraceValidationError(f"duplicate event_id {event_id!r}")

            seq = payload.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                raise TraceValidationError(
                    f"event {event_id!r} seq must be a non-negative integer"
                )
            if previous_seq is None and seq != 0:
                raise TraceValidationError(
                    f"event {event_id!r} starts at seq {seq}; canonical traces start at 0"
                )
            if previous_seq is not None and seq != previous_seq + 1:
                raise TraceValidationError(
                    f"event {event_id!r} seq {seq} is not contiguous after {previous_seq}"
                )

            sim_time = payload.get("sim_time_s")
            if (
                isinstance(sim_time, bool)
                or not isinstance(sim_time, (int, float))
                or not math.isfinite(float(sim_time))
            ):
                raise TraceValidationError(
                    f"event {event_id!r} sim_time_s must be a finite number"
                )
            sim_time_s = float(sim_time)
            if previous_time is not None and sim_time_s < previous_time:
                raise TraceValidationError(
                    f"event {event_id!r} sim_time_s {sim_time_s} moves backwards "
                    f"from {previous_time}"
                )

            event_type = payload.get("event_type")
            source = payload.get("source")
            if not isinstance(event_type, str) or not event_type:
                raise TraceValidationError(
                    f"event {event_id!r} has no non-empty event_type"
                )
            if not isinstance(source, str) or not source:
                raise TraceValidationError(
                    f"event {event_id!r} has no non-empty source"
                )
            data = payload.get("data", {})
            if not isinstance(data, Mapping):
                raise TraceValidationError(f"event {event_id!r} data must be an object")
            _validate_payload(payload, f"event[{index}]")

            correlation_id = payload.get("correlation_id")
            if correlation_id is not None and not isinstance(correlation_id, str):
                raise TraceValidationError(
                    f"event {event_id!r} correlation_id must be null or a string"
                )
            causal_parent = payload.get("causal_parent")
            if causal_parent is not None and not isinstance(causal_parent, str):
                raise TraceValidationError(
                    f"event {event_id!r} causal_parent must be null or a string"
                )
            if causal_parent is not None and causal_parent not in by_id:
                raise TraceValidationError(
                    f"event {event_id!r} causal parent {causal_parent!r} must appear earlier"
                )

            event = TraceEvent(
                event_id=event_id,
                event_type=event_type,
                source=source,
                seq=seq,
                sim_time_s=sim_time_s,
                correlation_id=correlation_id,
                causal_parent=causal_parent,
                data=dict(data),
                payload=payload,
            )
            normalized.append(event)
            by_id[event_id] = event
            previous_seq = seq
            previous_time = sim_time_s

        self.events = tuple(normalized)
        self.by_id = by_id
        by_type: dict[str, list[TraceEvent]] = {}
        by_correlation: dict[str, list[TraceEvent]] = {}
        for event in normalized:
            by_type.setdefault(event.event_type, []).append(event)
            if event.correlation_id is not None:
                by_correlation.setdefault(event.correlation_id, []).append(event)
        self.by_type = {key: tuple(value) for key, value in by_type.items()}
        self.by_correlation = {
            key: tuple(value) for key, value in by_correlation.items()
        }

    def events_between(
        self,
        start_time_s: float | None = None,
        end_time_s: float | None = None,
        *,
        after_seq: int | None = None,
    ) -> tuple[TraceEvent, ...]:
        return tuple(
            event
            for event in self.events
            if (start_time_s is None or event.sim_time_s >= start_time_s)
            and (end_time_s is None or event.sim_time_s <= end_time_s)
            and (after_seq is None or event.seq > after_seq)
        )

    def causal_chain(self, event_id: str) -> tuple[TraceEvent, ...]:
        """Return the complete root-to-event chain (validated, so no cycles)."""

        event = self.by_id[event_id]
        reverse_chain = [event]
        while event.causal_parent is not None:
            event = self.by_id[event.causal_parent]
            reverse_chain.append(event)
        reverse_chain.reverse()
        return tuple(reverse_chain)

    def is_descendant(self, event: TraceEvent, ancestor_id: str) -> bool:
        parent_id = event.causal_parent
        while parent_id is not None:
            if parent_id == ancestor_id:
                return True
            parent_id = self.by_id[parent_id].causal_parent
        return False


def strict_equal(left: Any, right: Any) -> bool:
    """Public helper shared by selector predicates and ``after.join_on``."""

    if left is _MISSING or right is _MISSING:
        return False
    return _strict_equal(left, right)


MISSING = _MISSING
