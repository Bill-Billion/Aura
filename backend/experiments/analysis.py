"""Reproducible analysis artifacts built from validated matrix evidence.

Analysis is deliberately a separate phase from execution.  Raw run directories
are admitted once into a sealed, self-contained results manifest; every paper
table and figure-data artifact is then a pure rendering of that manifest.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import stat
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from backend.engine.rng import MAX_JSON_SAFE_SEED
from backend.engine.provenance import (
    ResearchRuntimeProfile,
    research_runtime_profile_for_axes,
)

from .artifacts import atomic_create_bytes
from .pilot_bundle import MAX_PILOT_PAIRS, load_validated_pilot_bundle
from .runner import CompletedResultValidator, collect_validated_results
from .spec import (
    MAX_MATRIX_CELLS,
    ExperimentCell,
    ResolvedMatrix,
    canonical_json,
    sha256_json,
)
from .statistics import (
    BinaryPair,
    BootstrapConfig,
    ContinuousPair,
    HypothesisPValue,
    analyze_binary_pairs,
    analyze_continuous_pairs,
    holm_adjust,
    wilson_interval,
)


RESULTS_MANIFEST_SCHEMA_VERSION = "1.0"
ANALYSIS_ARTIFACT_SCHEMA_VERSION = "1.0"
MAX_ANALYSIS_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_ANALYSIS_PAIR_ROWS = 50_000
MAX_BOOTSTRAP_DRAW_OPERATIONS = 2_500_000_000

BINARY_METRICS: tuple[str, ...] = (
    "episode_complete",
    "final_state_success",
    "trajectory_properties_satisfied",
    "trajectory_safe_success",
    "user_intent_satisfied",
    "final_state_blind_spot",
)
CONTINUOUS_METRICS: tuple[str, ...] = (
    "first_action_latency_ms",
    "command_failure_count",
    "fallback_count",
    "conflict_count",
    "device_state_match_rate",
)
_METRIC_DATUM_NAMES = {
    "episode_complete",
    "first_action_latency_ms",
    "command_failure_count",
    "fallback_count",
    "conflict_count",
    "user_intent_satisfied",
    "device_state_match_rate",
}


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class AnalysisPlan(_FrozenStrictModel):
    """Pre-declared choices that become part of the sealed results manifest."""

    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    bootstrap_resamples: StrictInt = Field(default=10_000, ge=1, le=100_000)
    bootstrap_root_seed: StrictInt = Field(default=0, ge=0, le=MAX_JSON_SAFE_SEED)
    alpha: str = Field(default="0.05", pattern=r"^0\.[0-9]+$")
    binary_metrics: tuple[str, ...] = BINARY_METRICS
    continuous_metrics: tuple[str, ...] = CONTINUOUS_METRICS

    @model_validator(mode="after")
    def _fixed_preregistered_protocol(self) -> "AnalysisPlan":
        if self.binary_metrics != BINARY_METRICS:
            raise ValueError("binary metric registry is fixed by the analysis protocol")
        if self.continuous_metrics != CONTINUOUS_METRICS:
            raise ValueError(
                "continuous metric registry is fixed by the analysis protocol"
            )
        alpha = Decimal(self.alpha)
        if alpha <= 0 or alpha >= 1:
            raise ValueError("alpha must be between 0 and 1")
        return self


def _read_bounded_regular_file(path: Path, *, max_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open analysis artifact {path}: {exc}") from exc
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"analysis artifact must be a regular file: {path}")
        if metadata.st_size > max_bytes:
            raise ValueError(f"analysis artifact exceeds {max_bytes} bytes: {path}")
        encoded = handle.read(max_bytes + 1)
    if len(encoded) > max_bytes:
        raise ValueError(f"analysis artifact exceeds {max_bytes} bytes: {path}")
    return encoded


def _json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _sealed(payload: Mapping[str, Any]) -> dict[str, Any]:
    materialized = dict(payload)
    return {
        "manifest": materialized,
        "seal": {"algorithm": "sha256", "sha256": sha256_json(materialized)},
    }


def _require_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _optional_bool(value: object, *, field: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be boolean or null")


def _optional_number(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric or null")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field} must be finite")
    return normalized


def _metric_value(
    evaluation: Mapping[str, Any],
    metric: str,
    *,
    binary: bool,
) -> bool | float | None:
    metrics = _require_mapping(evaluation.get("metrics"), field="evaluation.metrics")
    datum = _require_mapping(metrics.get(metric), field=f"evaluation.metrics.{metric}")
    if datum.get("name") != metric:
        raise ValueError(f"evaluation metric name drift for {metric}")
    value = datum.get("value")
    if binary:
        return _optional_bool(value, field=f"evaluation.metrics.{metric}.value")
    return _optional_number(value, field=f"evaluation.metrics.{metric}.value")


def _evaluation_projection(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    outcome = evaluation.get("outcome")
    if outcome not in {"pass", "fail"}:
        raise ValueError("admitted evaluation outcome must be pass or fail")
    binary: dict[str, bool | None] = {}
    for metric in BINARY_METRICS:
        if metric in _METRIC_DATUM_NAMES:
            binary[metric] = _metric_value(  # type: ignore[assignment]
                evaluation, metric, binary=True
            )
        elif metric == "final_state_blind_spot":
            final_success = _optional_bool(
                evaluation.get("final_state_success"),
                field="evaluation.final_state_success",
            )
            trajectory_success = _optional_bool(
                evaluation.get("trajectory_properties_satisfied"),
                field="evaluation.trajectory_properties_satisfied",
            )
            binary[metric] = (
                not trajectory_success
                if final_success is True and trajectory_success is not None
                else None
            )
        else:
            binary[metric] = _optional_bool(
                evaluation.get(metric), field=f"evaluation.{metric}"
            )
    continuous = {
        metric: _metric_value(evaluation, metric, binary=False)
        for metric in CONTINUOUS_METRICS
    }
    raw_failed_metrics = evaluation.get("failed_metrics", [])
    if not isinstance(raw_failed_metrics, list) or any(
        not isinstance(item, str) for item in raw_failed_metrics
    ):
        raise ValueError("evaluation.failed_metrics must be a string list")
    raw_checks = evaluation.get("criteria_checks", {})
    if not isinstance(raw_checks, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, bool)
        for key, value in raw_checks.items()
    ):
        raise ValueError("evaluation.criteria_checks must be a boolean object")
    return {
        "outcome": outcome,
        "binary": binary,
        "continuous": continuous,
        "failed_metrics": sorted(set(raw_failed_metrics)),
        "criteria_checks": dict(sorted(raw_checks.items())),
    }


def _analysis_context(output: Mapping[str, Any]) -> dict[str, str | None]:
    raw = _require_mapping(output.get("analysis_context"), field="analysis_context")
    context: dict[str, str | None] = {}
    for field in (
        "counterfactual_group_id",
        "counterfactual_variant",
        "scenario_category",
    ):
        value = raw.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"analysis_context.{field} must be a string or null")
        context[field] = value
    if context["counterfactual_variant"] not in {"static", "dynamic"}:
        raise ValueError("analysis_context.counterfactual_variant is not static/dynamic")
    if not context["counterfactual_group_id"]:
        raise ValueError("analysis_context.counterfactual_group_id is required")
    return context


def _cell_projection(
    cell: ExperimentCell,
    *,
    output: Mapping[str, Any] | None,
    result_seal: str | None,
) -> dict[str, Any]:
    profile = research_runtime_profile_for_axes(
        topology=cell.topology,
        governance=cell.governance,
        observation=cell.observation,
    )
    base: dict[str, Any] = {
        "cell_id": cell.cell_id,
        "scenario_id": cell.scenario_id,
        "scenario_contract_hash": cell.scenario_contract_hash,
        "seed": cell.seed,
        "model": cell.model,
        "runtime_profile": profile.value,
        "topology": cell.topology,
        "governance": cell.governance,
        "observation": cell.observation,
        "repetition": cell.repetition,
        "source_revision": cell.source_revision,
        "admission_status": "not_admitted",
        "fairness_group_id": None,
        "result_seal": result_seal,
        "run_id": None,
        "analysis_context": None,
        "evaluation": None,
    }
    if output is None:
        return base
    run_id = output.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError(f"admitted cell {cell.cell_id} has no run_id")
    evaluation = _require_mapping(output.get("evaluation"), field="evaluation")
    fairness = output.get("fairness")
    fairness_group_id = (
        fairness.get("comparison_group_id")
        if isinstance(fairness, Mapping)
        and isinstance(fairness.get("comparison_group_id"), str)
        else None
    )
    base.update(
        admission_status="admitted",
        fairness_group_id=fairness_group_id,
        run_id=run_id,
        analysis_context=_analysis_context(output),
        evaluation=_evaluation_projection(evaluation),
    )
    return base


def build_results_manifest(
    matrix: ResolvedMatrix,
    *,
    result_root: Path | str,
    validator: CompletedResultValidator,
    benchmark_manifest: Path | str,
    analysis_plan: AnalysisPlan | None = None,
) -> dict[str, Any]:
    """Seal every admitted input needed to rebuild the analysis offline."""

    plan = analysis_plan or AnalysisPlan()
    pilot = load_validated_pilot_bundle(benchmark_manifest)
    if matrix.matrix_id != pilot.benchmark_id:
        raise ValueError("matrix_id does not match benchmark manifest")
    if matrix.spec_hash != pilot.matrix_contract_hash:
        raise ValueError("resolved matrix spec does not match benchmark manifest")
    if len(matrix.cells) != pilot.expected_cells:
        raise ValueError("resolved matrix cell count does not match benchmark manifest")
    if sorted({cell.seed for cell in matrix.cells}) != list(pilot.seeds):
        raise ValueError("resolved matrix seeds do not match benchmark manifest")
    expected_scenarios = {
        scenario_id
        for pair in pilot.pairs
        for scenario_id in (pair.static_scenario_id, pair.dynamic_scenario_id)
    }
    if {cell.scenario_id for cell in matrix.cells} != expected_scenarios:
        raise ValueError("resolved matrix scenarios do not match benchmark manifest")

    collected = collect_validated_results(
        matrix,
        output_dir=result_root,
        validator=validator,
    )
    cells: list[dict[str, Any]] = []
    for cell in matrix.cells:
        artifact = collected.completed_artifacts.get(cell.cell_id)
        cells.append(
            _cell_projection(
                cell,
                output=collected.completed_outputs.get(cell.cell_id),
                result_seal=artifact.seal.sha256 if artifact is not None else None,
            )
        )
    payload = {
        "results_manifest_schema_version": RESULTS_MANIFEST_SCHEMA_VERSION,
        "analysis_plan": plan.model_dump(mode="json"),
        "benchmark": {
            "benchmark_id": pilot.benchmark_id,
            "manifest_sha256": pilot.manifest_sha256,
            "pair_set_hash": pilot.pair_set_hash,
            "human_review_status": pilot.gate_status,
            "seeds": list(pilot.seeds),
            "pairs": [pair.model_dump(mode="json") for pair in pilot.pairs],
        },
        "matrix": {
            "matrix_id": matrix.matrix_id,
            "matrix_hash": matrix.matrix_hash,
            "source_revision": matrix.source_revision,
            "spec_hash": matrix.spec_hash,
            "planned_cells": len(matrix.cells),
            "expected_runtime_profiles": [
                profile.value for profile in matrix.expected_runtime_profiles
            ],
        },
        "validity": {
            "completed": collected.completed,
            "benchmark_pass": collected.benchmark_pass,
            "benchmark_fail": collected.benchmark_fail,
            "evaluation_error": collected.evaluation_error,
            "execution_failed": collected.execution_failed,
            "invalid_artifacts": collected.invalid_artifacts,
            "failed_cell_ids": list(collected.failed_cell_ids),
            "valid_fairness_group_ids": list(collected.fairness.valid_group_ids),
            "valid_fairness_cell_ids": list(collected.fairness.valid_cell_ids),
            "invalid_fairness_groups": collected.fairness.invalid_reasons,
        },
        "cells": cells,
    }
    return _sealed(payload)


_CELL_ID_PATTERN = re.compile(r"^cell-[0-9a-f]{32}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _validate_manifest_evaluation(value: object, *, cell_id: str) -> None:
    evaluation = _require_mapping(value, field=f"cells[{cell_id}].evaluation")
    if evaluation.get("outcome") not in {"pass", "fail"}:
        raise ValueError(f"admitted cell {cell_id} has an invalid outcome")
    binary = _require_mapping(evaluation.get("binary"), field="evaluation.binary")
    continuous = _require_mapping(
        evaluation.get("continuous"), field="evaluation.continuous"
    )
    if set(binary) != set(BINARY_METRICS):
        raise ValueError(f"admitted cell {cell_id} has an incomplete binary registry")
    if set(continuous) != set(CONTINUOUS_METRICS):
        raise ValueError(
            f"admitted cell {cell_id} has an incomplete continuous registry"
        )
    for metric in BINARY_METRICS:
        _optional_bool(binary[metric], field=f"evaluation.binary.{metric}")
    for metric in CONTINUOUS_METRICS:
        _optional_number(
            continuous[metric], field=f"evaluation.continuous.{metric}"
        )
    final_success = binary["final_state_success"]
    trajectory_success = binary["trajectory_properties_satisfied"]
    expected_safe = (
        final_success and trajectory_success
        if final_success is not None and trajectory_success is not None
        else None
    )
    if binary["trajectory_safe_success"] is not expected_safe:
        raise ValueError(f"admitted cell {cell_id} has inconsistent safe success")
    expected_blind_spot = (
        not trajectory_success
        if final_success is True and trajectory_success is not None
        else None
    )
    if binary["final_state_blind_spot"] is not expected_blind_spot:
        raise ValueError(f"admitted cell {cell_id} has inconsistent blind spot")
    failed_metrics = evaluation.get("failed_metrics")
    if not isinstance(failed_metrics, list) or any(
        not isinstance(item, str) for item in failed_metrics
    ):
        raise ValueError(f"admitted cell {cell_id} has invalid failed_metrics")
    if failed_metrics != sorted(set(failed_metrics)):
        raise ValueError(f"admitted cell {cell_id} failed_metrics are not canonical")
    checks = evaluation.get("criteria_checks")
    if not isinstance(checks, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, bool)
        for key, item in checks.items()
    ):
        raise ValueError(f"admitted cell {cell_id} has invalid criteria_checks")


def _validate_manifest_cells(
    manifest: Mapping[str, Any], cells: Sequence[object]
) -> None:
    benchmark = _require_mapping(manifest.get("benchmark"), field="benchmark")
    benchmark_id = benchmark.get("benchmark_id")
    if not isinstance(benchmark_id, str) or not benchmark_id:
        raise ValueError("benchmark.benchmark_id must be a non-empty string")
    if benchmark.get("human_review_status") != "pending":
        raise ValueError("unsupported human review status")
    pairs = benchmark.get("pairs")
    if (
        not isinstance(pairs, list)
        or not pairs
        or len(pairs) > MAX_PILOT_PAIRS
    ):
        raise ValueError("benchmark.pairs must be a non-empty list")
    for field in ("manifest_sha256", "pair_set_hash"):
        value = benchmark.get(field)
        if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(f"benchmark.{field} must be a sha256 digest")
    seeds = benchmark.get("seeds")
    if (
        not isinstance(seeds, list)
        or not seeds
        or len(seeds) > 256
        or any(
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or seed < 0
            or seed > MAX_JSON_SAFE_SEED
            for seed in seeds
        )
        or seeds != sorted(set(seeds))
    ):
        raise ValueError("benchmark.seeds must be unique sorted safe integers")
    scenario_contract: dict[str, tuple[str, str]] = {}
    group_ids: set[str] = set()
    for raw_pair in pairs:
        pair = _require_mapping(raw_pair, field="benchmark pair")
        group_id = pair.get("group_id")
        if not isinstance(group_id, str) or not group_id or group_id in group_ids:
            raise ValueError("benchmark pair group IDs must be unique strings")
        group_ids.add(group_id)
        fingerprint = pair.get("pair_fingerprint")
        if not isinstance(fingerprint, str) or not _SHA256_PATTERN.fullmatch(
            fingerprint
        ):
            raise ValueError("benchmark pair fingerprint must be a sha256 digest")
        for variant, field in (
            ("static", "static_scenario_id"),
            ("dynamic", "dynamic_scenario_id"),
        ):
            scenario_id = pair.get(field)
            if (
                not isinstance(scenario_id, str)
                or not scenario_id
                or scenario_id in scenario_contract
            ):
                raise ValueError("benchmark scenario IDs must be unique strings")
            scenario_contract[scenario_id] = (group_id, variant)

    matrix = _require_mapping(manifest.get("matrix"), field="matrix")
    if matrix.get("matrix_id") != benchmark_id:
        raise ValueError("matrix and benchmark identifiers do not match")
    for field in ("matrix_hash", "spec_hash"):
        value = matrix.get(field)
        if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
            raise ValueError(f"matrix.{field} must be a sha256 digest")
    expected_profiles = matrix.get("expected_runtime_profiles")
    if not isinstance(expected_profiles, list) or not expected_profiles:
        raise ValueError("matrix.expected_runtime_profiles must be non-empty")
    if expected_profiles != sorted(set(expected_profiles)):
        raise ValueError("matrix.expected_runtime_profiles must be unique and sorted")
    try:
        typed_profiles = {ResearchRuntimeProfile(item).value for item in expected_profiles}
    except (TypeError, ValueError) as exc:
        raise ValueError("matrix contains an unknown runtime profile") from exc

    seen_keys: set[tuple[str, int, str, int, str]] = set()
    seen_seeds: set[int] = set()
    admitted_cell_ids: set[str] = set()
    fairness_group_by_cell: dict[str, str | None] = {}
    admitted = 0
    outcomes: Counter[str] = Counter()
    for raw_cell in cells:
        cell = _require_mapping(raw_cell, field="cell")
        cell_id = cell.get("cell_id")
        if not isinstance(cell_id, str) or not _CELL_ID_PATTERN.fullmatch(cell_id):
            raise ValueError("results manifest contains an invalid cell_id")
        scenario_id = cell.get("scenario_id")
        if scenario_id not in scenario_contract:
            raise ValueError(f"cell {cell_id} is outside the benchmark pair set")
        profile = cell.get("runtime_profile")
        if profile not in typed_profiles:
            raise ValueError(f"cell {cell_id} uses an undeclared runtime profile")
        try:
            actual_profile = research_runtime_profile_for_axes(
                topology=str(cell.get("topology")),
                governance=str(cell.get("governance")),
                observation=str(cell.get("observation")),
            ).value
        except ValueError as exc:
            raise ValueError(f"cell {cell_id} has invalid runtime axes") from exc
        if profile != actual_profile:
            raise ValueError(f"cell {cell_id} profile does not match runtime axes")
        if cell.get("source_revision") != matrix.get("source_revision"):
            raise ValueError(f"cell {cell_id} source revision does not match matrix")
        for field in ("seed", "repetition"):
            number = cell.get(field)
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ValueError(f"cell {cell_id} has an invalid {field}")
        if cell.get("model") not in {"rule_based", "mocked"}:
            raise ValueError(f"cell {cell_id} uses an undeclared model")
        scenario_hash = cell.get("scenario_contract_hash")
        if not isinstance(scenario_hash, str) or not _SHA256_PATTERN.fullmatch(
            scenario_hash
        ):
            raise ValueError(f"cell {cell_id} has an invalid scenario contract hash")
        seen_seeds.add(int(cell["seed"]))
        key = _cell_key(cell)
        if key in seen_keys:
            raise ValueError("results manifest contains duplicate experimental cells")
        seen_keys.add(key)
        status = cell.get("admission_status")
        if status == "admitted":
            admitted += 1
            admitted_cell_ids.add(cell_id)
            result_seal = cell.get("result_seal")
            if not isinstance(result_seal, str) or not _SHA256_PATTERN.fullmatch(
                result_seal
            ):
                raise ValueError(f"admitted cell {cell_id} has an invalid result seal")
            run_id = cell.get("run_id")
            if not isinstance(run_id, str) or not run_id:
                raise ValueError(f"admitted cell {cell_id} has an invalid run_id")
            context = _require_mapping(
                cell.get("analysis_context"), field="analysis_context"
            )
            expected_group, expected_variant = scenario_contract[str(scenario_id)]
            if (
                context.get("counterfactual_group_id") != expected_group
                or context.get("counterfactual_variant") != expected_variant
            ):
                raise ValueError(
                    f"admitted cell {cell_id} counterfactual context does not match"
                )
            category = context.get("scenario_category")
            if category is not None and not isinstance(category, str):
                raise ValueError(f"admitted cell {cell_id} has an invalid category")
            _validate_manifest_evaluation(cell.get("evaluation"), cell_id=cell_id)
            outcomes[str(cell["evaluation"]["outcome"])] += 1
            fairness_group = cell.get("fairness_group_id")
            if fairness_group is not None and (
                not isinstance(fairness_group, str)
                or not re.fullmatch(r"group-[0-9a-f]{32}", fairness_group)
            ):
                raise ValueError(f"admitted cell {cell_id} has invalid fairness group")
            expected_fairness_group = "group-" + sha256_json(
                {
                    "scenario_id": scenario_id,
                    "scenario_contract_hash": scenario_hash,
                    "seed": cell["seed"],
                    "model": cell["model"],
                    "observation": cell["observation"],
                    "repetition": cell["repetition"],
                    "source_revision": cell["source_revision"],
                }
            )[:32]
            if fairness_group is not None and fairness_group != expected_fairness_group:
                raise ValueError(
                    f"admitted cell {cell_id} fairness group does not match its condition"
                )
            if len(expected_profiles) > 1 and fairness_group is None:
                raise ValueError(
                    f"multi-profile admitted cell {cell_id} has no fairness group"
                )
            fairness_group_by_cell[cell_id] = fairness_group
        elif status == "not_admitted":
            if any(
                cell.get(field) is not None
                for field in (
                    "result_seal",
                    "run_id",
                    "analysis_context",
                    "evaluation",
                    "fairness_group_id",
                )
            ):
                raise ValueError(f"unadmitted cell {cell_id} contains admitted evidence")
        else:
            raise ValueError(f"cell {cell_id} has an invalid admission status")

    planned_cells = matrix.get("planned_cells")
    if planned_cells != len(cells):
        raise ValueError("matrix planned cell count does not match manifest cells")
    if seen_seeds != set(seeds):
        raise ValueError("benchmark seed inventory does not match manifest cells")
    validity = _require_mapping(manifest.get("validity"), field="validity")
    for field in (
        "completed",
        "benchmark_pass",
        "benchmark_fail",
        "evaluation_error",
        "execution_failed",
        "invalid_artifacts",
    ):
        value = validity.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"validity.{field} must be a non-negative integer")
    if validity.get("completed") != admitted:
        raise ValueError("validity completed count does not match admitted cells")
    if validity.get("benchmark_pass") != outcomes["pass"]:
        raise ValueError("validity benchmark pass count does not match cells")
    if validity.get("benchmark_fail") != outcomes["fail"]:
        raise ValueError("validity benchmark fail count does not match cells")
    failed_cell_ids = validity.get("failed_cell_ids")
    all_cell_ids = {str(cell["cell_id"]) for cell in cells}
    if (
        not isinstance(failed_cell_ids, list)
        or any(not isinstance(item, str) for item in failed_cell_ids)
        or failed_cell_ids != sorted(set(failed_cell_ids))
        or not set(failed_cell_ids).issubset(all_cell_ids)
        or set(failed_cell_ids) & admitted_cell_ids
    ):
        raise ValueError("validity.failed_cell_ids do not match unadmitted cells")
    valid_cell_ids = validity.get("valid_fairness_cell_ids")
    valid_group_ids = validity.get("valid_fairness_group_ids")
    if (
        not isinstance(valid_cell_ids, list)
        or any(not isinstance(item, str) for item in valid_cell_ids)
        or valid_cell_ids != sorted(set(valid_cell_ids))
        or not set(valid_cell_ids).issubset(admitted_cell_ids)
    ):
        raise ValueError("validity.valid_fairness_cell_ids are invalid")
    if (
        not isinstance(valid_group_ids, list)
        or any(not isinstance(item, str) for item in valid_group_ids)
        or valid_group_ids != sorted(set(valid_group_ids))
    ):
        raise ValueError("validity.valid_fairness_group_ids are invalid")
    invalid_groups = validity.get("invalid_fairness_groups")
    if not isinstance(invalid_groups, Mapping) or any(
        not isinstance(group_id, str)
        or not isinstance(reasons, list)
        or any(not isinstance(reason, str) for reason in reasons)
        for group_id, reasons in invalid_groups.items()
    ):
        raise ValueError("validity.invalid_fairness_groups are invalid")
    if set(invalid_groups) & set(valid_group_ids):
        raise ValueError("fairness groups cannot be both valid and invalid")
    if len(expected_profiles) == 1:
        if valid_cell_ids or valid_group_ids:
            raise ValueError("single-profile manifests must not claim fairness groups")
    else:
        if any(
            fairness_group_by_cell[item] not in valid_group_ids
            for item in valid_cell_ids
        ):
            raise ValueError("fairness cell/group inventories do not match")
        cells_for_valid_groups = {
            cell_id
            for cell_id, group_id in fairness_group_by_cell.items()
            if group_id in valid_group_ids
        }
        if cells_for_valid_groups != set(valid_cell_ids):
            raise ValueError("fairness groups include unlisted admitted cells")
        for group_id in valid_group_ids:
            members = [
                cell
                for cell in cells
                if fairness_group_by_cell.get(str(cell["cell_id"])) == group_id
            ]
            if {str(cell["runtime_profile"]) for cell in members} != typed_profiles:
                raise ValueError("fairness group does not contain every runtime profile")
            fixed_conditions = {
                (
                    str(cell["scenario_id"]),
                    str(cell["scenario_contract_hash"]),
                    int(cell["seed"]),
                    str(cell["model"]),
                    str(cell["observation"]),
                    int(cell["repetition"]),
                    str(cell["source_revision"]),
                )
                for cell in members
            }
            if len(fixed_conditions) != 1:
                raise ValueError("fairness group changes a fixed comparison condition")


def _validate_results_manifest(raw: object) -> dict[str, Any]:
    artifact = _require_mapping(raw, field="results manifest")
    manifest = _require_mapping(artifact.get("manifest"), field="manifest")
    seal = _require_mapping(artifact.get("seal"), field="seal")
    if set(artifact) != {"manifest", "seal"}:
        raise ValueError("results manifest artifact has unexpected fields")
    if manifest.get("results_manifest_schema_version") != RESULTS_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported results manifest schema version")
    if seal.get("algorithm") != "sha256" or seal.get("sha256") != sha256_json(manifest):
        raise ValueError("results manifest seal does not match its contents")
    AnalysisPlan.model_validate(
        _require_mapping(manifest.get("analysis_plan"), field="analysis_plan")
    )
    cells = manifest.get("cells")
    if not isinstance(cells, list):
        raise ValueError("results manifest cells must be a list")
    if len(cells) > MAX_MATRIX_CELLS:
        raise ValueError(f"results manifest exceeds {MAX_MATRIX_CELLS} cells")
    cell_ids = [
        _require_mapping(cell, field="cell").get("cell_id") for cell in cells
    ]
    if any(not isinstance(cell_id, str) for cell_id in cell_ids):
        raise ValueError("results manifest cell_id values must be strings")
    if cell_ids != sorted(cell_ids) or len(cell_ids) != len(set(cell_ids)):
        raise ValueError("results manifest cells must have unique sorted IDs")
    _validate_manifest_cells(manifest, cells)
    return dict(artifact)


def read_results_manifest(path: Path | str) -> tuple[dict[str, Any], bytes]:
    path = Path(path)
    encoded = _read_bounded_regular_file(path, max_bytes=MAX_ANALYSIS_ARTIFACT_BYTES)
    try:
        raw = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid results manifest {path}: {exc}") from exc
    return _validate_results_manifest(raw), encoded


def write_results_manifest(path: Path | str, artifact: Mapping[str, Any]) -> Path:
    validated = _validate_results_manifest(artifact)
    return atomic_create_bytes(
        path,
        _json_bytes(validated),
        max_bytes=MAX_ANALYSIS_ARTIFACT_BYTES,
    )


def _cell_key(cell: Mapping[str, Any]) -> tuple[str, int, str, int, str]:
    return (
        str(cell["scenario_id"]),
        int(cell["seed"]),
        str(cell["model"]),
        int(cell["repetition"]),
        str(cell["runtime_profile"]),
    )


def _pair_values(
    treatment: Mapping[str, Any] | None,
    reference: Mapping[str, Any] | None,
    *,
    metric: str,
    kind: Literal["binary", "continuous"],
) -> tuple[bool | float | None, bool | float | None, str | None]:
    if treatment is None or reference is None:
        return None, None, "missing_planned_cell"
    if (
        treatment.get("admission_status") == "fairness_invalid"
        or reference.get("admission_status") == "fairness_invalid"
    ):
        return None, None, "invalid_fairness_group"
    if (
        treatment.get("admission_status") != "admitted"
        or reference.get("admission_status") != "admitted"
    ):
        return None, None, "unadmitted_pair_member"
    treatment_evaluation = _require_mapping(
        treatment.get("evaluation"), field="treatment.evaluation"
    )
    reference_evaluation = _require_mapping(
        reference.get("evaluation"), field="reference.evaluation"
    )
    treatment_values = _require_mapping(
        treatment_evaluation.get(kind), field=f"treatment.evaluation.{kind}"
    )
    reference_values = _require_mapping(
        reference_evaluation.get(kind), field=f"reference.evaluation.{kind}"
    )
    treatment_value = treatment_values.get(metric)
    reference_value = reference_values.get(metric)
    if treatment_value is None or reference_value is None:
        return treatment_value, reference_value, "missing_metric_value"
    if kind == "binary":
        if not isinstance(treatment_value, bool) or not isinstance(reference_value, bool):
            raise ValueError(f"binary metric {metric} has a non-boolean value")
    else:
        treatment_value = _optional_number(treatment_value, field=metric)
        reference_value = _optional_number(reference_value, field=metric)
    return treatment_value, reference_value, None


def _apply_fairness_gate(
    cell: Mapping[str, Any] | None,
    *,
    required: bool,
    valid_cell_ids: set[str],
) -> Mapping[str, Any] | None:
    if (
        not required
        or cell is None
        or cell.get("cell_id") in valid_cell_ids
    ):
        return cell
    return {**cell, "admission_status": "fairness_invalid"}


def _make_pair_row(
    *,
    row_id: str,
    comparison_type: Literal["counterfactual", "system"],
    comparison_id: str,
    model: str,
    treatment_label: str,
    reference_label: str,
    unit: Mapping[str, Any],
    treatment: Mapping[str, Any] | None,
    reference: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for kind, names in (
        ("binary", BINARY_METRICS),
        ("continuous", CONTINUOUS_METRICS),
    ):
        for metric in names:
            treatment_value, reference_value, invalid_reason = _pair_values(
                treatment,
                reference,
                metric=metric,
                kind=kind,  # type: ignore[arg-type]
            )
            metrics[metric] = {
                "kind": kind,
                "treatment": treatment_value,
                "reference": reference_value,
                "invalid_reason": invalid_reason,
            }
    return {
        "pair_row_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "row_id": row_id,
        "comparison_type": comparison_type,
        "comparison_id": comparison_id,
        "model": model,
        "treatment_label": treatment_label,
        "reference_label": reference_label,
        "unit": dict(unit),
        "treatment_cell_id": treatment.get("cell_id") if treatment else None,
        "reference_cell_id": reference.get("cell_id") if reference else None,
        "metrics": metrics,
    }


def _counterfactual_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    benchmark = _require_mapping(manifest.get("benchmark"), field="benchmark")
    raw_pairs = benchmark.get("pairs")
    if not isinstance(raw_pairs, list):
        raise ValueError("benchmark.pairs must be a list")
    cells = [
        _require_mapping(item, field="cell")
        for item in manifest.get("cells", [])
    ]
    by_key = {_cell_key(cell): cell for cell in cells}
    matrix = _require_mapping(manifest.get("matrix"), field="matrix")
    expected_profiles = matrix["expected_runtime_profiles"]
    fairness_required = len(expected_profiles) > 1
    validity = _require_mapping(manifest.get("validity"), field="validity")
    valid_cell_ids = set(validity["valid_fairness_cell_ids"])
    rows: list[dict[str, Any]] = []
    for pair in raw_pairs:
        pair = _require_mapping(pair, field="benchmark pair")
        group_id = str(pair["group_id"])
        static_id = str(pair["static_scenario_id"])
        dynamic_id = str(pair["dynamic_scenario_id"])
        contexts = sorted(
            {
                (
                    int(cell["seed"]),
                    str(cell["model"]),
                    int(cell["repetition"]),
                    str(cell["runtime_profile"]),
                )
                for cell in cells
                if cell["scenario_id"] in {static_id, dynamic_id}
            }
        )
        for seed, model, repetition, profile in contexts:
            treatment = by_key.get((dynamic_id, seed, model, repetition, profile))
            reference = by_key.get((static_id, seed, model, repetition, profile))
            treatment = _apply_fairness_gate(
                treatment,
                required=fairness_required,
                valid_cell_ids=valid_cell_ids,
            )
            reference = _apply_fairness_gate(
                reference,
                required=fairness_required,
                valid_cell_ids=valid_cell_ids,
            )
            row_id = sha256_json(
                {
                    "type": "counterfactual",
                    "group_id": group_id,
                    "seed": seed,
                    "model": model,
                    "repetition": repetition,
                    "profile": profile,
                }
            )[:32]
            rows.append(
                _make_pair_row(
                    row_id=f"pair-{row_id}",
                    comparison_type="counterfactual",
                    comparison_id=profile,
                    model=model,
                    treatment_label="dynamic",
                    reference_label="static",
                    unit={
                        "counterfactual_group_id": group_id,
                        "seed": seed,
                        "repetition": repetition,
                        "runtime_profile": profile,
                    },
                    treatment=treatment,
                    reference=reference,
                )
            )
            if len(rows) > MAX_ANALYSIS_PAIR_ROWS:
                raise ValueError(
                    f"counterfactual analysis exceeds {MAX_ANALYSIS_PAIR_ROWS} rows"
                )
    return rows


def _system_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    matrix = _require_mapping(manifest.get("matrix"), field="matrix")
    expected = matrix.get("expected_runtime_profiles")
    if not isinstance(expected, list) or any(not isinstance(item, str) for item in expected):
        raise ValueError("matrix.expected_runtime_profiles must be a string list")
    if ResearchRuntimeProfile.AURA.value not in expected or len(expected) < 2:
        return []
    validity = _require_mapping(manifest.get("validity"), field="validity")
    valid_cell_ids = set(validity.get("valid_fairness_cell_ids", []))
    cells = [
        _require_mapping(item, field="cell")
        for item in manifest.get("cells", [])
    ]
    by_key = {_cell_key(cell): cell for cell in cells}
    contexts = sorted(
        {
            (
                str(cell["scenario_id"]),
                int(cell["seed"]),
                str(cell["model"]),
                int(cell["repetition"]),
            )
            for cell in cells
        }
    )
    rows: list[dict[str, Any]] = []
    baselines = sorted(set(expected) - {ResearchRuntimeProfile.AURA.value})
    for scenario_id, seed, model, repetition in contexts:
        aura = by_key.get(
            (scenario_id, seed, model, repetition, ResearchRuntimeProfile.AURA.value)
        )
        for baseline in baselines:
            reference = by_key.get((scenario_id, seed, model, repetition, baseline))
            treatment = _apply_fairness_gate(
                aura,
                required=True,
                valid_cell_ids=valid_cell_ids,
            )
            reference = _apply_fairness_gate(
                reference,
                required=True,
                valid_cell_ids=valid_cell_ids,
            )
            row_id = sha256_json(
                {
                    "type": "system",
                    "scenario_id": scenario_id,
                    "seed": seed,
                    "model": model,
                    "repetition": repetition,
                    "baseline": baseline,
                }
            )[:32]
            rows.append(
                _make_pair_row(
                    row_id=f"pair-{row_id}",
                    comparison_type="system",
                    comparison_id=baseline,
                    model=model,
                    treatment_label=ResearchRuntimeProfile.AURA.value,
                    reference_label=baseline,
                    unit={
                        "scenario_id": scenario_id,
                        "seed": seed,
                        "repetition": repetition,
                        "baseline_profile": baseline,
                    },
                    treatment=treatment,
                    reference=reference,
                )
            )
            if len(rows) > MAX_ANALYSIS_PAIR_ROWS:
                raise ValueError(
                    f"system analysis exceeds {MAX_ANALYSIS_PAIR_ROWS} rows"
                )
    return rows


def build_pair_rows(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _counterfactual_rows(manifest) + _system_rows(manifest)
    if len(rows) > MAX_ANALYSIS_PAIR_ROWS:
        raise ValueError(f"analysis exceeds {MAX_ANALYSIS_PAIR_ROWS} pair rows")
    return sorted(rows, key=lambda row: row["row_id"])


def _result_json(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _aggregate_rows(
    manifest: Mapping[str, Any],
    pair_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plan = AnalysisPlan.model_validate(manifest["analysis_plan"])
    bootstrap = BootstrapConfig(
        root_seed=plan.bootstrap_root_seed,
        resamples=plan.bootstrap_resamples,
        confidence_level=plan.confidence_level,
    )
    valid_pair_metrics = sum(
        1
        for row in pair_rows
        for datum in _require_mapping(row.get("metrics"), field="row.metrics").values()
        if _require_mapping(datum, field="metric datum").get("invalid_reason")
        is None
    )
    bootstrap_draws = valid_pair_metrics * plan.bootstrap_resamples
    if bootstrap_draws > MAX_BOOTSTRAP_DRAW_OPERATIONS:
        raise ValueError(
            "analysis bootstrap workload exceeds "
            f"{MAX_BOOTSTRAP_DRAW_OPERATIONS} draw operations"
        )
    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in pair_rows:
        metrics = _require_mapping(row.get("metrics"), field="row.metrics")
        for metric in sorted(metrics):
            grouped[
                (
                    str(row["comparison_type"]),
                    str(row["comparison_id"]),
                    str(row["model"]),
                    metric,
                )
            ].append(row)

    aggregates: list[dict[str, Any]] = []
    bootstrap_audit: list[dict[str, Any]] = []
    for key in sorted(grouped):
        comparison_type, comparison_id, model, metric = key
        rows = sorted(grouped[key], key=lambda row: str(row["row_id"]))
        valid: list[tuple[str, bool | float, bool | float]] = []
        treatment_binary_values: list[bool] = []
        reference_binary_values: list[bool] = []
        invalid_reasons: Counter[str] = Counter()
        kind: str | None = None
        for row in rows:
            datum = _require_mapping(
                _require_mapping(row["metrics"], field="row.metrics").get(metric),
                field=f"row.metrics.{metric}",
            )
            kind = str(datum["kind"])
            if kind == "binary":
                if isinstance(datum.get("treatment"), bool):
                    treatment_binary_values.append(datum["treatment"])
                if isinstance(datum.get("reference"), bool):
                    reference_binary_values.append(datum["reference"])
            reason = datum.get("invalid_reason")
            if reason is not None:
                invalid_reasons[str(reason)] += 1
                continue
            valid.append(
                (str(row["row_id"]), datum["treatment"], datum["reference"])
            )
        analysis_id = ":".join(key)
        record: dict[str, Any] = {
            "aggregate_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
            "analysis_id": analysis_id,
            "comparison_type": comparison_type,
            "comparison_id": comparison_id,
            "model": model,
            "metric": metric,
            "kind": kind,
            "effect_direction": "treatment_minus_reference",
            "paired_estimand": (
                "complete_pair_among_final_state_success_in_both_arms"
                if metric == "final_state_blind_spot"
                else "complete_paired_runs"
            ),
            "n": len(valid),
            "invalid": sum(invalid_reasons.values()),
            "invalid_reasons": dict(sorted(invalid_reasons.items())),
            "status": "ok" if valid else "unevaluable",
            "statistics": None,
            "treatment_proportion": None,
            "reference_proportion": None,
            "holm_adjustment": None,
        }
        if kind == "binary":
            record["treatment_proportion"] = _result_json(
                wilson_interval(
                    sum(treatment_binary_values),
                    len(treatment_binary_values),
                    confidence_level=plan.confidence_level,
                )
            )
            record["reference_proportion"] = _result_json(
                wilson_interval(
                    sum(reference_binary_values),
                    len(reference_binary_values),
                    confidence_level=plan.confidence_level,
                )
            )
        if valid and kind == "binary":
            pairs = [
                BinaryPair(
                    pair_id=pair_id,
                    treatment=treatment,
                    reference=reference,
                )
                for pair_id, treatment, reference in valid
            ]
            result = analyze_binary_pairs(
                pairs, analysis_id=analysis_id, bootstrap=bootstrap
            )
            record["statistics"] = _result_json(result)
            bootstrap_audit.append(
                {
                    "analysis_id": analysis_id,
                    "pair_ids": [
                        item.pair_id
                        for item in sorted(pairs, key=lambda item: item.pair_id)
                    ],
                    "resamples": result.bootstrap.resamples,
                    "derived_seed": result.bootstrap.derived_seed,
                    "statistic": result.bootstrap.statistic,
                }
            )
        elif valid and kind == "continuous":
            pairs = [
                ContinuousPair(
                    pair_id=pair_id,
                    treatment=treatment,
                    reference=reference,
                )
                for pair_id, treatment, reference in valid
            ]
            result = analyze_continuous_pairs(
                pairs, analysis_id=analysis_id, bootstrap=bootstrap
            )
            record["statistics"] = _result_json(result)
            bootstrap_audit.append(
                {
                    "analysis_id": analysis_id,
                    "pair_ids": [
                        item.pair_id
                        for item in sorted(pairs, key=lambda item: item.pair_id)
                    ],
                    "resamples": result.bootstrap.resamples,
                    "derived_seed": result.bootstrap.derived_seed,
                    "statistic": result.bootstrap.statistic,
                }
            )
        aggregates.append(record)

    _apply_holm(aggregates, alpha=Decimal(plan.alpha))
    return aggregates, sorted(bootstrap_audit, key=lambda item: item["analysis_id"])


def _apply_holm(aggregates: list[dict[str, Any]], *, alpha: Decimal) -> None:
    """Adjust complete Aura-vs-baseline families; never shrink missing families."""

    families: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in aggregates:
        if record["comparison_type"] == "system":
            families[(record["model"], record["metric"])].append(record)
    for (model, metric), records in sorted(families.items()):
        planned_ids = sorted(record["analysis_id"] for record in records)
        observed: list[HypothesisPValue] = []
        for record in records:
            statistics = record.get("statistics")
            if not isinstance(statistics, Mapping):
                continue
            test = statistics.get("mcnemar") or statistics.get("wilcoxon")
            if not isinstance(test, Mapping) or test.get("p_value") is None:
                continue
            observed.append(
                HypothesisPValue(
                    hypothesis_id=record["analysis_id"],
                    p_value=Decimal(str(test["p_value"])),
                )
            )
        if len(observed) != len(planned_ids):
            for record in records:
                record["holm_adjustment"] = {
                    "status": "incomplete",
                    "family_id": f"system:{model}:{metric}",
                    "planned_hypothesis_ids": planned_ids,
                    "observed_hypothesis_ids": sorted(
                        item.hypothesis_id for item in observed
                    ),
                }
            continue
        result = holm_adjust(
            family_id=f"system:{model}:{metric}",
            planned_hypothesis_ids=planned_ids,
            observed=observed,
            alpha=alpha,
        )
        by_id = {
            item.hypothesis_id: item.model_dump(mode="json")
            for item in result.adjustments
        }
        for record in records:
            record["holm_adjustment"] = {
                "status": "complete",
                "family_id": result.family_id,
                **by_id[record["analysis_id"]],
            }


def _error_taxonomy(
    manifest: Mapping[str, Any], pair_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    outcome_counts: Counter[str] = Counter()
    failed_metric_counts: Counter[str] = Counter()
    for raw_cell in manifest.get("cells", []):
        cell = _require_mapping(raw_cell, field="cell")
        evaluation = cell.get("evaluation")
        if not isinstance(evaluation, Mapping):
            outcome_counts[str(cell.get("admission_status", "unknown"))] += 1
            continue
        outcome_counts[str(evaluation.get("outcome"))] += 1
        for metric in evaluation.get("failed_metrics", []):
            failed_metric_counts[str(metric)] += 1
    invalid_pair_counts: Counter[str] = Counter()
    for row in pair_rows:
        metrics = _require_mapping(row.get("metrics"), field="row.metrics")
        for datum in metrics.values():
            reason = _require_mapping(datum, field="metric datum").get(
                "invalid_reason"
            )
            if reason is not None:
                invalid_pair_counts[str(reason)] += 1
    return {
        "error_taxonomy_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "source": "typed_evaluation_fields_only",
        "cell_outcomes": dict(sorted(outcome_counts.items())),
        "failed_metrics": dict(sorted(failed_metric_counts.items())),
        "invalid_pair_metrics": dict(sorted(invalid_pair_counts.items())),
    }


_TABLE_FIELDS = (
    "comparison_type",
    "comparison_id",
    "model",
    "metric",
    "kind",
    "effect_direction",
    "effect",
    "ci_lower",
    "ci_upper",
    "treatment_n",
    "treatment_estimate",
    "treatment_ci_lower",
    "treatment_ci_upper",
    "reference_n",
    "reference_estimate",
    "reference_ci_lower",
    "reference_ci_upper",
    "n",
    "invalid",
    "p_value",
    "adjusted_p_value",
)


def _csv_text(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _table_bytes(
    aggregates: Sequence[Mapping[str, Any]], *, comparison_type: str
) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=_TABLE_FIELDS, lineterminator="\n")
    writer.writeheader()
    for record in aggregates:
        if record["comparison_type"] != comparison_type:
            continue
        statistics = record.get("statistics")
        effect = ci_lower = ci_upper = p_value = ""
        if isinstance(statistics, Mapping):
            effect = statistics.get(
                "risk_difference", statistics.get("median_difference", "")
            )
            bootstrap = statistics.get("bootstrap")
            if isinstance(bootstrap, Mapping):
                interval = bootstrap.get("confidence_interval")
                if isinstance(interval, Mapping):
                    ci_lower = interval.get("lower", "")
                    ci_upper = interval.get("upper", "")
            test = statistics.get("mcnemar") or statistics.get("wilcoxon")
            if isinstance(test, Mapping):
                p_value = test.get("p_value", "")
        adjustment = record.get("holm_adjustment")
        adjusted = (
            adjustment.get("adjusted_p_value", "")
            if isinstance(adjustment, Mapping)
            else ""
        )
        arm_values: dict[str, object] = {}
        for arm in ("treatment", "reference"):
            proportion = record.get(f"{arm}_proportion")
            interval = (
                proportion.get("confidence_interval")
                if isinstance(proportion, Mapping)
                else None
            )
            arm_values[f"{arm}_n"] = (
                proportion.get("total", "")
                if isinstance(proportion, Mapping)
                else ""
            )
            arm_values[f"{arm}_estimate"] = (
                proportion.get("estimate", "")
                if isinstance(proportion, Mapping)
                else ""
            )
            arm_values[f"{arm}_ci_lower"] = (
                interval.get("lower", "") if isinstance(interval, Mapping) else ""
            )
            arm_values[f"{arm}_ci_upper"] = (
                interval.get("upper", "") if isinstance(interval, Mapping) else ""
            )
        writer.writerow(
            {
                "comparison_type": _csv_text(record["comparison_type"]),
                "comparison_id": _csv_text(record["comparison_id"]),
                "model": _csv_text(record["model"]),
                "metric": _csv_text(record["metric"]),
                "kind": _csv_text(record["kind"]),
                "effect_direction": _csv_text(record["effect_direction"]),
                "effect": effect,
                "ci_lower": ci_lower,
                "ci_upper": ci_upper,
                **arm_values,
                "n": record["n"],
                "invalid": record["invalid"],
                "p_value": p_value,
                "adjusted_p_value": adjusted,
            }
        )
    return handle.getvalue().encode("utf-8")


def _artifact_reference(path: str, encoded: bytes, media_type: str) -> dict[str, Any]:
    return {
        "path": path,
        "media_type": media_type,
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def render_analysis_bundle(
    results_manifest_path: Path | str,
    *,
    output_dir: Path | str,
) -> dict[str, Any]:
    """Rebuild every analysis artifact using only a sealed results manifest."""

    artifact, source_bytes = read_results_manifest(results_manifest_path)
    manifest = _require_mapping(artifact["manifest"], field="manifest")
    output = Path(output_dir)
    pair_rows = build_pair_rows(manifest)
    aggregates, bootstrap_audit = _aggregate_rows(manifest, pair_rows)
    pair_jsonl = b"".join(_json_bytes(row) for row in pair_rows)
    aggregate_bytes = _json_bytes(
        {
            "aggregate_results_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
            "results": aggregates,
        }
    )
    bootstrap_bytes = _json_bytes(
        {
            "bootstrap_samples_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
            "replay_contract": "python_random_v1_seed_and_sorted_pair_ids",
            "analyses": bootstrap_audit,
        }
    )
    error_bytes = _json_bytes(_error_taxonomy(manifest, pair_rows))
    figure_bytes = _json_bytes(
        {
            "figure_data_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
            "effect_estimates": [
                {
                    key: record.get(key)
                    for key in (
                        "analysis_id",
                        "comparison_type",
                        "comparison_id",
                        "model",
                        "metric",
                        "kind",
                        "n",
                        "invalid",
                        "statistics",
                        "holm_adjustment",
                    )
                }
                for record in aggregates
            ],
        }
    )
    files: list[tuple[str, bytes, str]] = [
        ("results-manifest.json", source_bytes, "application/json"),
        ("pair-level-results.jsonl", pair_jsonl, "application/x-ndjson"),
        ("aggregate-results.json", aggregate_bytes, "application/json"),
        ("bootstrap-samples.json", bootstrap_bytes, "application/json"),
        ("error-taxonomy.json", error_bytes, "application/json"),
        (
            "table-main.csv",
            _table_bytes(aggregates, comparison_type="counterfactual"),
            "text/csv",
        ),
        (
            "table-ablation.csv",
            _table_bytes(aggregates, comparison_type="system"),
            "text/csv",
        ),
        ("figure-data/effect-estimates.json", figure_bytes, "application/json"),
    ]
    references: list[dict[str, Any]] = []
    for relative_path, encoded, media_type in files:
        atomic_create_bytes(
            output / relative_path,
            encoded,
            max_bytes=MAX_ANALYSIS_ARTIFACT_BYTES,
        )
        references.append(_artifact_reference(relative_path, encoded, media_type))
    artifact_manifest = _sealed(
        {
            "artifact_manifest_schema_version": ANALYSIS_ARTIFACT_SCHEMA_VERSION,
            "results_manifest_sha256": artifact["seal"]["sha256"],
            "artifacts": references,
        }
    )
    atomic_create_bytes(
        output / "artifact-manifest.json",
        _json_bytes(artifact_manifest),
        max_bytes=MAX_ANALYSIS_ARTIFACT_BYTES,
    )
    return {
        "results_manifest_sha256": artifact["seal"]["sha256"],
        "pair_rows": len(pair_rows),
        "aggregate_results": len(aggregates),
        "artifacts": len(references) + 1,
        "human_review_status": manifest["benchmark"]["human_review_status"],
    }


def analyze_matrix_results(
    matrix: ResolvedMatrix,
    *,
    result_root: Path | str,
    validator: CompletedResultValidator,
    benchmark_manifest: Path | str,
    output_dir: Path | str,
    analysis_plan: AnalysisPlan | None = None,
) -> dict[str, Any]:
    """Admit raw results once, then render the immutable analysis bundle."""

    output = Path(output_dir)
    artifact = build_results_manifest(
        matrix,
        result_root=result_root,
        validator=validator,
        benchmark_manifest=benchmark_manifest,
        analysis_plan=analysis_plan,
    )
    manifest_path = write_results_manifest(output / "results-manifest.json", artifact)
    return render_analysis_bundle(manifest_path, output_dir=output)


__all__ = [
    "ANALYSIS_ARTIFACT_SCHEMA_VERSION",
    "BINARY_METRICS",
    "CONTINUOUS_METRICS",
    "MAX_ANALYSIS_ARTIFACT_BYTES",
    "RESULTS_MANIFEST_SCHEMA_VERSION",
    "AnalysisPlan",
    "analyze_matrix_results",
    "build_pair_rows",
    "build_results_manifest",
    "read_results_manifest",
    "render_analysis_bundle",
    "write_results_manifest",
]
