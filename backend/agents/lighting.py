"""Lighting automation agent — time-based brightness and colour temperature."""

from __future__ import annotations

from typing import Any

from backend.agents.base import BaseAgent
from backend.engine.event_bus import SimEvent
from backend.engine.state import DeviceState, WorldState


def _parse_hour(time_of_day: str) -> int:
    return int(time_of_day.split(":")[0])


class LightingAgent(BaseAgent):
    """Controls ``light`` devices based on time of day and room occupancy."""

    def __init__(self) -> None:
        super().__init__(agent_id="lighting_agent", name="Lighting Agent")

    def get_controlled_device_types(self) -> list[str]:
        return ["light"]

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
                    {"device_id": device.id, "property": "extra.brightness"},
                    {"device_id": device.id, "property": "extra.color_temp"},
                ]
            )
        return specs

    def determine_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> str:
        if root_event.event_type == "user.command":
            return "direct_user_command"

        relevant_rooms = self.get_relevant_rooms(world_state, root_event)
        if any(world_state.rooms.get(room_id) and world_state.rooms[room_id].occupancy for room_id in relevant_rooms):
            return "user_comfort"

        return "convenience"

    def get_relevant_devices(self, world_state: WorldState, root_event: SimEvent) -> list[DeviceState]:
        if root_event.event_type == "user.command":
            device_id = str(root_event.data.get("device_id") or "")
            device = world_state.devices.get(device_id)
            if device is not None and device.type == "light":
                return [device]

        focus_rooms = self._focus_rooms(world_state, root_event)
        devices = self._get_my_devices(world_state)
        if not focus_rooms:
            return devices
        return [device for device in devices if device.location.room in focus_rooms]

    def decide(self, world_state: WorldState) -> list[dict]:
        hour = _parse_hour(world_state.environment.time_of_day)
        actions: list[dict] = []

        for device in self._get_my_devices(world_state):
            room_id = device.location.room
            room = world_state.rooms.get(room_id)
            occupied = room.occupancy if room else False

            target_brightness, target_color_temp = self._targets(hour, occupied)

            current_brightness = device.state.extra.get("brightness", 0)
            current_color_temp = device.state.extra.get("color_temp", 4000)

            if abs(current_brightness - target_brightness) > 5:
                actions.append({
                    "device_id": device.id,
                    "property": "extra.brightness",
                    "value": target_brightness,
                    "reason": f"Time-based brightness: hour={hour}, occupied={occupied}",
                })

            if abs(current_color_temp - target_color_temp) > 200:
                actions.append({
                    "device_id": device.id,
                    "property": "extra.color_temp",
                    "value": target_color_temp,
                    "reason": f"Time-based color_temp: hour={hour}, occupied={occupied}",
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

    @staticmethod
    def _targets(hour: int, occupied: bool) -> tuple[int, int]:
        if 6 <= hour < 9:
            return (90, 5000)
        if 9 <= hour < 17:
            brightness = 40 if occupied else 10
            return (brightness, 4500)
        if 17 <= hour < 21:
            brightness = 70 if occupied else 20
            return (brightness, 3000)
        if 21 <= hour < 23:
            brightness = 30 if occupied else 5
            return (brightness, 2700)
        return (5, 2700)
