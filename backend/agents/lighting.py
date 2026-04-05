"""Lighting automation agent — time-based brightness and colour temperature."""

from __future__ import annotations

from backend.agents.base import BaseAgent
from backend.engine.state import WorldState


def _parse_hour(time_of_day: str) -> int:
    return int(time_of_day.split(":")[0])


class LightingAgent(BaseAgent):
    """Controls ``light`` devices based on time of day and room occupancy."""

    def __init__(self) -> None:
        super().__init__(agent_id="lighting_agent", name="Lighting Agent")

    def get_controlled_device_types(self) -> list[str]:
        return ["light"]

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

            # Only act if difference exceeds threshold
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
    def _targets(hour: int, occupied: bool) -> tuple[int, int]:
        """Return (brightness, color_temp) for a given hour and occupancy."""
        if 6 <= hour < 9:
            # Morning: bright cool light
            return (90, 5000)
        elif 9 <= hour < 17:
            # Daytime
            brightness = 40 if occupied else 10
            return (brightness, 4500)
        elif 17 <= hour < 21:
            # Evening
            brightness = 70 if occupied else 20
            return (brightness, 3000)
        elif 21 <= hour < 23:
            # Late evening
            brightness = 30 if occupied else 5
            return (brightness, 2700)
        else:
            # Night
            return (5, 2700)
