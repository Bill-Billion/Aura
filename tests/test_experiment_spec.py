from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.experiments.resolve import load_and_resolve_matrix, resolve_matrix
from backend.experiments.spec import (
    MAX_MATRIX_STRING_LENGTH,
    MatrixSpec,
    ResolvedMatrix,
    ScenarioContract,
    sha256_json,
)


CONTRACT_HASH = "a" * 64


class StubScenarioResolver:
    def resolve(self, reference: str) -> ScenarioContract:
        return ScenarioContract(
            reference=reference,
            scenario_id=f"scenario-{reference}",
            scenario_contract_hash=CONTRACT_HASH,
        )


def matrix_mapping() -> dict:
    return {
        "matrix_schema_version": "1.0",
        "matrix_id": "test_matrix",
        "axes": {
            "scenario": ["static", "dynamic"],
            "seed": [7],
            "model": ["rule_based", "mocked"],
            "topology": ["domain_multi"],
            "governance": ["aura"],
            "observation": ["stale_offline"],
            "repetition": [0, 1],
        },
        "max_cells": 8,
        "max_total_cost_usd": 0,
        "cost_per_model_usd": {"rule_based": 0, "mocked": 0},
    }


def test_matrix_axes_must_be_nonempty_unique_and_strict() -> None:
    empty = matrix_mapping()
    empty["axes"]["scenario"] = []
    with pytest.raises(ValidationError, match="at least 1"):
        MatrixSpec.model_validate(empty)

    duplicate = matrix_mapping()
    duplicate["axes"]["model"] = ["mocked", "mocked"]
    with pytest.raises(ValidationError, match="must be unique"):
        MatrixSpec.model_validate(duplicate)

    extra = matrix_mapping()
    extra["axes"]["future"] = ["surprise"]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MatrixSpec.model_validate(extra)

    oversized = matrix_mapping()
    oversized["axes"]["model"] = ["x" * (MAX_MATRIX_STRING_LENGTH + 1)]
    oversized["cost_per_model_usd"] = {
        oversized["axes"]["model"][0]: 0,
    }
    with pytest.raises(ValidationError, match="must not exceed"):
        MatrixSpec.model_validate(oversized)


def test_exclusion_is_exact_validated_and_applied_before_cell_cap() -> None:
    raw = matrix_mapping()
    raw["max_cells"] = 7
    raw["exclude"] = [
        {
            "scenario": "static",
            "seed": 7,
            "model": "rule_based",
            "topology": "domain_multi",
            "governance": "aura",
            "observation": "stale_offline",
            "repetition": 0,
        }
    ]
    spec = MatrixSpec.model_validate(raw)
    assert len(spec.combinations()) == 7

    mistyped = deepcopy(raw)
    mistyped["exclude"][0]["observation"] = "stale_offlin"
    with pytest.raises(ValidationError, match="is not declared"):
        MatrixSpec.model_validate(mistyped)


def test_cell_cap_and_global_cost_are_checked_after_exclusion() -> None:
    too_many = matrix_mapping()
    too_many["max_cells"] = 7
    with pytest.raises(ValueError, match="exceeding max_cells"):
        MatrixSpec.model_validate(too_many).combinations()

    paid = matrix_mapping()
    paid["cost_per_model_usd"] = {"rule_based": 0, "mocked": 0.25}
    paid["max_total_cost_usd"] = 0.99
    with pytest.raises(ValueError, match="estimated cost"):
        MatrixSpec.model_validate(paid).combinations()

    huge = matrix_mapping()
    huge["axes"] = {
        name: [f"value-{index}" for index in range(20)]
        for name in ("scenario", "model", "topology", "governance", "observation")
    }
    huge["axes"]["seed"] = list(range(20))
    huge["axes"]["repetition"] = list(range(20))
    huge["max_cells"] = 10_000
    huge["cost_per_model_usd"] = {
        value: 0 for value in huge["axes"]["model"]
    }
    with pytest.raises(ValueError, match="exceeding max_cells"):
        MatrixSpec.model_validate(huge).combinations()


def test_resolution_is_order_independent_and_ids_cover_provenance() -> None:
    first_raw = matrix_mapping()
    second_raw = matrix_mapping()
    second_raw["axes"]["scenario"].reverse()
    second_raw["axes"]["model"].reverse()
    second_raw["axes"]["repetition"].reverse()

    first = resolve_matrix(
        MatrixSpec.model_validate(first_raw),
        scenario_resolver=StubScenarioResolver(),
        source_revision="sha256:revision-a",
    )
    second = resolve_matrix(
        MatrixSpec.model_validate(second_raw),
        scenario_resolver=StubScenarioResolver(),
        source_revision="sha256:revision-a",
    )
    assert first == second
    assert [cell.cell_id for cell in first.cells] == sorted(
        cell.cell_id for cell in first.cells
    )

    changed = resolve_matrix(
        MatrixSpec.model_validate(first_raw),
        scenario_resolver=StubScenarioResolver(),
        source_revision="sha256:revision-b",
    )
    assert {cell.cell_id for cell in first.cells}.isdisjoint(
        cell.cell_id for cell in changed.cells
    )
    assert first.matrix_hash != changed.matrix_hash


def test_resolved_matrix_rejects_cross_field_provenance_drift() -> None:
    matrix = resolve_matrix(
        MatrixSpec.model_validate(matrix_mapping()),
        scenario_resolver=StubScenarioResolver(),
        source_revision="sha256:revision-a",
    )
    raw = matrix.model_dump(mode="json")
    raw["cells"][0]["experiment_id"] = "other_matrix"
    raw["matrix_hash"] = sha256_json(
        {key: value for key, value in raw.items() if key != "matrix_hash"}
    )
    with pytest.raises(ValidationError, match="experiment_id does not match"):
        ResolvedMatrix.model_validate(raw)

    raw = matrix.model_dump(mode="json")
    raw["cells"][0]["repetition"] += 1
    raw["matrix_hash"] = sha256_json(
        {key: value for key, value in raw.items() if key != "matrix_hash"}
    )
    with pytest.raises(ValidationError, match="cell_id does not match"):
        ResolvedMatrix.model_validate(raw)

    raw = matrix.model_dump(mode="json")
    raw["total_estimated_cost_usd"] = float("inf")
    with pytest.raises(ValidationError, match="finite number"):
        ResolvedMatrix.model_validate(raw)


def test_pilot_matrix_resolves_to_exactly_eight_cells() -> None:
    from backend.experiments.resolve import (
        FileOrLibraryScenarioResolver,
        load_matrix_file,
    )

    matrix_path = "benchmarks/aurabench-dev/matrix.yaml"
    spec = load_matrix_file(matrix_path)
    resolved = resolve_matrix(
        spec,
        source_revision="test-pilot",
        scenario_resolver=FileOrLibraryScenarioResolver(
            base_dir="benchmarks/aurabench-dev"
        ),
    )
    assert len(resolved.cells) == 8
    assert {cell.model for cell in resolved.cells} == {"rule_based", "mocked"}
    assert {cell.topology for cell in resolved.cells} == {"domain_multi"}
    assert {cell.governance for cell in resolved.cells} == {"aura"}
    assert {cell.observation for cell in resolved.cells} == {"stale_offline"}


def test_resolved_scenario_reference_is_portable_across_working_directories(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    from backend.experiments.adapters import AuraCellExecutor
    from backend.experiments.resolve import (
        FileOrLibraryScenarioResolver,
        load_matrix_file,
    )

    matrix_path = Path("benchmarks/aurabench-dev/matrix.yaml").resolve()
    monkeypatch.chdir(tmp_path)
    resolved = load_and_resolve_matrix(
        matrix_path,
        source_revision="portable-test",
    )
    assert all(
        cell.scenario_reference.startswith("benchmarks/aurabench-dev/episodes/")
        for cell in resolved.cells
    )
    loaded, _ = AuraCellExecutor(enforce_source_revision=False)._load_scenario(
        resolved.cells[0]
    )
    assert loaded.id == resolved.cells[0].scenario_id
