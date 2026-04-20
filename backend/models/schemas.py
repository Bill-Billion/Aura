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


class SimCommand(BaseModel):
    """Command sent to the simulation engine."""

    command: Literal["start", "pause", "reset", "set_speed", "apply_action"]
    params: dict[str, Any] = Field(default_factory=dict)
