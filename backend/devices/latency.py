"""Device timing profiles expressed only in simulated seconds."""

from __future__ import annotations

from dataclasses import dataclass

from backend.devices.operation import OperationKind
from backend.execution.command import DeviceCommand


@dataclass(frozen=True)
class DeviceRuntimeProfile:
    kind: OperationKind = OperationKind.IMMEDIATE
    start_delay_s: float = 0.0
    duration_s: float = 0.0
    feedback_delay_s: float = 0.0
    feedback_timeout_s: float = 30.0
    feedback_causal_parent_effect: bool = True
    legacy_wall_clock_timeout: bool = False

    def __post_init__(self) -> None:
        for name in (
            "start_delay_s",
            "duration_s",
            "feedback_delay_s",
            "feedback_timeout_s",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    @property
    def is_zero_latency(self) -> bool:
        return self.start_delay_s == self.duration_s == self.feedback_delay_s == 0.0


def legacy_runtime_profile(_command: DeviceCommand) -> DeviceRuntimeProfile:
    """The v1 compatibility profile: submit remains synchronously terminal."""

    return DeviceRuntimeProfile(
        feedback_causal_parent_effect=False, legacy_wall_clock_timeout=True
    )


def simulated_v2_runtime_profile(_command: DeviceCommand) -> DeviceRuntimeProfile:
    """One-tick asynchronous contract shared by static and dynamic v2 twins."""

    return DeviceRuntimeProfile(start_delay_s=0.001)
