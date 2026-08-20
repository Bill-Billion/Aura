"""HVAC automation agent — comfort-zone temperature control."""

from __future__ import annotations

from typing import Any

from backend.agents.base import BaseAgent
from backend.engine.event_bus import SimEvent
from backend.engine.state import DeviceState, WorldState


class HVACAgent(BaseAgent):
    """Controls ``hvac`` devices to maintain comfort zone (22-26 °C)."""

    COMFORT_LOW = 22.0
    COMFORT_HIGH = 26.0

    def __init__(self) -> None:
        super().__init__(agent_id="hvac_agent", name="HVAC Agent")

    def get_controlled_device_types(self) -> list[str]:
        return ["hvac"]

    def get_allowed_command_specs(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for device in self.get_relevant_devices(world_state, root_event):
            specs.extend(
                [
                    {"device_id": device.id, "property": "power"},
                    {"device_id": device.id, "property": "extra.target_temp"},
                    {"device_id": device.id, "property": "extra.mode"},
                    {"device_id": device.id, "property": "extra.speed"},
                ]
            )
        return specs

    def determine_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> str:
        # 与 LightingAgent 同一条纪律：agent 替用户做事仍然是 agent，``explicit_user``
        # 留给行为体确实是真人的提案（UI 腿）。空调域两条路径本来就同档，分支删掉即可。
        return "user_comfort"

    def get_relevant_devices(self, world_state: WorldState, root_event: SimEvent) -> list[DeviceState]:
        if root_event.event_type == "user.command":
            device_id = str(root_event.data.get("device_id") or "")
            device = world_state.devices.get(device_id)
            if device is not None and device.type == "hvac":
                return [device]

        focus_rooms = self._focus_rooms(world_state, root_event)
        devices = self._get_my_devices(world_state)
        if not focus_rooms:
            return devices
        return [device for device in devices if device.location.room in focus_rooms]

    def decide(self, world_state: WorldState) -> list[dict]:
        actions: list[dict] = []

        for device in self._get_my_devices(world_state):
            if not device.state.power:
                continue

            room_id = device.location.room
            room = world_state.rooms.get(room_id)
            if room is None or not room.occupancy:
                continue

            current_temp = room.temperature
            mode = device.state.extra.get("mode", "cool")
            current_target = device.state.extra.get("target_temp", 24.0)

            if current_temp > self.COMFORT_HIGH and mode == "cool":
                new_target = max(self.COMFORT_LOW, current_temp - 3)
                if abs(current_target - new_target) > 0.5:
                    actions.append({
                        "device_id": device.id,
                        "property": "extra.target_temp",
                        "value": round(new_target, 1),
                        "reason": f"Room {room_id} at {current_temp}°C > {self.COMFORT_HIGH}°C, cooling to {new_target}°C",
                    })
            elif current_temp < self.COMFORT_LOW and mode == "heat":
                new_target = min(28, current_temp + 3)
                if abs(current_target - new_target) > 0.5:
                    actions.append({
                        "device_id": device.id,
                        "property": "extra.target_temp",
                        "value": round(new_target, 1),
                        "reason": f"Room {room_id} at {current_temp}°C < {self.COMFORT_LOW}°C, heating to {new_target}°C",
                    })

        return actions

    @staticmethod
    def _focus_rooms(world_state: WorldState, root_event: SimEvent) -> set[str]:
        rooms: set[str] = set()
        for key in ("from_room", "to_room"):
            room_id = str(root_event.data.get(key) or "")
            if room_id and room_id != "outside" and room_id in world_state.rooms:
                rooms.add(room_id)

        if root_event.event_type == "environment.state_refresh":
            rooms.update(
                room_id
                for room_id, room in world_state.rooms.items()
                if room.occupancy
            )

        return rooms
