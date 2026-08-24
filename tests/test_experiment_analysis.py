from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.experiments.analysis import (
    AnalysisPlan,
    build_results_manifest,
    read_results_manifest,
    render_analysis_bundle,
    write_results_manifest,
)
from backend.experiments.artifacts import CellResultArtifact, write_cell_result
from backend.experiments.cli import main
from backend.experiments.pilot_bundle import load_validated_pilot_bundle
from backend.experiments.resolve import (
    FileOrLibraryScenarioResolver,
    load_matrix_file,
    resolve_matrix,
)
from backend.experiments.spec import sha256_json


PILOT_MANIFEST = Path("benchmarks/aurabench-dev/manifest.json")


def _evaluation(*, trajectory_safe: bool) -> dict[str, object]:
    final_state = True
    trajectory = trajectory_safe

    def datum(name: str, value: object, unit: str) -> dict[str, object]:
        return {"name": name, "value": value, "unit": unit, "details": {}}

    return {
        "outcome": "pass" if trajectory_safe else "fail",
        "metrics": {
            "episode_complete": datum("episode_complete", True, "boolean"),
            "first_action_latency_ms": datum(
                "first_action_latency_ms", 10.0 if trajectory_safe else 20.0, "ms"
            ),
            "command_failure_count": datum("command_failure_count", 0.0, "count"),
            "fallback_count": datum("fallback_count", 0.0, "count"),
            "conflict_count": datum("conflict_count", 0.0, "count"),
            "user_intent_satisfied": datum(
                "user_intent_satisfied", trajectory_safe, "boolean"
            ),
            "device_state_match_rate": datum(
                "device_state_match_rate", 1.0, "ratio"
            ),
        },
        "criteria_checks": {"trajectory_safe_success": trajectory_safe},
        "failed_metrics": [] if trajectory_safe else ["trajectory_safe_success"],
        "final_state_success": final_state,
        "trajectory_properties_satisfied": trajectory,
        "trajectory_safe_success": trajectory_safe,
    }


def _cell(
    *,
    cell_id: str,
    scenario_id: str,
    variant: str,
    trajectory_safe: bool,
) -> dict[str, object]:
    return {
        "cell_id": cell_id,
        "scenario_id": scenario_id,
        "scenario_contract_hash": "a" * 64,
        "seed": 7,
        "model": "mocked",
        "runtime_profile": "aura",
        "topology": "domain_multi",
        "governance": "aura",
        "observation": "stale_offline",
        "repetition": 0,
        "source_revision": "test",
        "admission_status": "admitted",
        "fairness_group_id": None,
        "result_seal": "b" * 64,
        "run_id": f"run-{variant}",
        "analysis_context": {
            "counterfactual_group_id": "pair-1",
            "counterfactual_variant": variant,
            "scenario_category": "test",
        },
        "evaluation": {
            "outcome": "pass" if trajectory_safe else "fail",
            "binary": {
                "episode_complete": True,
                "final_state_success": True,
                "trajectory_properties_satisfied": trajectory_safe,
                "trajectory_safe_success": trajectory_safe,
                "user_intent_satisfied": trajectory_safe,
                "final_state_blind_spot": not trajectory_safe,
            },
            "continuous": {
                "first_action_latency_ms": 10.0 if trajectory_safe else 20.0,
                "command_failure_count": 0.0,
                "fallback_count": 0.0,
                "conflict_count": 0.0,
                "device_state_match_rate": 1.0,
            },
            "failed_metrics": (
                [] if trajectory_safe else ["trajectory_safe_success"]
            ),
            "criteria_checks": {
                "trajectory_safe_success": trajectory_safe
            },
        },
    }


def _results_artifact(*, root_seed: int = 17) -> dict[str, object]:
    manifest = {
        "results_manifest_schema_version": "1.0",
        "analysis_plan": AnalysisPlan(
            bootstrap_resamples=25,
            bootstrap_root_seed=root_seed,
        ).model_dump(mode="json"),
        "benchmark": {
            "benchmark_id": "test_benchmark",
            "manifest_sha256": "c" * 64,
            "pair_set_hash": "d" * 64,
            "human_review_status": "pending",
            "seeds": [7],
            "pairs": [
                {
                    "group_id": "pair-1",
                    "pair_fingerprint": "e" * 64,
                    "static_scenario_id": "scenario-static",
                    "dynamic_scenario_id": "scenario-dynamic",
                }
            ],
        },
        "matrix": {
            "matrix_id": "test_benchmark",
            "matrix_hash": "f" * 64,
            "source_revision": "test",
            "spec_hash": "0" * 64,
            "planned_cells": 2,
            "expected_runtime_profiles": ["aura"],
        },
        "validity": {
            "completed": 2,
            "benchmark_pass": 1,
            "benchmark_fail": 1,
            "evaluation_error": 0,
            "execution_failed": 0,
            "invalid_artifacts": 0,
            "failed_cell_ids": [],
            "valid_fairness_group_ids": [],
            "valid_fairness_cell_ids": [],
            "invalid_fairness_groups": {},
        },
        "cells": sorted(
            [
                _cell(
                    cell_id="cell-00000000000000000000000000000001",
                    scenario_id="scenario-static",
                    variant="static",
                    trajectory_safe=True,
                ),
                _cell(
                    cell_id="cell-00000000000000000000000000000002",
                    scenario_id="scenario-dynamic",
                    variant="dynamic",
                    trajectory_safe=False,
                ),
            ],
            key=lambda item: item["cell_id"],
        ),
    }
    return {
        "manifest": manifest,
        "seal": {"algorithm": "sha256", "sha256": sha256_json(manifest)},
    }


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_manifest_only_rebuild_is_byte_identical_and_records_pending_review(
    tmp_path,
) -> None:
    source = tmp_path / "source.json"
    write_results_manifest(source, _results_artifact())
    first = tmp_path / "first"
    second = tmp_path / "second"

    summary = render_analysis_bundle(source, output_dir=first)
    render_analysis_bundle(source, output_dir=second)

    assert summary["human_review_status"] == "pending"
    assert _tree_bytes(first) == _tree_bytes(second)
    aggregate = json.loads((first / "aggregate-results.json").read_text())
    safe = next(
        item
        for item in aggregate["results"]
        if item["metric"] == "trajectory_safe_success"
    )
    assert safe["statistics"]["risk_difference"] == -1.0
    blind_spot = next(
        item
        for item in aggregate["results"]
        if item["metric"] == "final_state_blind_spot"
    )
    assert blind_spot["statistics"]["risk_difference"] == 1.0
    assert (first / "artifact-manifest.json").is_file()


def test_results_manifest_seal_and_create_only_outputs_fail_closed(tmp_path) -> None:
    source = tmp_path / "source.json"
    artifact = _results_artifact()
    write_results_manifest(source, artifact)
    output = tmp_path / "analysis"
    render_analysis_bundle(source, output_dir=output)

    changed = tmp_path / "changed.json"
    write_results_manifest(changed, _results_artifact(root_seed=18))
    with pytest.raises(ValueError, match="different contents"):
        render_analysis_bundle(changed, output_dir=output)

    tampered = json.loads(source.read_text())
    tampered["manifest"]["matrix"]["matrix_hash"] = "1" * 64
    source.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="seal does not match"):
        read_results_manifest(source)

    inconsistent = _results_artifact()
    inconsistent_manifest = inconsistent["manifest"]
    inconsistent_manifest["cells"][0]["evaluation"]["binary"][
        "trajectory_safe_success"
    ] = False
    inconsistent["seal"]["sha256"] = sha256_json(inconsistent_manifest)
    inconsistent_path = tmp_path / "inconsistent.json"
    with pytest.raises(ValueError, match="inconsistent safe success"):
        write_results_manifest(inconsistent_path, inconsistent)

    malformed_fairness = _results_artifact()
    malformed_manifest = malformed_fairness["manifest"]
    malformed_manifest["validity"]["valid_fairness_cell_ids"] = "cell-not-a-list"
    malformed_fairness["seal"]["sha256"] = sha256_json(malformed_manifest)
    with pytest.raises(ValueError, match="valid_fairness_cell_ids"):
        write_results_manifest(tmp_path / "malformed-fairness.json", malformed_fairness)


def test_cli_manifest_only_mode_rebuilds_without_raw_runs(tmp_path, capsys) -> None:
    source = tmp_path / "source.json"
    write_results_manifest(source, _results_artifact())
    output = tmp_path / "analysis"

    assert main(
        [
            "analyze",
            "--results-manifest",
            str(source),
            "--output",
            str(output),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["artifacts"] == 10

    assert main(["analyze", "--output", str(tmp_path / "missing")]) == 1
    assert "raw analysis mode requires" in capsys.readouterr().err


def test_analysis_output_rejects_symlinked_subdirectory(tmp_path) -> None:
    source = tmp_path / "source.json"
    write_results_manifest(source, _results_artifact())
    output = tmp_path / "analysis"
    output.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (output / "figure-data").symlink_to(victim, target_is_directory=True)

    with pytest.raises(OSError):
        render_analysis_bundle(source, output_dir=output)
    assert not (victim / "effect-estimates.json").exists()


def test_blind_spot_wilson_denominators_are_independent_by_arm(tmp_path) -> None:
    artifact = _results_artifact()
    manifest = artifact["manifest"]
    static_binary = manifest["cells"][0]["evaluation"]["binary"]
    static_binary.update(
        final_state_success=False,
        trajectory_properties_satisfied=True,
        trajectory_safe_success=False,
        final_state_blind_spot=None,
    )
    artifact["seal"]["sha256"] = sha256_json(manifest)
    source = tmp_path / "source.json"
    write_results_manifest(source, artifact)
    output = tmp_path / "analysis"
    render_analysis_bundle(source, output_dir=output)

    aggregate = json.loads((output / "aggregate-results.json").read_text())
    blind_spot = next(
        item
        for item in aggregate["results"]
        if item["metric"] == "final_state_blind_spot"
    )
    assert blind_spot["n"] == 0
    assert blind_spot["invalid"] == 1
    assert blind_spot["treatment_proportion"]["total"] == 1
    assert blind_spot["treatment_proportion"]["successes"] == 1
    assert blind_spot["reference_proportion"]["status"] == "unevaluable"


def test_counterfactual_analysis_excludes_aura_when_baseline_group_is_invalid(
    tmp_path,
) -> None:
    artifact = _results_artifact()
    manifest = artifact["manifest"]
    invalid_groups = {}
    for aura_cell in manifest["cells"]:
        fairness_group = "group-" + sha256_json(
            {
                "scenario_id": aura_cell["scenario_id"],
                "scenario_contract_hash": aura_cell["scenario_contract_hash"],
                "seed": aura_cell["seed"],
                "model": aura_cell["model"],
                "observation": aura_cell["observation"],
                "repetition": aura_cell["repetition"],
                "source_revision": aura_cell["source_revision"],
            }
        )[:32]
        aura_cell["fairness_group_id"] = fairness_group
        invalid_groups[fairness_group] = [
            "no_arbiter: missing completed evidence"
        ]
    baseline_cells = []
    for index, aura_cell in enumerate(manifest["cells"], start=3):
        baseline = dict(aura_cell)
        baseline.update(
            cell_id=f"cell-{index:032x}",
            runtime_profile="no_arbiter",
            governance="none",
            admission_status="not_admitted",
            fairness_group_id=None,
            result_seal=None,
            run_id=None,
            analysis_context=None,
            evaluation=None,
        )
        baseline_cells.append(baseline)
    manifest["cells"] = sorted(
        [*manifest["cells"], *baseline_cells], key=lambda cell: cell["cell_id"]
    )
    manifest["matrix"]["planned_cells"] = 4
    manifest["matrix"]["expected_runtime_profiles"] = ["aura", "no_arbiter"]
    manifest["validity"]["invalid_fairness_groups"] = invalid_groups
    artifact["seal"]["sha256"] = sha256_json(manifest)
    source = tmp_path / "source.json"
    write_results_manifest(source, artifact)
    output = tmp_path / "analysis"
    render_analysis_bundle(source, output_dir=output)

    aggregate = json.loads((output / "aggregate-results.json").read_text())
    aura_safe = next(
        item
        for item in aggregate["results"]
        if item["comparison_type"] == "counterfactual"
        and item["comparison_id"] == "aura"
        and item["metric"] == "trajectory_safe_success"
    )
    assert aura_safe["n"] == 0
    assert aura_safe["invalid_reasons"] == {"invalid_fairness_group": 1}


def test_analysis_stratifies_counterfactuals_and_pairs_observation_direction(
    tmp_path,
) -> None:
    artifact = _results_artifact()
    manifest = artifact["manifest"]
    manifest["results_manifest_schema_version"] = "1.1"
    perfect_cells = []
    observation_groups: dict[str, list[str]] = {}
    for index, stale_cell in enumerate(manifest["cells"], start=3):
        perfect = json.loads(json.dumps(stale_cell))
        perfect.update(
            cell_id=f"cell-{index:032x}",
            observation="perfect",
            run_id=f"{stale_cell['run_id']}-perfect",
        )
        group_id = "observation-group-" + sha256_json(
            {
                "scenario_id": stale_cell["scenario_id"],
                "scenario_contract_hash": stale_cell["scenario_contract_hash"],
                "seed": stale_cell["seed"],
                "model": stale_cell["model"],
                "runtime_profile": stale_cell["runtime_profile"],
                "topology": stale_cell["topology"],
                "governance": stale_cell["governance"],
                "repetition": stale_cell["repetition"],
                "source_revision": stale_cell["source_revision"],
            }
        )[:32]
        stale_cell["observation_fairness_group_id"] = group_id
        perfect["observation_fairness_group_id"] = group_id
        observation_groups[group_id] = [stale_cell["cell_id"], perfect["cell_id"]]
        perfect_cells.append(perfect)

    manifest["cells"] = sorted(
        [*manifest["cells"], *perfect_cells], key=lambda cell: cell["cell_id"]
    )
    manifest["matrix"]["planned_cells"] = 4
    manifest["matrix"]["expected_observation_conditions"] = [
        "perfect",
        "stale_offline",
    ]
    manifest["validity"].update(
        completed=4,
        benchmark_pass=2,
        benchmark_fail=2,
        valid_observation_group_ids=sorted(observation_groups),
        valid_observation_cell_ids=sorted(
            cell_id for members in observation_groups.values() for cell_id in members
        ),
        invalid_observation_groups={},
    )
    artifact["seal"]["sha256"] = sha256_json(manifest)
    source = tmp_path / "observations.json"
    write_results_manifest(source, artifact)
    output = tmp_path / "analysis"
    render_analysis_bundle(source, output_dir=output)

    rows = [
        json.loads(line)
        for line in (output / "pair-level-results.jsonl").read_text().splitlines()
    ]
    counterfactual = [row for row in rows if row["comparison_type"] == "counterfactual"]
    observation = [row for row in rows if row["comparison_type"] == "observation"]
    assert {row["observation"] for row in counterfactual} == {
        "perfect",
        "stale_offline",
    }
    assert len(counterfactual) == 2
    assert len(observation) == 2
    assert all(row["treatment_label"] == "stale_offline" for row in observation)
    assert all(row["reference_label"] == "perfect" for row in observation)

    aggregates = json.loads((output / "aggregate-results.json").read_text())[
        "results"
    ]
    safe_counterfactual = [
        item
        for item in aggregates
        if item["comparison_type"] == "counterfactual"
        and item["metric"] == "trajectory_safe_success"
    ]
    assert len(safe_counterfactual) == 2
    assert all(item["n"] == 1 for item in safe_counterfactual)
    safe_observation = next(
        item
        for item in aggregates
        if item["comparison_type"] == "observation"
        and item["metric"] == "trajectory_safe_success"
    )
    assert safe_observation["observation"] == "stale_offline_minus_perfect"
    assert safe_observation["effect_direction"] == "stale_offline_minus_perfect"
    assert safe_observation["n"] == 2


def test_raw_results_manifest_uses_shared_validation_and_pilot_contract(tmp_path) -> None:
    spec = load_matrix_file("benchmarks/aurabench-dev/matrix.yaml")
    matrix = resolve_matrix(
        spec,
        scenario_resolver=FileOrLibraryScenarioResolver(
            base_dir="benchmarks/aurabench-dev"
        ),
        source_revision="test-analysis-revision",
    )
    pilot = load_validated_pilot_bundle(PILOT_MANIFEST)
    scenario_context = {
        pair.static_scenario_id: (pair.group_id, "static")
        for pair in pilot.pairs
    } | {
        pair.dynamic_scenario_id: (pair.group_id, "dynamic")
        for pair in pilot.pairs
    }
    for cell in matrix.cells:
        group_id, variant = scenario_context[cell.scenario_id]
        output = {
            "run_id": f"run-{cell.cell_id}",
            "analysis_context": {
                "counterfactual_group_id": group_id,
                "counterfactual_variant": variant,
                "scenario_category": "pilot",
            },
            "evaluation": _evaluation(trajectory_safe=variant == "static"),
        }
        write_cell_result(
            tmp_path,
            CellResultArtifact.completed(
                cell=cell,
                matrix_hash=matrix.matrix_hash,
                output=output,
            ),
        )

    class Validator:
        @staticmethod
        def validate_completed(cell, output, *, matrix_hash):
            return output.get("run_id") == f"run-{cell.cell_id}"

    artifact = build_results_manifest(
        matrix,
        result_root=tmp_path,
        validator=Validator(),
        benchmark_manifest=PILOT_MANIFEST,
        analysis_plan=AnalysisPlan(bootstrap_resamples=5),
    )
    manifest = artifact["manifest"]
    assert manifest["benchmark"]["human_review_status"] == "pending"
    assert manifest["validity"]["completed"] == 96
    assert all(cell["admission_status"] == "admitted" for cell in manifest["cells"])
    assert all(cell["result_seal"] for cell in manifest["cells"])
