"""Seal one completed AuraBench pilot and its independent human reviews."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.engine.event_log import (
    RUN_METADATA_FILENAME,
    run_dir,
    verify_finalized_event_log,
)

from .adapters import AuraCellExecutor
from .analysis import read_results_manifest
from .artifacts import (
    ArtifactSeal,
    atomic_create_bytes,
)
from .pilot_bundle import (
    MAX_PILOT_ARTIFACT_BYTES,
    ValidatedPilotBundle,
    _read_artifact_bytes,
    load_validated_pilot_bundle,
)
from .runner import CompletedResultValidator, collect_validated_results
from .spec import ResolvedMatrix, canonical_json, sha256_json

class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FreezeFileReference(_StrictModel):
    path: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EventLogEvidence(_StrictModel):
    event_count: int = Field(ge=0)
    final_seq: int = Field(ge=-1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PilotRunEvidence(_StrictModel):
    cell_id: str = Field(pattern=r"^cell-[0-9a-f]{32}$")
    run_id: str = Field(min_length=1)
    group_id: str = Field(min_length=1)
    variant: Literal["static", "dynamic"]
    result_seal: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    event_log: EventLogEvidence


class PilotRunInventoryPayload(_StrictModel):
    run_inventory_schema_version: Literal["1.0"] = "1.0"
    benchmark_id: str = Field(min_length=1)
    pair_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(min_length=1)
    results_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_cells: int = Field(gt=0)
    cells: list[PilotRunEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_cells(self) -> "PilotRunInventoryPayload":
        cell_ids = [cell.cell_id for cell in self.cells]
        run_ids = [cell.run_id for cell in self.cells]
        if cell_ids != sorted(cell_ids) or len(cell_ids) != len(set(cell_ids)):
            raise ValueError("run inventory cells must have unique sorted IDs")
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("run inventory run IDs must be unique")
        if len(self.cells) != self.expected_cells:
            raise ValueError("pilot freeze requires every expected cell")
        return self


class PilotRunInventory(_StrictModel):
    manifest: PilotRunInventoryPayload
    seal: ArtifactSeal

    @model_validator(mode="after")
    def _verify_seal(self) -> "PilotRunInventory":
        expected = sha256_json(self.manifest.model_dump(mode="json"))
        if self.seal.sha256 != expected:
            raise ValueError("run inventory seal does not match its contents")
        return self


class ReviewRunEvidence(_StrictModel):
    cell_id: str = Field(pattern=r"^cell-[0-9a-f]{32}$")
    run_id: str = Field(min_length=1)
    result_seal: str = Field(pattern=r"^[0-9a-f]{64}$")


class PairAssessment(_StrictModel):
    group_id: str = Field(min_length=1)
    intervention_realized: bool
    oracle_reasonable: bool
    only_declared_difference: bool
    tracespec_allows_reasonable_policies: bool
    rationale: str = Field(min_length=20, max_length=8_000)
    evidence: ReviewRunEvidence


class HumanReviewArtifact(_StrictModel):
    human_review_schema_version: Literal["1.1"] = "1.1"
    benchmark_id: str = Field(min_length=1)
    pair_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = Field(min_length=1)
    results_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str = Field(min_length=1, max_length=256)
    submitted_at: datetime
    assessments: list[PairAssessment] = Field(min_length=1)

    @field_validator("reviewer_id")
    @classmethod
    def _reviewer_id_is_not_whitespace(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("reviewer_id must not contain surrounding whitespace")
        return value

    @field_validator("submitted_at")
    @classmethod
    def _timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("submitted_at must include a timezone")
        return value

    @model_validator(mode="after")
    def _canonical_assessments(self) -> "HumanReviewArtifact":
        group_ids = [assessment.group_id for assessment in self.assessments]
        if group_ids != sorted(group_ids) or len(group_ids) != len(set(group_ids)):
            raise ValueError("review assessments must have unique sorted group IDs")
        return self


class PilotFreezePayload(_StrictModel):
    pilot_freeze_schema_version: Literal["1.0"] = "1.0"
    gate_status: Literal["approved", "needs_adjudication"]
    benchmark_manifest: FreezeFileReference
    resolved_matrix: FreezeFileReference
    results_manifest: FreezeFileReference
    run_inventory: FreezeFileReference
    reviews: list[FreezeFileReference] = Field(min_length=2, max_length=2)


class PilotFreezeArtifact(_StrictModel):
    manifest: PilotFreezePayload
    seal: ArtifactSeal

    @model_validator(mode="after")
    def _verify_seal(self) -> "PilotFreezeArtifact":
        expected = sha256_json(self.manifest.model_dump(mode="json"))
        if self.seal.sha256 != expected:
            raise ValueError("pilot freeze seal does not match its contents")
        return self


def _json_bytes(value: object) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _read_json(path: Path, model_type, *, expected_sha256: str | None = None):
    try:
        encoded = _read_artifact_bytes(path)
        if expected_sha256 is not None and (
            hashlib.sha256(encoded).hexdigest() != expected_sha256
        ):
            raise ValueError(f"freeze file reference drift: {path}")
        raw = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid pilot freeze artifact {path}: {exc}") from exc
    return model_type.model_validate(raw)


def _file_reference(root: Path, path: Path) -> FreezeFileReference:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"freeze artifact escapes bundle root: {path}")
    payload = _read_artifact_bytes(resolved)
    return FreezeFileReference(
        path=resolved.relative_to(root).as_posix(),
        bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _resolve_reference(root: Path, reference: FreezeFileReference) -> Path:
    path = (root / reference.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"invalid freeze file reference: {reference.path}")
    actual = _file_reference(root, path)
    if actual.bytes != reference.bytes or actual.sha256 != reference.sha256:
        raise ValueError(f"freeze file reference drift: {reference.path}")
    return path


def _matrix_facts(path: Path) -> tuple[ResolvedMatrix, str]:
    encoded = _read_artifact_bytes(path)
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid resolved matrix {path}: {exc}") from exc
    return ResolvedMatrix.model_validate(raw), hashlib.sha256(encoded).hexdigest()


def _results_manifest_facts(path: Path) -> tuple[dict[str, Any], str, str]:
    artifact, encoded = read_results_manifest(path)
    manifest = artifact["manifest"]
    return (
        manifest,
        artifact["seal"]["sha256"],
        hashlib.sha256(encoded).hexdigest(),
    )


def _validate_completed_pilot(
    *,
    matrix: ResolvedMatrix,
    pilot: ValidatedPilotBundle,
    results_manifest: Mapping[str, Any],
) -> None:
    benchmark = results_manifest["benchmark"]
    matrix_record = results_manifest["matrix"]
    validity = results_manifest["validity"]
    expected_scenarios = {
        scenario_id
        for pair in pilot.pairs
        for scenario_id in (pair.static_scenario_id, pair.dynamic_scenario_id)
    }
    if (
        matrix.matrix_id != pilot.benchmark_id
        or matrix.spec_hash != pilot.matrix_contract_hash
        or len(matrix.cells) != pilot.expected_cells
        or sorted({cell.seed for cell in matrix.cells}) != list(pilot.seeds)
        or {cell.scenario_id for cell in matrix.cells} != expected_scenarios
        or tuple(matrix.expected_observation_conditions) != pilot.observation_conditions
    ):
        raise ValueError("resolved matrix does not match pilot contract")
    if benchmark["benchmark_id"] != pilot.benchmark_id:
        raise ValueError("results manifest benchmark does not match pilot")
    if benchmark["manifest_sha256"] != pilot.manifest_sha256:
        raise ValueError("results manifest is not bound to this pilot manifest")
    if benchmark["pair_set_hash"] != pilot.pair_set_hash:
        raise ValueError("results manifest pair set does not match pilot")
    if matrix_record["matrix_hash"] != matrix.matrix_hash:
        raise ValueError("results manifest matrix hash does not match resolved matrix")
    if matrix_record["source_revision"] != matrix.source_revision:
        raise ValueError("results manifest source revision does not match matrix")
    if validity["completed"] != pilot.expected_cells:
        raise ValueError("pilot freeze requires every cell to be completed")
    for field in ("evaluation_error", "execution_failed", "invalid_artifacts"):
        if validity[field] != 0:
            raise ValueError(f"pilot freeze requires zero {field}")
    if any(cell["admission_status"] != "admitted" for cell in results_manifest["cells"]):
        raise ValueError("pilot freeze requires every cell to be admitted")
    if len(matrix.expected_runtime_profiles) > 1 and (
        validity["invalid_controller_groups"]
        or len(validity["valid_controller_cell_ids"]) != pilot.expected_cells
    ):
        raise ValueError("pilot freeze requires complete controller fairness evidence")
    if len(matrix.expected_observation_conditions) > 1 and (
        validity["invalid_observation_groups"]
        or len(validity["valid_observation_cell_ids"]) != pilot.expected_cells
    ):
        raise ValueError("pilot freeze requires complete observation fairness evidence")


def _validate_inventory_binding(
    *,
    inventory: PilotRunInventory,
    pilot: ValidatedPilotBundle,
    matrix: ResolvedMatrix,
    results_manifest: Mapping[str, Any],
    results_manifest_sha256: str,
) -> None:
    manifest = inventory.manifest
    if (
        manifest.benchmark_id != pilot.benchmark_id
        or manifest.pair_set_hash != pilot.pair_set_hash
        or manifest.matrix_hash != matrix.matrix_hash
        or manifest.source_revision != matrix.source_revision
        or manifest.expected_cells != pilot.expected_cells
        or manifest.results_manifest_sha256 != results_manifest_sha256
    ):
        raise ValueError("run inventory does not match frozen pilot")
    matrix_cell_ids = {cell.cell_id for cell in matrix.cells}
    inventory_by_id = {cell.cell_id: cell for cell in manifest.cells}
    results_by_id = {
        str(cell["cell_id"]): cell for cell in results_manifest["cells"]
    }
    if set(inventory_by_id) != matrix_cell_ids or set(results_by_id) != matrix_cell_ids:
        raise ValueError("run inventory cell set does not match frozen pilot")
    for cell_id in sorted(matrix_cell_ids):
        evidence = inventory_by_id[cell_id]
        result = results_by_id[cell_id]
        context = result["analysis_context"]
        if (
            evidence.run_id != result["run_id"]
            or evidence.result_seal != result["result_seal"]
            or evidence.group_id != context["counterfactual_group_id"]
            or evidence.variant != context["counterfactual_variant"]
        ):
            raise ValueError(f"run inventory does not match results for {cell_id}")


def _build_pilot_run_inventory(
    *,
    matrix: ResolvedMatrix,
    pilot: ValidatedPilotBundle,
    results: Mapping[str, Any],
    results_seal: str,
    result_root: Path,
    validator: CompletedResultValidator | None = None,
) -> PilotRunInventory:
    effective_validator = validator or AuraCellExecutor(data_root=result_root / "runs")
    collected = collect_validated_results(
        matrix,
        output_dir=result_root,
        validator=effective_validator,
    )
    if collected.completed != pilot.expected_cells or (
        collected.evaluation_error
        or collected.execution_failed
        or collected.invalid_artifacts
    ):
        raise ValueError("raw pilot results do not pass completed-evidence validation")

    cells_by_id = {cell["cell_id"]: cell for cell in results["cells"]}
    run_root = result_root / "runs"
    entries: list[PilotRunEvidence] = []
    for cell in matrix.cells:
        output_record = collected.completed_outputs[cell.cell_id]
        result_artifact = collected.completed_artifacts[cell.cell_id]
        run_id = str(output_record["run_id"])
        metadata_path = run_dir(run_id, root=run_root) / RUN_METADATA_FILENAME
        metadata_bytes = _read_artifact_bytes(metadata_path)
        try:
            metadata = json.loads(metadata_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid run metadata for {run_id}: {exc}") from exc
        if not isinstance(metadata, dict):
            raise ValueError(f"run metadata must be an object: {run_id}")
        event_log = verify_finalized_event_log(run_id, metadata=metadata, root=run_root)
        projected = cells_by_id[cell.cell_id]
        if projected["run_id"] != run_id or (
            projected["result_seal"] != result_artifact.seal.sha256
        ):
            raise ValueError(
                f"raw result does not match results manifest for {cell.cell_id}"
            )
        context = projected["analysis_context"]
        entries.append(
            PilotRunEvidence(
                cell_id=cell.cell_id,
                run_id=run_id,
                group_id=context["counterfactual_group_id"],
                variant=context["counterfactual_variant"],
                result_seal=result_artifact.seal.sha256,
                run_metadata_sha256=hashlib.sha256(metadata_bytes).hexdigest(),
                event_log=EventLogEvidence.model_validate(event_log),
            )
        )
    entries.sort(key=lambda item: item.cell_id)
    payload = PilotRunInventoryPayload(
        benchmark_id=pilot.benchmark_id,
        pair_set_hash=pilot.pair_set_hash,
        matrix_hash=matrix.matrix_hash,
        source_revision=matrix.source_revision,
        results_manifest_sha256=results_seal,
        expected_cells=pilot.expected_cells,
        cells=entries,
    )
    return PilotRunInventory(
        manifest=payload,
        seal=ArtifactSeal(sha256=sha256_json(payload.model_dump(mode="json"))),
    )


def write_pilot_run_inventory(
    *,
    resolved_matrix: Path | str,
    result_root: Path | str,
    benchmark_manifest: Path | str,
    results_manifest: Path | str,
    output: Path | str,
    validator: CompletedResultValidator | None = None,
) -> Path:
    """Verify all raw cell/run evidence and write its compact immutable inventory."""

    matrix, _ = _matrix_facts(Path(resolved_matrix))
    pilot = load_validated_pilot_bundle(benchmark_manifest)
    results, results_seal, _ = _results_manifest_facts(Path(results_manifest))
    _validate_completed_pilot(matrix=matrix, pilot=pilot, results_manifest=results)
    artifact = _build_pilot_run_inventory(
        matrix=matrix,
        pilot=pilot,
        results=results,
        results_seal=results_seal,
        result_root=Path(result_root),
        validator=validator,
    )
    return atomic_create_bytes(
        output,
        _json_bytes(artifact.model_dump(mode="json")),
        max_bytes=MAX_PILOT_ARTIFACT_BYTES,
    )


def _validate_reviews(
    *,
    pilot: ValidatedPilotBundle,
    matrix: ResolvedMatrix,
    results_manifest_sha256: str,
    inventory: PilotRunInventory,
    reviews: list[HumanReviewArtifact],
) -> Literal["approved", "needs_adjudication"]:
    reviewer_ids = {
        unicodedata.normalize("NFKC", review.reviewer_id).casefold()
        for review in reviews
    }
    if len(reviews) != 2 or len(reviewer_ids) != 2:
        raise ValueError("pilot freeze requires two distinct reviewers")
    expected_groups = {pair.group_id for pair in pilot.pairs}
    evidence_by_cell = {cell.cell_id: cell for cell in inventory.manifest.cells}
    all_true = True
    for review in reviews:
        bindings = (
            review.benchmark_id == pilot.benchmark_id,
            review.pair_set_hash == pilot.pair_set_hash,
            review.matrix_hash == matrix.matrix_hash,
            review.source_revision == matrix.source_revision,
            review.results_manifest_sha256 == results_manifest_sha256,
            review.run_inventory_sha256 == inventory.seal.sha256,
        )
        if not all(bindings):
            raise ValueError(f"review {review.reviewer_id!r} does not match frozen evidence")
        if {item.group_id for item in review.assessments} != expected_groups:
            raise ValueError(f"review {review.reviewer_id!r} does not cover every pair")
        for assessment in review.assessments:
            evidence = evidence_by_cell.get(assessment.evidence.cell_id)
            if evidence is None or (
                evidence.run_id != assessment.evidence.run_id
                or evidence.result_seal != assessment.evidence.result_seal
                or evidence.group_id != assessment.group_id
                or evidence.variant != "dynamic"
            ):
                raise ValueError(
                    f"review evidence does not match a frozen dynamic run: {assessment.group_id}"
                )
            decisions = (
                assessment.intervention_realized,
                assessment.oracle_reasonable,
                assessment.only_declared_difference,
                assessment.tracespec_allows_reasonable_policies,
            )
            all_true = all_true and all(decisions)
    return "approved" if all_true else "needs_adjudication"


def write_pilot_freeze(
    *,
    bundle_root: Path | str,
    result_root: Path | str,
    benchmark_manifest: Path | str,
    resolved_matrix: Path | str,
    results_manifest: Path | str,
    run_inventory: Path | str,
    review_artifacts: list[Path | str],
    output: Path | str,
    validator: CompletedResultValidator | None = None,
) -> Path:
    """Bind completed results and exactly two independent reviews into one root seal."""

    root = Path(bundle_root).resolve()
    output_path = Path(output).resolve()
    if output_path.parent != root:
        raise ValueError("pilot freeze must be written at the bundle root")
    benchmark_path = Path(benchmark_manifest).resolve()
    matrix_path = Path(resolved_matrix).resolve()
    results_path = Path(results_manifest).resolve()
    inventory_path = Path(run_inventory).resolve()
    review_paths = sorted(
        (Path(path).resolve() for path in review_artifacts),
        key=lambda path: path.as_posix(),
    )
    if len(review_paths) != 2:
        raise ValueError("pilot freeze requires exactly two review artifacts")
    references = [
        _file_reference(root, path)
        for path in (
            benchmark_path,
            matrix_path,
            results_path,
            inventory_path,
            *review_paths,
        )
    ]
    pilot = load_validated_pilot_bundle(benchmark_path)
    if pilot.manifest_sha256 != references[0].sha256:
        raise ValueError("benchmark manifest changed while freezing")
    matrix, matrix_file_sha256 = _matrix_facts(matrix_path)
    if matrix_file_sha256 != references[1].sha256:
        raise ValueError("resolved matrix changed while freezing")
    results, results_seal, results_file_sha256 = _results_manifest_facts(results_path)
    if results_file_sha256 != references[2].sha256:
        raise ValueError("results manifest changed while freezing")
    _validate_completed_pilot(matrix=matrix, pilot=pilot, results_manifest=results)
    inventory = _read_json(
        inventory_path,
        PilotRunInventory,
        expected_sha256=references[3].sha256,
    )
    _validate_inventory_binding(
        inventory=inventory,
        pilot=pilot,
        matrix=matrix,
        results_manifest=results,
        results_manifest_sha256=results_seal,
    )
    rebuilt_inventory = _build_pilot_run_inventory(
        matrix=matrix,
        pilot=pilot,
        results=results,
        results_seal=results_seal,
        result_root=Path(result_root),
        validator=validator,
    )
    if rebuilt_inventory != inventory:
        raise ValueError("raw pilot evidence does not match run inventory")
    reviews = [
        _read_json(path, HumanReviewArtifact, expected_sha256=reference.sha256)
        for path, reference in zip(review_paths, references[4:], strict=True)
    ]
    gate_status = _validate_reviews(
        pilot=pilot,
        matrix=matrix,
        results_manifest_sha256=results_seal,
        inventory=inventory,
        reviews=reviews,
    )
    payload = PilotFreezePayload(
        gate_status=gate_status,
        benchmark_manifest=references[0],
        resolved_matrix=references[1],
        results_manifest=references[2],
        run_inventory=references[3],
        reviews=references[4:],
    )
    artifact = PilotFreezeArtifact(
        manifest=payload,
        seal=ArtifactSeal(sha256=sha256_json(payload.model_dump(mode="json"))),
    )
    return atomic_create_bytes(
        output_path,
        _json_bytes(artifact.model_dump(mode="json")),
        max_bytes=MAX_PILOT_ARTIFACT_BYTES,
    )


def validate_pilot_freeze(
    path: Path | str,
    *,
    result_root: Path | str,
    validator: CompletedResultValidator | None = None,
) -> dict[str, object]:
    freeze_path = Path(path).resolve()
    root = freeze_path.parent
    artifact = _read_json(freeze_path, PilotFreezeArtifact)
    manifest = artifact.manifest
    benchmark_path = _resolve_reference(root, manifest.benchmark_manifest)
    matrix_path = _resolve_reference(root, manifest.resolved_matrix)
    results_path = _resolve_reference(root, manifest.results_manifest)
    inventory_path = _resolve_reference(root, manifest.run_inventory)
    review_paths = [_resolve_reference(root, item) for item in manifest.reviews]
    pilot = load_validated_pilot_bundle(benchmark_path)
    if pilot.manifest_sha256 != manifest.benchmark_manifest.sha256:
        raise ValueError("benchmark manifest snapshot does not match freeze")
    matrix, matrix_file_sha256 = _matrix_facts(matrix_path)
    if matrix_file_sha256 != manifest.resolved_matrix.sha256:
        raise ValueError("resolved matrix snapshot does not match freeze")
    results, results_seal, results_file_sha256 = _results_manifest_facts(results_path)
    if results_file_sha256 != manifest.results_manifest.sha256:
        raise ValueError("results manifest snapshot does not match freeze")
    _validate_completed_pilot(matrix=matrix, pilot=pilot, results_manifest=results)
    inventory = _read_json(
        inventory_path,
        PilotRunInventory,
        expected_sha256=manifest.run_inventory.sha256,
    )
    _validate_inventory_binding(
        inventory=inventory,
        pilot=pilot,
        matrix=matrix,
        results_manifest=results,
        results_manifest_sha256=results_seal,
    )
    rebuilt_inventory = _build_pilot_run_inventory(
        matrix=matrix,
        pilot=pilot,
        results=results,
        results_seal=results_seal,
        result_root=Path(result_root),
        validator=validator,
    )
    if rebuilt_inventory != inventory:
        raise ValueError("raw pilot evidence does not match run inventory")
    reviews = [
        _read_json(path, HumanReviewArtifact, expected_sha256=reference.sha256)
        for path, reference in zip(review_paths, manifest.reviews, strict=True)
    ]
    actual_status = _validate_reviews(
        pilot=pilot,
        matrix=matrix,
        results_manifest_sha256=results_seal,
        inventory=inventory,
        reviews=reviews,
    )
    if manifest.gate_status != actual_status:
        raise ValueError("pilot freeze gate does not match its reviews")
    return {
        "benchmark_id": pilot.benchmark_id,
        "cells": len(inventory.manifest.cells),
        "source_revision": matrix.source_revision,
        "gate_status": actual_status,
        "freeze_sha256": artifact.seal.sha256,
    }


__all__ = [
    "HumanReviewArtifact",
    "PairAssessment",
    "PilotFreezeArtifact",
    "PilotRunInventory",
    "ReviewRunEvidence",
    "validate_pilot_freeze",
    "write_pilot_freeze",
    "write_pilot_run_inventory",
]
