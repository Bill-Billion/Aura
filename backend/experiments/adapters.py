"""Fail-closed adapters for the implemented research runtime profiles.

Unsupported scientific conditions are rejected rather than approximated.  In
particular, every accepted topology/governance combination names one concrete
profile; independently mixing otherwise known axis values is not accepted.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from backend.engine.event_log import (
    read_run_metadata,
    read_run_events,
    verify_finalized_event_log,
)
from backend.engine.provenance import (
    RESEARCH_RUNTIME_PROFILES,
    SUPPORTED_OBSERVATION_CONDITIONS,
    ExperimentProvenance,
    ExperimentRuntimeSelection,
    ObservationCondition,
    ResearchRuntimeProfile,
    research_runtime_profile_for_axes,
)
from backend.engine.observation import (
    OBSERVATION_STALE_KEY,
    OBSERVATION_UNAVAILABLE_KEY,
    device_reports_online,
    observation_model_metadata,
)
from backend.engine.run_manager import canonical_json, read_source_revision
from backend.engine.state import WorldState
from backend.evaluation.evaluator import EvalOutcome, evaluate_run
from backend.models.schemas import BaselinePolicy
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import get_scenario, load_scenario_file
from backend.scenarios.runner import ScenarioRunner
from backend.scenarios.spec import ScenarioSpec

from .fairness import build_fairness_payload
from .spec import ExperimentCell
from .runner import CellExecutionResult

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

SUPPORTED_MODELS = frozenset({"rule_based", "mocked"})
SUPPORTED_RUNTIME_PROFILES = frozenset(RESEARCH_RUNTIME_PROFILES)
SUPPORTED_TOPOLOGIES = frozenset(
    axes[0] for axes in RESEARCH_RUNTIME_PROFILES.values()
)
SUPPORTED_GOVERNANCE = frozenset(
    axes[1] for axes in RESEARCH_RUNTIME_PROFILES.values()
)
SUPPORTED_OBSERVATIONS = frozenset(
    condition.value for condition in SUPPORTED_OBSERVATION_CONDITIONS
)

_BASELINE_BY_MODEL = {
    "rule_based": BaselinePolicy.RULE_BASED,
    "mocked": BaselinePolicy.LLM_MOCKED,
}

# Mirrors ``backend.agents.runtime.DEFAULT_AGENT_FACTORIES`` registration order.
# ``active_agent_ids`` is persisted by the runtime precisely so completed
# evidence can prove the active topology without inferring it from whichever
# agents happened to propose in one episode.
_ACTIVE_AGENT_IDS_BY_TOPOLOGY: dict[str, tuple[str, ...]] = {
    "single": ("single_direct_agent",),
    "domain_multi": (
        "lighting_agent",
        "hvac_agent",
        "security_agent",
        "energy_agent",
        "scene_agent",
    ),
}

_CONTENT_ADDRESSED_EVIDENCE: tuple[tuple[str, str, type], ...] = (
    ("observable_snapshot", "observable_snapshot_hash", Mapping),
    ("proposal_set", "proposal_set_hash", list),
    ("approved_command_set", "approved_command_set_hash", list),
    ("rejected_command_set", "rejected_command_set_hash", list),
)


class AdapterUnavailableError(RuntimeError):
    pass


class AuraCellExecutor:
    """Execute one implemented research profile through the shared Aura runtime."""

    def __init__(
        self,
        *,
        scenario_base_dir: Path | str | None = None,
        scenario_dirs: Sequence[Path | str] | None = None,
        data_root: Path | str | None = None,
        enforce_source_revision: bool = True,
    ) -> None:
        self.scenario_base_dir = (
            Path(scenario_base_dir)
            if scenario_base_dir is not None
            else _REPOSITORY_ROOT
        )
        self.scenario_dirs = tuple(Path(path) for path in (scenario_dirs or ()))
        self.data_root = Path(data_root) if data_root is not None else None
        self.enforce_source_revision = enforce_source_revision

    @staticmethod
    def _validate_adapters(cell: ExperimentCell) -> ResearchRuntimeProfile:
        if cell.model not in SUPPORTED_MODELS:
            raise AdapterUnavailableError(
                f"model adapter {cell.model!r} is not implemented; "
                f"implemented values: {', '.join(sorted(SUPPORTED_MODELS))}"
            )
        try:
            return research_runtime_profile_for_axes(
                topology=cell.topology,
                governance=cell.governance,
                observation=cell.observation,
            )
        except ValueError as exc:
            raise AdapterUnavailableError(
                f"runtime profile is not implemented: {exc}"
            ) from exc

    def _load_scenario(self, cell: ExperimentCell) -> tuple[ScenarioSpec, tuple[Path, ...]]:
        reference_path = Path(cell.scenario_reference)
        if not reference_path.is_absolute():
            reference_path = self.scenario_base_dir / reference_path
        if reference_path.is_file():
            spec = load_scenario_file(reference_path)
            evaluation_dirs = tuple(
                dict.fromkeys((reference_path.parent, *self.scenario_dirs))
            )
        else:
            spec = get_scenario(
                cell.scenario_reference,
                dirs=self.scenario_dirs if self.scenario_dirs else None,
            )
            if spec is None:
                raise AdapterUnavailableError(
                    f"scenario {cell.scenario_reference!r} cannot be resolved"
                )
            evaluation_dirs = self.scenario_dirs
        if spec.id != cell.scenario_id:
            raise AdapterUnavailableError(
                f"scenario id drift: cell records {cell.scenario_id!r}, "
                f"resolved {spec.id!r}"
            )
        current_hash = scenario_contract_fingerprint(spec)
        if current_hash != cell.scenario_contract_hash:
            raise AdapterUnavailableError(
                "scenario contract drift: resolved content no longer matches the cell"
            )
        return spec, evaluation_dirs

    @staticmethod
    def _experiment(
        cell: ExperimentCell,
        *,
        matrix_hash: str,
        runtime_profile: ResearchRuntimeProfile,
    ) -> ExperimentProvenance:
        return ExperimentProvenance(
            experiment_id=cell.experiment_id,
            matrix_spec_hash=cell.matrix_spec_hash,
            matrix_hash=matrix_hash,
            cell_id=cell.cell_id,
            runtime_profile=runtime_profile,
            model=cell.model,
            topology=cell.topology,
            governance=cell.governance,
            observation=cell.observation,
            repetition=cell.repetition,
        )

    @staticmethod
    def _runtime_selection(
        cell: ExperimentCell,
        runtime_profile: ResearchRuntimeProfile,
    ) -> ExperimentRuntimeSelection:
        return ExperimentRuntimeSelection.for_profile(
            runtime_profile,
            model=cast(Literal["rule_based", "mocked"], cell.model),
            baseline_policy=_BASELINE_BY_MODEL[cell.model],
            observation=cast(Literal["perfect", "stale_offline"], cell.observation),
        )

    @staticmethod
    def _observation_snapshot_matches(
        condition: ObservationCondition,
        frame: Mapping[str, Any],
    ) -> bool:
        raw_snapshot = frame.get("observable_snapshot")
        if not isinstance(raw_snapshot, Mapping):
            return False
        try:
            world = WorldState.model_validate(dict(raw_snapshot))
        except (TypeError, ValueError):
            return False
        if world.model_dump(mode="json", exclude={"agents"}) != raw_snapshot:
            return False

        stale = frame.get("stale_device_ids")
        unavailable = frame.get("unavailable_device_ids")
        if not isinstance(stale, list) or not isinstance(unavailable, list):
            return False
        stale_ids = set(stale)
        unavailable_ids = set(unavailable)
        if not unavailable_ids.issubset(stale_ids):
            return False
        if not stale_ids.issubset(world.devices):
            return False

        for device_id, device in world.devices.items():
            marked_stale = device.state.extra.get(OBSERVATION_STALE_KEY) is True
            marked_unavailable = (
                device.state.extra.get(OBSERVATION_UNAVAILABLE_KEY) is True
            )
            if condition is ObservationCondition.PERFECT:
                if marked_stale or marked_unavailable:
                    return False
                continue
            offline = not device_reports_online(device)
            if marked_stale != offline or (device_id in stale_ids) != offline:
                return False
            if marked_unavailable != (device_id in unavailable_ids):
                return False
        return condition is not ObservationCondition.PERFECT or not (
            stale_ids or unavailable_ids
        )

    @staticmethod
    def _observation_evidence_matches(
        cell: ExperimentCell,
        decision: Mapping[str, Any],
        events_by_id: Mapping[str, Mapping[str, Any]],
    ) -> bool:
        data = decision.get("data")
        if not isinstance(data, Mapping):
            return False
        try:
            condition = ObservationCondition(cell.observation)
        except ValueError:
            return False
        expected = observation_model_metadata(condition)
        if (
            data.get("observation_condition") != condition.value
            or data.get("observation_model_id") != expected["model_id"]
            or data.get("observation_contract_version")
            != expected["contract_version"]
            or data.get("observation_model_hash") != expected["model_hash"]
        ):
            return False

        perception_id = data.get("observation_perception_event_id")
        if not isinstance(perception_id, str):
            return False
        perception = events_by_id.get(perception_id)
        if (
            perception is None
            or perception.get("event_type") != "reasoning.perception_snapshot"
            or perception.get("correlation_id") != decision.get("correlation_id")
        ):
            return False
        perception_data = perception.get("data")
        if not isinstance(perception_data, Mapping):
            return False
        capture_id = data.get("observation_capture_event_id")
        if (
            not isinstance(capture_id, str)
            or perception_data.get("observation_capture_event_id") != capture_id
        ):
            return False
        capture = events_by_id.get(capture_id)
        if (
            capture is None
            or capture.get("event_type") != "observation.frame_captured"
            or capture.get("source") != "observation_projector"
            or capture.get("correlation_id") != decision.get("correlation_id")
        ):
            return False
        capture_data = capture.get("data")
        if not isinstance(capture_data, Mapping):
            return False
        frame = capture_data.get("observation_frame")
        frame_hash = capture_data.get("observation_frame_hash")
        if not isinstance(frame, Mapping) or not isinstance(frame_hash, str):
            return False
        recomputed_frame_hash = hashlib.sha256(
            canonical_json(frame).encode("utf-8")
        ).hexdigest()
        if (
            frame_hash != recomputed_frame_hash
            or data.get("observation_frame_hash") != frame_hash
            or perception_data.get("observation_frame_hash") != frame_hash
            or frame.get("observation_condition") != condition.value
            or frame.get("observation_model_id") != expected["model_id"]
            or frame.get("observation_contract_version")
            != expected["contract_version"]
            or frame.get("observation_model_hash") != expected["model_hash"]
            or frame.get("observable_snapshot_projection")
            != "world_state_without_agent_diagnostics.v1"
            or frame.get("observable_snapshot") != data.get("observable_snapshot")
            or not isinstance(frame.get("observed_root_event"), Mapping)
        ):
            return False
        root_id = capture.get("causal_parent")
        root = events_by_id.get(root_id) if isinstance(root_id, str) else None
        root_seq = root.get("seq") if root is not None else None
        capture_seq = capture.get("seq")
        perception_seq = perception.get("seq")
        decision_seq = decision.get("seq")
        if (
            root is None
            or perception.get("causal_parent") != root_id
            or root.get("correlation_id") != decision.get("correlation_id")
            or not isinstance(root_seq, int)
            or not isinstance(capture_seq, int)
            or not isinstance(perception_seq, int)
            or not isinstance(decision_seq, int)
            or not root_seq < capture_seq < perception_seq < decision_seq
        ):
            return False
        observed_root = frame.get("observed_root_event")
        if not isinstance(observed_root, Mapping) or any(
            observed_root.get(field) != root.get(field)
            for field in (
                "event_type",
                "source",
                "timestamp",
                "sim_time_s",
                "priority",
                "event_generation_mode",
                "generation_rule_id",
                "rng_stream",
            )
        ):
            return False
        captured = frame.get("captured_at_sim_time_s")
        if (
            isinstance(captured, bool)
            or not isinstance(captured, (int, float))
            or not math.isfinite(float(captured))
            or float(captured) < 0
        ):
            return False
        for field in ("stale_device_ids", "unavailable_device_ids"):
            values = frame.get(field)
            if (
                not isinstance(values, list)
                or any(not isinstance(item, str) for item in values)
                or values != sorted(set(values))
            ):
                return False
        if not AuraCellExecutor._observation_snapshot_matches(condition, frame):
            return False
        return True

    @staticmethod
    def _runtime_evidence_matches(
        cell: ExperimentCell,
        runtime_profile: ResearchRuntimeProfile,
        events: Sequence[Mapping[str, Any]],
    ) -> bool:
        expected_source = {
            "none": "proposal_passthrough",
            "flat_priority": "flat_priority",
            "aura": "arbiter",
        }[cell.governance]
        decisions = [
            event
            for event in events
            if event.get("event_type") == "reasoning.coordination_decision"
        ]
        if not decisions:
            return False
        events_by_id = {
            event_id: event
            for event in events
            if isinstance((event_id := event.get("event_id")), str)
        }
        expected_active_agent_ids = _ACTIVE_AGENT_IDS_BY_TOPOLOGY[cell.topology]
        for event in decisions:
            data = event.get("data")
            if not isinstance(data, Mapping):
                return False
            if (
                event.get("source") != expected_source
                or data.get("runtime_profile") != runtime_profile.value
                or data.get("requested_runtime_profile") != runtime_profile.value
                or data.get("effective_runtime_profile") != runtime_profile.value
                or data.get("governance") != cell.governance
                or data.get("observable_snapshot_projection")
                != "world_state_without_agent_diagnostics.v1"
            ):
                return False
            if not AuraCellExecutor._observation_evidence_matches(
                cell,
                event,
                events_by_id,
            ):
                return False
            for preimage_field, hash_field, expected_type in _CONTENT_ADDRESSED_EVIDENCE:
                preimage = data.get(preimage_field)
                recorded_hash = data.get(hash_field)
                if not isinstance(preimage, expected_type) or not isinstance(
                    recorded_hash, str
                ):
                    return False
                recomputed = hashlib.sha256(
                    canonical_json(preimage).encode("utf-8")
                ).hexdigest()
                if recorded_hash != recomputed:
                    return False

            active_agent_ids = data.get("active_agent_ids")
            if (
                not isinstance(active_agent_ids, list)
                or any(not isinstance(agent_id, str) for agent_id in active_agent_ids)
                or tuple(active_agent_ids) != expected_active_agent_ids
            ):
                return False
            for field in (
                "proposal_set",
                "approved_command_set",
                "rejected_command_set",
            ):
                entries = data[field]
                if any(
                    not isinstance(item, Mapping)
                    or item.get("agent_id") not in expected_active_agent_ids
                    for item in entries
                ):
                    return False
            per_agent = data.get("per_agent")
            if not isinstance(per_agent, list) or any(
                not isinstance(item, Mapping)
                or not isinstance(item.get("agent_id"), str)
                for item in per_agent
            ):
                return False
            agent_ids = [item["agent_id"] for item in per_agent]
            if len(agent_ids) != len(set(agent_ids)) or not set(agent_ids).issubset(
                expected_active_agent_ids
            ):
                return False
        return True

    async def execute(
        self,
        cell: ExperimentCell,
        *,
        matrix_hash: str,
    ) -> CellExecutionResult:
        runtime_profile = self._validate_adapters(cell)
        if self.enforce_source_revision:
            current_revision = read_source_revision()
            if current_revision != cell.source_revision:
                raise AdapterUnavailableError(
                    f"source revision drift: cell records {cell.source_revision!r}, "
                    f"runtime is {current_revision!r}"
                )
        spec, evaluation_dirs = self._load_scenario(cell)
        experiment = self._experiment(
            cell,
            matrix_hash=matrix_hash,
            runtime_profile=runtime_profile,
        )
        runtime_selection = self._runtime_selection(cell, runtime_profile)
        runner = ScenarioRunner(
            spec,
            seed=cell.seed,
            baseline_policy=_BASELINE_BY_MODEL[cell.model],
            experiment=experiment,
            experiment_runtime=runtime_selection,
            run_artifacts_root=self.data_root,
        )
        try:
            result = await runner.run()
        finally:
            await runner.engine.close()

        report = evaluate_run(
            result.run_id,
            data_root=self.data_root,
            scenario_dirs=evaluation_dirs or None,
        )
        metadata = read_run_metadata(result.run_id, root=self.data_root)
        if not isinstance(metadata, dict):
            raise AdapterUnavailableError("completed run metadata is unavailable")
        evaluation = report.to_dict()
        analysis_context = {
            "counterfactual_group_id": metadata.get("counterfactual_group_id"),
            "counterfactual_variant": metadata.get("counterfactual_variant"),
            "scenario_category": getattr(spec, "category", None),
        }
        qualification = (
            "mocked_pipeline_no_fixture"
            if cell.model == "mocked"
            else "deterministic_rule_based_baseline"
        )
        return CellExecutionResult.model_validate(
            {
                "run_id": result.run_id,
                "scenario_id": result.scenario_id,
                "seed": result.seed,
                "completed": result.completed,
                "ticks": result.ticks,
                "sim_time_s": result.sim_time_s,
                "experiment": experiment.model_dump(mode="json"),
                "model_qualification": qualification,
                "quality_baseline": False if cell.model == "mocked" else None,
                "analysis_context": analysis_context,
                "fairness": build_fairness_payload(
                    cell,
                    run_metadata=metadata,
                    evaluation=evaluation,
                ),
                "evaluation": evaluation,
            }
        )

    def validate_completed(
        self,
        cell: ExperimentCell,
        output: Mapping[str, Any],
        *,
        matrix_hash: str,
    ) -> bool:
        """Require both the result seal and its referenced run seal on resume."""

        if self.enforce_source_revision and read_source_revision() != cell.source_revision:
            return False
        run_id = output.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return False
        try:
            runtime_profile = self._validate_adapters(cell)
            spec, evaluation_dirs = self._load_scenario(cell)
            metadata = read_run_metadata(run_id, root=self.data_root)
            if not isinstance(metadata, dict):
                return False
            verify_finalized_event_log(run_id, metadata=metadata, root=self.data_root)
            events, _ = read_run_events(
                run_id,
                root=self.data_root,
                verify_integrity=True,
            )
        except Exception:
            return False
        expected_experiment = self._experiment(
            cell,
            matrix_hash=matrix_hash,
            runtime_profile=runtime_profile,
        ).model_dump(mode="json")
        expected_policy = _BASELINE_BY_MODEL[cell.model].value
        expected_llm_mode = "rule_based" if cell.model == "rule_based" else "mocked"
        metadata_matches = (
            metadata.get("run_id") == run_id
            and metadata.get("scenario_id") == cell.scenario_id
            and metadata.get("scenario_contract_hash")
            == cell.scenario_contract_hash
            and metadata.get("seed") == cell.seed
            and metadata.get("source_revision") == cell.source_revision
            and metadata.get("baseline_policy") == expected_policy
            and metadata.get("llm_mode") == expected_llm_mode
            and metadata.get("experiment") == expected_experiment
            and metadata.get("artifact_error") is None
            and metadata.get("end_reason") == "completed"
            and metadata.get("ended_at") is not None
        )
        if not metadata_matches:
            return False
        if not self._runtime_evidence_matches(cell, runtime_profile, events):
            return False
        report = evaluate_run(
            run_id,
            data_root=self.data_root,
            scenario_dirs=evaluation_dirs or None,
        )
        evaluation = report.to_dict()
        expected_analysis_context = {
            "counterfactual_group_id": metadata.get("counterfactual_group_id"),
            "counterfactual_variant": metadata.get("counterfactual_variant"),
            "scenario_category": getattr(spec, "category", None),
        }
        return (
            report.outcome is not EvalOutcome.ERROR
            and output.get("experiment") == expected_experiment
            and output.get("evaluation") == evaluation
            and output.get("analysis_context") == expected_analysis_context
            and output.get("fairness")
            == build_fairness_payload(
                cell,
                run_metadata=metadata,
                evaluation=evaluation,
            )
        )


__all__ = [
    "SUPPORTED_GOVERNANCE",
    "SUPPORTED_MODELS",
    "SUPPORTED_OBSERVATIONS",
    "SUPPORTED_RUNTIME_PROFILES",
    "SUPPORTED_TOPOLOGIES",
    "AdapterUnavailableError",
    "AuraCellExecutor",
]
