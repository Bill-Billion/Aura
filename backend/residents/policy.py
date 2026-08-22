"""Deterministic resident policies and the seeded reproducible variant."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from backend.engine.event_bus import SimEvent
from backend.engine.rng import SimStream
from backend.engine.state import DeviceState, WorldState
from backend.execution.executor import (
    DEVICE_EFFECT_APPLIED_EVENT_TYPE,
    FEEDBACK_EVENT_TYPE,
)
from backend.simulators.user_behavior import UserBehaviorSimulator

from .state import ResidentState


ResponseKind = Literal[
    "satisfied", "dissatisfied", "regret", "correction", "override"
]


@dataclass(frozen=True, slots=True)
class ResidentResponse:
    kind: ResponseKind
    user_id: str
    satisfaction: float
    goal: str
    room_id: str | None
    intent: str
    device_id: str | None = None
    capability: str | None = None
    value: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ResidentPolicy(Protocol):
    def respond(
        self,
        state: ResidentState,
        world: WorldState,
        event: SimEvent,
        *,
        sim_time_s: float,
    ) -> list[ResidentResponse]: ...


class ScriptedResidentPolicy:
    """Compatibility adapter around the existing fixed schedule simulator."""

    def __init__(self, simulator: UserBehaviorSimulator | None = None) -> None:
        self.simulator = simulator or UserBehaviorSimulator()

    def respond(
        self,
        state: ResidentState,
        world: WorldState,
        event: SimEvent,
        *,
        sim_time_s: float,
    ) -> list[ResidentResponse]:
        return []

    def step(self, world: WorldState) -> list[Any]:
        return self.simulator.step(world)


class DeterministicResponsivePolicy:
    """Small finite-state policy; no model calls and no wall-clock reads."""

    _LOW = 0.35
    _HIGH = 0.65

    def respond(
        self,
        state: ResidentState,
        world: WorldState,
        event: SimEvent,
        *,
        sim_time_s: float,
    ) -> list[ResidentResponse]:
        if state.location is None or state.location not in world.rooms:
            score = 0.8
        else:
            score = self._comfort_score(state, world)

        previous_band = state.satisfaction_band
        next_band = self._band(score, previous_band)
        state.satisfaction = score
        state.satisfaction_band = next_band

        responses: list[ResidentResponse] = []
        if next_band != previous_band:
            responses.append(
                ResidentResponse(
                    kind=("satisfied" if next_band == "satisfied" else "dissatisfied"),
                    user_id=state.user_id,
                    satisfaction=score,
                    goal=state.goal,
                    room_id=state.location,
                    intent=f"resident is {next_band} with {state.goal}",
                )
            )

        if next_band != "dissatisfied" or not self._may_correct(state, sim_time_s):
            return responses
        correction = self._correction(state, world)
        if correction is None:
            return responses

        device, capability, value, intent = correction
        source = str(event.data.get("source") or "")
        kind: ResponseKind = (
            "override"
            if source in {"agent", "rule_fallback"} and "override" in state.permissions
            else "correction"
        )
        state.correction_count += 1
        state.last_correction_at_s = sim_time_s
        responses.append(
            ResidentResponse(
                kind=kind,
                user_id=state.user_id,
                satisfaction=score,
                goal=state.goal,
                room_id=state.location,
                intent=intent,
                device_id=device.id,
                capability=capability,
                value=value,
            )
        )
        return responses

    @classmethod
    def _band(
        cls,
        score: float,
        previous: Literal["dissatisfied", "neutral", "satisfied"],
    ) -> Literal["dissatisfied", "neutral", "satisfied"]:
        # Two thresholds are the hysteresis: a dissatisfied resident does not
        # become satisfied until crossing HIGH, and vice versa for LOW.
        if score <= cls._LOW:
            return "dissatisfied"
        if score >= cls._HIGH:
            return "satisfied"
        return previous

    @staticmethod
    def _comfort_score(state: ResidentState, world: WorldState) -> float:
        assert state.location is not None
        room = world.rooms[state.location]
        prefs = state.long_term_preferences
        temperature = (
            room.perceived_temperature
            if room.perceived_temperature is not None
            else room.temperature
        )
        temperature_ok = prefs.temperature_min <= temperature <= prefs.temperature_max

        activity = state.activity.strip().lower()
        if activity == "reading":
            light_ok = room.light_level >= prefs.reading_min_lux
        elif activity in {"sleeping", "going_to_sleep"}:
            light_ok = room.light_level <= prefs.sleeping_max_lux
        else:
            light_ok = True
        # The resident's active goal dominates a background thermal preference:
        # a dark reading room is genuinely unsatisfactory even when temperature
        # happens to be comfortable.  Values deliberately straddle the policy's
        # two hysteresis thresholds.
        if not light_ok:
            return 0.2
        if not temperature_ok:
            return 0.3
        return 0.9

    @staticmethod
    def _may_correct(state: ResidentState, sim_time_s: float) -> bool:
        prefs = state.long_term_preferences
        if state.correction_count >= prefs.max_corrections:
            return False
        last = state.last_correction_at_s
        return last is None or sim_time_s - last >= prefs.correction_cooldown_s

    @staticmethod
    def _devices_in_room(
        state: ResidentState, world: WorldState, device_type: str
    ) -> list[DeviceState]:
        return sorted(
            (
                device
                for device in world.devices.values()
                if device.location.room == state.location and device.type == device_type
            ),
            key=lambda device: device.id,
        )

    def _correction(
        self, state: ResidentState, world: WorldState
    ) -> tuple[DeviceState, str, Any, str] | None:
        if state.location is None or state.location not in world.rooms:
            return None
        room = world.rooms[state.location]
        prefs = state.long_term_preferences
        activity = state.activity.strip().lower()
        lights = self._devices_in_room(state, world, "light")
        if activity == "reading" and room.light_level < prefs.reading_min_lux and lights:
            light = lights[0]
            if not light.state.power:
                return light, "power", True, "restore reading light"
            return (
                light,
                "brightness",
                prefs.preferred_brightness,
                "increase reading brightness",
            )
        if activity in {"sleeping", "going_to_sleep"} and room.light_level > prefs.sleeping_max_lux:
            powered = next((light for light in lights if light.state.power), None)
            if powered is not None:
                return powered, "power", False, "restore dark sleeping environment"

        temperature = (
            room.perceived_temperature
            if room.perceived_temperature is not None
            else room.temperature
        )
        if not prefs.temperature_min <= temperature <= prefs.temperature_max:
            hvacs = self._devices_in_room(state, world, "hvac")
            if hvacs:
                hvac = hvacs[0]
                if not hvac.state.power:
                    return hvac, "power", True, "restore thermal comfort"
                return (
                    hvac,
                    "target_temp",
                    round((prefs.temperature_min + prefs.temperature_max) / 2.0, 1),
                    "correct thermal target",
                )
        return None


class SeededStochasticResidentPolicy(DeterministicResponsivePolicy):
    """Responsive policy with reproducible, seed-controlled regret events."""

    def __init__(self, stream: SimStream) -> None:
        self.stream = stream

    def respond(
        self,
        state: ResidentState,
        world: WorldState,
        event: SimEvent,
        *,
        sim_time_s: float,
    ) -> list[ResidentResponse]:
        responses = super().respond(
            state, world, event, sim_time_s=sim_time_s
        )
        if (
            event.event_type == DEVICE_EFFECT_APPLIED_EVENT_TYPE
            and state.satisfaction_band == "satisfied"
            and self.stream.random() < state.long_term_preferences.regret_probability
        ):
            responses.append(
                ResidentResponse(
                    kind="regret",
                    user_id=state.user_id,
                    satisfaction=state.satisfaction,
                    goal=state.goal,
                    room_id=state.location,
                    intent="resident reconsidered the latest device effect",
                    metadata=self.stream.event_metadata(),
                )
            )
        return responses


RESPONSIVE_EVENT_TYPES = frozenset(
    {
        DEVICE_EFFECT_APPLIED_EVENT_TYPE,
        FEEDBACK_EVENT_TYPE,
        "environment.state_refresh",
        "user.activity_change",
        "user.arrives_home",
        "user.leaves_home",
        "user.enters_room",
        "user.exits_room",
        "user.starts_activity",
        "user.ends_activity",
    }
)
