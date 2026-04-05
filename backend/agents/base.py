"""Base agent abstract class for smart home automation agents."""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.engine.state import DeviceState, WorldState


class BaseAgent(ABC):
    """Abstract base class for all smart home agents."""

    def __init__(self, agent_id: str, name: str) -> None:
        self.agent_id = agent_id
        self.name = name

    @abstractmethod
    def get_controlled_device_types(self) -> list[str]:
        """Return the list of device type strings this agent controls."""

    @abstractmethod
    def decide(self, world_state: WorldState) -> list[dict]:
        """Analyse world state and return a list of actions to take.

        Each action dict has keys:
        - ``device_id``: str
        - ``property``: str (dot-notation path)
        - ``value``: new value
        - ``reason``: human-readable reason
        """

    def _get_my_devices(self, world_state: WorldState) -> list[DeviceState]:
        """Return all devices whose type matches this agent's controlled types."""
        controlled = set(self.get_controlled_device_types())
        return [d for d in world_state.devices.values() if d.type in controlled]
