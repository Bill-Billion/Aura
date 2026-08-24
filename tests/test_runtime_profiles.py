from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agents.base import BaseAgent
from backend.agents.generalist import SINGLE_DIRECT_AGENT_ID
from backend.agents.orchestrator import HomeOrchestratorAgent
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.provenance import (
    RESEARCH_RUNTIME_PROFILES,
    ExperimentProvenance,
    ExperimentRuntimeSelection,
    ResearchRuntimeProfile,
)
from backend.experiments.adapters import AuraCellExecutor
from backend.models.schemas import BaselinePolicy
from backend.engine.simulation import SimulationEngine
from backend.main import _init_default_state
from backend.scenarios.loader import load_library
from backend.scenarios.runner import ScenarioRunner


PILOT_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"


@pytest.mark.anyio
@pytest.mark.parametrize("profile", list(ResearchRuntimeProfile))
async def test_scenario_runner_activates_the_recorded_runtime_profile(
    profile: ResearchRuntimeProfile,
    tmp_path: Path,
) -> None:
    topology, governance, observation = RESEARCH_RUNTIME_PROFILES[profile]
    selection = ExperimentRuntimeSelection.for_profile(
        profile,
        model="rule_based",
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    provenance = ExperimentProvenance(
        experiment_id="runtime-profile-test",
        matrix_spec_hash="a" * 64,
        matrix_hash="b" * 64,
        cell_id=f"cell-{hashlib.sha256(profile.value.encode()).hexdigest()[:16]}",
        runtime_profile=profile,
        model="rule_based",
        topology=topology,
        governance=governance,
        observation=observation,
        repetition=0,
    )
    scenario = load_library([PILOT_DIR])["read_then_leave_001_static"]
    runner = ScenarioRunner(
        scenario,
        baseline_policy=BaselinePolicy.RULE_BASED,
        experiment=provenance,
        experiment_runtime=selection,
        run_artifacts_root=tmp_path / profile.value,
    )
    try:
        result = await runner.run()
        runtime = runner.engine.agent_runtime
        assert runtime.active_experiment_runtime == selection
        if profile is ResearchRuntimeProfile.SINGLE_DIRECT:
            assert [agent.agent_id for agent in runtime.agents] == [SINGLE_DIRECT_AGENT_ID]
            assert not isinstance(runtime.orchestrator, HomeOrchestratorAgent)
        else:
            assert len(runtime.agents) == 5

        decisions = [
            event
            for event in result.events
            if event.event_type == "reasoning.coordination_decision"
        ]
        assert decisions
        assert {event.data["runtime_profile"] for event in decisions} == {profile.value}
        assert {event.data["governance"] for event in decisions} == {governance}
        expected_source = {
            "none": "proposal_passthrough",
            "flat_priority": "flat_priority",
            "aura": "arbiter",
        }[governance]
        assert {event.source for event in decisions} == {expected_source}
        assert all(len(event.data["proposal_set_hash"]) == 64 for event in decisions)
        assert AuraCellExecutor._runtime_evidence_matches(
            SimpleNamespace(governance=governance, topology=topology),
            profile,
            [event.model_dump(mode="json") for event in result.events],
        )
        assert result.run_metadata.experiment == provenance
    finally:
        await runner.engine.close()


class _TargetAgent(BaseAgent):
    agent_role = "lighting"

    def __init__(
        self,
        agent_id: str,
        *,
        value: bool = False,
        priority: str = "convenience",
        relevant: bool = True,
    ) -> None:
        super().__init__(agent_id=agent_id, name=agent_id)
        self.value = value
        self.priority = priority
        self.relevant = relevant

    def get_controlled_device_types(self):
        return ["light"]

    def determine_priority(self, world_state, root_event):
        return self.priority

    def is_relevant(self, world_state, root_event):
        return self.relevant and root_event.event_type == "user.activity_change"

    def get_allowed_command_specs(self, world_state, root_event):
        return [{"device_id": "light_living_01", "property": "power"}]

    def decide(self, world_state):
        return [
            {
                "device_id": "light_living_01",
                "property": "power",
                "value": self.value,
                "reason": self.agent_id,
            }
        ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("profile", "expected_actions", "expected_rejections"),
    [
        (ResearchRuntimeProfile.NO_ARBITER, 1, 0),
        (ResearchRuntimeProfile.FLAT_PRIORITY, 1, 1),
        (ResearchRuntimeProfile.AURA, 1, 1),
    ],
)
async def test_runtime_profile_changes_real_same_target_admission(
    profile: ResearchRuntimeProfile,
    expected_actions: int,
    expected_rejections: int,
    tmp_path: Path,
) -> None:
    selection = ExperimentRuntimeSelection.for_profile(
        profile,
        model="rule_based",
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    engine = SimulationEngine(
        EventBus(),
        _init_default_state(),
        ConnectionManager(),
        run_artifacts_root=tmp_path / profile.value,
    )
    runtime = engine.agent_runtime
    runtime.activate_experiment_runtime(selection)
    runtime.agents.clear()
    runtime.register(
        _TargetAgent("lighting_agent", value=False, priority="convenience")
    )
    runtime.register(
        _TargetAgent("hvac_agent", value=True, priority="user_comfort")
    )
    runtime.register(_TargetAgent("security_agent", relevant=False))
    runtime.register(_TargetAgent("energy_agent", relevant=False))
    runtime.register(_TargetAgent("scene_agent", relevant=False))

    root = SimEvent(
        event_type="user.activity_change",
        source="test",
        timestamp=1.0,
        wall_time=1.0,
        correlation_id=f"corr-{profile.value}",
        data={"user_id": "user_01", "to_room": "living_room"},
    )
    try:
        await engine._publish_sim_event(root)
        assert await runtime.wait_for_idle(timeout=10.0)
        events = [
            event
            for event in engine.event_bus.get_history()
            if event.correlation_id == root.correlation_id
        ]
        assert sum(event.event_type == "action.device_control" for event in events) == expected_actions
        assert sum(
            event.event_type == "command.lifecycle"
            and event.data.get("to_status") == "rejected"
            for event in events
        ) == expected_rejections
        decision = next(
            event
            for event in events
            if event.event_type == "reasoning.coordination_decision"
        )
        assert decision.data["runtime_profile"] == profile.value
        assert len(decision.data["approved_commands"]) == (
            2 if profile is ResearchRuntimeProfile.NO_ARBITER else 1
        )
    finally:
        await engine.close()
