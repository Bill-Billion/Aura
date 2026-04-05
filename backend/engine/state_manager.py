from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any

from backend.engine.state import WorldState, DeviceState, DeviceStateValues, Location3D


class DeltaChange(BaseModel):
    """Records a single state mutation."""

    path: str
    old_value: Any
    new_value: Any
    caused_by: str
    reason: str = ""


class StateManager:
    """Owns the canonical WorldState and exposes mutation + query helpers."""

    def __init__(self, world: WorldState | None = None):
        self.world = world if world is not None else WorldState()

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def apply_action(
        self,
        agent_id: str,
        device_id: str,
        property_path: str,
        new_value: Any,
        reason: str = "",
    ) -> list[DeltaChange]:
        """Apply a change to a device's state via a dot-notation path.

        *property_path* examples: ``"power"``, ``"extra.brightness"``,
        ``"last_changed_by"``.

        Returns a list of :class:`DeltaChange` objects (one per changed leaf).
        """
        device = self.world.devices.get(device_id)
        if device is None:
            raise KeyError(f"Device '{device_id}' not found in world state")

        deltas: list[DeltaChange] = []

        old_value = self._get_nested(device.state, property_path)
        if old_value == new_value:
            return deltas  # no change needed

        self._set_nested(device.state, property_path, new_value)

        deltas.append(
            DeltaChange(
                path=f"devices.{device_id}.state.{property_path}",
                old_value=old_value,
                new_value=new_value,
                caused_by=agent_id,
                reason=reason,
            )
        )

        # Always record who last changed the device
        if property_path != "last_changed_by":
            prev_agent = device.state.last_changed_by
            device.state.last_changed_by = agent_id
            if prev_agent != agent_id:
                deltas.append(
                    DeltaChange(
                        path=f"devices.{device_id}.state.last_changed_by",
                        old_value=prev_agent,
                        new_value=agent_id,
                        caused_by=agent_id,
                        reason="auto-update on action",
                    )
                )

        return deltas

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_full_snapshot(self) -> dict:
        """Return a plain-dict deep copy of the entire world state."""
        return self.world.snapshot().model_dump()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_nested(obj: BaseModel | dict, path: str) -> Any:
        """Resolve a dot-notation *path* against a Pydantic model or dict."""
        parts = path.split(".")
        current: Any = obj
        for part in parts:
            if isinstance(current, dict):
                current = current[part]
            else:
                current = getattr(current, part)
        return current

    @staticmethod
    def _set_nested(obj: BaseModel | dict, path: str, value: Any) -> None:
        """Set a value at *path* (dot-notation) on a Pydantic model or dict."""
        parts = path.split(".")
        current: Any = obj
        for part in parts[:-1]:
            if isinstance(current, dict):
                current = current[part]
            else:
                current = getattr(current, part)
        last = parts[-1]
        if isinstance(current, dict):
            current[last] = value
        else:
            setattr(current, last, value)
