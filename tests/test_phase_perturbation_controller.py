"""ScenarioSpec 2.1 event-relative perturbation runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.ws import ConnectionManager
from backend.devices.latency import DeviceRuntimeProfile
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.simulation import SimulationEngine
from backend.execution.command import CommandSource, CommandStatus, DeviceCommand
from backend.scenarios.loader import load_library
from backend.scenarios.phase_controller import (
    PERTURBATION_INJECTED_EVENT_TYPE,
    PERTURBATION_PHASE_VIOLATION_EVENT_TYPE,
    PerturbationPhaseError,
    PhasePerturbationController,
)
from backend.scenarios.runner import (
    ScenarioRunError,
    ScenarioRunErrorCode,
    ScenarioRunner,
)
from backend.scenarios.spec_v2 import ScenarioSpecV2

PILOT_DIR = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"
)


def _dynamic_spec(*, offset_seconds: float = 0) -> ScenarioSpecV2:
    spec = load_library([PILOT_DIR], validate_pairs=True)["read_then_leave_001_dynamic"]
    if offset_seconds == 0:
        return spec
    payload = spec.model_dump(mode="json")
    payload["perturbations"][0]["offset_seconds"] = offset_seconds
    return ScenarioSpecV2.model_validate(payload)


def _factor_spec(factor: str) -> ScenarioSpecV2:
    payload = _dynamic_spec().model_dump(mode="json")
    common = {
        "type": factor,
        "phase": "after_plan_before_execution",
        "anchor": {
            "event_type": "reasoning.execution_plan",
            "relation": "same_correlation",
            "occurrence": "first",
        },
        "offset_seconds": 0,
        "must_precede": {"event_type": "action.device_control"},
    }
    details = {
        "resident_state_change": {
            "user_id": "user_01",
            "room_id": "outside",
            "activity": "away",
        },
        "device_failure": {
            "device_id": "light_living_01",
            "failure": "offline",
        },
        "conflicting_request": {
            "user_id": "user_01",
            "room_id": "living_room",
            "intent": "turn lights off",
        },
        "safety_interrupt": {
            "room_id": "living_room",
            "event_type": "safety.smoke_detected",
            "severity": "critical",
        },
        "feedback_loss": {"device_id": "light_living_01", "drop_count": 1},
    }
    payload["counterfactual"]["factor"] = factor
    payload["perturbations"] = [{**common, **details[factor]}]
    payload["intervention_response"]["trigger"]["where"][0]["value"] = factor
    return ScenarioSpecV2.model_validate(payload)


def _phase_spec(
    factor: str,
    phase: str,
    anchor_event_type: str,
    successor_event_type: str,
    *,
    anchor_where: list[dict] | None = None,
    offset_seconds: float = 0,
) -> ScenarioSpecV2:
    payload = _factor_spec(factor).model_dump(mode="json")
    perturbation = payload["perturbations"][0]
    perturbation["phase"] = phase
    perturbation["anchor"] = {
        "event_type": anchor_event_type,
        "relation": "same_correlation",
        "occurrence": "first",
        "where": anchor_where or [],
    }
    perturbation["must_precede"] = {"event_type": successor_event_type}
    perturbation["offset_seconds"] = offset_seconds
    return ScenarioSpecV2.model_validate(payload)


def _event(
    event_type: str,
    *,
    sim_time_s: float,
    correlation_id: str = "episode-1",
) -> SimEvent:
    return SimEvent(
        event_type=event_type,
        source="test",
        timestamp=sim_time_s,
        sim_time_s=sim_time_s,
        correlation_id=correlation_id,
    )


def _controller(
    bus: EventBus,
    spec: ScenarioSpecV2,
    injected: list[tuple[SimEvent, float]],
    *,
    run_id: str = "run-1",
) -> PhasePerturbationController:
    async def inject(_perturbation, evidence: SimEvent, sim_time_s: float) -> None:
        injected.append((evidence, sim_time_s))

    controller = PhasePerturbationController(
        spec,
        run_id=run_id,
        publish=bus.publish_visible,
        stamp=bus.stamp,
        inject=inject,
        tick_source=lambda: 1,
    )
    bus.set_run_context(run_id, spec.id)
    bus.subscribe("*", controller.handle_event)
    return controller


@pytest.mark.anyio
async def test_zero_offset_injection_is_strictly_between_plan_and_action() -> None:
    bus = EventBus()
    injected: list[tuple[SimEvent, float]] = []
    controller = _controller(bus, _dynamic_spec(), injected)

    plan = await bus.publish_visible(_event("reasoning.execution_plan", sim_time_s=4))
    action = await bus.publish_visible(_event("action.device_control", sim_time_s=4))
    await controller.finalize()

    evidence, injected_at = injected[0]
    assert plan.seq is not None and evidence.seq is not None and action.seq is not None
    assert plan.seq < evidence.seq < action.seq
    assert injected_at == 4
    assert evidence.correlation_id == plan.correlation_id
    assert evidence.causal_parent == plan.event_id
    assert evidence.data["actual_seq"] == evidence.seq
    assert evidence.data["anchor_event_id"] == plan.event_id
    assert evidence.data["expected_predecessor"]["event_type"] == plan.event_type
    assert evidence.data["expected_successor"]["event_type"] == action.event_type


@pytest.mark.anyio
async def test_nonzero_offset_waits_for_exact_simulated_time() -> None:
    bus = EventBus()
    injected: list[tuple[SimEvent, float]] = []
    controller = _controller(bus, _dynamic_spec(offset_seconds=3), injected)

    await bus.publish_visible(_event("reasoning.execution_plan", sim_time_s=1))
    await controller.advance(3.999)
    assert injected == []
    await controller.advance(4)

    assert injected[0][1] == 4
    assert injected[0][0].sim_time_s == 4


@pytest.mark.anyio
async def test_successor_before_due_time_invalidates_phase() -> None:
    bus = EventBus()
    injected: list[tuple[SimEvent, float]] = []
    controller = _controller(bus, _dynamic_spec(offset_seconds=3), injected)

    await bus.publish_visible(_event("reasoning.execution_plan", sim_time_s=1))
    await bus.publish_visible(_event("action.device_control", sim_time_s=2))

    with pytest.raises(
        PerturbationPhaseError, match="successor_observed_before_injection"
    ):
        await controller.finalize()
    violation = next(
        event
        for event in bus.get_history()
        if event.event_type == PERTURBATION_PHASE_VIOLATION_EVENT_TYPE
    )
    assert violation.data["reason"] == "successor_observed_before_injection"


@pytest.mark.anyio
async def test_suppressed_injection_evidence_fails_without_physical_change() -> None:
    bus = EventBus(max_causal_depth=1)
    injected: list[tuple[SimEvent, float]] = []
    controller = _controller(bus, _dynamic_spec(), injected)
    root = await bus.publish_visible(_event("user.command", sim_time_s=1))
    anchor = _event("reasoning.execution_plan", sim_time_s=1)
    anchor.causal_parent = root.event_id

    await bus.publish_visible(anchor)

    assert injected == []
    with pytest.raises(PerturbationPhaseError, match="injection_evidence_suppressed"):
        await controller.finalize()
    assert bus.get_history()[-1].event_type == PERTURBATION_PHASE_VIOLATION_EVENT_TYPE


@pytest.mark.anyio
async def test_stale_event_from_previous_run_cannot_trigger_new_controller() -> None:
    bus = EventBus()
    injected: list[tuple[SimEvent, float]] = []
    controller = _controller(bus, _dynamic_spec(), injected, run_id="run-new")
    stale = _event("reasoning.execution_plan", sim_time_s=1)
    stale.run_id = "run-old"

    await bus.publish_visible(stale)

    assert injected == []
    with pytest.raises(PerturbationPhaseError, match="anchor_not_observed"):
        await controller.finalize()


@pytest.mark.anyio
async def test_runner_marks_missing_anchor_run_invalid() -> None:
    payload = _dynamic_spec().model_dump(mode="json")
    payload["duration_seconds"] = 1
    payload["timeline"] = [
        {
            "at": 0,
            "type": "user.command",
            "device_id": "light_living_01",
            "payload": {"capability": "power"},
        }
    ]
    spec = ScenarioSpecV2.model_validate(payload)

    runner = ScenarioRunner(spec)
    runner.engine.agent_runtime.agents.clear()
    with pytest.raises(ScenarioRunError) as excinfo:
        await runner.run()

    assert excinfo.value.code is ScenarioRunErrorCode.PERTURBATION_PHASE_INVALID
    assert excinfo.value.details["reason"] == "anchor_not_observed"


@pytest.mark.anyio
async def test_production_finalizer_cannot_complete_missing_anchor() -> None:
    import backend.main as main_module

    spec = _dynamic_spec()
    state = main_module._init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    previous_engine = main_module.simulation_engine
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        run_id = engine.run_id
        assert run_id is not None
        main_module.simulation_engine = engine

        await main_module._finalize_scenario_run(run_id, 0)

        metadata = engine.run_manager.finished[-1]
        assert metadata.run_id == run_id
        assert metadata.end_reason == "perturbation_phase_invalid"
        assert any(
            event.event_type == PERTURBATION_PHASE_VIOLATION_EVENT_TYPE
            for event in engine.event_bus.get_history()
        )
    finally:
        main_module.simulation_engine = previous_engine
        await engine.close()


@pytest.mark.anyio
async def test_dynamic_pilot_persists_anchor_evidence_and_physical_change() -> None:
    spec = _dynamic_spec()
    runner = ScenarioRunner(spec)
    result = await runner.run()
    history = list(result.events)

    seqs = [event.seq for event in history]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))

    evidence = next(
        event
        for event in history
        if event.event_type == PERTURBATION_INJECTED_EVENT_TYPE
    )
    plan = next(event for event in history if event.event_id == evidence.causal_parent)
    actions = [
        event
        for event in history
        if event.event_type == "action.device_control"
        and event.data.get("device_id") == "light_living_01"
        and event.seq is not None
        and evidence.seq is not None
        and event.seq > evidence.seq
    ]
    physical = next(
        event
        for event in history
        if event.event_type == "user.activity_change"
        and event.data.get("perturbation_type") == "resident_state_change"
    )
    discarded_events = [
        event
        for event in history
        if event.event_type == "reasoning.decision_discarded"
        and event.causal_parent == plan.event_id
    ]
    assert len(discarded_events) == 1
    discarded = discarded_events[0]

    assert all(
        seq is not None
        for seq in (plan.seq, evidence.seq, physical.seq, discarded.seq)
    )
    assert plan.seq < evidence.seq < physical.seq < discarded.seq
    assert physical.causal_parent == evidence.event_id
    assert physical.correlation_id == evidence.correlation_id
    assert discarded.data["reason"] == "invalidated_assumption"
    assert {
        item["path"]: (item["expected"], item["actual"], item["missing"])
        for item in discarded.data["invalidated_assumptions"]
    } == {
        "rooms[living_room].occupancy": (True, False, False),
        "users[user_01].activity": ("relaxing", "away", False),
        "users[user_01].location.room": ("living_room", None, False),
    }
    assert discarded.data["discarded_commands"] == plan.data["commands"]
    assert not actions
    assert not any(
        event.event_type == "action.device_control"
        and event.causal_parent == plan.event_id
        for event in history
    )
    assert not any(
        event.event_type == "command.lifecycle"
        and event.causal_parent == plan.event_id
        and event.data.get("to_status")
        in {"approved", "validated", "executing", "succeeded"}
        for event in history
    )
    assert runner.state_manager.world.users["user_01"].location is None
    assert runner.state_manager.world.users["user_01"].activity == "away"
    assert runner.state_manager.world.devices["light_living_01"].state.power is True


@pytest.mark.anyio
async def test_resident_change_at_executor_boundary_cancels_stale_plan() -> None:
    spec = _phase_spec(
        "resident_state_change",
        "during_execution",
        "command.lifecycle",
        "device.effect_applied",
        anchor_where=[
            {"path": "data.to_status", "comparator": "eq", "value": "executing"}
        ],
    )
    result = await ScenarioRunner(spec).run()
    history = list(result.events)

    evidence = next(
        event
        for event in history
        if event.event_type == PERTURBATION_INJECTED_EVENT_TYPE
    )
    executing = next(event for event in history if event.event_id == evidence.causal_parent)
    plan = next(event for event in history if event.event_id == executing.causal_parent)
    discarded_events = [
        event
        for event in history
        if event.event_type == "reasoning.decision_discarded"
        and event.causal_parent == plan.event_id
    ]
    assert len(discarded_events) == 1
    discarded = discarded_events[0]

    assert executing.data["to_status"] == "executing"
    assert discarded.data["reason"] == "invalidated_assumption"
    assert discarded.data["discarded_commands"] == plan.data["commands"]
    assert not any(
        event.event_type == "action.device_control"
        and event.causal_parent == plan.event_id
        for event in history
    )
    cancelled = [
        event
        for event in history
        if event.event_type == "command.lifecycle"
        and event.causal_parent == plan.event_id
        and event.data.get("to_status") == "cancelled"
        and event.data.get("detail")
        == "proposal assumption invalidated before execution"
    ]
    assert len(cancelled) == len(plan.data["commands"])


@pytest.mark.anyio
async def test_device_change_at_executor_boundary_cancels_stale_target() -> None:
    spec = _phase_spec(
        "device_failure",
        "during_execution",
        "command.lifecycle",
        "device.effect_applied",
        anchor_where=[
            {"path": "data.to_status", "comparator": "eq", "value": "executing"}
        ],
    )
    result = await ScenarioRunner(spec).run()
    history = list(result.events)

    evidence = next(
        event
        for event in history
        if event.event_type == PERTURBATION_INJECTED_EVENT_TYPE
    )
    executing = next(event for event in history if event.event_id == evidence.causal_parent)
    plan = next(event for event in history if event.event_id == executing.causal_parent)
    discarded = next(
        event
        for event in history
        if event.event_type == "reasoning.decision_discarded"
        and event.causal_parent == plan.event_id
    )

    assert discarded.data["reason"] == "stale"
    assert discarded.data["stale_device_ids"] == ["light_living_01"]
    living_commands = [
        item
        for item in plan.data["commands"]
        if item["device_id"] == "light_living_01"
    ]
    assert discarded.data["discarded_commands"] == living_commands
    assert not any(
        event.event_type == "action.device_control"
        and event.data.get("device_id") == "light_living_01"
        and event.seq is not None
        and evidence.seq is not None
        and event.seq > evidence.seq
        for event in history
    )
    assert [
        (event.data["device_id"], event.data["property"])
        for event in history
        if event.event_type == "action.device_control"
        and event.causal_parent == plan.event_id
        and event.seq is not None
        and evidence.seq is not None
        and event.seq > evidence.seq
    ] == [
        (item["device_id"], item["property"])
        for item in plan.data["commands"]
        if item["device_id"] != "light_living_01"
    ]


@pytest.mark.anyio
async def test_reset_detaches_previous_phase_controller() -> None:
    dynamic = _dynamic_spec()
    static = load_library([PILOT_DIR], validate_pairs=True)[
        "read_then_leave_001_static"
    ]
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    try:
        await engine.reset(new_state_manager=state, scenario=dynamic, seed=dynamic.seed)
        assert engine.phase_controller is not None
        await engine.reset(new_state_manager=state, scenario=static, seed=static.seed)
        assert engine.phase_controller is None
        assert engine.agent_runtime.before_observation_hook is None
        assert not engine.agent_runtime.emit_perception_before_plan
        await engine.event_bus.publish_visible(
            SimEvent(
                event_type="reasoning.execution_plan",
                source="test",
                timestamp=0,
                scenario_id=dynamic.id,
            )
        )
        assert all(
            event.event_type != PERTURBATION_INJECTED_EVENT_TYPE
            for event in engine.event_bus.get_history()
        )
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_before_perception_injects_before_observable_snapshot() -> None:
    spec = _phase_spec(
        "resident_state_change",
        "before_perception",
        "user.enters_room",
        "reasoning.perception_snapshot",
    )
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    original_plan = engine.agent_runtime._plan_episode
    planned_activities: list[str | None] = []

    async def plan_from_post_intervention_snapshot(root_event, snapshot, candidates):
        if root_event.event_type == "user.enters_room":
            planned_activities.append(snapshot.users["user_01"].activity)
        return await original_plan(root_event, snapshot, candidates)

    engine.agent_runtime._plan_episode = plan_from_post_intervention_snapshot
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        await engine.start(drive_timer=False)
        await engine.timer.tick_once()
        assert await engine.agent_runtime.wait_for_idle(timeout=5)
        await engine.finalize_perturbation_phase()

        history = engine.event_bus.get_history()
        root = next(
            event for event in history if event.event_type == "user.enters_room"
        )
        evidence = next(
            event
            for event in history
            if event.event_type == PERTURBATION_INJECTED_EVENT_TYPE
        )
        physical = next(
            event
            for event in history
            if event.event_type == "user.activity_change"
            and event.data.get("perturbation_type") == "resident_state_change"
        )
        perception = next(
            event
            for event in history
            if event.event_type == "reasoning.perception_snapshot"
            and event.causal_parent == root.event_id
        )
        assert planned_activities == ["away"]
        assert root.seq < evidence.seq < physical.seq < perception.seq
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_after_perception_injects_before_planning_starts() -> None:
    spec = _phase_spec(
        "resident_state_change",
        "after_perception_before_plan",
        "reasoning.perception_snapshot",
        "reasoning.execution_plan",
    )
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    original_plan = engine.agent_runtime._plan_episode
    planned_world_activities: dict[str, str | None] = {}

    async def plan_after_intervention(root_event, snapshot, candidates):
        planned_world_activities[root_event.event_id] = state.world.users[
            "user_01"
        ].activity
        return await original_plan(root_event, snapshot, candidates)

    engine.agent_runtime._plan_episode = plan_after_intervention
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        await engine.start(drive_timer=False)
        await engine.timer.tick_once()
        assert await engine.agent_runtime.wait_for_idle(timeout=5)
        await engine.finalize_perturbation_phase()

        history = engine.event_bus.get_history()
        evidence = next(
            event
            for event in history
            if event.event_type == PERTURBATION_INJECTED_EVENT_TYPE
        )
        perception = next(
            event
            for event in history
            if event.event_type == "reasoning.perception_snapshot"
            and event.event_id == evidence.causal_parent
        )
        root = next(
            event for event in history if event.event_id == perception.causal_parent
        )
        plan = next(
            event
            for event in history
            if event.event_type == "reasoning.execution_plan"
            and event.correlation_id == perception.correlation_id
        )
        perceptions = [
            event
            for event in history
            if event.event_type == "reasoning.perception_snapshot"
            and event.causal_parent == root.event_id
        ]
        assert planned_world_activities[root.event_id] == "away"
        assert len(perceptions) == 1
        assert root.seq < perception.seq < evidence.seq < plan.seq
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_during_execution_safety_cancels_before_action_is_emitted() -> None:
    spec = _phase_spec(
        "safety_interrupt",
        "during_execution",
        "command.lifecycle",
        "device.effect_applied",
        anchor_where=[
            {"path": "data.to_status", "comparator": "eq", "value": "executing"}
        ],
    )
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        record = await engine.command_executor.submit(
            DeviceCommand(
                source=CommandSource.SCENARIO,
                device_id="light_living_01",
                capability="power",
                value=True,
            ),
            publish=engine._publish_sim_event,
        )
        await engine.finalize_perturbation_phase()

        assert record.status is CommandStatus.CANCELLED
        assert not engine.command_executor.device_runtime.operations
        history = engine.event_bus.get_history()
        assert any(
            event.event_type == PERTURBATION_INJECTED_EVENT_TYPE for event in history
        )
        assert not any(event.event_type == "action.device_control" for event in history)
        assert not any(event.event_type == "device.effect_applied" for event in history)
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_after_execution_feedback_loss_is_armed_before_feedback() -> None:
    spec = _phase_spec(
        "feedback_loss",
        "after_execution_before_feedback",
        "device.effect_applied",
        "feedback.state_delta",
    )
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        engine.command_executor.runtime_profile = lambda _command: DeviceRuntimeProfile(
            start_delay_s=1,
            feedback_delay_s=1,
            feedback_timeout_s=2,
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
        await engine.command_executor.advance_device_runtime(1, tick=1)

        history = engine.event_bus.get_history()
        effect = next(
            event for event in history if event.event_type == "device.effect_applied"
        )
        evidence = next(
            event
            for event in history
            if event.event_type == PERTURBATION_INJECTED_EVENT_TYPE
        )
        assert effect.seq < evidence.seq
        assert not any(event.event_type == "feedback.state_delta" for event in history)

        await engine.command_executor.advance_device_runtime(3, tick=2)
        assert record.status is CommandStatus.TIMED_OUT
        await engine.finalize_perturbation_phase()
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_device_deadline_can_create_an_earlier_nonzero_phase_deadline() -> None:
    spec = _phase_spec(
        "resident_state_change",
        "after_execution_before_feedback",
        "device.effect_applied",
        "feedback.state_delta",
        offset_seconds=2,
    )
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        engine.command_executor.runtime_profile = lambda _command: DeviceRuntimeProfile(
            start_delay_s=5,
            feedback_delay_s=3,
            feedback_timeout_s=4,
        )
        await engine.command_executor.submit(
            DeviceCommand(
                source=CommandSource.SCENARIO,
                device_id="light_living_01",
                capability="power",
                value=False,
            ),
            publish=engine._publish_sim_event,
        )

        await engine._advance_scheduled_runtimes(timer_tick=1, sim_time_s=10)

        ordered = [
            (event.event_type, event.sim_time_s)
            for event in engine.event_bus.get_history()
            if event.event_type
            in {
                "device.effect_applied",
                PERTURBATION_INJECTED_EVENT_TYPE,
                "user.activity_change",
                "feedback.state_delta",
            }
        ]
        assert ordered[:3] == [
            ("device.effect_applied", 5),
            (PERTURBATION_INJECTED_EVENT_TYPE, 7),
            ("user.activity_change", 7),
        ]
        assert ordered[3:]
        assert set(ordered[3:]) == {("feedback.state_delta", 8)}
        await engine.finalize_perturbation_phase()
    finally:
        await engine.close()


@pytest.mark.parametrize(
    ("factor", "physical_event_type"),
    [
        ("device_failure", "device.offline"),
        ("conflicting_request", "user.command"),
    ],
)
@pytest.mark.anyio
async def test_remaining_factors_inject_immediately_after_anchor(
    factor: str, physical_event_type: str | None
) -> None:
    spec = _factor_spec(factor)
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(), state, ConnectionManager())
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        plan = await engine._publish_sim_event(
            SimEvent(
                event_type="reasoning.execution_plan",
                source="test",
                timestamp=0,
                sim_time_s=0,
                correlation_id="episode-factor",
            )
        )
        history = engine.event_bus.get_history()
        evidence = next(
            event
            for event in history
            if event.event_type == PERTURBATION_INJECTED_EVENT_TYPE
        )
        assert plan.seq is not None and evidence.seq == plan.seq + 1
        if physical_event_type is not None:
            physical = next(
                event
                for event in history
                if event.event_type == physical_event_type
                and event.data.get("perturbation_type") == factor
            )
            assert physical.causal_parent == evidence.event_id
            assert physical.correlation_id == evidence.correlation_id

        if factor == "device_failure":
            device = state.world.devices["light_living_01"]
            assert device.state.extra["online"] is False
        await engine.finalize_perturbation_phase()
    finally:
        await engine.close()


@pytest.mark.parametrize("factor", ["resident_state_change", "device_failure"])
@pytest.mark.anyio
async def test_suppressed_physical_event_invalidates_without_mutation(
    factor: str,
) -> None:
    spec = _factor_spec(factor)
    from backend.main import _init_default_state

    state = _init_default_state()
    engine = SimulationEngine(EventBus(max_causal_depth=2), state, ConnectionManager())
    try:
        await engine.reset(new_state_manager=state, scenario=spec, seed=spec.seed)
        initial_activity = state.world.users["user_01"].activity
        root = await engine._publish_sim_event(
            SimEvent(
                event_type="test.root",
                source="test",
                timestamp=0,
                sim_time_s=0,
                correlation_id="depth-cap",
            )
        )
        plan = SimEvent(
            event_type="reasoning.execution_plan",
            source="test",
            timestamp=0,
            sim_time_s=0,
            correlation_id=root.correlation_id,
            causal_parent=root.event_id,
        )
        await engine._publish_sim_event(plan)

        with pytest.raises(
            PerturbationPhaseError, match="perturbation_injection_failed"
        ):
            await engine.finalize_perturbation_phase()
        assert state.world.users["user_01"].activity == initial_activity
        assert not engine.command_executor.device_runtime.failures.is_offline(
            "light_living_01", 0
        )
        assert any(
            event.event_type == PERTURBATION_PHASE_VIOLATION_EVENT_TYPE
            for event in engine.event_bus.get_history()
        )
    finally:
        await engine.close()
