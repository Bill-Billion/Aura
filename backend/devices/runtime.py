"""Asynchronous device semantics advanced exclusively by simulated time."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from backend.devices.failure import DeviceFailureController
from backend.devices.latency import DeviceRuntimeProfile
from backend.devices.operation import DeviceOperation, OperationKind, OperationPhase
from backend.devices.scheduler import SimTimeScheduler
from backend.execution.command import CommandRecord, CommandStatus, PublishEvent


class DeviceRuntimeDriver(Protocol):
    async def apply_device_operation(
        self, operation: DeviceOperation, *, sim_time_s: float, tick: int | None
    ) -> bool: ...

    async def deliver_device_feedback(
        self, operation: DeviceOperation, *, sim_time_s: float, tick: int | None
    ) -> None: ...

    async def fail_device_operation(
        self,
        operation: DeviceOperation,
        *,
        status: CommandStatus,
        failure_code: str,
        detail: str,
        tick: int | None,
        sim_time_s: float | None = None,
    ) -> None: ...

    async def activate_device_failure(
        self, device_id: str, *, sim_time_s: float, tick: int | None
    ) -> None: ...


class DeviceRuntime:
    """Owns operation timing, but has no reference to ``StateManager``."""

    def __init__(self, driver: DeviceRuntimeDriver | None = None) -> None:
        self.driver = driver
        self.scheduler = SimTimeScheduler()
        self.failures = DeviceFailureController()
        self.operations: dict[str, DeviceOperation] = {}

    def bind_driver(self, driver: DeviceRuntimeDriver) -> None:
        self.driver = driver

    @property
    def next_due_at_s(self) -> float | None:
        return self.scheduler.next_due_at_s

    def schedule(
        self,
        record: CommandRecord,
        *,
        publish: PublishEvent,
        action_event_id: str,
        profile: DeviceRuntimeProfile,
        sim_time_s: float,
        run_id: str | None,
    ) -> DeviceOperation:
        start_at = sim_time_s + profile.start_delay_s
        operation = DeviceOperation(
            operation_id=f"operation:{record.command.command_id}",
            record=record,
            publish=publish,
            run_id=run_id,
            kind=profile.kind,
            issued_at_s=sim_time_s,
            start_at_s=start_at,
            finish_at_s=start_at + profile.duration_s,
            feedback_delay_s=profile.feedback_delay_s,
            feedback_timeout_s=profile.feedback_timeout_s,
            feedback_causal_parent_effect=profile.feedback_causal_parent_effect,
            legacy_wall_clock_timeout=profile.legacy_wall_clock_timeout,
            action_event_id=action_event_id,
        )
        self.operations[operation.operation_id] = operation
        self.scheduler.schedule(start_at, "start", operation.operation_id)
        return operation

    async def advance(
        self, sim_time_s: float, *, tick: int | None, active_run_id: str | None
    ) -> None:
        if self.driver is None:
            raise RuntimeError("DeviceRuntime has no driver")
        # Pop one at a time: a t=1 handler may enqueue t=3 while t=5 was already
        # pending, and the new item must still run before t=5 during a large jump.
        while True:
            item = self.scheduler.pop_next_due(sim_time_s)
            if item is None:
                return
            occurrence_time_s = item.due_at_s
            if item.event == "device_offline":
                await self.driver.activate_device_failure(
                    item.operation_id,
                    sim_time_s=occurrence_time_s,
                    tick=tick,
                )
                continue
            operation = self.operations.get(item.operation_id)
            if operation is None:
                continue
            if operation.record.is_terminal:
                operation.phase = (
                    OperationPhase.SUPERSEDED
                    if operation.record.status == CommandStatus.SUPERSEDED
                    else OperationPhase.CANCELLED
                )
                self.operations.pop(operation.operation_id, None)
                continue
            if operation.run_id != active_run_id:
                operation.phase = OperationPhase.DISCARDED
                await self.driver.fail_device_operation(
                    operation,
                    status=CommandStatus.CANCELLED,
                    failure_code="old_run_completion_discarded",
                    detail="operation belongs to an inactive run",
                    tick=tick,
                    sim_time_s=occurrence_time_s,
                )
                self.operations.pop(operation.operation_id, None)
                continue
            if item.event in {"start", "finish"}:
                await self._advance_execution(
                    operation, item.event, occurrence_time_s, tick
                )
            elif item.event == "feedback":
                operation.phase = OperationPhase.FEEDBACK_PENDING
                await self.driver.deliver_device_feedback(
                    operation, sim_time_s=occurrence_time_s, tick=tick
                )
                operation.phase = OperationPhase.COMPLETED
                self.operations.pop(operation.operation_id, None)
            elif item.event == "feedback_timeout":
                operation.phase = OperationPhase.TIMED_OUT
                await self.driver.fail_device_operation(
                    operation,
                    status=CommandStatus.TIMED_OUT,
                    failure_code="state_feedback_missing",
                    detail="device effect applied but state feedback was not observed",
                    tick=tick,
                    sim_time_s=occurrence_time_s,
                )
                self.operations.pop(operation.operation_id, None)

    async def _advance_execution(
        self,
        operation: DeviceOperation,
        event: str,
        sim_time_s: float,
        tick: int | None,
    ) -> None:
        assert self.driver is not None
        device_id = operation.command.device_id
        if self.failures.is_offline(device_id, sim_time_s):
            operation.phase = OperationPhase.FAILED
            await self.driver.fail_device_operation(
                operation,
                status=CommandStatus.FAILED,
                failure_code="device_offline",
                detail="device went offline before operation completed",
                tick=tick,
                sim_time_s=sim_time_s,
            )
            self.operations.pop(operation.operation_id, None)
            return

        operation.phase = OperationPhase.RUNNING
        if (
            event == "start"
            and operation.kind == OperationKind.CYCLE
            and operation.finish_at_s > sim_time_s
        ):
            self.scheduler.schedule(
                operation.finish_at_s, "finish", operation.operation_id
            )
            return

        if operation.effect_applied_at_s is None:
            applied = await self.driver.apply_device_operation(
                operation, sim_time_s=sim_time_s, tick=tick
            )
            if not applied:
                self.operations.pop(operation.operation_id, None)
                return
            operation.phase = OperationPhase.EFFECT_APPLIED

        if (
            event == "start"
            and operation.kind == OperationKind.CONTINUOUS
            and operation.finish_at_s > sim_time_s
        ):
            self._arm_feedback(operation, sim_time_s, schedule_feedback=False)
            self.scheduler.schedule(
                operation.finish_at_s, "finish", operation.operation_id
            )
            return
        self._arm_feedback(operation, sim_time_s, schedule_feedback=True)

    def _arm_feedback(
        self,
        operation: DeviceOperation,
        sim_time_s: float,
        *,
        schedule_feedback: bool,
    ) -> None:
        """Race observable feedback against its simulated-time deadline."""

        if operation.feedback_dropped is None:
            operation.feedback_dropped = self.failures.consume_feedback_loss(
                operation.command.device_id, sim_time_s
            )
        if schedule_feedback and not operation.feedback_dropped:
            self.scheduler.schedule(
                sim_time_s + operation.feedback_delay_s,
                "feedback",
                operation.operation_id,
            )
        if operation.feedback_deadline_at_s is None:
            operation.feedback_deadline_at_s = (
                operation.effect_applied_at_s or sim_time_s
            ) + operation.feedback_timeout_s
            self.scheduler.schedule(
                operation.feedback_deadline_at_s,
                "feedback_timeout",
                operation.operation_id,
            )

    async def interrupt(
        self,
        *,
        reason: str = "safety_interrupt",
        tick: int | None = None,
        predicate: Callable[[DeviceOperation], bool] | None = None,
    ) -> list[DeviceOperation]:
        if self.driver is None:
            raise RuntimeError("DeviceRuntime has no driver")
        interrupted: list[DeviceOperation] = []
        for operation in list(self.operations.values()):
            if operation.record.is_terminal or (predicate and not predicate(operation)):
                continue
            operation.phase = OperationPhase.CANCELLED
            await self.driver.fail_device_operation(
                operation,
                status=CommandStatus.CANCELLED,
                failure_code="policy_denied",
                detail=reason,
                tick=tick,
                sim_time_s=None,
            )
            self.operations.pop(operation.operation_id, None)
            interrupted.append(operation)
        return interrupted

    def reset(self) -> None:
        self.scheduler.clear()
        self.failures.reset()
        self.operations.clear()

    def inject_device_failure(
        self, device_id: str, *, at_sim_time_s: float = 0.0
    ) -> None:
        self.failures.inject_offline(device_id, at_sim_time_s=at_sim_time_s)
        self.scheduler.schedule(at_sim_time_s, "device_offline", device_id)

    def inject_feedback_loss(
        self, device_id: str, *, drop_count: int = 1, at_sim_time_s: float = 0.0
    ) -> None:
        self.failures.inject_feedback_loss(
            device_id, drop_count=drop_count, at_sim_time_s=at_sim_time_s
        )
