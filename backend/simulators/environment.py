"""Physics-based environment simulator.

Updates room temperature and light_level each simulation step.
"""

from __future__ import annotations

import math

from backend.engine.state import WorldState


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Temperature
TEMP_OUTDOOR_DIFFUSION_RATE = 0.002  # per dt unit, toward outdoor temp
HVAC_COOL_RATE = 0.05  # per dt unit, toward target temp
HVAC_HEAT_RATE = 0.03  # per dt unit, toward target temp

# Light
LIGHT_LUX_PER_BRIGHTNESS = 8.0  # lux per brightness unit from a light
NATURAL_LIGHT_MAX_LUX = 500.0
NATURAL_LIGHT_PEAK_HOUR = 12.0
NATURAL_LIGHT_START_HOUR = 6.0
NATURAL_LIGHT_END_HOUR = 18.0


def _natural_light_lux(hour: float) -> float:
    """Compute natural sunlight intensity using a cosine model.

    Peak at 12:00, range 6:00–18:00, max 500 lux.
    Outside the range the contribution is 0.
    """
    if hour < NATURAL_LIGHT_START_HOUR or hour > NATURAL_LIGHT_END_HOUR:
        return 0.0

    # Normalised position in [0, 1] where 0.5 = peak
    t = (hour - NATURAL_LIGHT_START_HOUR) / (
        NATURAL_LIGHT_END_HOUR - NATURAL_LIGHT_START_HOUR
    )
    # Cosine curve: 1 at t=0.5 (peak), 0 at t=0 and t=1
    factor = max(0.0, math.cos(math.pi * (t - 0.5) * 2) * 0.5 + 0.5)
    # Actually a simpler approach: use sin curve peaking at 0.5
    # sin(pi * t) gives 0 at t=0, 1 at t=0.5, 0 at t=1
    factor = math.sin(math.pi * t)
    return NATURAL_LIGHT_MAX_LUX * factor


def _parse_time_hour(time_of_day: str) -> float:
    """Parse 'HH:MM' string to hour as float."""
    parts = time_of_day.split(":")
    return int(parts[0]) + int(parts[1]) / 60.0


class EnvironmentSimulator:
    """Step-based environment physics engine."""

    def step(self, state: WorldState, dt: float) -> None:
        """Advance environment by *dt* simulation-time units.

        Modifies ``state.rooms`` in place.

        Parameters
        ----------
        state : WorldState
            The canonical world state (mutated in place).
        dt : float
            Simulation time step (in arbitrary time units).
        """
        hour = _parse_time_hour(state.environment.time_of_day)
        outdoor_temp = state.environment.outdoor_temp

        # Build a lookup of HVAC active devices per room
        hvac_by_room: dict[str, dict] = {}
        for device in state.devices.values():
            if device.type == "hvac" and device.state.power:
                room = device.location.room
                target_temp = device.state.extra.get("target_temp", 24.0)
                mode = device.state.extra.get("mode", "cool")
                hvac_by_room[room] = {
                    "target_temp": target_temp,
                    "mode": mode,
                }

        # Build a lookup of curtains per room
        curtain_by_room: dict[str, float] = {}
        for device in state.devices.values():
            if device.type == "curtain":
                room = device.location.room
                open_pct = device.state.extra.get("open_percent", 100.0)
                curtain_by_room[room] = open_pct

        # Build a lookup of total light brightness per room
        brightness_by_room: dict[str, float] = {}
        for device in state.devices.values():
            if device.type == "light" and device.state.power:
                room = device.location.room
                brightness = device.state.extra.get("brightness", 0.0)
                brightness_by_room[room] = brightness_by_room.get(room, 0) + brightness

        natural_lux = _natural_light_lux(hour)

        for room_id, room in state.rooms.items():
            # --- Temperature ---
            # Outdoor diffusion
            diffusion = TEMP_OUTDOOR_DIFFUSION_RATE * dt * (outdoor_temp - room.temperature)
            room.temperature += diffusion

            # HVAC effect
            hvac = hvac_by_room.get(room_id)
            if hvac is not None:
                target = hvac["target_temp"]
                mode = hvac["mode"]
                if mode == "cool" and room.temperature > target:
                    room.temperature -= HVAC_COOL_RATE * dt * (room.temperature - target)
                elif mode == "heat" and room.temperature < target:
                    room.temperature += HVAC_HEAT_RATE * dt * (target - room.temperature)

            # --- Light level ---
            # Artificial light
            artificial_lux = brightness_by_room.get(room_id, 0.0) * LIGHT_LUX_PER_BRIGHTNESS

            # Natural light through curtains
            curtain_pct = curtain_by_room.get(room_id, 100.0)
            natural_contribution = natural_lux * (curtain_pct / 100.0)

            room.light_level = artificial_lux + natural_contribution
