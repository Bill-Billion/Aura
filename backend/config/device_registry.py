from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import BaseModel, Field

from backend.engine.state import (
    DeviceCapability,
    DeviceState,
    DeviceStateValues,
    DeviceType,
    DeviceUIGroup,
    Location3D,
    RoomState,
)


READ_ONLY_CAPABILITIES = {"read", "view"}


class DeviceRegistryEntry(BaseModel):
    """定义默认场景里的设备注册信息。"""

    id: str
    type: DeviceType
    display_name: str
    room_id: str
    floor_id: str
    ui_group: DeviceUIGroup
    capabilities: list[DeviceCapability] = Field(default_factory=list)
    default_power: bool = False
    default_extra: dict[str, Any] = Field(default_factory=dict)
    scene_bindings: dict[str, Any] = Field(default_factory=dict)

    def to_device_state(self) -> DeviceState:
        return DeviceState(
            id=self.id,
            type=self.type,
            location=Location3D(room=self.room_id),
            display_name=self.display_name,
            floor_id=self.floor_id,
            ui_group=self.ui_group,
            capabilities=list(self.capabilities),
            scene_bindings=dict(self.scene_bindings),
            state=DeviceStateValues(
                power=self.default_power,
                extra=dict(self.default_extra),
            ),
        )


def build_default_rooms() -> dict[str, RoomState]:
    return {
        "living_room": RoomState(id="living_room", temperature=25.0, humidity=0.5),
        "kitchen": RoomState(id="kitchen", temperature=26.0, humidity=0.6),
        "bedroom": RoomState(id="bedroom", temperature=24.0, humidity=0.5),
        "bathroom": RoomState(id="bathroom", temperature=25.0, humidity=0.7),
        "loft": RoomState(id="loft", temperature=23.0, humidity=0.45),
        "utility": RoomState(id="utility", temperature=22.0, humidity=0.4),
    }


@lru_cache(maxsize=1)
def get_default_device_registry() -> tuple[DeviceRegistryEntry, ...]:
    return (
        DeviceRegistryEntry(
            id="light_living_01",
            type="light",
            display_name="客厅主灯",
            room_id="living_room",
            floor_id="F1",
            ui_group="lighting",
            capabilities=["power", "brightness", "color_temp"],
            default_power=True,
            default_extra={"brightness": 80, "color_temp": 4000},
            scene_bindings={"floor_chip": "light"},
        ),
        DeviceRegistryEntry(
            id="light_kitchen_01",
            type="light",
            display_name="厨房灯光",
            room_id="kitchen",
            floor_id="F1",
            ui_group="lighting",
            capabilities=["power", "brightness", "color_temp"],
            default_power=False,
            default_extra={"brightness": 55, "color_temp": 4200},
            scene_bindings={"floor_chip": "light"},
        ),
        DeviceRegistryEntry(
            id="ac_living_01",
            type="hvac",
            display_name="一层空调 A",
            room_id="living_room",
            floor_id="F1",
            ui_group="device",
            capabilities=["power", "target_temp", "mode", "speed"],
            default_power=True,
            default_extra={"target_temp": 24, "mode": "cool", "speed": "medium"},
            scene_bindings={
                "animation": "hvac",
                "pick_nodes": ["ac1"],
                "body_nodes": ["ac1"],
                "effect_nodes": ["effect1"],
            },
        ),
        DeviceRegistryEntry(
            id="ac_living_02",
            type="hvac",
            display_name="一层空调 B",
            room_id="living_room",
            floor_id="F1",
            ui_group="device",
            capabilities=["power", "target_temp", "mode", "speed"],
            default_power=False,
            default_extra={"target_temp": 25, "mode": "cool", "speed": "low"},
            scene_bindings={
                "animation": "hvac",
                "pick_nodes": ["ac2"],
                "body_nodes": ["ac2"],
                "effect_nodes": ["effect2"],
            },
        ),
        DeviceRegistryEntry(
            id="curtain_living_01",
            type="curtain",
            display_name="一层窗帘",
            room_id="living_room",
            floor_id="F1",
            ui_group="device",
            capabilities=["open_percent"],
            default_power=True,
            default_extra={"open_percent": 100},
            scene_bindings={
                "animation": "curtain",
                "pick_nodes": ["curtain", "curtain01", "curtain02"],
                "track_nodes": ["curtain"],
                "panel_nodes": [
                    {"node": "curtain01", "side": "left", "axis": "z"},
                    {"node": "curtain02", "side": "right", "axis": "z"},
                ],
            },
        ),
        DeviceRegistryEntry(
            id="fan_living_01",
            type="fan",
            display_name="客厅风扇",
            room_id="living_room",
            floor_id="F1",
            ui_group="device",
            capabilities=["power", "speed", "shake", "timeout"],
            default_power=False,
            default_extra={"speed": "low", "shake": False, "timeout": 0},
            scene_bindings={
                "animation": "fan",
                "pick_nodes": ["fan", "fan01", "fan02"],
                "head_nodes": ["fan02"],
                "rotor_nodes": ["fan01"],
                "rotor_axis": "x",
                "shake_axis": "y",
            },
        ),
        DeviceRegistryEntry(
            id="camera_entry_01",
            type="camera",
            display_name="一层摄像头 A",
            room_id="living_room",
            floor_id="F1",
            ui_group="security",
            capabilities=["view"],
            default_power=True,
            default_extra={
                "online": True,
                "feed_key": "f1_cam_a",
                "preview_label": "F1 A 机位",
            },
            scene_bindings={
                "animation": "camera",
                "pick_nodes": ["cam1"],
                "body_nodes": ["cam1"],
                "cone_nodes": ["visualcone1"],
            },
        ),
        DeviceRegistryEntry(
            id="camera_living_02",
            type="camera",
            display_name="一层摄像头 B",
            room_id="living_room",
            floor_id="F1",
            ui_group="security",
            capabilities=["view"],
            default_power=True,
            default_extra={
                "online": True,
                "feed_key": "f1_cam_b",
                "preview_label": "F1 B 机位",
            },
            scene_bindings={
                "animation": "camera",
                "pick_nodes": ["cam2"],
                "body_nodes": ["cam2"],
                "cone_nodes": ["visualcone2"],
            },
        ),
        DeviceRegistryEntry(
            id="sensor_living_temp_01",
            type="sensor",
            display_name="客厅温度传感器",
            room_id="living_room",
            floor_id="F1",
            ui_group="environment",
            capabilities=["read"],
            default_power=True,
            default_extra={"sensor_type": "temperature", "value": 24.5, "unit": "°C"},
        ),
        DeviceRegistryEntry(
            id="light_bedroom_01",
            type="light",
            display_name="卧室主灯",
            room_id="bedroom",
            floor_id="F2",
            ui_group="lighting",
            capabilities=["power", "brightness", "color_temp"],
            default_power=True,
            default_extra={"brightness": 60, "color_temp": 3500},
        ),
        DeviceRegistryEntry(
            id="light_bathroom_01",
            type="light",
            display_name="卫浴灯光",
            room_id="bathroom",
            floor_id="F2",
            ui_group="lighting",
            capabilities=["power", "brightness", "color_temp"],
            default_power=False,
            default_extra={"brightness": 48, "color_temp": 4200},
        ),
        DeviceRegistryEntry(
            id="ac_bedroom_01",
            type="hvac",
            display_name="二层空调",
            room_id="bedroom",
            floor_id="F2",
            ui_group="device",
            capabilities=["power", "target_temp", "mode", "speed"],
            default_power=False,
            default_extra={"target_temp": 23, "mode": "cool", "speed": "low"},
            scene_bindings={
                "animation": "hvac",
                "pick_nodes": ["ac1"],
                "body_nodes": ["ac1"],
                "effect_nodes": ["effect1"],
            },
        ),
        DeviceRegistryEntry(
            id="curtain_bedroom_01",
            type="curtain",
            display_name="二层窗帘 A",
            room_id="bedroom",
            floor_id="F2",
            ui_group="device",
            capabilities=["open_percent"],
            default_power=True,
            default_extra={"open_percent": 100},
            scene_bindings={
                "animation": "curtain",
                "pick_nodes": ["curtain1", "curtain01", "curtain02"],
                "track_nodes": ["curtain1"],
                "panel_nodes": [
                    {"node": "curtain01", "side": "left", "axis": "z"},
                    {"node": "curtain02", "side": "right", "axis": "z"},
                ],
            },
        ),
        DeviceRegistryEntry(
            id="curtain_bedroom_02",
            type="curtain",
            display_name="二层窗帘 B",
            room_id="bedroom",
            floor_id="F2",
            ui_group="device",
            capabilities=["open_percent"],
            default_power=True,
            default_extra={"open_percent": 100},
            scene_bindings={
                "animation": "curtain",
                "pick_nodes": ["curtain2", "curtain01001", "curtain02001"],
                "track_nodes": ["curtain2"],
                "panel_nodes": [
                    {"node": "curtain01001", "side": "left", "axis": "z"},
                    {"node": "curtain02001", "side": "right", "axis": "z"},
                ],
            },
        ),
        DeviceRegistryEntry(
            id="camera_hall_02",
            type="camera",
            display_name="二层摄像头 A",
            room_id="bedroom",
            floor_id="F2",
            ui_group="security",
            capabilities=["view"],
            default_power=True,
            default_extra={
                "online": True,
                "feed_key": "f2_cam_a",
                "preview_label": "F2 A 机位",
            },
            scene_bindings={
                "animation": "camera",
                "pick_nodes": ["cam1"],
                "body_nodes": ["cam1"],
                "cone_nodes": ["visualcone1"],
            },
        ),
        DeviceRegistryEntry(
            id="camera_bedroom_02",
            type="camera",
            display_name="二层摄像头 B",
            room_id="bedroom",
            floor_id="F2",
            ui_group="security",
            capabilities=["view"],
            default_power=True,
            default_extra={
                "online": True,
                "feed_key": "f2_cam_b",
                "preview_label": "F2 B 机位",
            },
            scene_bindings={
                "animation": "camera",
                "pick_nodes": ["cam2"],
                "body_nodes": ["cam2"],
                "cone_nodes": ["visualcone2"],
            },
        ),
        DeviceRegistryEntry(
            id="sensor_bedroom_temp_01",
            type="sensor",
            display_name="卧室温度传感器",
            room_id="bedroom",
            floor_id="F2",
            ui_group="environment",
            capabilities=["read"],
            default_power=True,
            default_extra={"sensor_type": "temperature", "value": 23.0, "unit": "°C"},
        ),
        DeviceRegistryEntry(
            id="light_loft_01",
            type="light",
            display_name="阁楼灯光",
            room_id="loft",
            floor_id="F3",
            ui_group="lighting",
            capabilities=["power", "brightness", "color_temp"],
            default_power=False,
            default_extra={"brightness": 35, "color_temp": 3900},
        ),
        DeviceRegistryEntry(
            id="curtain_loft_01",
            type="curtain",
            display_name="三层窗帘 A",
            room_id="loft",
            floor_id="F3",
            ui_group="device",
            capabilities=["open_percent"],
            default_power=True,
            default_extra={"open_percent": 100},
            scene_bindings={
                "animation": "curtain",
                "pick_nodes": ["curtain1", "curtain01", "curtain02"],
                "track_nodes": ["curtain1"],
                "panel_nodes": [
                    {"node": "curtain01", "side": "left", "axis": "z"},
                    {"node": "curtain02", "side": "right", "axis": "z"},
                ],
            },
        ),
        DeviceRegistryEntry(
            id="curtain_loft_02",
            type="curtain",
            display_name="三层窗帘 B",
            room_id="loft",
            floor_id="F3",
            ui_group="device",
            capabilities=["open_percent"],
            default_power=True,
            default_extra={"open_percent": 100},
            scene_bindings={
                "animation": "curtain",
                "pick_nodes": ["curtain2", "curtain01001", "curtain02001"],
                "track_nodes": ["curtain2"],
                "panel_nodes": [
                    {"node": "curtain01001", "side": "left", "axis": "z"},
                    {"node": "curtain02001", "side": "right", "axis": "z"},
                ],
            },
        ),
        DeviceRegistryEntry(
            id="camera_loft_01",
            type="camera",
            display_name="三层摄像头 A",
            room_id="loft",
            floor_id="F3",
            ui_group="security",
            capabilities=["view"],
            default_power=True,
            default_extra={
                "online": False,
                "feed_key": "f3_cam_a",
                "preview_label": "F3 A 机位",
            },
            scene_bindings={
                "animation": "camera",
                "pick_nodes": ["cam1"],
                "body_nodes": ["cam1"],
                "cone_nodes": ["visualcone1"],
            },
        ),
        DeviceRegistryEntry(
            id="camera_loft_02",
            type="camera",
            display_name="三层摄像头 B",
            room_id="loft",
            floor_id="F3",
            ui_group="security",
            capabilities=["view"],
            default_power=True,
            default_extra={
                "online": True,
                "feed_key": "f3_cam_b",
                "preview_label": "F3 B 机位",
            },
            scene_bindings={
                "animation": "camera",
                "pick_nodes": ["cam2"],
                "body_nodes": ["cam2"],
                "cone_nodes": ["visualcone2"],
            },
        ),
        DeviceRegistryEntry(
            id="sensor_utility_air_01",
            type="sensor",
            display_name="设备间空气传感器",
            room_id="utility",
            floor_id="F3",
            ui_group="environment",
            capabilities=["read"],
            default_power=True,
            default_extra={"sensor_type": "air_quality", "value": 42, "unit": "AQI"},
        ),
    )


def build_default_devices() -> dict[str, DeviceState]:
    return {
        entry.id: entry.to_device_state()
        for entry in get_default_device_registry()
    }


def get_registry_entry(device_id: str) -> DeviceRegistryEntry | None:
    for entry in get_default_device_registry():
        if entry.id == device_id:
            return entry
    return None


def get_writable_fields(device: DeviceState) -> set[str]:
    return {
        capability
        for capability in device.capabilities
        if capability not in READ_ONLY_CAPABILITIES and capability != "power"
    }


def validate_device_command(
    device: DeviceState,
    *,
    action: str,
    params: dict[str, Any] | None = None,
    property_path: str | None = None,
) -> tuple[str | None, str]:
    """校验设备控制命令，返回错误码和可读说明。"""

    if action in {"turn_on", "turn_off"}:
        if "power" not in device.capabilities:
            return (
                "DEVICE_NOT_CONTROLLABLE",
                f"{device.display_name or device.id} 当前不支持电源控制",
            )
        return (None, "")

    if action == "set_state":
        if not params:
            return ("INVALID_DEVICE_COMMAND", "set_state 需要至少一个参数")

        allowed_fields = get_writable_fields(device)
        invalid_fields = sorted(set(params) - allowed_fields)
        if invalid_fields:
            return (
                "DEVICE_NOT_CONTROLLABLE",
                f"{device.display_name or device.id} 不支持修改: {', '.join(invalid_fields)}",
            )
        return (None, "")

    if property_path:
        normalized = property_path.removeprefix("extra.")
        if normalized == "power":
            return validate_device_command(device, action="turn_on")
        if normalized not in get_writable_fields(device):
            return (
                "DEVICE_NOT_CONTROLLABLE",
                f"{device.display_name or device.id} 不支持修改: {normalized}",
            )
        return (None, "")

    return ("INVALID_DEVICE_COMMAND", f"未知动作: {action}")
