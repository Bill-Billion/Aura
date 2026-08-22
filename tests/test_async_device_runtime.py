"""PR-2 deterministic device operation and feedback semantics."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.api.ws import ConnectionManager
from backend.devices.latency import DeviceRuntimeProfile
from backend.devices.operation import OperationKind
from backend.devices.scheduler import SimTimeScheduler
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.simulation import SimulationEngine
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.execution.command import (
    LIFECYCLE_EVENT_TYPE,
    CommandSource,
    CommandStatus,
    DeviceCommand,
)
from backend.execution.executor import (
    COMMAND_FAILED_EVENT_TYPE,
    DEVICE_EFFECT_APPLIED_EVENT_TYPE,
    FEEDBACK_EVENT_TYPE,
    CommandExecutor,
)
from backend.scenarios.spec_v2 import (
    PERTURBATION_HANDLER_REGISTRY,
    ConflictingRequestPerturbation,
    DeviceFailurePerturbation,
    FeedbackLossPerturbation,
    ResidentStateChangePerturbation,
    SafetyInterruptPerturbation,
)


def _world(*, online: bool = True) -> WorldState:
    world = WorldState()
    world.rooms = {"living_room": RoomState(id="living_room", light_level=300.0)}
    world.devices = {
        "light": DeviceState(
            id="light",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness", "color_temp"],
            state=DeviceStateValues(
                power=False,
                extra={"online": online, "brightness": 80, "color_temp": 3000},
            ),
        )
    }
    return world


def _command(*, capability: str = "power", value=True) -> DeviceCommand:
    return DeviceCommand(
        source=CommandSource.SCENARIO,
        device_id="light",
        capability=capability,
        value=value,
    )


def _executor(profile: DeviceRuntimeProfile, *, online: bool = True):
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    sim_time = [0.0]
    run_id = ["run-1"]
    state = StateManager(_world(online=online))
    executor = CommandExecutor(
        state,
        publish,
        runtime_profile=lambda _command: profile,
        sim_time_source=lambda: sim_time[0],
        run_id_source=lambda: run_id[0],
    )
    return executor, state, events, sim_time, run_id


def test_scheduler_has_stable_tie_breaking() -> None:
    scheduler = SimTimeScheduler()
    scheduler.schedule(5, "start", "first")
    scheduler.schedule(5, "start", "second")
    assert [item.operation_id for item in scheduler.pop_due(5)] == ["first", "second"]


def test_device_perturbation_registry_is_explicit_and_requires_time() -> None:
    assert set(PERTURBATION_HANDLER_REGISTRY) == {
        "device_failure",
        "feedback_loss",
        "resident_state_change",
        "conflicting_request",
        "safety_interrupt",
    }
    with pytest.raises(ValidationError, match="requires at_sim_time_s"):
        DeviceFailurePerturbation(phase="during_execution", device_id="light")
    with pytest.raises(ValidationError, match="requires at_sim_time_s"):
        FeedbackLossPerturbation(
            phase="after_execution_before_feedback", device_id="light"
        )
    with pytest.raises(ValidationError, match="requires at_sim_time_s"):
        ResidentStateChangePerturbation(
            phase="after_plan_before_execution", user_id="user", activity="away"
        )
    with pytest.raises(ValidationError, match="requires at_sim_time_s"):
        ConflictingRequestPerturbation(
            phase="after_plan_before_execution",
            user_id="user",
            room_id="living_room",
            intent="turn it off",
        )
    with pytest.raises(ValidationError, match="requires at_sim_time_s"):
        SafetyInterruptPerturbation(
            phase="during_execution", room_id="living_room"
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "effect_before_finish"),
    [
        (OperationKind.IMMEDIATE, False),
        (OperationKind.CONTINUOUS, True),
        (OperationKind.CYCLE, False),
    ],
)
async def test_operation_kinds_follow_simulated_time(
    kind: OperationKind, effect_before_finish: bool
) -> None:
    profile = DeviceRuntimeProfile(
        kind=kind,
        start_delay_s=2 if kind is OperationKind.IMMEDIATE else 0,
        duration_s=0 if kind is OperationKind.IMMEDIATE else 2,
    )
    executor, state, events, sim_time, _ = _executor(profile)
    record = await executor.submit(_command())

    assert record.status is CommandStatus.EXECUTING
    assert state.world.devices["light"].state.power is effect_before_finish
    sim_time[0] = 2
    await executor.advance_device_runtime(2, tick=2)

    assert record.status is CommandStatus.SUCCEEDED
    assert state.world.devices["light"].state.power is True
    assert sum(e.event_type == DEVICE_EFFECT_APPLIED_EVENT_TYPE for e in events) == 1
    assert sum(e.event_type == FEEDBACK_EVENT_TYPE for e in events) >= 1
    effect = next(e for e in events if e.event_type == DEVICE_EFFECT_APPLIED_EVENT_TYPE)
    feedback = next(e for e in events if e.event_type == FEEDBACK_EVENT_TYPE)
    assert feedback.causal_parent == effect.event_id


@pytest.mark.anyio
async def test_offline_before_and_during_operation() -> None:
    immediate, state, _, _, _ = _executor(DeviceRuntimeProfile(), online=False)
    rejected = await immediate.submit(_command())
    assert rejected.failure_code == "device_offline"
    assert state.world.devices["light"].state.power is False

    cycle = DeviceRuntimeProfile(kind=OperationKind.CYCLE, duration_s=10)
    executor, state, _, sim_time, _ = _executor(cycle)
    running = await executor.submit(_command())
    executor.device_runtime.inject_device_failure("light", at_sim_time_s=5)
    sim_time[0] = 10
    await executor.advance_device_runtime(10, tick=2)
    assert running.status is CommandStatus.FAILED
    assert running.failure_code == "device_offline"
    assert state.world.devices["light"].state.power is False


@pytest.mark.anyio
async def test_feedback_loss_times_out_without_reverting_ground_truth() -> None:
    profile = DeviceRuntimeProfile(feedback_timeout_s=5)
    executor, state, events, sim_time, _ = _executor(profile)
    executor.device_runtime.inject_feedback_loss("light", at_sim_time_s=0)

    record = await executor.submit(_command())
    assert record.status is CommandStatus.EXECUTING
    assert state.world.devices["light"].state.power is True
    assert any(e.event_type == DEVICE_EFFECT_APPLIED_EVENT_TYPE for e in events)
    assert not any(e.event_type == FEEDBACK_EVENT_TYPE for e in events)

    sim_time[0] = 5
    await executor.advance_device_runtime(5, tick=2)
    assert record.status is CommandStatus.TIMED_OUT
    assert record.failure_code == "state_feedback_missing"
    assert state.world.devices["light"].state.power is True


@pytest.mark.anyio
async def test_feedback_deadline_beats_late_feedback() -> None:
    profile = DeviceRuntimeProfile(feedback_delay_s=10, feedback_timeout_s=5)
    executor, state, events, sim_time, _ = _executor(profile)
    record = await executor.submit(_command())
    effect = next(e for e in events if e.event_type == DEVICE_EFFECT_APPLIED_EVENT_TYPE)
    assert record.status is CommandStatus.EXECUTING

    sim_time[0] = 10
    await executor.advance_device_runtime(10, tick=2)
    assert record.status is CommandStatus.TIMED_OUT
    assert state.world.devices["light"].state.power is True
    timed_out = next(
        e
        for e in events
        if e.event_type == LIFECYCLE_EVENT_TYPE
        and e.data["to_status"] == "timed_out"
    )
    failed = next(e for e in events if e.event_type == COMMAND_FAILED_EVENT_TYPE)
    assert timed_out.sim_time_s == 5
    assert failed.sim_time_s == 5

    sim_time[0] = 10
    await executor.advance_device_runtime(10, tick=3)
    assert not any(e.event_type == FEEDBACK_EVENT_TYPE for e in events)
    assert effect.causal_parent is not None


@pytest.mark.anyio
async def test_async_profile_ignores_legacy_wall_clock_timeout() -> None:
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    def forbidden_clock() -> float:
        raise AssertionError("async device semantics consulted the wall clock")

    state = StateManager(_world())
    executor = CommandExecutor(
        state,
        publish,
        feedback_timeout=1,
        clock=forbidden_clock,
        runtime_profile=lambda _command: DeviceRuntimeProfile(),
    )
    record = await executor.submit(_command())
    assert record.status is CommandStatus.SUCCEEDED
    assert state.world.devices["light"].state.power is True
    assert not any(e.event_type == COMMAND_FAILED_EVENT_TYPE for e in events)


@pytest.mark.anyio
async def test_large_time_jump_processes_each_due_item_at_its_own_time() -> None:
    profile = DeviceRuntimeProfile(
        start_delay_s=1, feedback_delay_s=2, feedback_timeout_s=10
    )
    executor, state, events, sim_time, _ = _executor(profile)
    record = await executor.submit(_command())
    executor.device_runtime.inject_device_failure("light", at_sim_time_s=5)

    sim_time[0] = 10
    await executor.advance_device_runtime(10, tick=2)

    assert record.status is CommandStatus.SUCCEEDED
    assert state.world.devices["light"].state.power is True
    effect = next(e for e in events if e.event_type == DEVICE_EFFECT_APPLIED_EVENT_TYPE)
    feedback = next(e for e in events if e.event_type == FEEDBACK_EVENT_TYPE)
    succeeded = next(
        e
        for e in events
        if e.event_type == LIFECYCLE_EVENT_TYPE
        and e.data["to_status"] == "succeeded"
    )
    assert effect.sim_time_s == 1
    assert feedback.sim_time_s == 3
    assert succeeded.sim_time_s == 3
    assert effect.data["issued_at_sim_time_s"] == 0
    assert effect.data["scheduled_start_at_sim_time_s"] == 1
    assert effect.data["scheduled_finish_at_sim_time_s"] == 1
    assert effect.data["feedback_deadline_at_sim_time_s"] == 11


@pytest.mark.anyio
async def test_supersede_safety_interrupt_and_old_run_discard() -> None:
    profile = DeviceRuntimeProfile(start_delay_s=5)
    executor, state, _, sim_time, run_id = _executor(profile)
    first = await executor.submit(_command(capability="brightness", value=20))
    second = await executor.submit(_command(capability="brightness", value=30))
    assert first.status is CommandStatus.SUPERSEDED

    interrupted = await executor.interrupt_device_operations(tick=1)
    assert len(interrupted) == 1
    assert interrupted[0].record is second
    assert second.status is CommandStatus.CANCELLED
    assert state.world.devices["light"].state.extra["brightness"] == 80

    stale = await executor.submit(_command())
    run_id[0] = "run-2"
    sim_time[0] = 5
    await executor.advance_device_runtime(5, tick=2)
    assert stale.status is CommandStatus.CANCELLED
    assert stale.failure_code == "old_run_completion_discarded"
    assert state.world.devices["light"].state.power is False


@pytest.mark.anyio
async def test_due_device_failure_is_visible_in_world_and_event_log() -> None:
    bus = EventBus()
    state = StateManager(_world())
    engine = SimulationEngine(bus, state, ConnectionManager())
    try:
        engine.command_executor.device_runtime.inject_device_failure(
            "light", at_sim_time_s=0
        )
        await engine.timer.tick_once()
        assert state.world.devices["light"].state.extra["online"] is False
        failure = next(e for e in bus.get_history() if e.event_type == "device.offline")
        assert failure.data["device_id"] == "light"
        assert failure.source == "failure_injector"
    finally:
        await engine.close()


@pytest.mark.anyio
async def test_safety_root_event_interrupts_executing_operation() -> None:
    bus = EventBus()
    state = StateManager(_world())
    engine = SimulationEngine(bus, state, ConnectionManager())
    engine.command_executor.runtime_profile = lambda _command: DeviceRuntimeProfile(
        start_delay_s=10
    )
    try:
        record = await engine.command_executor.submit(
            _command(), publish=engine._publish_sim_event
        )
        assert record.status is CommandStatus.EXECUTING
        await engine._publish_sim_event(
            SimEvent(
                event_type="safety.smoke_detected",
                source="test",
                timestamp=0.0,
                data={},
            )
        )
        assert record.status is CommandStatus.CANCELLED
        assert record.detail == "safety interrupt: safety.smoke_detected"
    finally:
        await engine.close()
