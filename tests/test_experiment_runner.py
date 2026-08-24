from __future__ import annotations

import json

import pytest

from backend.experiments.artifacts import (
    archive_cell_result,
    atomic_create_json,
    cell_result_path,
    read_cell_result,
)
from backend.experiments.adapters import AdapterUnavailableError, AuraCellExecutor
from backend.experiments.resolve import resolve_matrix
from backend.experiments.runner import (
    MatrixRunError,
    MatrixRunner,
    cell_shard_index,
    select_shard,
    summarize_results,
)
from backend.experiments.spec import MatrixSpec, ScenarioContract


class StubScenarioResolver:
    def resolve(self, reference: str) -> ScenarioContract:
        return ScenarioContract(
            reference=reference,
            scenario_id=reference,
            scenario_contract_hash="b" * 64,
        )


def resolved_matrix(source_revision: str = "test-revision"):
    spec = MatrixSpec.model_validate(
        {
            "matrix_id": "runner_matrix",
            "axes": {
                "scenario": ["static", "dynamic"],
                "seed": [42],
                "model": ["rule_based"],
                "topology": ["domain_multi"],
                "governance": ["aura"],
                "observation": ["stale_offline"],
                "repetition": [0, 1],
            },
            "max_cells": 4,
            "max_total_cost_usd": 0,
            "cost_per_model_usd": {"rule_based": 0},
        }
    )
    return resolve_matrix(
        spec,
        scenario_resolver=StubScenarioResolver(),
        source_revision=source_revision,
    )


class AcceptingValidator:
    @staticmethod
    def validate_completed(cell, output, *, matrix_hash):
        return output.get("evaluation", {}).get("outcome") in {"pass", "fail"}


class RecordingExecutor(AcceptingValidator):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, cell, *, matrix_hash):
        self.calls.append(cell.cell_id)
        return {
            "cell_id": cell.cell_id,
            "sequence": len(self.calls),
            "evaluation": {"outcome": "pass"},
        }


def test_atomic_create_enforces_write_limit_without_partial_artifact(tmp_path) -> None:
    path = tmp_path / "bounded.json"
    with pytest.raises(ValueError, match="exceeds 16 bytes"):
        atomic_create_json(path, {"value": "x" * 100}, max_bytes=16)
    assert not path.exists()


def test_retry_archive_rejects_symlinked_result(tmp_path) -> None:
    cell_id = resolved_matrix().cells[0].cell_id
    cell_dir = tmp_path / "cells" / cell_id
    cell_dir.mkdir(parents=True)
    victim = tmp_path / "outside.json"
    victim.write_text('{"secret":"preserve-me"}', encoding="utf-8")
    (cell_dir / "result.json").symlink_to(victim)

    with pytest.raises(OSError):
        archive_cell_result(tmp_path, cell_id)
    assert victim.read_text(encoding="utf-8") == '{"secret":"preserve-me"}'
    assert not (cell_dir / "attempts").exists()


@pytest.mark.anyio
async def test_runner_is_serial_ordered_and_resumes_only_valid_completed_results(
    tmp_path,
) -> None:
    matrix = resolved_matrix()
    executor = RecordingExecutor()
    first = await MatrixRunner(executor).run(matrix, output_dir=tmp_path)
    assert executor.calls == [cell.cell_id for cell in matrix.cells]
    assert first.completed == 4
    assert first.skipped == 0
    assert first.fairness_audited is True

    second = await MatrixRunner(executor).run(matrix, output_dir=tmp_path)
    assert len(executor.calls) == 4
    assert second.completed == 4
    assert second.skipped == 4

    tampered_path = cell_result_path(tmp_path, matrix.cells[0].cell_id)
    tampered = json.loads(tampered_path.read_text(encoding="utf-8"))
    tampered["result"]["output"]["sequence"] = 999
    tampered_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(MatrixRunError, match="invalid result artifact"):
        await MatrixRunner(executor).run(matrix, output_dir=tmp_path)
    assert len(executor.calls) == 4

    third = await MatrixRunner(executor).run(
        matrix,
        output_dir=tmp_path,
        retry_results=True,
    )
    assert executor.calls[-1] == matrix.cells[0].cell_id
    assert third.invalid_artifacts == 1
    assert third.skipped == 3
    assert read_cell_result(tampered_path).result.status == "completed"
    assert len(list((tampered_path.parent / "attempts").glob("result-*.json"))) == 1


@pytest.mark.anyio
async def test_output_directory_never_replaces_a_different_matrix(tmp_path) -> None:
    first = resolved_matrix("revision-a")
    second = resolved_matrix("revision-b")
    await MatrixRunner(RecordingExecutor()).run(first, output_dir=tmp_path)
    frozen = (tmp_path / "resolved-matrix.json").read_bytes()
    with pytest.raises(ValueError, match="already belongs to matrix"):
        await MatrixRunner(RecordingExecutor()).run(second, output_dir=tmp_path)
    assert (tmp_path / "resolved-matrix.json").read_bytes() == frozen


def test_shards_are_disjoint_and_their_union_is_the_matrix() -> None:
    matrix = resolved_matrix()
    shards = [
        select_shard(matrix.cells, shard_index=index, shard_count=3)
        for index in range(3)
    ]
    all_ids = [cell.cell_id for shard in shards for cell in shard]
    assert len(all_ids) == len(set(all_ids))
    assert set(all_ids) == {cell.cell_id for cell in matrix.cells}
    for index, shard in enumerate(shards):
        assert all(cell_shard_index(cell.cell_id, 3) == index for cell in shard)


@pytest.mark.anyio
async def test_each_shard_writes_an_independent_summary(tmp_path) -> None:
    matrix = resolved_matrix()
    executor = RecordingExecutor()
    for index in range(2):
        shard_summary = await MatrixRunner(executor).run(
            matrix,
            output_dir=tmp_path,
            shard_index=index,
            shard_count=2,
        )
        assert shard_summary.fairness_audited is False
        assert (tmp_path / "shards" / f"{index:04d}-of-0002" / "summary.json").is_file()
    summary = summarize_results(
        matrix,
        output_dir=tmp_path,
        validator=RecordingExecutor(),
    )
    assert summary.completed == len(matrix.cells)
    assert summary.pending == 0
    assert summary.fairness_audited is True


class FailingExecutor(AcceptingValidator):
    async def execute(self, cell, *, matrix_hash):
        raise RuntimeError("injected failure")


@pytest.mark.anyio
async def test_runner_seals_failure_writes_summary_and_stops_by_default(tmp_path) -> None:
    matrix = resolved_matrix()
    with pytest.raises(MatrixRunError, match="injected failure") as excinfo:
        await MatrixRunner(FailingExecutor()).run(matrix, output_dir=tmp_path)
    assert excinfo.value.cell_id == matrix.cells[0].cell_id
    artifact = read_cell_result(cell_result_path(tmp_path, matrix.cells[0].cell_id))
    assert artifact.result.status == "failed"
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["execution_failed"] == 1
    assert summary["pending"] == len(matrix.cells) - 1


@pytest.mark.anyio
async def test_executor_must_return_a_typed_execution_result(tmp_path) -> None:
    class BadExecutor(AcceptingValidator):
        async def execute(self, cell, *, matrix_hash):
            return [cell.cell_id]

    with pytest.raises(MatrixRunError, match="CellExecutionResult"):
        await MatrixRunner(BadExecutor()).run(
            resolved_matrix(),
            output_dir=tmp_path,
        )


@pytest.mark.anyio
async def test_executor_must_supply_completed_evidence_validator(tmp_path) -> None:
    class NoValidator:
        async def execute(self, cell, *, matrix_hash):
            return {"evaluation": {"outcome": "pass"}}

    with pytest.raises(TypeError, match="validate_completed"):
        await MatrixRunner(NoValidator()).run(
            resolved_matrix(),
            output_dir=tmp_path,
        )


@pytest.mark.anyio
async def test_summary_separates_benchmark_outcome_from_execution_status(tmp_path) -> None:
    class EvaluatedExecutor(AcceptingValidator):
        async def execute(self, cell, *, matrix_hash):
            return {
                "evaluation": {
                    "outcome": "pass" if cell.repetition == 0 else "fail"
                }
            }

    matrix = resolved_matrix()
    summary = await MatrixRunner(EvaluatedExecutor()).run(
        matrix,
        output_dir=tmp_path,
    )
    assert summary.completed == 4
    assert summary.benchmark_pass == 2
    assert summary.benchmark_fail == 2
    assert summary.evaluation_error == 0
    assert summary.execution_failed == 0
    condition = summary.by_condition[0]
    assert condition.model == "rule_based"
    assert condition.completed == 4
    assert condition.benchmark_pass == 2
    assert condition.benchmark_fail == 2


@pytest.mark.anyio
async def test_evaluation_error_is_not_counted_as_completed(tmp_path) -> None:
    class ErrorExecutor(AcceptingValidator):
        async def execute(self, cell, *, matrix_hash):
            return {"evaluation": {"outcome": "error"}}

    matrix = resolved_matrix()
    summary = await MatrixRunner(ErrorExecutor()).run(
        matrix,
        output_dir=tmp_path,
    )
    assert summary.completed == 0
    assert summary.evaluation_error == len(matrix.cells)
    assert summary.pending == 0
    aggregated = summarize_results(
        matrix,
        output_dir=tmp_path,
        validator=ErrorExecutor(),
    )
    assert aggregated.completed == 0
    assert aggregated.evaluation_error == len(matrix.cells)


@pytest.mark.anyio
async def test_summarize_revalidates_completed_run_evidence(tmp_path) -> None:
    matrix = resolved_matrix()
    await MatrixRunner(RecordingExecutor()).run(matrix, output_dir=tmp_path)

    class RejectingValidator:
        def validate_completed(self, cell, output, *, matrix_hash):
            return False

    summary = summarize_results(
        matrix,
        output_dir=tmp_path,
        validator=RejectingValidator(),
    )
    assert summary.completed == 0
    assert summary.invalid_artifacts == len(matrix.cells)
    assert summary.pending == len(matrix.cells)


@pytest.mark.anyio
async def test_fresh_result_must_pass_completed_evidence_validator(tmp_path) -> None:
    class RejectingExecutor(RecordingExecutor):
        @staticmethod
        def validate_completed(cell, output, *, matrix_hash):
            return False

    matrix = resolved_matrix()
    with pytest.raises(MatrixRunError, match="failed completed-result validation"):
        await MatrixRunner(RejectingExecutor()).run(matrix, output_dir=tmp_path)
    artifact = read_cell_result(cell_result_path(tmp_path, matrix.cells[0].cell_id))
    assert artifact.result.status == "invalid_evidence"
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["completed"] == 0
    assert summary["invalid_artifacts"] == 1
    assert summary["pending"] == len(matrix.cells)


@pytest.mark.anyio
async def test_aura_adapter_runs_sealed_artifact_and_revalidates_provenance_on_resume(
    tmp_path,
) -> None:
    from backend.experiments.resolve import (
        FileOrLibraryScenarioResolver,
        load_matrix_file,
    )

    spec = load_matrix_file("benchmarks/aurabench-dev/matrix.yaml")
    matrix = resolve_matrix(
        spec,
        scenario_resolver=FileOrLibraryScenarioResolver(
            base_dir="benchmarks/aurabench-dev"
        ),
    )
    cell = next(
        cell
        for cell in matrix.cells
        if cell.model == "rule_based" and cell.scenario_id.endswith("static")
    )
    explicit_runs_root = tmp_path / "explicit-runs"
    executor = AuraCellExecutor(data_root=explicit_runs_root)
    result = await executor.execute(cell, matrix_hash=matrix.matrix_hash)
    output = result.model_dump(mode="json")
    assert output["model_qualification"] == "deterministic_rule_based_baseline"
    assert output["evaluation"]["outcome"] in {"pass", "fail"}
    assert (explicit_runs_root / output["run_id"] / "run.json").is_file()
    assert executor.validate_completed(
        cell,
        output,
        matrix_hash=matrix.matrix_hash,
    ) is True
    changed_condition = cell.model_copy(update={"repetition": cell.repetition + 100})
    assert executor.validate_completed(
        changed_condition,
        output,
        matrix_hash=matrix.matrix_hash,
    ) is False


@pytest.mark.anyio
async def test_aura_adapter_fails_closed_for_unimplemented_conditions() -> None:
    matrix = resolved_matrix()
    unsupported = matrix.cells[0].model_copy(update={"topology": "single"})
    with pytest.raises(AdapterUnavailableError, match="not implemented"):
        await AuraCellExecutor(enforce_source_revision=False).execute(
            unsupported,
            matrix_hash=matrix.matrix_hash,
        )


@pytest.mark.anyio
async def test_aura_adapter_returns_evaluation_error_as_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    from backend.evaluation.evaluator import EvalOutcome
    from backend.experiments import adapters
    from backend.experiments.resolve import (
        FileOrLibraryScenarioResolver,
        load_matrix_file,
    )

    matrix = resolve_matrix(
        load_matrix_file("benchmarks/aurabench-dev/matrix.yaml"),
        scenario_resolver=FileOrLibraryScenarioResolver(
            base_dir="benchmarks/aurabench-dev"
        ),
    )

    class ErrorReport:
        outcome = EvalOutcome.ERROR

        @staticmethod
        def to_dict():
            return {
                "report_schema_version": "1.0",
                "outcome": "error",
                "failure_reasons": ["injected"],
                "provenance": {
                    "evaluator_source_revision": matrix.source_revision,
                },
            }

    monkeypatch.setattr(adapters, "evaluate_run", lambda *args, **kwargs: ErrorReport())
    result = await AuraCellExecutor(data_root=tmp_path / "runs").execute(
        matrix.cells[0],
        matrix_hash=matrix.matrix_hash,
    )
    assert result.evaluation.outcome == "error"
