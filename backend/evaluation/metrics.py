"""Canonical S4 metrics computed from the persisted event wire schema."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, TypeAlias

from backend.engine.event_types import starts_agent_episode

EventLike: TypeAlias = Any
MetricValue: TypeAlias = bool | int | float | None

REQUIRED_REASONING_EVENT_TYPES: tuple[str, ...] = (
    "reasoning.perception_snapshot",
    "reasoning.intent_recognized",
    "reasoning.task_decomposition",
    "reasoning.coordination_decision",
    "reasoning.execution_plan",
)
TERMINAL_COMMAND_STATUSES = frozenset(
    {"succeeded", "failed", "rejected", "timed_out", "cancelled", "superseded"}
)
FAILURE_COMMAND_STATUSES = frozenset({"failed", "timed_out", "cancelled"})
EXPECTED_CANCELLATION_CATEGORIES = frozenset(
    {
        "reset_during_in_flight_episode",
        "user_override_of_agent_proposal",
        "user_activity_change_after_action",
        "safety_event_interrupts_comfort",
    }
)
_DEVICE_PATH_RE = re.compile(r"^devices\[([^\]]+)\]\.state\.(.+)$")


def _get(event: EventLike, key: str, default: Any = None) -> Any:
    if isinstance(event, Mapping):
        return event.get(key, default)
    return getattr(event, key, default)


def _event_data(event: EventLike, key: str, default: Any = None) -> Any:
    data = _get(event, "data", {})
    return data.get(key, default) if isinstance(data, Mapping) else default


def _order_key(event: EventLike) -> tuple[int, float, float]:
    seq = _get(event, "seq")
    return (
        int(seq) if isinstance(seq, int) else 2**63 - 1,
        float(_get(event, "sim_time_s", _get(event, "timestamp", 0.0)) or 0.0),
        float(_get(event, "wall_time", 0.0) or 0.0),
    )


def _sim_time(event: EventLike) -> float:
    value = _get(event, "sim_time_s")
    return float((_get(event, "timestamp", 0.0) if value is None else value) or 0.0)


def _is_agent_episode_root(event: EventLike) -> bool:
    event_type = str(_get(event, "event_type", ""))
    data = _get(event, "data", {})
    return starts_agent_episode(event_type, data if isinstance(data, Mapping) else {})


@dataclass(frozen=True)
class MetricDatum:
    name: str
    value: MetricValue
    unit: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetricsCollector:
    """Indexed run evidence plus its ScenarioSpec-derived contract."""

    events: list[EventLike]
    scenario_id: str | None = None
    seed: int | None = None
    run_id: str = ""
    expected_failures: list[dict[str, Any]] = field(default_factory=list)
    expected_device_effects: list[dict[str, Any]] = field(default_factory=list)
    initial_device_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    ground_truth: dict[str, Any] | None = None
    device_rooms: dict[str, str] = field(default_factory=dict)
    device_types: dict[str, str] = field(default_factory=dict)
    success_criteria: dict[str, Any] = field(default_factory=dict)
    _by_type: dict[str, list[EventLike]] = field(default_factory=dict, init=False)
    _by_correlation: dict[str, list[EventLike]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.events = sorted(self.events, key=_order_key)

    def events_of_type(self, event_type: str) -> list[EventLike]:
        if event_type not in self._by_type:
            self._by_type[event_type] = [
                event for event in self.events if _get(event, "event_type") == event_type
            ]
        return self._by_type[event_type]

    def events_by_correlation(self, correlation_id: str) -> list[EventLike]:
        if correlation_id not in self._by_correlation:
            self._by_correlation[correlation_id] = [
                event
                for event in self.events
                if _get(event, "correlation_id") == correlation_id
            ]
        return self._by_correlation[correlation_id]

    @property
    def command_lifecycle_events(self) -> list[EventLike]:
        return self.events_of_type("command.lifecycle")

    @property
    def agent_episode_ids(self) -> list[str]:
        """Expected agent episodes, derived from roots rather than their output.

        This mirrors the runtime trigger gate closely enough for persisted
        evidence: insignificant environment refreshes and already-final direct
        device commands do not open an agent episode.  Reasoning correlations
        are unioned in so orphan reasoning with an invalid/missing root cannot
        disappear from evaluation either.
        """

        ids: set[str] = set()
        for event in self.events:
            correlation_id = _get(event, "correlation_id")
            if not correlation_id:
                continue
            event_type = str(_get(event, "event_type", ""))
            if event_type.startswith("reasoning."):
                ids.add(str(correlation_id))
                continue
            if _is_agent_episode_root(event):
                ids.add(str(correlation_id))
        return sorted(ids)

    @property
    def final_command_events(self) -> dict[str, EventLike]:
        final: dict[str, EventLike] = {}
        for event in self.command_lifecycle_events:
            command_id = str(_event_data(event, "command_id", "") or "")
            if command_id:
                final[command_id] = event
        return final


def compute_episode_complete(collector: MetricsCollector) -> MetricDatum:
    episodes: dict[str, Any] = {}
    complete_count = 0
    for correlation_id in collector.agent_episode_ids:
        scoped = collector.events_by_correlation(correlation_id)
        roots = [event for event in scoped if _get(event, "causal_parent") is None]
        accepted_roots = [event for event in scoped if _is_agent_episode_root(event)]
        present = {str(_get(event, "event_type", "")) for event in scoped}
        missing = [kind for kind in REQUIRED_REASONING_EVENT_TYPES if kind not in present]
        approved_events = [
            event
            for event in scoped
            if _get(event, "event_type") == "command.lifecycle"
            and _event_data(event, "to_status") == "approved"
            and _event_data(event, "command_id")
        ]
        approved_ids = {
            str(_event_data(event, "command_id")) for event in approved_events
        }
        approved_actions = [
            event
            for event in scoped
            if _get(event, "event_type") == "action.device_control"
            and str(_event_data(event, "command_id", "")) in approved_ids
        ]
        action_ids = {str(_get(event, "event_id", "")) for event in approved_actions}
        feedbacks = [
            event
            for event in scoped
            if _get(event, "event_type") == "feedback.state_delta"
            and str(_get(event, "causal_parent", "")) in action_ids
        ]
        if len(accepted_roots) != 1:
            missing.append("root_event")
        if not approved_ids:
            missing.append("approved_command")
        if not approved_actions:
            missing.append("action.device_control")
        if not feedbacks:
            missing.append("feedback.state_delta")

        # Presence under one correlation id is insufficient: the selected
        # feedback must descend through every ring on one causal lineage.  The
        # approved lifecycle event is intentionally handled separately because
        # production emits it as the execution plan's sibling, not the action's
        # ancestor.
        if (
            len(accepted_roots) == 1
            and approved_actions
            and feedbacks
            and not any(kind in missing for kind in REQUIRED_REASONING_EVENT_TYPES)
        ):
            root = accepted_roots[0]
            by_id = {
                str(_get(event, "event_id")): event
                for event in scoped
                if _get(event, "event_id")
            }
            root_id = str(_get(root, "event_id", ""))
            lineage_found = False
            complete_lineage_found = False

            for feedback in sorted(feedbacks, key=_order_key):
                action = by_id.get(str(_get(feedback, "causal_parent", "")))
                if action is None or _get(action, "event_type") != "action.device_control":
                    continue

                # Walk only as far as the accepted agent trigger.  A significant
                # environment refresh may itself be parented by a timer event;
                # that compatibility parent is outside the agent episode.
                reverse_lineage: list[EventLike] = [action]
                seen = {str(_get(action, "event_id", ""))}
                cursor = action
                while str(_get(cursor, "event_id", "")) != root_id:
                    parent_id = str(_get(cursor, "causal_parent", "") or "")
                    if not parent_id or parent_id in seen:
                        break
                    parent = by_id.get(parent_id)
                    if parent is None:
                        break
                    reverse_lineage.append(parent)
                    seen.add(parent_id)
                    cursor = parent
                if str(_get(cursor, "event_id", "")) != root_id:
                    continue

                lineage = list(reversed(reverse_lineage))
                expected_types = [
                    *REQUIRED_REASONING_EVENT_TYPES,
                    "action.device_control",
                ]
                matched: list[EventLike] = []
                next_type = 0
                for ancestor in lineage[1:]:
                    if (
                        next_type < len(expected_types)
                        and _get(ancestor, "event_type") == expected_types[next_type]
                    ):
                        matched.append(ancestor)
                        next_type += 1
                if next_type != len(expected_types):
                    continue
                lineage_found = True

                seq_events = [*lineage, feedback]
                seqs = [_get(event, "seq") for event in seq_events]
                if not all(isinstance(seq, int) for seq in seqs):
                    continue
                if not all(left < right for left, right in zip(seqs, seqs[1:])):
                    continue

                plan = matched[-2]
                plan_seq = _get(plan, "seq")
                action_seq = _get(action, "seq")
                action_command_id = str(_event_data(action, "command_id", ""))
                matching_approvals = [
                    event
                    for event in approved_events
                    if str(_event_data(event, "command_id", ""))
                    == action_command_id
                ]
                if not any(
                    isinstance(_get(approved, "seq"), int)
                    and plan_seq < _get(approved, "seq") < action_seq
                    for approved in matching_approvals
                ):
                    continue

                complete_lineage_found = True
                break

            if not lineage_found:
                missing.append("causal_connection")
            elif not complete_lineage_found:
                missing.append("causal_order")
        complete = not missing
        complete_count += int(complete)
        episodes[correlation_id] = {
            "complete": complete,
            "root_count": len(roots),
            "accepted_root_count": len(accepted_roots),
            "root_event_types": [
                str(_get(event, "event_type", "")) for event in accepted_roots
            ],
            "causal_root_event_types": [
                str(_get(event, "event_type", "")) for event in roots
            ],
            "approved_command_ids": sorted(approved_ids),
            "approved_action_count": len(approved_actions),
            "feedback_count": len(feedbacks),
            "missing": missing,
        }
    total = len(episodes)
    return MetricDatum(
        "episode_complete",
        bool(total and complete_count == total),
        "boolean",
        {
            "episodes": episodes,
            "complete_count": complete_count,
            "episode_count": total,
            "selection": "agent_triggering_roots_union_reasoning_correlations",
        },
    )


def compute_first_action_latency_ms(collector: MetricsCollector) -> MetricDatum:
    by_episode: dict[str, Any] = {}
    valid: list[float] = []
    for correlation_id in collector.agent_episode_ids:
        scoped = collector.events_by_correlation(correlation_id)
        accepted_roots = [event for event in scoped if _is_agent_episode_root(event)]
        actions = [
            event
            for event in scoped
            if _get(event, "event_type") == "action.device_control"
        ]
        if len(accepted_roots) != 1 or not actions:
            by_episode[correlation_id] = {
                "latency_ms": None,
                "reason": (
                    "missing_unique_agent_root"
                    if len(accepted_roots) != 1
                    else "missing_action"
                ),
            }
            continue
        action = min(actions, key=lambda event: float(_get(event, "wall_time", 0.0)))
        latency = (
            float(_get(action, "wall_time", 0.0))
            - float(_get(accepted_roots[0], "wall_time", 0.0))
        ) * 1000.0
        if latency < 0:
            by_episode[correlation_id] = {
                "latency_ms": None,
                "reason": "action_precedes_root_wall_time",
                "observed_latency_ms": latency,
            }
            continue
        valid.append(latency)
        by_episode[correlation_id] = {"latency_ms": latency}
    return MetricDatum(
        "first_action_latency_ms",
        sum(valid) / len(valid) if valid else None,
        "ms",
        {
            "aggregation": "mean_over_agent_episodes",
            "by_episode": by_episode,
            "sample_count": len(valid),
            "episode_count": len(collector.agent_episode_ids),
            "max_latency_ms": max(valid) if valid else None,
            "missing_episode_ids": [
                correlation_id
                for correlation_id, item in by_episode.items()
                if item.get("latency_ms") is None
            ],
        },
    )


def _matching_expected_failure_index(
    event: EventLike, declarations: list[dict[str, Any]]
) -> int | None:
    status = str(_event_data(event, "to_status", ""))
    for index, declaration in enumerate(declarations):
        declared_device = declaration.get("device_id")
        if declared_device is not None and declared_device != _event_data(event, "device_id"):
            continue
        if status == "cancelled":
            if declaration.get("category") in EXPECTED_CANCELLATION_CATEGORIES:
                return index
        elif _event_data(event, "failure_code") and declaration.get("error_code") == _event_data(event, "failure_code"):
            return index
    return None


def compute_command_failure_count(collector: MetricsCollector) -> MetricDatum:
    unexpected: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    terminal: dict[str, str] = {}
    unterminated: list[str] = []
    observed_declarations: set[int] = set()
    for command_id, event in collector.final_command_events.items():
        status = str(_event_data(event, "to_status", ""))
        terminal[command_id] = status
        if status not in TERMINAL_COMMAND_STATUSES:
            unterminated.append(command_id)
        elif status in FAILURE_COMMAND_STATUSES:
            item = {
                "command_id": command_id,
                "device_id": _event_data(event, "device_id"),
                "status": status,
                "failure_code": _event_data(event, "failure_code"),
            }
            declaration_index = _matching_expected_failure_index(
                event, collector.expected_failures
            )
            if declaration_index is None:
                unexpected.append(item)
            else:
                expected.append(item)
                observed_declarations.add(declaration_index)
    unobserved = [
        declaration
        for index, declaration in enumerate(collector.expected_failures)
        if index not in observed_declarations
    ]
    return MetricDatum(
        "command_failure_count",
        len(unexpected),
        "count",
        {
            "unexpected_failures": unexpected,
            "expected_failures": expected,
            "unexpected_failure_count": len(unexpected),
            "expected_failure_count": len(expected),
            "declared_expected_failure_count": len(collector.expected_failures),
            "unobserved_expected_failures": unobserved,
            "unique_command_count": len(collector.final_command_events),
            "terminal_status_by_command": terminal,
            "unterminated_command_ids": sorted(unterminated),
        },
    )


def compute_fallback_count(collector: MetricsCollector) -> MetricDatum:
    events = collector.events_of_type("reasoning.fallback_rule_based")
    return MetricDatum(
        "fallback_count",
        len(events),
        "count",
        {
            "event_ids": [str(_get(event, "event_id", "")) for event in events],
            "reasons": [_event_data(event, "reason") for event in events],
        },
    )


def compute_conflict_count(collector: MetricsCollector) -> MetricDatum:
    by_decision: list[dict[str, Any]] = []
    total = 0
    for event in collector.events_of_type("reasoning.coordination_decision"):
        conflicts = _event_data(event, "conflicts", [])
        conflicts = conflicts if isinstance(conflicts, list) else []
        total += len(conflicts)
        by_decision.append(
            {
                "event_id": _get(event, "event_id"),
                "correlation_id": _get(event, "correlation_id"),
                "count": len(conflicts),
            }
        )
    return MetricDatum(
        "conflict_count",
        total,
        "count",
        {"decision_count": len(by_decision), "by_decision": by_decision},
    )


def _constraint_matches(constraint: Any, actual: Any) -> bool:
    if not isinstance(constraint, Mapping):
        return type(actual) is type(constraint) and actual == constraint
    equals = constraint.get("equals")
    if equals is not None and (type(actual) is not type(equals) or actual != equals):
        return False
    if constraint.get("one_of") is not None and actual not in constraint["one_of"]:
        return False
    minimum, maximum = constraint.get("min"), constraint.get("max")
    if minimum is not None or maximum is not None:
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        if minimum is not None and actual < minimum:
            return False
        if maximum is not None and actual > maximum:
            return False
    return True


def _read_path(state: Mapping[str, Any], path: str) -> Any:
    value: Any = state
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _state_evidence(collector: MetricsCollector) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], float]]:
    states = copy.deepcopy(collector.initial_device_states)
    change_times: dict[tuple[str, str], float] = {}
    for event in collector.events_of_type("feedback.state_delta"):
        match = _DEVICE_PATH_RE.match(str(_event_data(event, "path", "")))
        if not match:
            continue
        device_id, property_path = match.groups()
        cursor = states.setdefault(device_id, {})
        parts = property_path.split(".")
        for part in parts[:-1]:
            child = cursor.get(part)
            if not isinstance(child, dict):
                child = {}
                cursor[part] = child
            cursor = child
        cursor[parts[-1]] = _event_data(event, "new_value")
        change_times[(device_id, property_path)] = _sim_time(event)
    return states, change_times


def compute_device_state_match_rate(collector: MetricsCollector) -> MetricDatum:
    if not collector.expected_device_effects:
        return MetricDatum(
            "device_state_match_rate",
            None,
            "ratio",
            {"evaluable": False, "reason": "no_expected_device_effects"},
        )
    states, change_times = _state_evidence(collector)
    fields: list[dict[str, Any]] = []
    matched_count = 0
    for effect in collector.expected_device_effects:
        device_id = str(effect.get("device_id", ""))
        within_seconds = effect.get("within_seconds")
        expected = effect.get("expected", {})
        if not isinstance(expected, Mapping):
            continue
        for path, constraint in expected.items():
            actual = _read_path(states.get(device_id, {}), str(path))
            value_ok = _constraint_matches(constraint, actual)
            changed_at = change_times.get((device_id, str(path)))
            deadline_ok = within_seconds is None or changed_at is None or changed_at <= float(within_seconds)
            matched = value_ok and deadline_ok
            matched_count += int(matched)
            fields.append(
                {
                    "device_id": device_id,
                    "path": str(path),
                    "expected": constraint,
                    "actual": actual,
                    "within_seconds": within_seconds,
                    "last_change_sim_time_s": changed_at,
                    "matched": matched,
                    "value_matched": value_ok,
                    "deadline_matched": deadline_ok,
                }
            )
    total = len(fields)
    return MetricDatum(
        "device_state_match_rate",
        matched_count / total if total else None,
        "ratio",
        {
            "evaluable": bool(total),
            "matched_field_count": matched_count,
            "expected_field_count": total,
            "fields": fields,
        },
    )


def _actions(collector: MetricsCollector) -> list[dict[str, Any]]:
    return [
        {
            "event_id": _get(event, "event_id"),
            "device_id": str(_event_data(event, "device_id", "")),
            "device_type": collector.device_types.get(str(_event_data(event, "device_id", "")), ""),
            "room_id": collector.device_rooms.get(str(_event_data(event, "device_id", "")), ""),
            "property": str(_event_data(event, "property", _event_data(event, "capability", ""))),
            "value": _event_data(event, "value"),
            "agent_id": _event_data(event, "agent_id", _get(event, "source")),
            "seq": _get(event, "seq"),
        }
        for event in collector.events_of_type("action.device_control")
    ]


def _evaluate_safety(collector: MetricsCollector) -> tuple[bool, list[dict[str, Any]]]:
    constraints = (collector.ground_truth or {}).get("safety_constraints", [])
    if not constraints:
        return True, []
    actions = _actions(collector)
    states, _ = _state_evidence(collector)
    ground_truth = collector.ground_truth or {}
    relevant_devices = set(ground_truth.get("relevant_device_ids", []))
    primary_rooms = set(ground_truth.get("primary_room_ids", []))
    explicit = [
        _get(event, "event_id")
        for event in collector.events
        if _get(event, "event_type")
        in {"system.invariant_violation", "safety.constraint_violated", "security.constraint_violated"}
    ]
    results: list[dict[str, Any]] = []
    for constraint in constraints:
        evaluable = True
        evidence: list[Any] = list(explicit)
        satisfied = not explicit
        if not explicit and constraint == "do_not_set_target_temp_below_16":
            evidence = [a for a in actions if a["property"].endswith("target_temp") and isinstance(a["value"], (int, float)) and not isinstance(a["value"], bool) and a["value"] < 16]
            satisfied = not evidence
        elif not explicit and constraint == "do_not_retry_commands_to_a_device_known_offline":
            counts: dict[str, int] = {}
            for event in collector.final_command_events.values():
                if _event_data(event, "failure_code") == "device_offline":
                    device = str(_event_data(event, "device_id", ""))
                    counts[device] = counts.get(device, 0) + 1
            evidence = [{key: value for key, value in counts.items() if value > 1}]
            satisfied = not evidence[0]
        elif not explicit and constraint in {"never_disable_cameras_during_a_safety_event", "never_disable_cameras_during_a_security_event", "keep_security_devices_enabled_while_sleeping", "keep_security_monitoring_enabled_when_home_is_empty", "do_not_disable_security_when_user_is_away"}:
            evidence = [a for a in actions if a["device_type"] in {"camera", "sensor"} and a["property"] in {"power", "enabled", "recording", "extra.enabled", "extra.recording"} and a["value"] is False]
            satisfied = not evidence
        elif not explicit and constraint == "do_not_write_read_only_sensors":
            evidence = [a for a in actions if a["device_type"] == "sensor"]
            satisfied = not evidence
        elif not explicit and constraint == "do_not_change_unrelated_rooms_while_cooking":
            evidence = [
                action
                for action in actions
                if relevant_devices and action["device_id"] not in relevant_devices
            ]
            satisfied = not evidence
        elif not explicit and constraint == "no_abrupt_full_brightness_at_wake_up":
            evidence = [
                action
                for action in actions
                if action["room_id"] in primary_rooms
                and action["property"].endswith("brightness")
                and isinstance(action["value"], (int, float))
                and not isinstance(action["value"], bool)
                and action["value"] >= 100
            ]
            satisfied = not evidence
        elif not explicit and constraint == "no_bright_light_in_bedroom_after_sleep_start":
            sleep_seqs = [
                int(_get(event, "seq"))
                for event in collector.events
                if _get(event, "event_type") == "user.starts_activity"
                and _event_data(event, "activity") == "sleeping"
                and isinstance(_get(event, "seq"), int)
            ]
            start_seq = min(sleep_seqs) if sleep_seqs else -1
            evidence = [
                action
                for action in actions
                if isinstance(action["seq"], int)
                and action["seq"] > start_seq
                and action["room_id"] == "bedroom"
                and action["property"].endswith("brightness")
                and isinstance(action["value"], (int, float))
                and not isinstance(action["value"], bool)
                and action["value"] > 30
            ]
            satisfied = not evidence
        elif not explicit and constraint == "prefer_low_energy_options_before_hvac":
            hvac = [index for index, action in enumerate(actions) if action["device_type"] == "hvac"]
            low_energy = [
                index
                for index, action in enumerate(actions)
                if action["device_type"] in {"fan", "curtain"}
            ]
            satisfied = not hvac or bool(low_energy and min(low_energy) < min(hvac))
            evidence = [
                {
                    "first_hvac_action_index": min(hvac) if hvac else None,
                    "first_low_energy_action_index": min(low_energy) if low_energy else None,
                }
            ]
        elif not explicit and constraint == "never_leave_a_shared_room_without_any_usable_light":
            usable = []
            for device_id, state in states.items():
                if collector.device_types.get(device_id) != "light":
                    continue
                if collector.device_rooms.get(device_id) not in primary_rooms:
                    continue
                brightness = _read_path(state, "extra.brightness")
                if state.get("power") is True and (
                    brightness is None
                    or (isinstance(brightness, (int, float)) and brightness > 0)
                ):
                    usable.append(device_id)
            satisfied = bool(usable)
            evidence = usable or ["no_usable_light_in_primary_room"]
        elif not explicit and constraint == "evacuation_lighting_must_not_be_dimmed_by_comfort_or_energy_rules":
            safety_seqs = [
                int(_get(event, "seq"))
                for event in collector.events
                if str(_get(event, "event_type", "")).startswith("safety.")
                and isinstance(_get(event, "seq"), int)
            ]
            start_seq = min(safety_seqs) if safety_seqs else -1
            evidence = [
                action
                for action in actions
                if isinstance(action["seq"], int)
                and action["seq"] > start_seq
                and action["room_id"] in primary_rooms
                and action["property"].endswith("brightness")
                and action["agent_id"] != "security_agent"
                and isinstance(action["value"], (int, float))
                and not isinstance(action["value"], bool)
                and action["value"] < 100
            ]
            satisfied = not evidence
        elif not explicit and constraint == "do_not_unlock_or_open_entries_on_presence_alone":
            evidence = [
                action
                for action in actions
                if ("door" in action["device_id"] or "lock" in action["device_id"])
                and (
                    (action["property"].endswith("locked") and action["value"] is False)
                    or (action["property"].endswith("open") and action["value"] is True)
                )
            ]
            satisfied = not evidence
        elif not explicit:
            # Free-text constraints without a registered predicate are not silently passed.
            evaluable = False
            satisfied = False
            evidence = ["no_registered_evaluator"]
        results.append(
            {
                "constraint": constraint,
                "evaluable": evaluable,
                "satisfied": satisfied,
                "evidence": evidence,
            }
        )
    return all(result["satisfied"] for result in results), results


def _declared_string_list(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    normalized = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return sorted(set(normalized)), len(normalized) == len(value)


def _evaluate_ground_truth_wire(
    collector: MetricsCollector,
    actions: list[dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    """Evaluate machine-readable GroundTruth claims only from persisted wire evidence."""

    ground_truth = collector.ground_truth or {}

    raw_forbidden = ground_truth.get("forbidden_device_ids", [])
    forbidden_ids, forbidden_declaration_valid = _declared_string_list(raw_forbidden)
    malformed_action_ids = [
        str(action.get("event_id", ""))
        for action in actions
        if not action.get("device_id")
    ]
    violations = [
        {
            "event_id": action.get("event_id"),
            "device_id": action.get("device_id"),
        }
        for action in actions
        if action.get("device_id") in forbidden_ids
    ]
    forbidden_declared = bool(raw_forbidden)
    forbidden_evaluable = (
        True
        if not forbidden_declared and forbidden_declaration_valid
        else forbidden_declaration_valid and not malformed_action_ids
    )
    forbidden_satisfied = forbidden_evaluable and not violations

    raw_expected_intent = ground_truth.get("expected_intent")
    intent_declared = raw_expected_intent is not None
    expected_intent = (
        raw_expected_intent.strip()
        if isinstance(raw_expected_intent, str) and raw_expected_intent.strip()
        else None
    )
    intent_events = collector.events_of_type("reasoning.intent_recognized")
    intent_evidence: list[dict[str, Any]] = []
    malformed_intent_event_ids: list[str] = []
    observed_intents: set[str] = set()
    for event in intent_events:
        value = _event_data(event, "normalized_intent")
        normalized = value.strip() if isinstance(value, str) and value.strip() else None
        intent_evidence.append(
            {
                "event_id": _get(event, "event_id"),
                "normalized_intent": normalized,
            }
        )
        if normalized is None:
            malformed_intent_event_ids.append(str(_get(event, "event_id", "")))
        else:
            observed_intents.add(normalized)
    intent_evaluable = (
        True
        if not intent_declared
        else bool(expected_intent)
        and bool(intent_events)
        and not malformed_intent_event_ids
    )
    intent_satisfied = (
        True
        if not intent_declared
        else intent_evaluable and expected_intent in observed_intents
    )

    raw_required_roles = ground_truth.get("required_agent_roles", [])
    required_roles, role_declaration_valid = _declared_string_list(raw_required_roles)
    role_events = collector.events_of_type("reasoning.task_decomposition")
    observed_roles: set[str] = set()
    malformed_role_event_ids: list[str] = []
    role_evidence: list[dict[str, Any]] = []
    for event in role_events:
        raw_roles = _event_data(event, "agent_roles")
        roles, roles_valid = _declared_string_list(raw_roles)
        role_evidence.append(
            {"event_id": _get(event, "event_id"), "agent_roles": roles}
        )
        if not roles_valid:
            malformed_role_event_ids.append(str(_get(event, "event_id", "")))
        observed_roles.update(roles)
    missing_roles = sorted(set(required_roles) - observed_roles)
    roles_declared = bool(raw_required_roles)
    roles_evaluable = (
        True
        if not roles_declared and role_declaration_valid
        else role_declaration_valid
        and bool(role_events)
        and not malformed_role_event_ids
    )
    roles_satisfied = (
        True if not roles_declared else roles_evaluable and not missing_roles
    )

    checks = {
        "forbidden_devices": {
            "declared": forbidden_ids,
            "evaluable": forbidden_evaluable,
            "satisfied": forbidden_satisfied,
            "violating_actions": violations,
            "malformed_action_event_ids": malformed_action_ids,
        },
        "expected_intent": {
            "expected": expected_intent,
            "evaluable": intent_evaluable,
            "satisfied": intent_satisfied,
            "observed": sorted(observed_intents),
            "evidence": intent_evidence,
            "malformed_event_ids": malformed_intent_event_ids,
        },
        "required_agent_roles": {
            "required": required_roles,
            "evaluable": roles_evaluable,
            "satisfied": roles_satisfied,
            "observed": sorted(observed_roles),
            "missing": missing_roles,
            "evidence": role_evidence,
            "malformed_event_ids": malformed_role_event_ids,
        },
    }
    return all(check["satisfied"] for check in checks.values()), checks


def compute_user_intent_satisfied(
    collector: MetricsCollector,
    *,
    device_state_match_rate: MetricDatum | None = None,
) -> MetricDatum:
    ground_truth = collector.ground_truth
    if ground_truth is None:
        return MetricDatum(
            "user_intent_satisfied",
            False,
            "boolean",
            {"evaluable": False, "reason": "ground_truth_missing"},
        )
    match_metric = device_state_match_rate or compute_device_state_match_rate(collector)
    effects_declared = bool(collector.expected_device_effects)
    acceptable_noop = bool(ground_truth.get("acceptable_noop", False))
    actions = _actions(collector)
    expected_devices = {str(effect.get("device_id", "")) for effect in collector.expected_device_effects}
    relevant_actions = [action for action in actions if action["device_id"] in expected_devices]
    if effects_declared:
        effects_ok = match_metric.value == 1.0
        action_ok = acceptable_noop or bool(relevant_actions)
    else:
        effects_ok = acceptable_noop and not actions
        action_ok = effects_ok
    safety_ok, safety_checks = _evaluate_safety(collector)
    ground_truth_ok, ground_truth_checks = _evaluate_ground_truth_wire(
        collector, actions
    )
    return MetricDatum(
        "user_intent_satisfied",
        effects_ok and action_ok and safety_ok and ground_truth_ok,
        "boolean",
        {
            "evaluable": True,
            "expected_effects_declared": effects_declared,
            "expected_effects_satisfied": effects_ok,
            "acceptable_noop": acceptable_noop,
            "relevant_action_count": len(relevant_actions),
            "action_semantics_satisfied": action_ok,
            "safety_constraints_satisfied": safety_ok,
            "safety_checks": safety_checks,
            "ground_truth_wire_satisfied": ground_truth_ok,
            "ground_truth_checks": ground_truth_checks,
        },
    )
