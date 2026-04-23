"""Physics-based environment simulator."""

from __future__ import annotations

import math

from backend.engine.state import WorldState


TEMP_OUTDOOR_DIFFUSION_RATE = 0.002
HVAC_COOL_RATE = 0.05
HVAC_HEAT_RATE = 0.03

LIGHT_LUX_PER_BRIGHTNESS = 8.0
NATURAL_LIGHT_MAX_LUX = 500.0
NATURAL_LIGHT_START_HOUR = 6.0
NATURAL_LIGHT_END_HOUR = 18.0


def _natural_light_lux(hour: float) -> float:
    if hour < NATURAL_LIGHT_START_HOUR or hour > NATURAL_LIGHT_END_HOUR:
        return 0.0

    t = (hour - NATURAL_LIGHT_START_HOUR) / (
        NATURAL_LIGHT_END_HOUR - NATURAL_LIGHT_START_HOUR
    )
    factor = math.sin(math.pi * t)
    return NATURAL_LIGHT_MAX_LUX * factor


def _parse_time_hour(time_of_day: str) -> float:
    hour, minute = time_of_day.split(":")
    return int(hour) + int(minute) / 60.0


def _weather_for_hour(hour: float) -> str:
    if 6 <= hour < 11:
        return "clear"
    if 11 <= hour < 17:
        return "cloudy"
    if 17 <= hour < 21:
        return "rainy"
    return "cloudy"


def _outdoor_temp_for_hour(hour: float, weather: str) -> float:
    base = 23.0 + math.sin((hour - 6.0) / 24.0 * math.pi * 2) * 6.0
    if weather == "cloudy":
        base -= 1.5
    elif weather == "rainy":
        base -= 3.0
    return round(base, 1)


def _outdoor_humidity_for_weather(weather: str) -> float:
    if weather == "rainy":
        return 0.82
    if weather == "cloudy":
        return 0.62
    return 0.45


class EnvironmentSimulator:
    """Step-based environment physics engine."""

    def step(self, state: WorldState, dt: float) -> dict[str, float | str]:
        hour = _parse_time_hour(state.environment.time_of_day)
        weather = _weather_for_hour(hour)
        outdoor_temp = _outdoor_temp_for_hour(hour, weather)
        outdoor_humidity = _outdoor_humidity_for_weather(weather)

        hvac_by_room: dict[str, dict[str, float | str]] = {}
        for device in state.devices.values():
            if device.type == "hvac" and device.state.power:
                hvac_by_room[device.location.room] = {
                    "target_temp": float(device.state.extra.get("target_temp", 24.0)),
                    "mode": str(device.state.extra.get("mode", "cool")),
                }

        curtain_by_room: dict[str, float] = {}
        for device in state.devices.values():
            if device.type == "curtain":
                curtain_by_room[device.location.room] = float(
                    device.state.extra.get("open_percent", 100.0)
                )

        brightness_by_room: dict[str, float] = {}
        for device in state.devices.values():
            if device.type == "light" and device.state.power:
                room = device.location.room
                brightness = float(device.state.extra.get("brightness", 0.0))
                brightness_by_room[room] = brightness_by_room.get(room, 0.0) + brightness

        natural_lux = _natural_light_lux(hour)
        updates: dict[str, float | str] = {
            "environment.weather": weather,
            "environment.outdoor_temp": outdoor_temp,
            "environment.outdoor_humidity": outdoor_humidity,
        }

        # 设计意图：天气和室外温度先走确定性日变化曲线，方便对照调试和截图回归。
        for room_id, room in state.rooms.items():
            next_temperature = room.temperature
            diffusion = TEMP_OUTDOOR_DIFFUSION_RATE * dt * (outdoor_temp - next_temperature)
            next_temperature += diffusion

            hvac = hvac_by_room.get(room_id)
            if hvac is not None:
                target = float(hvac["target_temp"])
                mode = str(hvac["mode"])
                if mode == "cool" and next_temperature > target:
                    next_temperature -= HVAC_COOL_RATE * dt * (next_temperature - target)
                elif mode == "heat" and next_temperature < target:
                    next_temperature += HVAC_HEAT_RATE * dt * (target - next_temperature)

            artificial_lux = brightness_by_room.get(room_id, 0.0) * LIGHT_LUX_PER_BRIGHTNESS
            curtain_pct = curtain_by_room.get(room_id, 100.0)
            natural_contribution = natural_lux * (curtain_pct / 100.0)

            updates[f"rooms[{room_id}].temperature"] = round(next_temperature, 2)
            updates[f"rooms[{room_id}].light_level"] = round(
                artificial_lux + natural_contribution, 2
            )

        return updates
