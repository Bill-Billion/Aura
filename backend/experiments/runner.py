"""Serial, resumable execution of an immutable resolved matrix."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .artifacts import (
    CellResultArtifact,
    archive_cell_result,
    atomic_write_json,
    cell_result_exists,
    read_cell_result_at,
    write_cell_result,
    write_resolved_matrix,
)
from .fairness import FairnessAudit, audit_comparison_outputs, validate_comparison_plan
from .spec import ExperimentCell, ResolvedMatrix


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    outcome: Literal["pass", "fail", "error"]


class CellExecutionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    evaluation: EvaluationResult


class CompletedResultValidator(Protocol):
    def validate_completed(
        self,
        cell: ExperimentCell,
        output: Mapping[str, Any],
        *,
        matrix_hash: str,
    ) -> bool: ...


class CellExecutor(CompletedResultValidator, Protocol):
    async def execute(
        self,
        cell: ExperimentCell,
        *,
        matrix_hash: str,
    ) -> CellExecutionResult: ...


class MatrixRunError(RuntimeError):
    def __init__(self, cell_id: str, message: str) -> None:
        super().__init__(message)
        self.cell_id = cell_id


class MatrixSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary_schema_version: str = "1.1"
    matrix_id: str
    matrix_hash: str
    shard_index: int | None = Field(default=None, ge=0)
    shard_count: int | None = Field(default=None, ge=1)
    planned: int = Field(ge=0)
    selected: int = Field(ge=0)
    completed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    benchmark_pass: int = Field(ge=0)
    benchmark_fail: int = Field(ge=0)
    evaluation_error: int = Field(ge=0)
    execution_failed: int = Field(ge=0)
    pending: int = Field(ge=0)
    invalid_artifacts: int = Field(default=0, ge=0)
    fairness_audited: bool = False
    valid_baseline_groups: int = Field(default=0, ge=0)
    invalid_baseline_groups: int = Field(default=0, ge=0)
    invalid_baseline_group_reasons: dict[str, list[str]] = Field(default_factory=dict)
    scientific_valid_cells: int = Field(default=0, ge=0)
    scientific_benchmark_pass: int = Field(default=0, ge=0)
    scientific_benchmark_fail: int = Field(default=0, ge=0)
    failed_cell_ids: list[str] = Field(default_factory=list)
    by_condition: list["ConditionSummary"] = Field(default_factory=list)


class ConditionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    topology: str
    governance: str
    observation: str
    planned: int = Field(default=0, ge=0)
    selected: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    benchmark_pass: int = Field(default=0, ge=0)
    benchmark_fail: int = Field(default=0, ge=0)
    evaluation_error: int = Field(default=0, ge=0)
    execution_failed: int = Field(default=0, ge=0)
    pending: int = Field(default=0, ge=0)


ConditionKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class ValidatedMatrixResults:
    """Full-matrix evidence admitted through the same gate as summarization."""

    completed_outputs: dict[str, Mapping[str, Any]]
    completed_artifacts: dict[str, CellResultArtifact]
    fairness: FairnessAudit
    completed: int
    benchmark_pass: int
    benchmark_fail: int
    evaluation_error: int
    execution_failed: int
    invalid_artifacts: int
    failed_cell_ids: tuple[str, ...]
    by_condition: tuple[ConditionSummary, ...]


def _condition_key(cell: ExperimentCell) -> ConditionKey:
    return (
        cell.model,
        cell.topology,
        cell.governance,
        cell.observation,
    )


def _condition_summaries(
    planned: list[ExperimentCell],
    selected: list[ExperimentCell],
) -> dict[ConditionKey, ConditionSummary]:
    summaries: dict[ConditionKey, ConditionSummary] = {}
    for cell in planned:
        key = _condition_key(cell)
        summary = summaries.setdefault(
            key,
            ConditionSummary(
                model=cell.model,
                topology=cell.topology,
                governance=cell.governance,
                observation=cell.observation,
            ),
        )
        summary.planned += 1
    for cell in selected:
        summaries[_condition_key(cell)].selected += 1
    return summaries


def _record_completed(
    summary: ConditionSummary,
    output: Mapping[str, Any],
) -> str | None:
    summary.completed += 1
    evaluation = output.get("evaluation")
    outcome = evaluation.get("outcome") if isinstance(evaluation, Mapping) else None
    if outcome == "pass":
        summary.benchmark_pass += 1
        return "pass"
    if outcome == "fail":
        summary.benchmark_fail += 1
        return "fail"
    if outcome == "error":
        summary.evaluation_error += 1
        return "error"
    return None


def _evaluation_outcome(output: Mapping[str, Any]) -> str | None:
    evaluation = output.get("evaluation")
    outcome = evaluation.get("outcome") if isinstance(evaluation, Mapping) else None
    return outcome if isinstance(outcome, str) else None


def _scientific_counts(
    audit: FairnessAudit,
    completed_outputs: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int, int]:
    """Count benchmark outcomes only inside fairness-valid comparison groups."""

    passed = 0
    failed = 0
    for cell_id in audit.valid_cell_ids:
        outcome = _evaluation_outcome(completed_outputs[cell_id])
        passed += int(outcome == "pass")
        failed += int(outcome == "fail")
    return len(audit.valid_cell_ids), passed, failed


def _validate_completed_output(
    validator: CompletedResultValidator,
    cell: ExperimentCell,
    output: Mapping[str, Any],
    *,
    matrix_hash: str,
) -> bool:
    if _evaluation_outcome(output) == "error":
        return False
    validate = validator.validate_completed
    try:
        return bool(validate(cell, output, matrix_hash=matrix_hash))
    except Exception:
        return False


def cell_shard_index(cell_id: str, shard_count: int) -> int:
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    digest = hashlib.sha256(cell_id.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") % shard_count


def select_shard(
    cells: list[ExperimentCell],
    *,
    shard_index: int,
    shard_count: int,
) -> list[ExperimentCell]:
    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if shard_index < 0 or shard_index >= shard_count:
        raise ValueError("shard_index must satisfy 0 <= index < shard_count")
    return [
        cell
        for cell in cells
        if cell_shard_index(cell.cell_id, shard_count) == shard_index
    ]


def shard_summary_path(
    root: Path | str,
    *,
    shard_index: int,
    shard_count: int,
) -> Path:
    root = Path(root)
    if shard_count == 1:
        return root / "summary.json"
    return (
        root
        / "shards"
        / f"{shard_index:04d}-of-{shard_count:04d}"
        / "summary.json"
    )


class MatrixRunner:
    """Run one cell at a time; parallelism is explicit process-level sharding."""

    def __init__(self, executor: CellExecutor | None = None) -> None:
        self.executor = executor

    async def run(
        self,
        matrix: ResolvedMatrix,
        *,
        output_dir: Path | str,
        shard_index: int = 0,
        shard_count: int = 1,
        resume: bool = True,
        continue_on_error: bool = False,
        retry_results: bool = False,
    ) -> MatrixSummary:
        root = Path(output_dir)
        executor = self.executor
        if executor is None:
            from .adapters import AuraCellExecutor

            executor = AuraCellExecutor(data_root=root / "runs")
        if not callable(getattr(executor, "validate_completed", None)):
            raise TypeError("cell executor must implement validate_completed")
        validate_comparison_plan(
            matrix.cells,
            expected_profiles=matrix.expected_runtime_profiles,
        )
        write_resolved_matrix(root, matrix)
        selected = select_shard(
            matrix.cells,
            shard_index=shard_index,
            shard_count=shard_count,
        )
        completed = 0
        skipped = 0
        benchmark_pass = 0
        benchmark_fail = 0
        evaluation_error = 0
        execution_failed = 0
        invalid_artifacts = 0
        failed_ids: list[str] = []
        terminal_error: MatrixRunError | None = None
        write_summary = True
        by_condition = _condition_summaries(matrix.cells, selected)
        completed_outputs: dict[str, Mapping[str, Any]] = {}

        for cell in selected:
            if cell_result_exists(root, cell.cell_id):
                try:
                    existing = read_cell_result_at(root, cell.cell_id)
                except ValueError as exc:
                    invalid_artifacts += 1
                    if not retry_results:
                        terminal_error = MatrixRunError(
                            cell.cell_id,
                            f"cell {cell.cell_id} has an invalid result artifact: {exc}; "
                            "rerun with explicit retry enabled to archive it",
                        )
                        break
                    archive_cell_result(root, cell.cell_id)
                else:
                    identity_matches = (
                        existing.result.cell_id == cell.cell_id
                        and existing.result.cell_hash == cell.contract_hash()
                        and existing.result.matrix_hash == matrix.matrix_hash
                    )
                    if not identity_matches:
                        terminal_error = MatrixRunError(
                            cell.cell_id,
                            f"cell {cell.cell_id} result identity collides with the "
                            "resolved matrix; existing evidence was preserved",
                        )
                        break
                    output = existing.result.output or {}
                    if (
                        resume
                        and existing.result.status == "completed"
                        and _validate_completed_output(
                            executor,
                            cell,
                            output,
                            matrix_hash=matrix.matrix_hash,
                        )
                    ):
                        completed += 1
                        skipped += 1
                        outcome = _record_completed(
                            by_condition[_condition_key(cell)],
                            output,
                        )
                        benchmark_pass += int(outcome == "pass")
                        benchmark_fail += int(outcome == "fail")
                        completed_outputs[cell.cell_id] = output
                        continue
                    if resume and existing.result.status == "evaluation_error":
                        if not retry_results:
                            evaluation_error += 1
                            by_condition[_condition_key(cell)].evaluation_error += 1
                            continue
                    elif resume and existing.result.status == "failed":
                        if not retry_results:
                            execution_failed += 1
                            failed_ids.append(cell.cell_id)
                            by_condition[_condition_key(cell)].execution_failed += 1
                            continue
                    elif resume and existing.result.status == "invalid_evidence":
                        invalid_artifacts += 1
                    elif resume and existing.result.status == "completed":
                        invalid_artifacts += 1
                        if _evaluation_outcome(output) == "error" and not retry_results:
                            evaluation_error += 1
                            by_condition[_condition_key(cell)].evaluation_error += 1
                            continue
                    if not retry_results:
                        terminal_error = MatrixRunError(
                            cell.cell_id,
                            f"cell {cell.cell_id} already has evidence that cannot be "
                            "reused; rerun with explicit retry enabled to archive it",
                        )
                        break
                    archive_cell_result(root, cell.cell_id)

            try:
                raw_output = await executor.execute(
                    cell,
                    matrix_hash=matrix.matrix_hash,
                )
                output = CellExecutionResult.model_validate(raw_output).model_dump(
                    mode="json"
                )
            except Exception as exc:
                artifact = CellResultArtifact.failed(
                    cell=cell,
                    matrix_hash=matrix.matrix_hash,
                    exc=exc,
                )
                try:
                    write_cell_result(root, artifact)
                except FileExistsError:
                    write_summary = False
                    terminal_error = MatrixRunError(
                        cell.cell_id,
                        f"cell {cell.cell_id} was written by another process; "
                        "existing evidence was preserved",
                    )
                    break
                execution_failed += 1
                failed_ids.append(cell.cell_id)
                by_condition[_condition_key(cell)].execution_failed += 1
                if not continue_on_error:
                    terminal_error = MatrixRunError(
                        cell.cell_id,
                        f"cell {cell.cell_id} failed: {exc}",
                    )
                    break
            else:
                outcome = _evaluation_outcome(output)
                if outcome == "error":
                    artifact = CellResultArtifact.evaluation_error(
                        cell=cell,
                        matrix_hash=matrix.matrix_hash,
                        output=output,
                    )
                elif not _validate_completed_output(
                    executor,
                    cell,
                    output,
                    matrix_hash=matrix.matrix_hash,
                ):
                    artifact = CellResultArtifact.invalid_evidence(
                        cell=cell,
                        matrix_hash=matrix.matrix_hash,
                        output=output,
                    )
                else:
                    artifact = CellResultArtifact.completed(
                        cell=cell,
                        matrix_hash=matrix.matrix_hash,
                        output=output,
                    )
                try:
                    write_cell_result(root, artifact)
                except FileExistsError:
                    write_summary = False
                    terminal_error = MatrixRunError(
                        cell.cell_id,
                        f"cell {cell.cell_id} was written by another process; "
                        "existing evidence was preserved",
                    )
                    break
                if outcome == "error":
                    evaluation_error += 1
                    by_condition[_condition_key(cell)].evaluation_error += 1
                elif artifact.result.status == "invalid_evidence":
                    invalid_artifacts += 1
                    if not continue_on_error:
                        terminal_error = MatrixRunError(
                            cell.cell_id,
                            f"cell {cell.cell_id} produced evidence that failed "
                            "completed-result validation",
                        )
                        break
                else:
                    completed += 1
                    recorded = _record_completed(
                        by_condition[_condition_key(cell)],
                        output,
                    )
                    benchmark_pass += int(recorded == "pass")
                    benchmark_fail += int(recorded == "fail")
                    completed_outputs[cell.cell_id] = output

        for condition in by_condition.values():
            condition.pending = (
                condition.selected
                - condition.completed
                - condition.evaluation_error
                - condition.execution_failed
            )
        fairness: FairnessAudit | None = None
        scientific_valid_cells = 0
        scientific_benchmark_pass = 0
        scientific_benchmark_fail = 0
        if shard_count == 1:
            fairness = audit_comparison_outputs(
                matrix.cells,
                completed_outputs,
                expected_profiles=matrix.expected_runtime_profiles,
            )
            (
                scientific_valid_cells,
                scientific_benchmark_pass,
                scientific_benchmark_fail,
            ) = _scientific_counts(fairness, completed_outputs)

        summary = MatrixSummary(
            matrix_id=matrix.matrix_id,
            matrix_hash=matrix.matrix_hash,
            shard_index=(shard_index if shard_count > 1 else None),
            shard_count=(shard_count if shard_count > 1 else None),
            planned=len(matrix.cells),
            selected=len(selected),
            completed=completed,
            skipped=skipped,
            benchmark_pass=benchmark_pass,
            benchmark_fail=benchmark_fail,
            evaluation_error=evaluation_error,
            execution_failed=execution_failed,
            pending=(
                len(selected) - completed - evaluation_error - execution_failed
            ),
            invalid_artifacts=invalid_artifacts,
            fairness_audited=fairness is not None,
            valid_baseline_groups=(fairness.valid_groups if fairness else 0),
            invalid_baseline_groups=(fairness.invalid_groups if fairness else 0),
            invalid_baseline_group_reasons=(
                fairness.invalid_reasons if fairness else {}
            ),
            scientific_valid_cells=scientific_valid_cells,
            scientific_benchmark_pass=scientific_benchmark_pass,
            scientific_benchmark_fail=scientific_benchmark_fail,
            failed_cell_ids=failed_ids,
            by_condition=[by_condition[key] for key in sorted(by_condition)],
        )
        if write_summary:
            atomic_write_json(
                shard_summary_path(
                    root,
                    shard_index=shard_index,
                    shard_count=shard_count,
                ),
                summary.model_dump(mode="json"),
            )
        if terminal_error is not None:
            raise terminal_error
        return summary


def collect_validated_results(
    matrix: ResolvedMatrix,
    *,
    output_dir: Path | str,
    validator: CompletedResultValidator,
) -> ValidatedMatrixResults:
    """Validate every cell artifact and retain the admitted scientific inputs."""

    root = Path(output_dir)
    if not callable(getattr(validator, "validate_completed", None)):
        raise TypeError("summarization requires a completed-result validator")
    validate_comparison_plan(
        matrix.cells,
        expected_profiles=matrix.expected_runtime_profiles,
    )
    completed = 0
    benchmark_pass = 0
    benchmark_fail = 0
    evaluation_error = 0
    execution_failed = 0
    invalid = 0
    failed_ids: list[str] = []
    completed_outputs: dict[str, Mapping[str, Any]] = {}
    completed_artifacts: dict[str, CellResultArtifact] = {}
    by_condition = _condition_summaries(matrix.cells, matrix.cells)
    for cell in matrix.cells:
        if not cell_result_exists(root, cell.cell_id):
            continue
        try:
            artifact = read_cell_result_at(root, cell.cell_id)
        except ValueError:
            invalid += 1
            continue
        result = artifact.result
        if (
            result.cell_id != cell.cell_id
            or result.cell_hash != cell.contract_hash()
            or result.matrix_hash != matrix.matrix_hash
        ):
            invalid += 1
        elif result.status == "completed" and _validate_completed_output(
            validator,
            cell,
            result.output or {},
            matrix_hash=matrix.matrix_hash,
        ):
            completed += 1
            completed_outputs[cell.cell_id] = result.output or {}
            completed_artifacts[cell.cell_id] = artifact
            outcome = _record_completed(
                by_condition[_condition_key(cell)],
                result.output or {},
            )
            benchmark_pass += int(outcome == "pass")
            benchmark_fail += int(outcome == "fail")
        elif result.status == "evaluation_error" or (
            result.status == "completed"
            and _evaluation_outcome(result.output or {}) == "error"
        ):
            evaluation_error += 1
            by_condition[_condition_key(cell)].evaluation_error += 1
        elif result.status == "completed":
            invalid += 1
        elif result.status == "invalid_evidence":
            invalid += 1
        else:
            execution_failed += 1
            failed_ids.append(cell.cell_id)
            by_condition[_condition_key(cell)].execution_failed += 1

    for condition in by_condition.values():
        condition.pending = (
            condition.selected
            - condition.completed
            - condition.evaluation_error
            - condition.execution_failed
        )

    fairness = audit_comparison_outputs(
        matrix.cells,
        completed_outputs,
        expected_profiles=matrix.expected_runtime_profiles,
    )
    return ValidatedMatrixResults(
        completed_outputs=completed_outputs,
        completed_artifacts=completed_artifacts,
        fairness=fairness,
        completed=completed,
        benchmark_pass=benchmark_pass,
        benchmark_fail=benchmark_fail,
        evaluation_error=evaluation_error,
        execution_failed=execution_failed,
        invalid_artifacts=invalid,
        failed_cell_ids=tuple(failed_ids),
        by_condition=tuple(by_condition[key] for key in sorted(by_condition)),
    )


def summarize_results(
    matrix: ResolvedMatrix,
    *,
    output_dir: Path | str,
    validator: CompletedResultValidator,
) -> MatrixSummary:
    """Validate all result seals and write the cross-shard summary."""

    root = Path(output_dir)
    collected = collect_validated_results(
        matrix,
        output_dir=root,
        validator=validator,
    )
    (
        scientific_valid_cells,
        scientific_benchmark_pass,
        scientific_benchmark_fail,
    ) = _scientific_counts(
        collected.fairness,
        collected.completed_outputs,
    )

    summary = MatrixSummary(
        matrix_id=matrix.matrix_id,
        matrix_hash=matrix.matrix_hash,
        planned=len(matrix.cells),
        selected=len(matrix.cells),
        completed=collected.completed,
        skipped=0,
        benchmark_pass=collected.benchmark_pass,
        benchmark_fail=collected.benchmark_fail,
        evaluation_error=collected.evaluation_error,
        execution_failed=collected.execution_failed,
        pending=(
            len(matrix.cells)
            - collected.completed
            - collected.evaluation_error
            - collected.execution_failed
        ),
        invalid_artifacts=collected.invalid_artifacts,
        fairness_audited=True,
        valid_baseline_groups=collected.fairness.valid_groups,
        invalid_baseline_groups=collected.fairness.invalid_groups,
        invalid_baseline_group_reasons=collected.fairness.invalid_reasons,
        scientific_valid_cells=scientific_valid_cells,
        scientific_benchmark_pass=scientific_benchmark_pass,
        scientific_benchmark_fail=scientific_benchmark_fail,
        failed_cell_ids=list(collected.failed_cell_ids),
        by_condition=list(collected.by_condition),
    )
    atomic_write_json(root / "summary.json", summary.model_dump(mode="json"))
    return summary


__all__ = [
    "CellExecutor",
    "CellExecutionResult",
    "CompletedResultValidator",
    "ConditionSummary",
    "MatrixRunError",
    "MatrixRunner",
    "MatrixSummary",
    "ValidatedMatrixResults",
    "cell_shard_index",
    "collect_validated_results",
    "select_shard",
    "shard_summary_path",
    "summarize_results",
]
