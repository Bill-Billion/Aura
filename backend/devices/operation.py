"""Data carried by one simulated device operation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from backend.engine.state_manager import DeltaChange
from backend.execution.command import CommandRecord, DeviceCommand, PublishEvent


class OperationKind(str, Enum):
    IMMEDIATE = "immediate"
    CONTINUOUS = "continuous"
    CYCLE = "cycle"


class OperationPhase(str, Enum):
    SCHEDULED = "scheduled"
    RUNNING = "running"
    EFFECT_APPLIED = "effect_applied"
    FEEDBACK_PENDING = "feedback_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    DISCARDED = "discarded"


@dataclass
class DeviceOperation:
    """Runtime-owned timing state; it never owns or mutates the world."""

    operation_id: str
    record: CommandRecord
    publish: PublishEvent
    run_id: str | None
    kind: OperationKind
    issued_at_s: float
    start_at_s: float
    finish_at_s: float
    feedback_delay_s: float
    feedback_timeout_s: float
    feedback_causal_parent_effect: bool
    legacy_wall_clock_timeout: bool
    action_event_id: str
    phase: OperationPhase = OperationPhase.SCHEDULED
    deltas: list[DeltaChange] = field(default_factory=list)
    effect_event_id: str | None = None
    effect_applied_at_s: float | None = None
    feedback_deadline_at_s: float | None = None
    feedback_dropped: bool | None = None

    @property
    def command(self) -> DeviceCommand:
        return self.record.command
