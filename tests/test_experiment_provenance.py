"""PR-5 explicit experiment conditions persist with every sealed run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.engine.event_log import RUN_METADATA_FILENAME, run_dir
from backend.engine.provenance import (
    ExperimentProvenance,
    ExperimentRuntimeSelection,
)
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
        model="mocked",
        topology="domain_multi",
        governance="aura",
        observation="stale_offline",
        repetition=1,
    )


def _runtime_selection() -> ExperimentRuntimeSelection:
    return ExperimentRuntimeSelection(
        model="mocked",
        baseline_policy=BaselinePolicy.LLM_MOCKED,
    )


def test_experiment_provenance_rejects_untyped_conditions() -> None:
    payload = _provenance().model_dump(mode="json")
    payload["governance"] = "pretend_aura"
    with pytest.raises(ValidationError):
        ExperimentProvenance.model_validate(payload)


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
