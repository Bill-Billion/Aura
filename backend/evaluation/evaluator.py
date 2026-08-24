"""Scenario-grounded S4 evaluation and persisted-run entry point."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from backend.config.device_registry import get_default_device_registry
from backend.engine.event_log import (
    RunArtifactError,
    read_run_events,
    read_run_metadata,
    verify_finalized_event_log,
)
from backend.engine.run_manager import SPEC11_REQUIRED_FIELDS, read_source_revision
from backend.evaluation.metrics import (
    MetricDatum,
    MetricsCollector,
    compute_command_failure_count,
    compute_conflict_count,
    compute_device_state_match_rate,
    compute_episode_complete,
    compute_fallback_count,
    compute_first_action_latency_ms,
    compute_user_intent_satisfied,
)
from backend.models.versioning import (
    SCHEMA_VERSIONS,
    SUPPORTED_REPORT_SCHEMA_VERSION,
    check_scenario_schema_compatibility,
    check_schema_compatibility,
)
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import get_scenario
from backend.scenarios.spec import ScenarioSpec

REPORT_SCHEMA_VERSION = SUPPORTED_REPORT_SCHEMA_VERSION
CANONICAL_METRIC_NAMES: tuple[str, ...] = (
    "episode_complete",
    "first_action_latency_ms",
    "command_failure_count",
    "fallback_count",
    "conflict_count",
    "user_intent_satisfied",
    "device_state_match_rate",
)


class EvalOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"


@dataclass(frozen=True)
class EvalMetrics:
    episode_complete: MetricDatum
    first_action_latency_ms: MetricDatum
    command_failure_count: MetricDatum
    fallback_count: MetricDatum
    conflict_count: MetricDatum
    user_intent_satisfied: MetricDatum
    device_state_match_rate: MetricDatum

    def to_dict(self) -> dict[str, Any]:
        def datum(value: MetricDatum) -> dict[str, Any]:
            return {
                "name": value.name,
                "value": value.value,
                "unit": value.unit,
                "details": value.details,
            }

        return {
            "episode_complete": datum(self.episode_complete),
            "first_action_latency_ms": datum(self.first_action_latency_ms),
            "command_failure_count": datum(self.command_failure_count),
            "fallback_count": datum(self.fallback_count),
            "conflict_count": datum(self.conflict_count),
            "user_intent_satisfied": datum(self.user_intent_satisfied),
            "device_state_match_rate": datum(self.device_state_match_rate),
        }


@dataclass
class EvalReport:
    run_id: str
    scenario_id: str | None
    seed: int | None
    outcome: EvalOutcome
    metrics: EvalMetrics
    report_schema_version: str = REPORT_SCHEMA_VERSION
    criteria_checks: dict[str, bool] = field(default_factory=dict)
    failed_metrics: list[str] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_schema_version": self.report_schema_version,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "outcome": self.outcome.value,
            "metrics": self.metrics.to_dict(),
            "criteria_checks": self.criteria_checks,
            "failed_metrics": list(self.failed_metrics),
            "failure_reasons": list(self.failure_reasons),
            "provenance": dict(self.provenance),
            "metadata": dict(self.metadata),
        }


def _empty_metrics() -> EvalMetrics:
    return EvalMetrics(
        episode_complete=MetricDatum("episode_complete", False, "boolean"),
        first_action_latency_ms=MetricDatum("first_action_latency_ms", None, "ms"),
        command_failure_count=MetricDatum("command_failure_count", None, "count"),
        fallback_count=MetricDatum("fallback_count", None, "count"),
        conflict_count=MetricDatum("conflict_count", None, "count"),
        user_intent_satisfied=MetricDatum("user_intent_satisfied", False, "boolean"),
        device_state_match_rate=MetricDatum("device_state_match_rate", None, "ratio"),
    )


def _error_report(
    run_id: str,
    reason: str,
    *,
    scenario_id: str | None = None,
    seed: int | None = None,
    provenance: dict[str, Any] | None = None,
) -> EvalReport:
    report_provenance = dict(provenance or {})
    report_provenance.setdefault("evaluator_source_revision", read_source_revision())
    return EvalReport(
        run_id=run_id,
        scenario_id=scenario_id,
        seed=seed,
        outcome=EvalOutcome.ERROR,
        metrics=_empty_metrics(),
        failed_metrics=list(CANONICAL_METRIC_NAMES),
        failure_reasons=[reason],
        provenance=report_provenance,
    )


def _validate_artifact_versions(
    metadata: dict[str, Any], events: list[Any] | None = None
) -> None:
    """Reject persisted evidence whose public schema cannot be established."""

    if not isinstance(metadata, dict):
        raise ValueError("run metadata must be a JSON object")
    if metadata.get("artifact_error"):
        raise ValueError(f"run artifact is invalid: {metadata['artifact_error']}")
    required_metadata_fields = (*SPEC11_REQUIRED_FIELDS, "llm_mode", *SCHEMA_VERSIONS)
    for field_name in required_metadata_fields:
        if field_name not in metadata or metadata[field_name] is None:
            raise ValueError(f"run metadata is missing required {field_name}")
    for field_name, supported in SCHEMA_VERSIONS.items():
        if field_name == "scenario_schema_version":
            check_scenario_schema_compatibility(metadata[field_name])
        else:
            check_schema_compatibility(
                metadata[field_name], supported=supported, field=field_name
            )

    if events is None:
        return
    declared_event_version = str(metadata["event_schema_version"])
    declared_command_version = str(metadata["command_schema_version"])
    required_event_fields = (
        "event_schema_version",
        "event_id",
        "event_type",
        "source",
        "timestamp",
        "data",
        "wall_time",
        "correlation_id",
        "causal_parent",
        "priority",
        "run_id",
        "scenario_id",
        "seq",
        "sim_time_s",
        "depth",
    )
    seen_event_ids: set[str] = set()
    seen_seqs: set[int] = set()
    previous_seq = -1
    expected_seq = 0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValueError(
                f"events.jsonl line {index + 1} must be a JSON object"
            )
        for field_name in required_event_fields:
            if field_name not in event:
                raise ValueError(
                    f"events.jsonl line {index + 1} is missing required {field_name}"
                )
        for field_name in ("event_id", "event_type", "source", "correlation_id"):
            if not isinstance(event[field_name], str) or not event[field_name]:
                raise ValueError(
                    f"events.jsonl line {index + 1} {field_name} must be a non-empty string"
                )
        for field_name in ("timestamp", "wall_time", "sim_time_s"):
            value = event[field_name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"events.jsonl line {index + 1} {field_name} must be numeric"
                )
        if not isinstance(event["data"], dict):
            raise ValueError(
                f"events.jsonl line {index + 1} data must be a JSON object"
            )
        if event["causal_parent"] is not None and not isinstance(
            event["causal_parent"], str
        ):
            raise ValueError(
                f"events.jsonl line {index + 1} causal_parent must be null or a string"
            )
        if (
            isinstance(event["seq"], bool)
            or not isinstance(event["seq"], int)
            or event["seq"] < 0
        ):
            raise ValueError(
                f"events.jsonl line {index + 1} seq must be a non-negative integer"
            )
        if (
            isinstance(event["depth"], bool)
            or not isinstance(event["depth"], int)
            or event["depth"] < 0
        ):
            raise ValueError(
                f"events.jsonl line {index + 1} depth must be a non-negative integer"
            )
        if event["event_id"] in seen_event_ids:
            raise ValueError(
                f"events.jsonl line {index + 1} duplicates event_id {event['event_id']!r}"
            )
        seen_event_ids.add(event["event_id"])
        if event["seq"] in seen_seqs or event["seq"] <= previous_seq:
            raise ValueError(
                f"events.jsonl line {index + 1} seq {event['seq']} is duplicate or not increasing"
            )
        seen_seqs.add(event["seq"])
        previous_seq = event["seq"]
        if event["seq"] != expected_seq:
            raise ValueError(
                f"events.jsonl line {index + 1} seq {event['seq']} leaves a gap; "
                f"expected {expected_seq}"
            )
        expected_seq += 1
        if event["run_id"] != metadata["run_id"]:
            raise ValueError(
                f"events.jsonl line {index + 1} run_id {event['run_id']!r} "
                f"disagrees with run metadata {metadata['run_id']!r}"
            )
        if event["scenario_id"] != metadata["scenario_id"]:
            raise ValueError(
                f"events.jsonl line {index + 1} scenario_id {event['scenario_id']!r} "
                f"disagrees with run metadata {metadata['scenario_id']!r}"
            )
        event_version = event.get("event_schema_version")
        if event_version is None:
            raise ValueError(
                f"events.jsonl line {index + 1} is missing required event_schema_version"
            )
        check_schema_compatibility(
            event_version,
            supported=SCHEMA_VERSIONS["event_schema_version"],
            field="event_schema_version",
        )
        if str(event_version) != declared_event_version:
            raise ValueError(
                f"events.jsonl line {index + 1} event_schema_version "
                f"{event_version!r} disagrees with run metadata "
                f"{metadata['event_schema_version']!r}"
            )
        if event.get("event_type") != "command.lifecycle":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            raise ValueError(
                f"events.jsonl line {index + 1} command.lifecycle data must be a JSON object"
            )
        command_version = data.get("command_schema_version")
        if command_version is None:
            raise ValueError(
                f"events.jsonl line {index + 1} command.lifecycle is missing "
                "required command_schema_version"
            )
        check_schema_compatibility(
            command_version,
            supported=SCHEMA_VERSIONS["command_schema_version"],
            field="command_schema_version",
        )
        if str(command_version) != declared_command_version:
            raise ValueError(
                f"events.jsonl line {index + 1} command_schema_version "
                f"{command_version!r} disagrees with run metadata "
                f"{metadata['command_schema_version']!r}"
            )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _scenario_context(spec: ScenarioSpec) -> dict[str, Any]:
    registry = get_default_device_registry()
    states: dict[str, dict[str, Any]] = {}
    rooms: dict[str, str] = {}
    types: dict[str, str] = {}
    for entry in registry:
        states[entry.id] = entry.to_device_state().state.model_dump(
            mode="json", exclude_none=True
        )
        rooms[entry.id] = entry.room_id
        raw_type = entry.type
        types[entry.id] = str(getattr(raw_type, "value", raw_type))
    for device_id, override in spec.initial_state.devices.items():
        states[device_id] = _deep_merge(
            states.get(device_id, {}),
            override.state.model_dump(mode="json", exclude_none=True),
        )
    return {
        "success_criteria": spec.success_criteria.model_dump(mode="json"),
        "expected_failures": [
            failure.model_dump(mode="json") for failure in spec.expected_failures
        ],
        "expected_device_effects": [
            effect.model_dump(mode="json") for effect in spec.expected_device_effects
        ],
        "initial_device_states": states,
        "ground_truth": (
            spec.ground_truth.model_dump(mode="json") if spec.ground_truth is not None else None
        ),
        "device_rooms": rooms,
        "device_types": types,
    }


class ScenarioEvaluator:
    """Compute the seven canonical metrics against one ScenarioSpec contract."""

    def __init__(self, scenario: ScenarioSpec) -> None:
        self._scenario = scenario
        self._criteria = scenario.success_criteria.model_dump(mode="json")

    @classmethod
    def from_scenario(cls, scenario: ScenarioSpec) -> "ScenarioEvaluator":
        return cls(scenario)

    def evaluate(
        self,
        events: list[Any],
        *,
        run_id: str = "",
        scenario_id: str | None = None,
        seed: int | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> EvalReport:
        scenario = self._scenario
        unknown_metrics = sorted(set(scenario.metrics) - set(CANONICAL_METRIC_NAMES))
        if unknown_metrics:
            return _error_report(
                run_id,
                "ScenarioSpec declares unsupported required metric(s): "
                + ", ".join(unknown_metrics),
                scenario_id=scenario_id or scenario.id,
                seed=seed,
                provenance=self._provenance(
                    run_id=run_id,
                    scenario=scenario,
                    scenario_id=scenario_id or scenario.id,
                    seed=seed,
                    run_metadata=run_metadata or {},
                    events=events,
                ),
            )
        context = _scenario_context(scenario)
        scenario_id = scenario_id or scenario.id
        criteria = self._criteria

        collector = MetricsCollector(
            events=events,
            scenario_id=scenario_id,
            seed=seed,
            run_id=run_id,
            expected_failures=context["expected_failures"],
            expected_device_effects=context["expected_device_effects"],
            initial_device_states=context["initial_device_states"],
            ground_truth=context["ground_truth"],
            device_rooms=context["device_rooms"],
            device_types=context["device_types"],
            success_criteria=criteria,
        )
        device_match = compute_device_state_match_rate(collector)
        metrics = EvalMetrics(
            episode_complete=compute_episode_complete(collector),
            first_action_latency_ms=compute_first_action_latency_ms(collector),
            command_failure_count=compute_command_failure_count(collector),
            fallback_count=compute_fallback_count(collector),
            conflict_count=compute_conflict_count(collector),
            user_intent_satisfied=compute_user_intent_satisfied(
                collector, device_state_match_rate=device_match
            ),
            device_state_match_rate=device_match,
        )
        checks, failed_metrics, reasons = self._check_criteria(
            metrics,
            criteria,
            has_expected_effects=bool(context["expected_device_effects"]),
            acceptable_noop=bool((context["ground_truth"] or {}).get("acceptable_noop", False)),
            has_expected_failures=bool(context["expected_failures"]),
            required_metrics=list(scenario.metrics),
        )
        provenance = self._provenance(
            run_id=run_id,
            scenario=scenario,
            scenario_id=scenario_id,
            seed=seed,
            run_metadata=run_metadata or {},
            events=events,
        )
        return EvalReport(
            run_id=run_id,
            scenario_id=scenario_id,
            seed=seed,
            outcome=EvalOutcome.FAIL if failed_metrics else EvalOutcome.PASS,
            metrics=metrics,
            criteria_checks=checks,
            failed_metrics=failed_metrics,
            failure_reasons=reasons,
            provenance=provenance,
            metadata={
                "total_events": len(events),
                "total_episodes": len(collector.agent_episode_ids),
                "total_commands": len(collector.final_command_events),
            },
        )

    @staticmethod
    def _check_criteria(
        metrics: EvalMetrics,
        criteria: dict[str, Any],
        *,
        has_expected_effects: bool,
        acceptable_noop: bool,
        has_expected_failures: bool,
        required_metrics: list[str],
    ) -> tuple[dict[str, bool], list[str], list[str]]:
        checks: dict[str, bool] = {}
        failed: list[str] = []
        reasons: list[str] = []

        def record(metric: str, check: str, ok: bool, reason: str) -> None:
            checks[check] = ok
            if not ok:
                if metric not in failed:
                    failed.append(metric)
                reasons.append(reason)

        metric_values = metrics.to_dict()
        for metric_name in required_metrics:
            available = metric_values[metric_name]["value"] is not None
            record(
                metric_name,
                f"required_metric:{metric_name}",
                available,
                f"required metric {metric_name} is not evaluable from this run",
            )

        if criteria.get("require_complete_episode", True):
            record(
                "episode_complete",
                "require_complete_episode",
                metrics.episode_complete.value is True,
                "episode_complete: one or more agent episodes lack root/reasoning/approved action/feedback evidence",
            )

        maximum = criteria.get("max_first_action_latency_ms")
        if maximum is not None:
            value = metrics.first_action_latency_ms.value
            details = metrics.first_action_latency_ms.details
            episode_count = details.get("episode_count", 0)
            complete_samples = details.get("sample_count") == episode_count and episode_count > 0
            max_latency = details.get("max_latency_ms")
            if acceptable_noop:
                ok = max_latency is None or (
                    isinstance(max_latency, (int, float))
                    and not isinstance(max_latency, bool)
                    and max_latency <= maximum
                )
            else:
                ok = (
                    isinstance(max_latency, (int, float))
                    and not isinstance(max_latency, bool)
                    and complete_samples
                    and max_latency <= maximum
                )
            record(
                "first_action_latency_ms",
                "max_first_action_latency_ms",
                ok,
                f"first_action_latency_ms: mean={value!r}, max={max_latency!r}, required max <= {maximum} with one sample per non-noop episode",
            )

        max_failures = criteria.get("max_command_failures")
        if max_failures is not None:
            value = metrics.command_failure_count.value
            ok = isinstance(value, int) and not isinstance(value, bool) and value <= max_failures
            record(
                "command_failure_count",
                "max_command_failures",
                ok,
                f"command_failure_count: value={value!r}, allowed <= {max_failures}",
            )
        unterminated = metrics.command_failure_count.details.get(
            "unterminated_command_ids", []
        )
        record(
            "command_failure_count",
            "command_ledger_complete",
            not unterminated,
            f"command_failure_count: {len(unterminated)} command(s) have no terminal lifecycle state",
        )
        if has_expected_failures:
            unobserved = metrics.command_failure_count.details.get(
                "unobserved_expected_failures", []
            )
            record(
                "command_failure_count",
                "expected_failures_observed",
                not unobserved,
                f"command_failure_count: {len(unobserved)} declared expected failure(s) were not observed",
            )

        if not criteria.get("allow_fallback", True):
            record(
                "fallback_count",
                "allow_fallback",
                metrics.fallback_count.value == 0,
                f"fallback_count: fallback forbidden but observed {metrics.fallback_count.value!r}",
            )

        minimum_conflicts = criteria.get("min_conflict_count")
        if minimum_conflicts is not None:
            value = metrics.conflict_count.value
            minimum_valid = isinstance(minimum_conflicts, int) and not isinstance(
                minimum_conflicts, bool
            )
            ok = (
                minimum_valid
                and minimum_conflicts >= 0
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value >= minimum_conflicts
            )
            record(
                "conflict_count",
                "min_conflict_count",
                ok,
                f"conflict_count: value={value!r}, required >= {minimum_conflicts}",
            )

        record(
            "user_intent_satisfied",
            "user_intent_satisfied",
            metrics.user_intent_satisfied.value is True,
            "user_intent_satisfied: expected effects, action/noop policy, ground-truth wire constraints, or safety constraints were not satisfied",
        )
        if has_expected_effects:
            record(
                "device_state_match_rate",
                "expected_device_effects",
                metrics.device_state_match_rate.value == 1.0,
                f"device_state_match_rate: value={metrics.device_state_match_rate.value!r}, required 1.0",
            )
        return checks, failed, reasons

    @staticmethod
    def _provenance(
        *,
        run_id: str,
        scenario: ScenarioSpec | None,
        scenario_id: str | None,
        seed: int | None,
        run_metadata: dict[str, Any],
        events: list[Any],
    ) -> dict[str, Any]:
        event_versions = sorted(
            {
                str(event.get("event_schema_version"))
                for event in events
                if isinstance(event, dict) and event.get("event_schema_version")
            }
        )
        command_versions = sorted(
            {
                str(event.get("data", {}).get("command_schema_version"))
                for event in events
                if isinstance(event, dict)
                and isinstance(event.get("data"), dict)
                and event["data"].get("command_schema_version")
            }
        )
        return {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "run_id": run_id,
            "scenario_id": scenario_id,
            "seed": seed,
            "scenario_schema_version": (
                scenario.scenario_schema_version if scenario is not None else None
            ),
            "run_scenario_schema_version": run_metadata.get("scenario_schema_version"),
            "scenario_contract_hash": run_metadata.get("scenario_contract_hash"),
            "event_schema_version": run_metadata.get("event_schema_version"),
            "command_schema_version": run_metadata.get("command_schema_version"),
            "event_schema_versions": event_versions,
            "command_schema_versions": command_versions,
            "device_registry_version": run_metadata.get("device_registry_version"),
            "sim_version": run_metadata.get("sim_version"),
            "source_revision": run_metadata.get("source_revision"),
            "evaluator_source_revision": read_source_revision(),
            "agent_versions": run_metadata.get("agent_versions", {}),
            "llm_provider": run_metadata.get("llm_provider"),
            "llm_model": run_metadata.get("llm_model"),
            "llm_mode": run_metadata.get("llm_mode"),
            "baseline_policy": run_metadata.get("baseline_policy"),
            "recording_source_run_id": run_metadata.get("recording_source_run_id"),
            "initial_state_hash": run_metadata.get("initial_state_hash"),
            "required_metrics": list(scenario.metrics) if scenario is not None else [],
        }


def evaluate_run(
    run_id: str,
    *,
    scenario_id: str | None = None,
    seed: int | None = None,
    data_root: Path | str | None = None,
    scenario_dirs: Iterable[Path | str] | None = None,
) -> EvalReport:
    """Evaluate a persisted run; metadata is the authority for its ScenarioSpec."""

    try:
        metadata = read_run_metadata(run_id, root=data_root)
    except Exception as exc:
        return _error_report(run_id, f"cannot read metadata for run {run_id}: {exc}")
    if not isinstance(metadata, dict):
        return _error_report(
            run_id, "run artifact schema is unsupported: run metadata must be a JSON object"
        )
    metadata_scenario_id = metadata.get("scenario_id")
    metadata_seed = metadata.get("seed")
    base_provenance = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "run_id": run_id,
        "scenario_id": metadata_scenario_id,
        "seed": metadata_seed,
        "scenario_contract_hash": metadata.get("scenario_contract_hash"),
        "source_revision": metadata.get("source_revision"),
        "evaluator_source_revision": read_source_revision(),
    }
    try:
        _validate_artifact_versions(metadata)
    except (TypeError, ValueError) as exc:
        return _error_report(
            run_id,
            f"run artifact schema is unsupported: {exc}",
            scenario_id=(str(metadata_scenario_id) if metadata_scenario_id else None),
            seed=metadata_seed if isinstance(metadata_seed, int) else None,
            provenance=base_provenance,
        )
    if metadata.get("run_id") != run_id:
        return _error_report(
            run_id,
            f"run metadata run_id {metadata.get('run_id')!r} disagrees with requested {run_id!r}",
            scenario_id=(str(metadata_scenario_id) if metadata_scenario_id else None),
            seed=metadata_seed if isinstance(metadata_seed, int) else None,
            provenance=base_provenance,
        )
    if metadata.get("ended_at") is None:
        return _error_report(
            run_id,
            f"run {run_id} is not finalized: ended_at is null",
            scenario_id=(str(metadata_scenario_id) if metadata_scenario_id else None),
            seed=metadata_seed if isinstance(metadata_seed, int) else None,
            provenance=base_provenance,
        )
    if metadata.get("end_reason") != "completed":
        return _error_report(
            run_id,
            f"run {run_id} did not complete successfully: end_reason={metadata.get('end_reason')!r}",
            scenario_id=(str(metadata_scenario_id) if metadata_scenario_id else None),
            seed=metadata_seed if isinstance(metadata_seed, int) else None,
            provenance=base_provenance,
        )
    if not metadata_scenario_id:
        return _error_report(
            run_id,
            f"run {run_id} metadata has no scenario_id; ScenarioSpec cannot be resolved",
            seed=metadata_seed,
            provenance=base_provenance,
        )
    if not isinstance(metadata_seed, int) or isinstance(metadata_seed, bool):
        return _error_report(
            run_id,
            f"run {run_id} metadata has invalid seed {metadata_seed!r}",
            scenario_id=str(metadata_scenario_id),
            provenance=base_provenance,
        )
    if scenario_id is not None and scenario_id != metadata_scenario_id:
        return _error_report(
            run_id,
            f"scenario override {scenario_id!r} disagrees with run metadata {metadata_scenario_id!r}",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    if seed is not None and seed != metadata_seed:
        return _error_report(
            run_id,
            f"seed override {seed!r} disagrees with run metadata {metadata_seed!r}",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    try:
        scenario = get_scenario(str(metadata_scenario_id), dirs=scenario_dirs)
    except Exception as exc:
        return _error_report(
            run_id,
            f"cannot load ScenarioSpec {metadata_scenario_id!r}: {exc}",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    if scenario is None:
        return _error_report(
            run_id,
            f"ScenarioSpec {metadata_scenario_id!r} referenced by run metadata was not found",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    declared_scenario_version = metadata.get("scenario_schema_version")
    if (
        declared_scenario_version is not None
        and str(declared_scenario_version) != scenario.scenario_schema_version
    ):
        return _error_report(
            run_id,
            "ScenarioSpec scenario_schema_version drift: "
            f"run recorded {declared_scenario_version!r}, current library has "
            f"{scenario.scenario_schema_version!r}",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    recorded_contract_hash = metadata.get("scenario_contract_hash")
    if not isinstance(recorded_contract_hash, str) or len(recorded_contract_hash) != 64:
        return _error_report(
            run_id,
            "run artifact has no valid scenario_contract_hash; the historical "
            "ScenarioSpec/evaluation contract cannot be proven",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    current_contract_hash = scenario_contract_fingerprint(scenario)
    if recorded_contract_hash != current_contract_hash:
        return _error_report(
            run_id,
            "ScenarioSpec evaluation contract drift: "
            f"run recorded {recorded_contract_hash!r}, current library resolves "
            f"{current_contract_hash!r}",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    try:
        events, _ = read_run_events(
            run_id, root=data_root, verify_integrity=False
        )
    except Exception as exc:
        return _error_report(
            run_id,
            f"cannot read events for run {run_id}: {exc}",
            scenario_id=metadata_scenario_id,
            seed=metadata_seed,
            provenance=base_provenance,
        )
    try:
        _validate_artifact_versions(metadata, list(events))
    except (TypeError, ValueError) as exc:
        return _error_report(
            run_id,
            f"run artifact schema is unsupported: {exc}",
            scenario_id=str(metadata_scenario_id),
            seed=int(metadata_seed),
            provenance=base_provenance,
        )
    try:
        verify_finalized_event_log(run_id, metadata=metadata, root=data_root)
    except RunArtifactError as exc:
        return _error_report(
            run_id,
            "run event log integrity check failed "
            f"[{exc.code.value}]: {exc.message}",
            scenario_id=str(metadata_scenario_id),
            seed=int(metadata_seed),
            provenance=base_provenance,
        )
    evaluator = ScenarioEvaluator.from_scenario(scenario)
    report = evaluator.evaluate(
        list(events),
        run_id=run_id,
        scenario_id=str(metadata_scenario_id),
        seed=int(metadata_seed),
        run_metadata=metadata,
    )
    return report
