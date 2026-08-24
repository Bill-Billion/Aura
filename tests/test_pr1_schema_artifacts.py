"""PR-1 v1/v2 run provenance and evaluator compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import backend.main as main_module
from backend.api.routes import configure_scenario_dirs
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus
from backend.engine.event_log import (
    RUN_METADATA_FILENAME,
    run_dir,
)
from backend.evaluation.evaluator import EvalOutcome, evaluate_run
from backend.models.schemas import (
    BaselinePolicy,
    RunScenarioPayload,
)
from backend.scenarios.loader import load_library
from backend.scenarios.runner import ScenarioRunner, run_scenario

PILOT_DIR = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"
)


@pytest.mark.anyio
async def test_old_v1_finalized_artifact_remains_evaluable() -> None:
    result = await run_scenario("user_arrives_home_evening")
    report = evaluate_run(result.run_id)
    assert report.outcome is not EvalOutcome.ERROR
    assert report.provenance["scenario_schema_version"] == "1.0"


@pytest.mark.anyio
async def test_v2_artifact_persists_pair_and_trace_provenance_and_is_evaluable() -> (
    None
):
    static = load_library([PILOT_DIR])["read_then_leave_001_static"]
    result = await run_scenario(static)
    metadata = json.loads(
        (run_dir(result.run_id) / RUN_METADATA_FILENAME).read_text(encoding="utf-8")
    )
    assert metadata["scenario_schema_version"] == "2.0"
    assert metadata["counterfactual_group_id"] == "read_then_leave_001"
    assert metadata["counterfactual_variant"] == "static"
    assert len(metadata["trace_spec_hash"]) == 64

    report = evaluate_run(result.run_id, scenario_dirs=[PILOT_DIR])
    assert report.outcome is not EvalOutcome.ERROR
    assert report.provenance["scenario_schema_version"] == "2.0"


@pytest.mark.anyio
async def test_engine_installs_dynamic_v2_resident_runtime() -> None:
    library = load_library([PILOT_DIR], validate_pairs=True)
    static = library["read_then_leave_001_static"]
    dynamic = library["read_then_leave_001_dynamic"]
    runner = ScenarioRunner(static)
    engine = runner.engine
    await engine.reset(
        new_state_manager=runner.state_manager,
        scenario=dynamic,
        seed=dynamic.seed,
    )
    assert engine.run_manager.current is not None
    assert engine.run_manager.current.scenario_id == dynamic.id
    assert engine.resident_engine is not None
    assert engine.resident_engine.state_for("user_01") is not None
    await engine.close()


@pytest.mark.anyio
async def test_runner_executes_dynamic_v2_resident_state_change() -> None:
    dynamic = load_library([PILOT_DIR], validate_pairs=True)[
        "read_then_leave_001_dynamic"
    ]

    runner = ScenarioRunner(dynamic)
    result = await runner.run()
    perturbation = next(
        event
        for event in result.events
        if event.generation_rule_id == "perturbation.resident_state_change"
    )
    assert perturbation.event_generation_mode == "scripted"
    assert perturbation.sim_time_s == 10
    assert result.run_metadata.scenario_id == dynamic.id
    assert runner.state_manager.world.users["user_01"].location is None


@pytest.mark.anyio
async def test_live_launcher_accepts_dynamic_v2_resident_runtime() -> None:
    previous_engine = main_module.simulation_engine
    previous_state_manager = main_module.state_manager
    launch_state = main_module._init_default_state()
    engine = main_module.SimulationEngine(EventBus(), launch_state, ConnectionManager())
    main_module.simulation_engine = engine
    main_module.state_manager = launch_state
    configure_scenario_dirs([PILOT_DIR])
    try:
        launched = await main_module.start_scenario_run(
            RunScenarioPayload(
                scenario_id="read_then_leave_001_dynamic",
                baseline_policy=BaselinePolicy.RULE_BASED,
            )
        )
        assert launched["scenario_id"] == "read_then_leave_001_dynamic"
        assert engine.resident_engine is not None
    finally:
        await main_module._cancel_scenario_finalizer()
        await engine.close()
        configure_scenario_dirs(None)
        main_module.simulation_engine = previous_engine
        main_module.state_manager = previous_state_manager


@pytest.mark.anyio
async def test_unknown_scenario_major_in_artifact_is_an_evaluation_error() -> None:
    result = await run_scenario("user_arrives_home_evening")
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["scenario_schema_version"] = "9.0"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    report = evaluate_run(result.run_id)
    assert report.outcome is EvalOutcome.ERROR
    assert "unknown major" in report.failure_reasons[0]
