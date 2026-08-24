"""A genuine single-controller baseline for research comparisons."""

from __future__ import annotations

from typing import Any

from backend.agents.base import BaseAgent
from backend.agents.contracts import DomainTask, PriorityLevel, ProposalAssumption
from backend.agents.types import PriorityLabel
from backend.config.event_mapping import DeviceSearchSpace, get_device_search_space
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import (
    ENVIRONMENT_LIGHT_LEVEL_THRESHOLD,
    ENVIRONMENT_TEMPERATURE_THRESHOLD,
    SAFETY_SMOKE_DETECTED,
    SECURITY_DOOR_OPENED,
    SECURITY_PRESENCE_DETECTED,
    USER_ARRIVES_HOME,
    USER_COMMAND,
    USER_ENDS_ACTIVITY,
    USER_ENTERS_ROOM,
    USER_EXITS_ROOM,
    USER_LEAVES_HOME,
    USER_STARTS_ACTIVITY,
)
from backend.engine.state import DeviceState, WorldState
from backend.execution.capability_matrix import get_writable_capability_names

__all__ = ["SINGLE_DIRECT_AGENT_ID", "SingleDirectAgent"]

SINGLE_DIRECT_AGENT_ID = "single_direct_agent"
_CONTROLLED_DEVICE_TYPES = frozenset(
    {"camera", "curtain", "fan", "hvac", "light", "sensor"}
)


class SingleDirectAgent(BaseAgent):
    """One actor that observes, decides, and proposes commands exactly once."""

    agent_role = "generalist"

    def __init__(self) -> None:
        super().__init__(agent_id=SINGLE_DIRECT_AGENT_ID, name="Single Direct Agent")

    def get_controlled_device_types(self) -> list[str]:
        return sorted(_CONTROLLED_DEVICE_TYPES)

    @staticmethod
    def _affected_device_type(world: WorldState, event: SimEvent) -> str | None:
        device_id = str(event.data.get("device_id") or "")
        device = world.devices.get(device_id)
        if device is not None:
            return device.type
        declared = event.data.get("device_type")
        return str(declared) if declared else None

    def _search_space(self, world: WorldState, event: SimEvent) -> DeviceSearchSpace:
        activity = event.data.get("activity")
        return get_device_search_space(
            event.event_type,
            activity=str(activity) if activity else None,
            affected_device_type=self._affected_device_type(world, event),
        )

    @staticmethod
    def _focus_rooms(world: WorldState, event: SimEvent) -> set[str]:
        rooms = {
            str(event.data.get(key))
            for key in ("room_id", "from_room", "to_room")
            if event.data.get(key) and str(event.data.get(key)) != "outside"
        }
        user_id = str(event.data.get("user_id") or "")
        user = world.users.get(user_id)
        if user is not None and user.location is not None and user.location.room:
            rooms.add(user.location.room)
        return {room_id for room_id in rooms if room_id in world.rooms}

    def is_relevant(self, world_state: WorldState, root_event: SimEvent) -> bool:
        if not super().is_relevant(world_state, root_event):
            return False
        return bool(self.get_relevant_devices(world_state, root_event))

    def get_relevant_devices(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[DeviceState]:
        if root_event.event_type == USER_COMMAND:
            device_id = str(root_event.data.get("device_id") or "")
            device = world_state.devices.get(device_id)
            if device is not None and device.type in _CONTROLLED_DEVICE_TYPES:
                return [device]

        space = self._search_space(world_state, root_event)
        device_types = set(space.device_types) & _CONTROLLED_DEVICE_TYPES
        focus_rooms = self._focus_rooms(world_state, root_event)
        if not focus_rooms and space.room_scope:
            focus_rooms = set(space.room_scope)
        return sorted(
            (
                device
                for device in world_state.devices.values()
                if device.type in device_types
                and (not focus_rooms or device.location.room in focus_rooms)
            ),
            key=lambda device: device.id,
        )

    def get_allowed_command_specs(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict[str, Any]]:
        space = self._search_space(world_state, root_event)
        narrowed = set(space.capabilities)
        specs: list[dict[str, Any]] = []
        for device in self.get_relevant_devices(world_state, root_event):
            capabilities = sorted(get_writable_capability_names(device.type))  # type: ignore[arg-type]
            if narrowed:
                capabilities = [item for item in capabilities if item in narrowed]
            for capability in capabilities:
                specs.append(
                    {
                        "device_id": device.id,
                        "property": (
                            capability
                            if capability == "power"
                            else f"extra.{capability}"
                        ),
                    }
                )
        return specs

    def decide(self, world_state: WorldState) -> list[dict]:
        return []

    def decide_for_event(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict]:
        if root_event.event_type == USER_COMMAND:
            return self._direct_user_command(world_state, root_event)
        if root_event.event_type == SAFETY_SMOKE_DETECTED:
            return self._safety_actions(world_state, root_event)
        if root_event.event_type in {
            SECURITY_PRESENCE_DETECTED,
            SECURITY_DOOR_OPENED,
        }:
            return self._security_actions(world_state, root_event)
        if root_event.event_type in {
            USER_LEAVES_HOME,
            USER_EXITS_ROOM,
            USER_ENDS_ACTIVITY,
        }:
            return self._release_actions(world_state, root_event)
        if root_event.event_type == ENVIRONMENT_TEMPERATURE_THRESHOLD:
            return self._temperature_actions(world_state, root_event)
        if root_event.event_type == ENVIRONMENT_LIGHT_LEVEL_THRESHOLD:
            return self._lighting_actions(world_state, root_event)
        if root_event.event_type in {
            USER_ARRIVES_HOME,
            USER_ENTERS_ROOM,
            USER_STARTS_ACTIVITY,
        }:
            return [
                *self._lighting_actions(world_state, root_event),
                *self._temperature_actions(world_state, root_event),
            ]
        return []

    def determine_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> PriorityLabel:
        return "user_comfort"

    def proposal_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> PriorityLevel:
        return PriorityLevel.COMFORT

    def proposal_intent(self, world_state: WorldState, root_event: SimEvent) -> str:
        return f"single direct response to {root_event.event_type}"

    def proposal_assumptions(
        self,
        world_state: WorldState,
        root_event: SimEvent,
        domain_task: DomainTask | None,
    ) -> list[ProposalAssumption]:
        """Seal the observable facts supplied to the single controller."""

        assumptions = [
            ProposalAssumption(
                path="environment.time_of_day",
                equals=world_state.environment.time_of_day,
            ),
            ProposalAssumption(
                path="environment.weather",
                equals=world_state.environment.weather,
            ),
        ]
        user_id = str(root_event.data.get("user_id") or "")
        if user_id in world_state.users:
            user = world_state.users[user_id]
            assumptions.extend(
                [
                    ProposalAssumption(
                        path=f"users[{user_id}].activity",
                        equals=user.activity,
                    ),
                    ProposalAssumption(
                        path=f"users[{user_id}].location.room",
                        equals=(
                            user.location.room if user.location is not None else None
                        ),
                    ),
                ]
            )
        room_ids = {
            device.location.room
            for device in self.get_relevant_devices(world_state, root_event)
            if device.location.room in world_state.rooms
        }
        if root_event.event_type in {
            SECURITY_PRESENCE_DETECTED,
            SECURITY_DOOR_OPENED,
        }:
            room_ids.update(world_state.rooms)
        for room_id in sorted(room_ids):
            room = world_state.rooms[room_id]
            assumptions.extend(
                [
                    ProposalAssumption(
                        path=f"rooms[{room_id}].occupancy",
                        equals=room.occupancy,
                    ),
                    ProposalAssumption(
                        path=f"rooms[{room_id}].temperature",
                        equals=room.temperature,
                    ),
                    ProposalAssumption(
                        path=f"rooms[{room_id}].light_level",
                        equals=room.light_level,
                    ),
                ]
            )
        return sorted(assumptions, key=lambda item: item.path)

    def _direct_user_command(self, world: WorldState, event: SimEvent) -> list[dict]:
        device_id = str(event.data.get("device_id") or "")
        if device_id not in world.devices:
            return []
        payload = event.data.get("payload")
        values = payload if isinstance(payload, dict) else event.data
        capability = str(values.get("capability") or values.get("property") or "")
        action = str(values.get("action") or event.data.get("action") or "")
        value = values.get("value")
        if not capability and action in {"turn_on", "turn_off"}:
            capability = "power"
            value = action == "turn_on"
        if not capability:
            return []
        return [
            {
                "device_id": device_id,
                "property": (
                    capability if capability == "power" else f"extra.{capability}"
                ),
                "value": value,
                "reason": str(
                    values.get("reason") or "single direct user command"
                ),
            }
        ]

    def _lighting_actions(self, world: WorldState, event: SimEvent) -> list[dict]:
        hour = int(world.environment.time_of_day.split(":", 1)[0])
        if 6 <= hour < 9:
            brightness, color_temp = 90, 5000
        elif 9 <= hour < 17:
            brightness, color_temp = 40, 4500
        elif 17 <= hour < 21:
            brightness, color_temp = 70, 3000
        else:
            brightness, color_temp = 30, 2700

        actions: list[dict] = []
        for device in self.get_relevant_devices(world, event):
            if device.type != "light":
                continue
            room = world.rooms.get(device.location.room)
            target = brightness if room is not None and room.occupancy else 10
            if abs(float(device.state.extra.get("brightness", 0)) - target) > 5:
                actions.append(
                    self._action(device.id, "brightness", target, "direct comfort")
                )
            if abs(
                float(device.state.extra.get("color_temp", 4000)) - color_temp
            ) > 200:
                actions.append(
                    self._action(
                        device.id,
                        "color_temp",
                        color_temp,
                        "direct comfort",
                    )
                )
        return actions

    def _temperature_actions(self, world: WorldState, event: SimEvent) -> list[dict]:
        actions: list[dict] = []
        for device in self.get_relevant_devices(world, event):
            room = world.rooms.get(device.location.room)
            if room is None or not room.occupancy:
                continue
            if device.type == "hvac" and device.state.power:
                if room.temperature > 26:
                    actions.append(
                        self._action(device.id, "target_temp", 24.0, "direct cooling")
                    )
                elif room.temperature < 22:
                    actions.append(
                        self._action(device.id, "target_temp", 24.0, "direct heating")
                    )
            elif device.type == "fan" and room.temperature > 26 and not device.state.power:
                actions.append(
                    self._action(device.id, "power", True, "direct cooling")
                )
        return actions

    def _release_actions(self, world: WorldState, event: SimEvent) -> list[dict]:
        actions: list[dict] = []
        for device in self.get_relevant_devices(world, event):
            room = world.rooms.get(device.location.room)
            if device.type in {"light", "hvac", "fan"} and device.state.power:
                if room is None or not room.occupancy:
                    actions.append(
                        self._action(device.id, "power", False, "direct release")
                    )
        return actions

    def _security_actions(self, world: WorldState, event: SimEvent) -> list[dict]:
        if any(room.occupancy for room in world.rooms.values()):
            return []
        return [
            self._action(device.id, "brightness", 100, "direct security response")
            for device in self.get_relevant_devices(world, event)
            if device.type == "light"
        ]

    def _safety_actions(self, world: WorldState, event: SimEvent) -> list[dict]:
        actions: list[dict] = []
        for device in self.get_relevant_devices(world, event):
            if device.type == "light":
                actions.append(
                    self._action(device.id, "power", True, "direct safety")
                )
                actions.append(
                    self._action(device.id, "brightness", 100, "direct safety")
                )
            elif device.type in {"hvac", "fan"}:
                actions.append(
                    self._action(device.id, "power", False, "direct safety")
                )
            elif device.type == "curtain":
                actions.append(
                    self._action(device.id, "open_percent", 100, "direct safety")
                )
        return actions

    @staticmethod
    def _action(device_id: str, capability: str, value: Any, reason: str) -> dict:
        return {
            "device_id": device_id,
            "property": (
                capability if capability == "power" else f"extra.{capability}"
            ),
            "value": value,
            "reason": reason,
        }
