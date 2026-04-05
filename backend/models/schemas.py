from pydantic import BaseModel, Field
from typing import Literal, Any
import time
import uuid


class WSMessage(BaseModel):
    """Generic WebSocket message envelope."""

    type: str
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: float = Field(default_factory=time.time)
    payload: dict[str, Any] = Field(default_factory=dict)


class SimCommand(BaseModel):
    """Command sent to the simulation engine."""

    command: Literal["start", "pause", "reset", "set_speed", "apply_action"]
    params: dict[str, Any] = Field(default_factory=dict)
