"""PR-5 explicit experiment conditions persist with every sealed run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.engine.event_log import RUN_METADATA_FILENAME, run_dir
from backend.engine.provenance import (
    RESEARCH_RUNTIME_PROFILES,
    ExperimentProvenance,
    ExperimentRuntimeSelection,
    ResearchRuntimeProfile,
    research_runtime_profile_for_axes,
)
from backend.engine.run_manager import canonical_json
from backend.experiments.adapters import AdapterUnavailableError, AuraCellExecutor
from backend.evaluation.evaluator import evaluate_run
from backend.models.schemas import BaselinePolicy
from backend.scenarios.loader import load_library
from backend.scenarios.runner import ScenarioRunner


PILOT_DIR = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"
)


def _provenance() -> ExperimentProvenance:
    return ExperimentProvenance(
        experiment_id="aurabench-dev",
        matrix_spec_hash="a" * 64,
        matrix_hash="b" * 64,
        cell_id="cell-0123456789abcdef",
        runtime_profile=ResearchRuntimeProfile.AURA,
        model="mocked",
        topology="domain_multi",
        governance="aura",
        observation="stale_offline",
        repetition=1,
    )


def _runtime_selection() -> ExperimentRuntimeSelection:
    return ExperimentRuntimeSelection(
        runtime_profile=ResearchRuntimeProfile.AURA,
        model="mocked",
        baseline_policy=BaselinePolicy.LLM_MOCKED,
    )


def test_experiment_provenance_rejects_untyped_conditions() -> None:
    payload = _provenance().model_dump(mode="json")
    payload["governance"] = "pretend_aura"
    with pytest.raises(ValidationError):
        ExperimentProvenance.model_validate(payload)


@pytest.mark.parametrize(
    ("profile", "topology", "governance"),
    [
        (ResearchRuntimeProfile.SINGLE_DIRECT, "single", "none"),
        (ResearchRuntimeProfile.NO_ARBITER, "domain_multi", "none"),
        (ResearchRuntimeProfile.FLAT_PRIORITY, "domain_multi", "flat_priority"),
        (ResearchRuntimeProfile.AURA, "domain_multi", "aura"),
    ],
)
def test_research_runtime_profiles_build_exact_typed_selections(
    profile: ResearchRuntimeProfile,
    topology: str,
    governance: str,
) -> None:
    selection = ExperimentRuntimeSelection.for_profile(
        profile,
        model="rule_based",
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    assert selection.runtime_profile is profile
    assert selection.topology == topology
    assert selection.governance == governance
    assert selection.observation == "stale_offline"
    assert RESEARCH_RUNTIME_PROFILES[profile] == (
        topology,
        governance,
        "stale_offline",
    )

    payload = _provenance().model_dump(mode="json")
    payload.update(
        runtime_profile=profile.value,
        model="rule_based",
        topology=topology,
        governance=governance,
    )
    provenance = ExperimentProvenance.model_validate(payload)
    selection.validate_provenance(provenance)


@pytest.mark.parametrize(
    ("topology", "governance", "observation"),
    [
        ("single", "aura", "stale_offline"),
        ("single", "flat_priority", "stale_offline"),
        ("domain_multi", "aura", "perfect"),
    ],
)
def test_runtime_profile_axes_fail_closed(
    topology: str,
    governance: str,
    observation: str,
) -> None:
    with pytest.raises(ValueError, match="do not identify an implemented"):
        research_runtime_profile_for_axes(
            topology=topology,
            governance=governance,
            observation=observation,
        )

    payload = _provenance().model_dump(mode="json")
    payload.update(
        topology=topology,
        governance=governance,
        observation=observation,
    )
    with pytest.raises(ValidationError, match="implemented research profile"):
        ExperimentProvenance.model_validate(payload)


def test_runtime_profile_id_cannot_disagree_with_legal_axes() -> None:
    with pytest.raises(ValidationError, match="runtime_profile does not match"):
        ExperimentRuntimeSelection(
            runtime_profile=ResearchRuntimeProfile.NO_ARBITER,
            model="rule_based",
            topology="domain_multi",
            governance="aura",
            observation="stale_offline",
            baseline_policy=BaselinePolicy.RULE_BASED,
        )


@pytest.mark.parametrize("profile", list(ResearchRuntimeProfile))
def test_adapter_constructs_runtime_selection_from_profile(
    profile: ResearchRuntimeProfile,
) -> None:
    topology, governance, observation = RESEARCH_RUNTIME_PROFILES[profile]
    cell = SimpleNamespace(
        model="mocked",
        topology=topology,
        governance=governance,
        observation=observation,
    )
    resolved = AuraCellExecutor._validate_adapters(cell)  # type: ignore[arg-type]
    selection = AuraCellExecutor._runtime_selection(  # type: ignore[arg-type]
        cell,
        resolved,
    )
    assert resolved is profile
    assert selection.runtime_profile is profile
    assert selection.baseline_policy is BaselinePolicy.LLM_MOCKED


def test_adapter_rejects_an_illegal_independent_axis_combination() -> None:
    cell = SimpleNamespace(
        model="rule_based",
        topology="single",
        governance="flat_priority",
        observation="stale_offline",
    )
    with pytest.raises(AdapterUnavailableError, match="implemented research profile"):
        AuraCellExecutor._validate_adapters(cell)  # type: ignore[arg-type]


def _runtime_evidence(
    *,
    profile: ResearchRuntimeProfile = ResearchRuntimeProfile.AURA,
    topology: str = "domain_multi",
    governance: str = "aura",
) -> tuple[SimpleNamespace, dict[str, object]]:
    actor_id = "single_direct_agent" if topology == "single" else "lighting_agent"
    preimages: dict[str, object] = {
        "observable_snapshot": {
            "environment": {"time_of_day": "19:00"},
            "devices": {"light_01": {"power": True}},
        },
        "proposal_set": [
            {
                "agent_id": actor_id,
                "commands": [
                    {
                        "device_id": "light_01",
                        "property": "extra.brightness",
                        "value": 70,
                    }
                ],
            }
        ],
        "approved_command_set": [
            {
                "agent_id": actor_id,
                "device_id": "light_01",
                "property": "extra.brightness",
                "value": 70,
            }
        ],
        "rejected_command_set": [],
    }
    active_agent_ids = (
        ["single_direct_agent"]
        if topology == "single"
        else [
            "lighting_agent",
            "hvac_agent",
            "security_agent",
            "energy_agent",
            "scene_agent",
        ]
    )
    data: dict[str, object] = {
        "runtime_profile": profile.value,
        "requested_runtime_profile": profile.value,
        "effective_runtime_profile": profile.value,
        "governance": governance,
        "observable_snapshot_projection": (
            "world_state_without_agent_diagnostics.v1"
        ),
        "active_agent_ids": active_agent_ids,
        "per_agent": [{"agent_id": actor_id}],
        **preimages,
    }
    for field, preimage in preimages.items():
        data[f"{field}_hash"] = hashlib.sha256(
            canonical_json(preimage).encode("utf-8")
        ).hexdigest()
    source = {
        "none": "proposal_passthrough",
        "flat_priority": "flat_priority",
        "aura": "arbiter",
    }[governance]
    return (
        SimpleNamespace(topology=topology, governance=governance),
        {
            "event_type": "reasoning.coordination_decision",
            "source": source,
            "data": data,
        },
    )


@pytest.mark.parametrize(
    ("profile", "topology", "governance"),
    [
        (ResearchRuntimeProfile.SINGLE_DIRECT, "single", "none"),
        (ResearchRuntimeProfile.NO_ARBITER, "domain_multi", "none"),
        (ResearchRuntimeProfile.FLAT_PRIORITY, "domain_multi", "flat_priority"),
        (ResearchRuntimeProfile.AURA, "domain_multi", "aura"),
    ],
)
def test_adapter_recomputes_content_addressed_runtime_evidence(
    profile: ResearchRuntimeProfile,
    topology: str,
    governance: str,
) -> None:
    cell, event = _runtime_evidence(
        profile=profile,
        topology=topology,
        governance=governance,
    )

    assert AuraCellExecutor._runtime_evidence_matches(  # type: ignore[arg-type]
        cell,
        profile,
        [event],
    )


@pytest.mark.parametrize(
    "preimage_field",
    [
        "observable_snapshot",
        "proposal_set",
        "approved_command_set",
        "rejected_command_set",
    ],
)
def test_adapter_rejects_runtime_evidence_whose_preimage_does_not_match_hash(
    preimage_field: str,
) -> None:
    cell, event = _runtime_evidence()
    data = event["data"]
    assert isinstance(data, dict)
    preimage = data[preimage_field]
    if isinstance(preimage, list):
        preimage.append({"tampered": True})
    else:
        assert isinstance(preimage, dict)
        preimage["tampered"] = True

    assert not AuraCellExecutor._runtime_evidence_matches(  # type: ignore[arg-type]
        cell,
        ResearchRuntimeProfile.AURA,
        [event],
    )


@pytest.mark.parametrize(
    ("preimage_field", "wrong_type"),
    [
        ("observable_snapshot", []),
        ("proposal_set", {}),
        ("approved_command_set", {}),
        ("rejected_command_set", {}),
    ],
)
def test_adapter_rejects_wrong_runtime_evidence_preimage_type_even_with_matching_hash(
    preimage_field: str,
    wrong_type: object,
) -> None:
    cell, event = _runtime_evidence()
    data = event["data"]
    assert isinstance(data, dict)
    data[preimage_field] = wrong_type
    data[f"{preimage_field}_hash"] = hashlib.sha256(
        canonical_json(wrong_type).encode("utf-8")
    ).hexdigest()

    assert not AuraCellExecutor._runtime_evidence_matches(  # type: ignore[arg-type]
        cell,
        ResearchRuntimeProfile.AURA,
        [event],
    )


@pytest.mark.parametrize(
    "preimage_field",
    ["proposal_set", "approved_command_set", "rejected_command_set"],
)
def test_adapter_rejects_content_addressed_commands_from_inactive_agent(
    preimage_field: str,
) -> None:
    cell, event = _runtime_evidence()
    data = event["data"]
    assert isinstance(data, dict)
    preimage = data[preimage_field]
    assert isinstance(preimage, list)
    preimage.append({"agent_id": "single_direct_agent"})
    data[f"{preimage_field}_hash"] = hashlib.sha256(
        canonical_json(preimage).encode("utf-8")
    ).hexdigest()

    assert not AuraCellExecutor._runtime_evidence_matches(  # type: ignore[arg-type]
        cell,
        ResearchRuntimeProfile.AURA,
        [event],
    )


@pytest.mark.parametrize(
    ("profile", "topology", "governance", "active_agent_ids"),
    [
        (
            ResearchRuntimeProfile.SINGLE_DIRECT,
            "single",
            "none",
            [],
        ),
        (
            ResearchRuntimeProfile.SINGLE_DIRECT,
            "single",
            "none",
            ["single_direct_agent", "lighting_agent"],
        ),
        (
            ResearchRuntimeProfile.AURA,
            "domain_multi",
            "aura",
            ["single_direct_agent"],
        ),
        (
            ResearchRuntimeProfile.AURA,
            "domain_multi",
            "aura",
            ["lighting_agent", "hvac_agent"],
        ),
        (
            ResearchRuntimeProfile.AURA,
            "domain_multi",
            "aura",
            [
                "lighting_agent",
                "hvac_agent",
                "security_agent",
                "energy_agent",
                "scene_agent",
                "extra_agent",
            ],
        ),
    ],
)
def test_adapter_rejects_runtime_evidence_with_wrong_active_agent_set(
    profile: ResearchRuntimeProfile,
    topology: str,
    governance: str,
    active_agent_ids: list[str],
) -> None:
    cell, event = _runtime_evidence(
        profile=profile,
        topology=topology,
        governance=governance,
    )
    data = event["data"]
    assert isinstance(data, dict)
    data["active_agent_ids"] = active_agent_ids

    assert not AuraCellExecutor._runtime_evidence_matches(  # type: ignore[arg-type]
        cell,
        profile,
        [event],
    )


def test_runner_rejects_provenance_that_does_not_match_its_runtime() -> None:
    scenario = load_library([PILOT_DIR])["read_then_leave_001_static"]
    with pytest.raises(ValueError, match="baseline policy"):
        ScenarioRunner(
            scenario,
            baseline_policy=BaselinePolicy.RULE_BASED,
            experiment=_provenance(),
            experiment_runtime=_runtime_selection(),
        )
    unsupported = _provenance().model_copy(update={"observation": "perfect"})
    with pytest.raises(ValueError, match="activated runtime condition"):
        ScenarioRunner(
            scenario,
            baseline_policy=BaselinePolicy.LLM_MOCKED,
            experiment=unsupported,
            experiment_runtime=_runtime_selection(),
        )


@pytest.mark.anyio
async def test_runner_persists_explicit_model_and_experiment_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_MODE", "live")
    scenario = load_library([PILOT_DIR])["read_then_leave_001_static"]
    provenance = _provenance()
    runner = ScenarioRunner(
        scenario,
        baseline_policy=BaselinePolicy.LLM_MOCKED,
        experiment=provenance,
        experiment_runtime=_runtime_selection(),
    )
    try:
        result = await runner.run()
    finally:
        await runner.engine.close()

    assert result.run_metadata.baseline_policy is BaselinePolicy.LLM_MOCKED
    assert result.run_metadata.llm_mode.value == "mocked"
    assert result.run_metadata.experiment == provenance
    persisted = json.loads(
        (run_dir(result.run_id) / RUN_METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert persisted["experiment"] == provenance.model_dump(mode="json")

    report = evaluate_run(result.run_id, scenario_dirs=[PILOT_DIR])
    assert report.provenance["experiment"] == provenance.model_dump(mode="json")


@pytest.mark.anyio
async def test_runner_default_has_no_experiment_provenance() -> None:
    scenario = load_library([PILOT_DIR])["read_then_leave_001_static"]
    runner = ScenarioRunner(scenario)
    try:
        result = await runner.run()
    finally:
        await runner.engine.close()
    assert result.run_metadata.experiment is None


@pytest.mark.anyio
async def test_engine_reset_revalidates_experiment_provenance_at_core_boundary() -> None:
    scenario = load_library([PILOT_DIR])["read_then_leave_001_static"]
    runner = ScenarioRunner(scenario)
    selection = runner.engine.agent_runtime.prepare_baseline_policy(
        BaselinePolicy.RULE_BASED
    )
    previous_run = runner.engine.run_manager.current
    try:
        with pytest.raises(ValueError, match="activated runtime condition"):
            await runner.engine.reset(
                new_state_manager=runner.state_manager,
                scenario=scenario,
                seed=scenario.seed,
                policy_selection=selection,
                experiment=_provenance(),
            )
        assert runner.engine.run_manager.current is previous_run
    finally:
        await runner.engine.close()
