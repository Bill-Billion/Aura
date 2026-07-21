from typing import Any, Literal

from pydantic import BaseModel, Field


DeviceType = Literal["light", "hvac", "curtain", "sensor", "fan", "camera"]
DeviceUIGroup = Literal["lighting", "device", "security", "environment"]
DeviceCapability = Literal[
    "power",
    "brightness",
    "color_temp",
    "target_temp",
    "mode",
    "speed",
    "open_percent",
    "shake",
    "timeout",
    "view",
    # §3.2：camera.online 是只读能力语义位；设备侧同时保留 state.extra.online 镜像，
    # 前端 CameraPanel/deviceFloorMap 读的是那个镜像，两者必须同时存在。
    "online",
    # §3.2：sensor 的只读读数位。名字必须与 state.extra.value 一致——effects.py 写它、
    # 前端 SensorPanel 读它；叫 "read" 时能力名指向了一个并不存在的字段。
    "value",
]


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
    type: DeviceType
    location: Location3D
    display_name: str = ""
    floor_id: str = ""
    ui_group: DeviceUIGroup = "device"
    capabilities: list[DeviceCapability] = Field(default_factory=list)
    scene_bindings: dict[str, Any] = Field(default_factory=dict)
    state: DeviceStateValues


class RoomState(BaseModel):
    id: str
    temperature: float = 25.0
    humidity: float = 0.5
    light_level: float = 300.0
    occupancy: bool = False
    persons: list[str] = Field(default_factory=list)
    # §3.4 派生量（非物理 ground truth）：风扇只降体感、摄像头只掉覆盖，二者都不许改
    # temperature。None = 尚未被效果模型算过，避免默认值冒充已计算结果。
    perceived_temperature: float | None = None
    security_coverage: float | None = None


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
    mode: str = "idle"
    active_correlation_id: str | None = None
    last_reasoning_step: str = ""
    last_fallback_reason: str | None = None
    provider: str = "disabled"
    provider_configured: bool = False
    last_latency_ms: int | None = None
    last_trigger_event: str = ""


class UserState(BaseModel):
    id: str
    name: str = "User"
    location: Location3D | None = None
    activity: str = "idle"
    comfort_score: float = 0.8


class WorldState(BaseModel):
    simulation_tick: int = 0
    simulation_speed: float = 1.0
    simulation_mode: Literal["observe", "demo"] = "observe"
    wall_tick_ms: int = 2000
    simulated_dt_seconds: float = 10.0
    is_running: bool = False
    scene_id: str = ""
    environment: EnvironmentState = Field(default_factory=EnvironmentState)
    devices: dict[str, DeviceState] = Field(default_factory=dict)
    rooms: dict[str, RoomState] = Field(default_factory=dict)
    agents: dict[str, AgentRuntimeState] = Field(default_factory=dict)
    users: dict[str, UserState] = Field(default_factory=dict)

    def snapshot(self) -> "WorldState":
        return self.model_copy(deep=True)
