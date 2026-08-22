"""PR-3 deterministic resident ground truth and perturbation integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.ws import ConnectionManager
from backend.devices.latency import DeviceRuntimeProfile
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.observation import build_observable_view
from backend.engine.rng import SimRandom
from backend.engine.simulation import (
    PerturbationRuntimeUnavailableError,
    SimulationEngine,
)
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    UserState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.execution.command import CommandSource, CommandStatus, DeviceCommand
from backend.residents import ResidentEngine
from backend.residents.policy import (
    DeterministicResponsivePolicy,
    ScriptedResidentPolicy,
    SeededStochasticResidentPolicy,
)
from backend.scenarios.loader import load_library
from backend.scenarios.runner import ScenarioRunner
from backend.scenarios.spec_v2 import ResidentReference, ScenarioSpecV2
from backend.scenarios.trace import canonical_trace_text


PILOT_DIR = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"
)


def _world(*, users: tuple[str, ...] = ("alice",)) -> WorldState:
    world = WorldState()
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=23.0,
            light_level=20.0,
            occupancy=True,
            persons=list(users),
        )
    }
    world.users = {
        user_id: UserState(
            id=user_id,
            location=Location3D(room="living_room"),
            activity="reading",
        )
        for user_id in users
    }
    world.devices = {
        "light": DeviceState(
            id="light",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(
                power=False,
                extra={"online": True, "brightness": 0},
            ),
        )
    }
    return world


def _reference(user_id: str) -> ResidentReference:
    return ResidentReference(
        user_id=user_id,
        profile_id="resident_reader_v1",
        authority_level="adult",
    )


def _trigger(sim_time_s: float, *, event_type: str = "environment.state_refresh") -> SimEvent:
    return SimEvent(
        event_type=event_type,
        source="test",
        timestamp=sim_time_s,
        sim_time_s=sim_time_s,
        data={"significant_change_reasons": ["test"]},
    )


def test_resident_state_is_hidden_and_corrections_are_bounded() -> None:
    world = _world()
    runtime = ResidentEngine(
        [_reference("alice")], world=world, policy_kind="responsive", rng=SimRandom(7)
    )
    state = runtime.state_for("alice")
    assert state is not None
    assert state.goal == "reading_comfort"
    assert state.location == "living_room"
    assert "override" in state.permissions
    assert isinstance(runtime.policies["alice"], DeterministicResponsivePolicy)

    events = []
    for sim_time_s in (0.0, 5.0, 10.0, 20.0, 30.0):
        events.extend(
            runtime.handle_event(
                world, _trigger(sim_time_s), sim_time_s=sim_time_s
            )
        )
    requests = [event for event in events if event.event_type == "user.command"]
    assert len(requests) == state.long_term_preferences.max_corrections == 3
    assert requests[0].data["resident_request"] is True
    assert requests[0].data["device_id"] == "light"
    assert requests[0].data["capability"] == "power"
    assert requests[0].data["value"] is True

    # Latent profile/preferences are absent from the canonical and observable
    # world, and behavior events reveal only the resulting request.
    assert "long_term_preferences" not in world.model_dump(mode="json")
    assert "long_term_preferences" not in build_observable_view(world).model_dump(
        mode="json"
    )
    assert all("long_term_preferences" not in event.data for event in events)


def test_seeded_policy_uses_per_resident_substreams() -> None:
    single = ResidentEngine(
        [_reference("alice")],
        world=_world(),
        policy_kind="seeded_stochastic",
        rng=SimRandom(42),
    )
    pair = ResidentEngine(
        [_reference("alice"), _reference("bob")],
        world=_world(users=("alice", "bob")),
        policy_kind="seeded_stochastic",
        rng=SimRandom(42),
    )
    single_policy = single.policies["alice"]
    pair_policy = pair.policies["alice"]
    bob_policy = pair.policies["bob"]
    assert isinstance(single_policy, SeededStochasticResidentPolicy)
    assert isinstance(pair_policy, SeededStochasticResidentPolicy)
    assert isinstance(bob_policy, SeededStochasticResidentPolicy)
    assert single_policy.stream.name == pair_policy.stream.name == "user_sim:alice"
    assert bob_policy.stream.name == "user_sim:bob"
    assert [single_policy.stream.random() for _ in range(5)] == [
        pair_policy.stream.random() for _ in range(5)
    ]


def test_scripted_policy_retains_existing_user_simulator() -> None:
    runtime = ResidentEngine(
        [_reference("alice")], world=_world(), policy_kind="scripted"
    )
    policy = runtime.policies["alice"]
    assert isinstance(policy, ScriptedResidentPolicy)
    assert policy.step(_world())


@pytest.mark.anyio
async def test_dynamic_resident_trace_replays_byte_identically() -> None:
    dynamic = load_library([PILOT_DIR], validate_pairs=True)[
        "read_then_leave_001_dynamic"
    ]
    first = await ScenarioRunner(dynamic).run()
    second = await ScenarioRunner(dynamic).run()
    assert first.seed == second.seed == dynamic.seed
    assert canonical_trace_text(first.events) == canonical_trace_text(second.events)


@pytest.mark.anyio
async def test_observation_delay_remains_fail_closed() -> None:
    dynamic = load_library([PILOT_DIR], validate_pairs=True)[
        "read_then_leave_001_dynamic"
    ]
    payload = dynamic.model_dump(mode="json")
    payload["counterfactual"]["factor"] = "observation_delay"
    payload["perturbations"] = [
        {
            "type": "observation_delay",
            "phase": "after_perception_before_plan",
            "at_sim_time_s": 5,
            "device_id": "light_living_01",
            "delay_seconds": 10,
        }
    ]
    spec = ScenarioSpecV2.model_validate(payload)
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    try:
        with pytest.raises(PerturbationRuntimeUnavailableError) as excinfo:
            await engine.reset(
                new_state_manager=state,
                scenario=spec,
                seed=spec.seed,
            )
        assert excinfo.value.unsupported_perturbation_types == ("observation_delay",)
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_scheduled_safety_interrupt_preempts_equal_time_operation() -> None:
    dynamic = load_library([PILOT_DIR], validate_pairs=True)[
        "read_then_leave_001_dynamic"
    ]
    payload = dynamic.model_dump(mode="json")
    payload["counterfactual"]["factor"] = "safety_interrupt"
    payload["perturbations"] = [
        {
            "type": "safety_interrupt",
            "phase": "during_execution",
            "at_sim_time_s": 10,
            "room_id": "living_room",
            "severity": "critical",
        }
    ]
    spec = ScenarioSpecV2.model_validate(payload)
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        engine.command_executor.runtime_profile = lambda _command: DeviceRuntimeProfile(
            start_delay_s=10
        )
        record = await engine.command_executor.submit(
            DeviceCommand(
                source=CommandSource.SCENARIO,
                device_id="light_living_01",
                capability="power",
                value=True,
            ),
            publish=engine._publish_sim_event,
        )
        assert record.status is CommandStatus.EXECUTING
        await engine.timer.tick_once()  # t=0
        await engine.timer.tick_once()  # t=10: safety queue runs before device queue
        assert record.status is CommandStatus.CANCELLED
        safety = next(
            event
            for event in engine.event_bus.get_history()
            if event.generation_rule_id == "perturbation.safety_interrupt"
        )
        assert safety.event_generation_mode == "scripted"
        assert safety.causal_parent is None
    finally:
        await engine.close()
