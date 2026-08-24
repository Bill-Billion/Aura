from __future__ import annotations

import pytest

from backend.engine.provenance import ObservationCondition, ResearchRuntimeProfile
from backend.experiments.fairness import (
    audit_comparison_outputs,
    audit_observation_comparison_outputs,
    build_fairness_payload,
    comparison_group_id,
    observation_comparison_group_id,
    validate_comparison_plan,
    validate_observation_comparison_plan,
)
from backend.experiments.resolve import resolve_matrix
from backend.experiments.runner import MatrixRunner, summarize_results
from backend.experiments.spec import (
    ExactExclusion,
    ExperimentCell,
    MatrixSpec,
    ScenarioContract,
)


PROFILES = (
    ("single", "none"),
    ("domain_multi", "none"),
    ("domain_multi", "flat_priority"),
    ("domain_multi", "aura"),
)


def _cell(
    scenario_id: str,
    topology: str,
    governance: str,
    observation: str = "stale_offline",
) -> ExperimentCell:
    return ExperimentCell.build(
        combination=ExactExclusion(
            scenario=scenario_id,
            seed=42,
            model="rule_based",
            topology=topology,
            governance=governance,
            observation=observation,
            repetition=0,
        ),
        scenario=ScenarioContract(
            reference=f"{scenario_id}.yaml",
            scenario_id=scenario_id,
            scenario_contract_hash=("a" if scenario_id == "scenario-a" else "b") * 64,
        ),
        experiment_id="fairness_matrix",
        matrix_spec_hash="c" * 64,
        source_revision="test-revision",
        estimated_cost_usd=0,
    )


def _group(scenario_id: str = "scenario-a") -> list[ExperimentCell]:
    return [_cell(scenario_id, topology, governance) for topology, governance in PROFILES]


def _observation_group(scenario_id: str = "scenario-a") -> list[ExperimentCell]:
    return [
        _cell(scenario_id, "domain_multi", "aura", observation)
        for observation in ("perfect", "stale_offline")
    ]


def _metadata(
    cell: ExperimentCell,
    *,
    initial_state_hash: str = "a" * 64,
    duration_seconds: float = 60.0,
    multi_agent_version: str = "multi-v1",
) -> dict[str, object]:
    agent_versions = (
        {"single_direct_agent": "single-v1"}
        if cell.topology == "single"
        else {
            agent_id: multi_agent_version
            for agent_id in (
                "lighting_agent",
                "hvac_agent",
                "security_agent",
                "energy_agent",
                "scene_agent",
            )
        }
    )
    return {
        "source_revision": "test-revision",
        "sim_version": "1.0",
        "agent_versions": agent_versions,
        "llm_provider": "rule_based",
        "llm_model": "rule_based",
        "llm_mode": "rule_based",
        "baseline_policy": "rule_based",
        "duration_seconds": duration_seconds,
        "initial_state_hash": initial_state_hash,
        "scenario_schema_version": "2.1",
        "event_schema_version": "1.0",
        "command_schema_version": "1.0",
        "device_registry_version": "1.0",
        "trace_spec_hash": "d" * 64,
    }


EVALUATION = {
    "report_schema_version": "1.0",
    "provenance": {"evaluator_source_revision": "test-revision"},
}


def test_comparison_plan_requires_every_profile_in_every_group() -> None:
    complete = [*_group("scenario-a"), *_group("scenario-b")]
    validate_comparison_plan(
        complete,
        expected_profiles=list(ResearchRuntimeProfile),
    )

    with pytest.raises(ValueError, match="unbalanced"):
        validate_comparison_plan(
            complete[:-1],
            expected_profiles=list(ResearchRuntimeProfile),
        )


def test_full_matrix_fairness_audit_accepts_only_identical_fixed_provenance() -> None:
    cells = _group()
    outputs = {
        cell.cell_id: {
            "fairness": build_fairness_payload(
                cell,
                run_metadata=_metadata(cell),
                evaluation=EVALUATION,
            )
        }
        for cell in cells
    }
    passed = audit_comparison_outputs(
        cells,
        outputs,
        expected_profiles=list(ResearchRuntimeProfile),
    )
    assert passed.valid_groups == 1
    assert passed.invalid_groups == 0

    drifted = dict(outputs)
    changed = cells[0]
    drifted[changed.cell_id] = {
        "fairness": build_fairness_payload(
            changed,
            run_metadata=_metadata(changed, initial_state_hash="b" * 64),
            evaluation=EVALUATION,
        )
    }
    failed = audit_comparison_outputs(
        cells,
        drifted,
        expected_profiles=list(ResearchRuntimeProfile),
    )
    assert failed.valid_groups == 0
    assert failed.invalid_groups == 1
    assert "initial_state_hash" in next(iter(failed.invalid_reasons.values()))[0]


def test_full_matrix_fairness_audit_invalidates_a_missing_partner() -> None:
    cells = _group()
    outputs = {
        cell.cell_id: {
            "fairness": build_fairness_payload(
                cell,
                run_metadata=_metadata(cell),
                evaluation=EVALUATION,
            )
        }
        for cell in cells[:-1]
    }
    audit = audit_comparison_outputs(
        cells,
        outputs,
        expected_profiles=list(ResearchRuntimeProfile),
    )
    assert audit.valid_groups == 0
    assert audit.invalid_groups == 1
    assert "missing or invalid" in next(iter(audit.invalid_reasons.values()))[0]


def test_single_profile_matrix_is_not_misreported_as_a_comparison() -> None:
    aura = [_cell("scenario-a", "domain_multi", "aura")]
    validate_comparison_plan(
        aura,
        expected_profiles=[ResearchRuntimeProfile.AURA],
    )
    audit = audit_comparison_outputs(
        aura,
        {},
        expected_profiles=[ResearchRuntimeProfile.AURA],
    )
    assert audit.valid_groups == 0
    assert audit.invalid_groups == 0


def test_observation_plan_and_audit_require_both_conditions_at_fixed_profile() -> None:
    cells = _observation_group()
    validate_observation_comparison_plan(
        cells,
        expected_observations=list(ObservationCondition),
    )
    with pytest.raises(ValueError, match=r"missing=\['stale_offline'\]"):
        validate_observation_comparison_plan(
            cells[:1],
            expected_observations=list(ObservationCondition),
        )

    outputs = {
        cell.cell_id: {
            "fairness": build_fairness_payload(
                cell,
                run_metadata=_metadata(cell),
                evaluation=EVALUATION,
            )
        }
        for cell in cells
    }
    audit = audit_observation_comparison_outputs(
        cells,
        outputs,
        expected_observations=list(ObservationCondition),
    )
    assert audit.valid_groups == 1
    assert audit.invalid_groups == 0
    assert audit.valid_group_ids == (observation_comparison_group_id(cells[0]),)


def test_observation_audit_invalidates_fixed_profile_provenance_drift() -> None:
    cells = _observation_group()
    outputs = {
        cell.cell_id: {
            "fairness": build_fairness_payload(
                cell,
                run_metadata=_metadata(
                    cell,
                    duration_seconds=60.0 if cell.observation == "perfect" else 61.0,
                ),
                evaluation=EVALUATION,
            )
        }
        for cell in cells
    }
    audit = audit_observation_comparison_outputs(
        cells,
        outputs,
        expected_observations=list(ObservationCondition),
    )
    assert audit.valid_groups == 0
    assert audit.invalid_groups == 1
    assert "duration_seconds" in next(iter(audit.invalid_reasons.values()))[0]


def test_truncated_fairness_payload_is_invalid_evidence() -> None:
    cells = _group()
    outputs = {
        cell.cell_id: {
            "fairness": {"comparison_group_id": comparison_group_id(cell)}
        }
        for cell in cells
    }
    audit = audit_comparison_outputs(
        cells,
        outputs,
        expected_profiles=list(ResearchRuntimeProfile),
    )
    assert audit.valid_groups == 0
    assert audit.invalid_groups == 1
    assert "invalid fairness payload" in next(
        iter(audit.invalid_reasons.values())
    )[0]


@pytest.mark.parametrize(
    ("field", "metadata_change"),
    [
        ("duration_seconds", {"duration_seconds": 61.0}),
        ("agent_versions", {"multi_agent_version": "multi-v2"}),
    ],
)
def test_fixed_runtime_provenance_drift_invalidates_group(
    field: str,
    metadata_change: dict[str, object],
) -> None:
    cells = _group()
    outputs = {
        cell.cell_id: {
            "fairness": build_fairness_payload(
                cell,
                run_metadata=_metadata(cell),
                evaluation=EVALUATION,
            )
        }
        for cell in cells
    }
    changed = next(
        cell
        for cell in cells
        if cell.governance == "none" and cell.topology == "domain_multi"
    )
    outputs[changed.cell_id] = {
        "fairness": build_fairness_payload(
            changed,
            run_metadata=_metadata(changed, **metadata_change),
            evaluation=EVALUATION,
        )
    }
    audit = audit_comparison_outputs(
        cells,
        outputs,
        expected_profiles=list(ResearchRuntimeProfile),
    )
    assert audit.valid_groups == 0
    assert field in next(iter(audit.invalid_reasons.values()))[-1]


def test_declared_profiles_survive_exclusions_that_remove_a_whole_profile() -> None:
    spec = MatrixSpec.model_validate(
        {
            "matrix_id": "excluded_profile",
            "axes": {
                "scenario": ["scenario-a"],
                "seed": [42],
                "model": ["rule_based"],
                "topology": ["domain_multi"],
                "governance": ["none", "aura"],
                "observation": ["stale_offline"],
                "repetition": [0],
            },
            "exclude": [
                {
                    "scenario": "scenario-a",
                    "seed": 42,
                    "model": "rule_based",
                    "topology": "domain_multi",
                    "governance": "aura",
                    "observation": "stale_offline",
                    "repetition": 0,
                }
            ],
            "max_cells": 1,
            "max_total_cost_usd": 0,
            "cost_per_model_usd": {"rule_based": 0},
        }
    )

    class Resolver:
        @staticmethod
        def resolve(reference: str) -> ScenarioContract:
            return ScenarioContract(
                reference=reference,
                scenario_id=reference,
                scenario_contract_hash="e" * 64,
            )

    matrix = resolve_matrix(
        spec,
        scenario_resolver=Resolver(),
        source_revision="test-revision",
    )
    assert set(matrix.expected_runtime_profiles) == {
        ResearchRuntimeProfile.NO_ARBITER,
        ResearchRuntimeProfile.AURA,
    }
    with pytest.raises(ValueError, match=r"missing=\['aura'\]"):
        validate_comparison_plan(
            matrix.cells,
            expected_profiles=matrix.expected_runtime_profiles,
        )


@pytest.mark.anyio
async def test_cross_shard_summary_exposes_invalid_fairness_group(tmp_path) -> None:
    class Resolver:
        @staticmethod
        def resolve(reference: str) -> ScenarioContract:
            return ScenarioContract(
                reference=reference,
                scenario_id=reference,
                scenario_contract_hash="e" * 64,
            )

    spec = MatrixSpec.model_validate(
        {
            "matrix_id": "fairness_summary",
            "axes": {
                "scenario": ["scenario-a"],
                "seed": [42],
                "model": ["rule_based"],
                "topology": ["domain_multi"],
                "governance": ["none", "aura"],
                "observation": ["stale_offline"],
                "repetition": [0],
            },
            "max_cells": 2,
            "max_total_cost_usd": 0,
            "cost_per_model_usd": {"rule_based": 0},
        }
    )
    matrix = resolve_matrix(
        spec,
        scenario_resolver=Resolver(),
        source_revision="test-revision",
    )

    class DriftedExecutor:
        @staticmethod
        def validate_completed(cell, output, *, matrix_hash):
            return True

        async def execute(self, cell, *, matrix_hash):
            evaluation = {
                "outcome": "pass",
                **EVALUATION,
            }
            return {
                "evaluation": evaluation,
                "fairness": build_fairness_payload(
                    cell,
                    run_metadata=_metadata(
                        cell,
                        initial_state_hash=(
                            "a" * 64 if cell.governance == "none" else "b" * 64
                        ),
                    ),
                    evaluation=evaluation,
                ),
            }

    executor = DriftedExecutor()
    serial = await MatrixRunner(executor).run(matrix, output_dir=tmp_path)
    assert serial.fairness_audited is True
    assert serial.valid_baseline_groups == 0
    assert serial.invalid_baseline_groups == 1
    assert serial.benchmark_pass == 2
    assert serial.scientific_benchmark_pass == 0

    sharded_dir = tmp_path / "sharded"
    shard_summaries = [
        await MatrixRunner(executor).run(
            matrix,
            output_dir=sharded_dir,
            shard_index=index,
            shard_count=2,
        )
        for index in range(2)
    ]
    assert all(summary.fairness_audited is False for summary in shard_summaries)
    summary = summarize_results(
        matrix,
        output_dir=sharded_dir,
        validator=executor,
    )
    assert summary.fairness_audited is True
    assert summary.valid_baseline_groups == 0
    assert summary.invalid_baseline_groups == 1
    assert summary.benchmark_pass == 2
    assert summary.scientific_valid_cells == 0
    assert summary.scientific_benchmark_pass == 0
    assert "initial_state_hash" in next(
        iter(summary.invalid_baseline_group_reasons.values())
    )[0]

    class StableExecutor(DriftedExecutor):
        async def execute(self, cell, *, matrix_hash):
            evaluation = {"outcome": "pass", **EVALUATION}
            return {
                "evaluation": evaluation,
                "fairness": build_fairness_payload(
                    cell,
                    run_metadata=_metadata(cell),
                    evaluation=evaluation,
                ),
            }

    valid_summary = await MatrixRunner(StableExecutor()).run(
        matrix,
        output_dir=tmp_path / "valid",
    )
    assert valid_summary.valid_baseline_groups == 1
    assert valid_summary.invalid_baseline_groups == 0
    assert valid_summary.scientific_valid_cells == 2
    assert valid_summary.scientific_benchmark_pass == 2


@pytest.mark.anyio
async def test_summary_records_valid_observation_group_ids(tmp_path) -> None:
    class Resolver:
        @staticmethod
        def resolve(reference: str) -> ScenarioContract:
            return ScenarioContract(
                reference=reference,
                scenario_id=reference,
                scenario_contract_hash="e" * 64,
            )

    spec = MatrixSpec.model_validate(
        {
            "matrix_id": "observation_summary",
            "axes": {
                "scenario": ["scenario-a"],
                "seed": [42],
                "model": ["rule_based"],
                "topology": ["domain_multi"],
                "governance": ["aura"],
                "observation": ["perfect", "stale_offline"],
                "repetition": [0],
            },
            "max_cells": 2,
            "max_total_cost_usd": 0,
            "cost_per_model_usd": {"rule_based": 0},
        }
    )
    matrix = resolve_matrix(
        spec,
        scenario_resolver=Resolver(),
        source_revision="test-revision",
    )

    class Executor:
        @staticmethod
        def validate_completed(cell, output, *, matrix_hash):
            return True

        async def execute(self, cell, *, matrix_hash):
            evaluation = {"outcome": "pass", **EVALUATION}
            return {
                "evaluation": evaluation,
                "fairness": build_fairness_payload(
                    cell,
                    run_metadata=_metadata(cell),
                    evaluation=evaluation,
                ),
            }

    summary = await MatrixRunner(Executor()).run(matrix, output_dir=tmp_path)
    assert summary.valid_baseline_groups == 0
    assert summary.valid_observation_groups == 1
    assert summary.invalid_observation_groups == 0
    assert summary.valid_observation_group_ids == [
        observation_comparison_group_id(matrix.cells[0])
    ]
    assert summary.scientific_valid_cells == 2
