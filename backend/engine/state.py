from pydantic import BaseModel, Field
from typing import Literal, Any


class Location3D(BaseModel):
    room: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class DeviceStateValues(BaseModel):
    power: bool = False
    last_changed_by: str = "system"
    extra: dict[str, Any] = Field(default_factory=dict)


class DeviceState(BaseModel):
    id: str
    type: Literal["light", "hvac", "curtain", "sensor"]
    location: Location3D
    state: DeviceStateValues


class RoomState(BaseModel):
    id: str
    temperature: float = 25.0
    humidity: float = 0.5
    light_level: float = 300.0
    occupancy: bool = False
    persons: list[str] = Field(default_factory=list)


class EnvironmentState(BaseModel):
    time_of_day: str = "12:00"
    outdoor_temp: float = 25.0
    outdoor_humidity: float = 0.5
    weather: Literal["clear", "cloudy", "rainy", "snowy"] = "clear"


class AgentRuntimeState(BaseModel):
    id: str
    name: str
    status: Literal["idle", "active", "thinking"] = "idle"
    current_strategy: str = ""
    confidence: float = 0.5
    last_action: str = ""


class UserState(BaseModel):
    id: str
    name: str = "User"
    location: Location3D | None = None
    activity: str = "idle"
    comfort_score: float = 0.8


class WorldState(BaseModel):
    simulation_tick: int = 0
    simulation_speed: float = 1.0
    is_running: bool = False
    scene_id: str = ""
    environment: EnvironmentState = Field(default_factory=EnvironmentState)
    devices: dict[str, DeviceState] = Field(default_factory=dict)
    rooms: dict[str, RoomState] = Field(default_factory=dict)
    agents: dict[str, AgentRuntimeState] = Field(default_factory=dict)
    users: dict[str, UserState] = Field(default_factory=dict)

    def snapshot(self) -> "WorldState":
        return self.model_copy(deep=True)
