"""HVAC automation agent — comfort-zone temperature control."""

from __future__ import annotations

from backend.agents.base import BaseAgent
from backend.engine.state import WorldState


class HVACAgent(BaseAgent):
    """Controls ``hvac`` devices to maintain comfort zone (22-26 °C)."""

    COMFORT_LOW = 22.0
    COMFORT_HIGH = 26.0

    def __init__(self) -> None:
        super().__init__(agent_id="hvac_agent", name="HVAC Agent")

    def get_controlled_device_types(self) -> list[str]:
        return ["hvac"]

    def decide(self, world_state: WorldState) -> list[dict]:
        actions: list[dict] = []

        for device in self._get_my_devices(world_state):
            if not device.state.power:
                continue

            room_id = device.location.room
            room = world_state.rooms.get(room_id)
            if room is None:
                continue

            # Only act when room is occupied
            if not room.occupancy:
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
