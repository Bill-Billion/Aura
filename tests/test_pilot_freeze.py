from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from backend.engine.event_log import RunArtifactError
from backend.experiments.analysis import AnalysisPlan, build_results_manifest, write_results_manifest
from backend.experiments.artifacts import (
    CellResultArtifact,
    read_resolved_matrix,
    write_cell_result,
    write_resolved_matrix,
)
from backend.experiments.fairness import build_fairness_payload
from backend.experiments.pilot_bundle import load_validated_pilot_bundle
from backend.experiments.pilot_freeze import (
    HumanReviewArtifact,
    PairAssessment,
    ReviewRunEvidence,
    validate_pilot_freeze,
    write_pilot_freeze,
    write_pilot_run_inventory,
)
from backend.experiments.resolve import (
    FileOrLibraryScenarioResolver,
    load_matrix_file,
    resolve_matrix,
)
from backend.experiments.spec import sha256_json


PILOT_ROOT = Path("benchmarks/aurabench-dev")


def _evaluation(*, passed: bool, source_revision: str) -> dict[str, object]:
    def datum(name: str, value: object, unit: str) -> dict[str, object]:
        return {"name": name, "value": value, "unit": unit, "details": {}}

    return {
        "report_schema_version": "1.0",
        "outcome": "pass" if passed else "fail",
        "metrics": {
            "episode_complete": datum("episode_complete", True, "boolean"),
            "first_action_latency_ms": datum("first_action_latency_ms", 10.0, "ms"),
            "command_failure_count": datum("command_failure_count", 0.0, "count"),
            "fallback_count": datum("fallback_count", 0.0, "count"),
            "conflict_count": datum("conflict_count", 0.0, "count"),
            "user_intent_satisfied": datum("user_intent_satisfied", passed, "boolean"),
            "device_state_match_rate": datum("device_state_match_rate", 1.0, "ratio"),
        },
        "criteria_checks": {"trajectory_safe_success": passed},
        "failed_metrics": [] if passed else ["trajectory_safe_success"],
        "final_state_success": True,
        "trajectory_properties_satisfied": passed,
        "trajectory_safe_success": passed,
        "provenance": {"evaluator_source_revision": source_revision},
    }


def _metadata(cell) -> dict[str, object]:
    return {
        "source_revision": cell.source_revision,
        "sim_version": "test",
        "agent_versions": {
            agent_id: "test"
            for agent_id in (
                "lighting_agent",
                "hvac_agent",
                "security_agent",
                "energy_agent",
                "scene_agent",
            )
        },
        "llm_provider": "rule_based",
        "llm_model": "rule_based",
        "llm_mode": "rule_based",
        "baseline_policy": "rule_based",
        "duration_seconds": 60.0,
        "initial_state_hash": hashlib.sha256(
            f"{cell.scenario_id}:{cell.seed}".encode()
        ).hexdigest(),
        "scenario_schema_version": "2.1",
        "event_schema_version": "1.0",
        "command_schema_version": "1.0",
        "device_registry_version": "1.0",
        "trace_spec_hash": "d" * 64,
    }


class _Validator:
    @staticmethod
    def validate_completed(cell, output, *, matrix_hash):
        return output.get("run_id") is not None


def _write_review(
    path: Path,
    *,
    reviewer_id: str,
    pilot,
    matrix,
    results_seal: str,
    inventory,
) -> None:
    dynamic = {}
    for cell in inventory.manifest.cells:
        if cell.variant == "dynamic":
            dynamic.setdefault(cell.group_id, cell)
    review = HumanReviewArtifact(
        benchmark_id=pilot.benchmark_id,
        pair_set_hash=pilot.pair_set_hash,
        matrix_hash=matrix.matrix_hash,
        source_revision=matrix.source_revision,
        results_manifest_sha256=results_seal,
        run_inventory_sha256=inventory.seal.sha256,
        reviewer_id=reviewer_id,
        submitted_at="2026-08-25T12:00:00+08:00",
        assessments=[
            PairAssessment(
                group_id=group_id,
                intervention_realized=True,
                oracle_reasonable=True,
                only_declared_difference=True,
                tracespec_allows_reasonable_policies=True,
                rationale=f"Independent review of the sealed dynamic evidence for {group_id}.",
                evidence=ReviewRunEvidence(
                    cell_id=dynamic[group_id].cell_id,
                    run_id=dynamic[group_id].run_id,
                    result_seal=dynamic[group_id].result_seal,
                ),
            )
            for group_id in sorted(dynamic)
        ],
    )
    path.write_text(
        json.dumps(review.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )


def _frozen_pilot(tmp_path: Path):
    bundle = tmp_path / "aurabench-dev"
    shutil.copytree(PILOT_ROOT, bundle)
    frozen = bundle / "freeze"
    frozen.mkdir()
    matrix = resolve_matrix(
        load_matrix_file(bundle / "matrix.yaml"),
        scenario_resolver=FileOrLibraryScenarioResolver(base_dir=bundle),
        source_revision="test-freeze-revision",
    )
    resolved_path = write_resolved_matrix(frozen, matrix)
    result_root = tmp_path / "raw-results"
    pilot = load_validated_pilot_bundle(bundle / "manifest.json")
    contexts = {
        pair.static_scenario_id: (pair.group_id, "static") for pair in pilot.pairs
    } | {
        pair.dynamic_scenario_id: (pair.group_id, "dynamic") for pair in pilot.pairs
    }
    empty_hash = hashlib.sha256(b"").hexdigest()
    for index, cell in enumerate(matrix.cells):
        group_id, variant = contexts[cell.scenario_id]
        run_id = f"run-20260825T000000-{index:08x}"
        evaluation = _evaluation(
            passed=variant == "static",
            source_revision=matrix.source_revision,
        )
        output = {
            "run_id": run_id,
            "analysis_context": {
                "counterfactual_group_id": group_id,
                "counterfactual_variant": variant,
                "scenario_category": "pilot",
            },
            "fairness": build_fairness_payload(
                cell,
                run_metadata=_metadata(cell),
                evaluation=evaluation,
            ),
            "evaluation": evaluation,
        }
        write_cell_result(
            result_root,
            CellResultArtifact.completed(
                cell=cell,
                matrix_hash=matrix.matrix_hash,
                output=output,
            ),
        )
        directory = result_root / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "events.jsonl").write_bytes(b"")
        (directory / "run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "ended_at": "2026-08-25T00:01:00+00:00",
                    "events_integrity": {
                        "event_count": 0,
                        "final_seq": -1,
                        "sha256": empty_hash,
                    },
                }
            ),
            encoding="utf-8",
        )

    results = build_results_manifest(
        matrix,
        result_root=result_root,
        validator=_Validator(),
        benchmark_manifest=bundle / "manifest.json",
        analysis_plan=AnalysisPlan(bootstrap_resamples=5),
    )
    results_path = write_results_manifest(frozen / "results-manifest.json", results)
    inventory_path = write_pilot_run_inventory(
        resolved_matrix=resolved_path,
        result_root=result_root,
        benchmark_manifest=bundle / "manifest.json",
        results_manifest=results_path,
        output=frozen / "run-inventory.json",
        validator=_Validator(),
    )
    from backend.experiments.pilot_freeze import PilotRunInventory

    inventory = PilotRunInventory.model_validate(
        json.loads(inventory_path.read_text(encoding="utf-8"))
    )
    results_seal = results["seal"]["sha256"]
    first = bundle / "reviews" / "reviewer-one.json"
    second = bundle / "reviews" / "reviewer-two.json"
    _write_review(
        first,
        reviewer_id="reviewer-one",
        pilot=pilot,
        matrix=matrix,
        results_seal=results_seal,
        inventory=inventory,
    )
    _write_review(
        second,
        reviewer_id="reviewer-two",
        pilot=pilot,
        matrix=matrix,
        results_seal=results_seal,
        inventory=inventory,
    )
    return bundle, resolved_path, results_path, inventory_path, first, second


def test_completed_pilot_freeze_binds_all_runs_and_two_distinct_reviews(tmp_path) -> None:
    bundle, resolved, results, inventory, first, second = _frozen_pilot(tmp_path)
    output = bundle / "freeze.json"
    write_pilot_freeze(
        bundle_root=bundle,
        result_root=tmp_path / "raw-results",
        benchmark_manifest=bundle / "manifest.json",
        resolved_matrix=resolved,
        results_manifest=results,
        run_inventory=inventory,
        review_artifacts=[second, first],
        output=output,
        validator=_Validator(),
    )
    reversed_bytes = output.read_bytes()
    output.unlink()
    write_pilot_freeze(
        bundle_root=bundle,
        result_root=tmp_path / "raw-results",
        benchmark_manifest=bundle / "manifest.json",
        resolved_matrix=resolved,
        results_manifest=results,
        run_inventory=inventory,
        review_artifacts=[first, second],
        output=output,
        validator=_Validator(),
    )
    assert output.read_bytes() == reversed_bytes
    validated = validate_pilot_freeze(
        output,
        result_root=tmp_path / "raw-results",
        validator=_Validator(),
    )
    assert validated["cells"] == 96
    assert validated["gate_status"] == "approved"
    assert validated["source_revision"] == "test-freeze-revision"

    next((tmp_path / "raw-results" / "runs").glob("*/events.jsonl")).unlink()
    with pytest.raises(RunArtifactError, match="events.jsonl"):
        validate_pilot_freeze(
            output,
            result_root=tmp_path / "raw-results",
            validator=_Validator(),
        )


def test_pilot_freeze_rejects_duplicate_reviewer_identity(tmp_path) -> None:
    bundle, resolved, results, inventory, first, second = _frozen_pilot(tmp_path)
    duplicate = json.loads(second.read_text(encoding="utf-8"))
    duplicate["reviewer_id"] = "REVIEWER-ONE"
    second.write_text(json.dumps(duplicate), encoding="utf-8")

    with pytest.raises(ValueError, match="two distinct reviewers"):
        write_pilot_freeze(
            bundle_root=bundle,
            result_root=tmp_path / "raw-results",
            benchmark_manifest=bundle / "manifest.json",
            resolved_matrix=resolved,
            results_manifest=results,
            run_inventory=inventory,
            review_artifacts=[first, second],
            output=bundle / "freeze.json",
            validator=_Validator(),
        )


def test_run_inventory_rejects_results_from_a_different_valid_run(tmp_path) -> None:
    bundle, resolved, results, _, _, _ = _frozen_pilot(tmp_path)
    matrix = read_resolved_matrix(resolved)
    first, second = matrix.cells[:2]
    result_path = tmp_path / "raw-results" / "cells" / first.cell_id / "result.json"
    artifact = json.loads(result_path.read_text(encoding="utf-8"))
    artifact["result"]["output"]["run_id"] = (
        f"run-20260825T000000-{matrix.cells.index(second):08x}"
    )
    artifact["seal"]["sha256"] = sha256_json(artifact["result"])
    result_path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match results manifest"):
        write_pilot_run_inventory(
            resolved_matrix=resolved,
            result_root=tmp_path / "raw-results",
            benchmark_manifest=bundle / "manifest.json",
            results_manifest=results,
            output=bundle / "freeze" / "changed-inventory.json",
            validator=_Validator(),
        )


def test_freeze_rejects_resealed_inventory_with_swapped_cell_evidence(tmp_path) -> None:
    bundle, resolved, results, inventory, first, second = _frozen_pilot(tmp_path)
    artifact = json.loads(inventory.read_text(encoding="utf-8"))
    static = [
        cell
        for cell in artifact["manifest"]["cells"]
        if cell["variant"] == "static"
    ][:2]
    evidence_fields = ("run_id", "result_seal", "run_metadata_sha256", "event_log")
    left = {field: static[0][field] for field in evidence_fields}
    for field in evidence_fields:
        static[0][field] = static[1][field]
        static[1][field] = left[field]
    artifact["seal"]["sha256"] = sha256_json(artifact["manifest"])
    inventory.write_text(json.dumps(artifact), encoding="utf-8")
    for review_path in (first, second):
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review["run_inventory_sha256"] = artifact["seal"]["sha256"]
        review_path.write_text(json.dumps(review), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match results"):
        write_pilot_freeze(
            bundle_root=bundle,
            result_root=tmp_path / "raw-results",
            benchmark_manifest=bundle / "manifest.json",
            resolved_matrix=resolved,
            results_manifest=results,
            run_inventory=inventory,
            review_artifacts=[first, second],
            output=bundle / "freeze.json",
            validator=_Validator(),
        )
