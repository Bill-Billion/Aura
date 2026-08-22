"""The one real Aura runtime adapter supported by the pilot matrix.

Unsupported scientific conditions are rejected rather than approximated.  In
particular, this module does not claim to implement the planned single-agent,
no-governance, flat-priority, or perfect-observation baselines.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from backend.engine.event_log import (
    read_run_metadata,
    verify_finalized_event_log,
)
from backend.engine.provenance import (
    ExperimentProvenance,
    ExperimentRuntimeSelection,
)
from backend.engine.run_manager import read_source_revision
from backend.evaluation.evaluator import EvalOutcome, evaluate_run
from backend.models.schemas import BaselinePolicy
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import get_scenario, load_scenario_file
from backend.scenarios.runner import ScenarioRunner
from backend.scenarios.spec import ScenarioSpec

from .spec import ExperimentCell
from .runner import CellExecutionResult

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

SUPPORTED_MODELS = frozenset({"rule_based", "mocked"})
SUPPORTED_TOPOLOGIES = frozenset({"domain_multi"})
SUPPORTED_GOVERNANCE = frozenset({"aura"})
SUPPORTED_OBSERVATIONS = frozenset({"stale_offline"})

_BASELINE_BY_MODEL = {
    "rule_based": BaselinePolicy.RULE_BASED,
    "mocked": BaselinePolicy.LLM_MOCKED,
}


class AdapterUnavailableError(RuntimeError):
    pass


class AuraCellExecutor:
    """Execute the implemented domain-multi/Aura/stale-offline condition."""

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
    def _validate_adapters(cell: ExperimentCell) -> None:
        selections = (
            ("model", cell.model, SUPPORTED_MODELS),
            ("topology", cell.topology, SUPPORTED_TOPOLOGIES),
            ("governance", cell.governance, SUPPORTED_GOVERNANCE),
            ("observation", cell.observation, SUPPORTED_OBSERVATIONS),
        )
        for axis, value, implemented in selections:
            if value not in implemented:
                raise AdapterUnavailableError(
                    f"{axis} adapter {value!r} is not implemented; "
                    f"implemented values: {', '.join(sorted(implemented))}"
                )

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
    ) -> ExperimentProvenance:
        return ExperimentProvenance(
            experiment_id=cell.experiment_id,
            matrix_spec_hash=cell.matrix_spec_hash,
            matrix_hash=matrix_hash,
            cell_id=cell.cell_id,
            model=cell.model,
            topology=cell.topology,
            governance=cell.governance,
            observation=cell.observation,
            repetition=cell.repetition,
        )

    async def execute(
        self,
        cell: ExperimentCell,
        *,
        matrix_hash: str,
    ) -> CellExecutionResult:
        self._validate_adapters(cell)
        if self.enforce_source_revision:
            current_revision = read_source_revision()
            if current_revision != cell.source_revision:
                raise AdapterUnavailableError(
                    f"source revision drift: cell records {cell.source_revision!r}, "
                    f"runtime is {current_revision!r}"
                )
        spec, evaluation_dirs = self._load_scenario(cell)
        experiment = self._experiment(cell, matrix_hash=matrix_hash)
        runtime_selection = ExperimentRuntimeSelection(
            model=cell.model,
            baseline_policy=_BASELINE_BY_MODEL[cell.model],
        )
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
                "evaluation": report.to_dict(),
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
            self._validate_adapters(cell)
            _, evaluation_dirs = self._load_scenario(cell)
            metadata = read_run_metadata(run_id, root=self.data_root)
            if not isinstance(metadata, dict):
                return False
            verify_finalized_event_log(run_id, metadata=metadata, root=self.data_root)
        except Exception:
            return False
        expected_experiment = self._experiment(
            cell,
            matrix_hash=matrix_hash,
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
        report = evaluate_run(
            run_id,
            data_root=self.data_root,
            scenario_dirs=evaluation_dirs or None,
        )
        return (
            report.outcome is not EvalOutcome.ERROR
            and output.get("evaluation") == report.to_dict()
        )


__all__ = [
    "SUPPORTED_GOVERNANCE",
    "SUPPORTED_MODELS",
    "SUPPORTED_OBSERVATIONS",
    "SUPPORTED_TOPOLOGIES",
    "AdapterUnavailableError",
    "AuraCellExecutor",
]
