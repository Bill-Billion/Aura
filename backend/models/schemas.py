from enum import Enum
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


class RunScenarioPayload(BaseModel):
    """启动一个场景 run 的载荷（``POST /api/runs`` 与 WS ``CMD_RUN_SCENARIO`` 共用）。

    一个模型服务两个入口是有意的：两条入口若各自校验，迟早会在"seed 可不可选""id 能不能
    为空"这类小事上分叉，而它们开出来的是同一种 run。``seed`` 不传即取场景声明的 seed
    （§11 要求每个 run 都有 seed，"没设 seed"不是合法状态）。
    """

    scenario_id: str = Field(min_length=1)
    seed: int | None = Field(default=None, ge=0)


class ScenarioLaunchErrorCode(str, Enum):
    """场景启动失败词表。与 §10.2 命令失败码正交：这些描述"这个 run 为什么没开起来"。"""

    SCENARIO_NOT_FOUND = "scenario_not_found"
    SCENARIO_LIBRARY_INVALID = "scenario_library_invalid"
    INITIAL_STATE_INVALID = "initial_state_invalid"
    INVALID_SEED = "invalid_seed"
    ENGINE_UNAVAILABLE = "engine_unavailable"


class ScenarioLaunchError(Exception):
    """结构化的场景启动失败（REST 与 WS 共用，与 :class:`ErrorMessage` 同形）。

    放在 models 层而不是 main.py：``backend.api.routes`` 不能反向 import main
    （main 引的是 routes），但两边必须认同一份错误词表。
    """

    def __init__(
        self,
        code: ScenarioLaunchErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_error_message(self) -> ErrorMessage:
        return ErrorMessage(code=self.code.value, message=self.message, details=self.details)

    def to_dict(self) -> dict[str, Any]:
        return self.to_error_message().model_dump()


class SimCommand(BaseModel):
    """Command sent to the simulation engine."""

    command: Literal["start", "pause", "reset", "set_speed", "apply_action"]
    params: dict[str, Any] = Field(default_factory=dict)
