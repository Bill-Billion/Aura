from typing import Any, Literal
import time
import uuid

from pydantic import BaseModel, Field


MessageType = Literal[
    "STATE_FULL",
    "STATE_DELTA",
    "SIM_EVENT",
    "AGENT_STATUS",
    "SIMULATION_STATUS",
    "ERROR",
]


class WSMessage(BaseModel):
    """Generic WebSocket message envelope."""

    type: str | MessageType
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = Field(default_factory=time.time)
    payload: Any = Field(default_factory=dict)


class ErrorMessage(BaseModel):
    """Structured error payload shared by REST/WS boundaries."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class CmdDeviceControlPayload(BaseModel):
    """CMD_DEVICE_CONTROL 入站载荷结构守卫（治『WS 零校验』）。

    只做结构校验：device_id 必填非空、四种载荷字段类型正确。能力存在性 / 值类型 /
    值域 / 策略等 §3.3 六级语义校验由 CommandExecutor 的 validate_command 负责，不在此重复。
    兼容两种格式：新格式 device_id+action(+params) 与旧格式 device_id+property+value。
    """

    device_id: str = Field(min_length=1)
    action: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    property: str | None = None
    value: Any = None


class SimCommand(BaseModel):
    """Command sent to the simulation engine."""

    command: Literal["start", "pause", "reset", "set_speed", "apply_action"]
    params: dict[str, Any] = Field(default_factory=dict)
