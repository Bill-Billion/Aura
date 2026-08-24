"""Deterministic failure injections keyed by simulated time."""

from __future__ import annotations

from collections import defaultdict


class DeviceFailureController:
    def __init__(self) -> None:
        self._offline_from: dict[str, float] = {}
        self._feedback_loss: dict[str, list[tuple[float, int]]] = defaultdict(list)

    def reset(self) -> None:
        self._offline_from.clear()
        self._feedback_loss.clear()

    def inject_offline(self, device_id: str, *, at_sim_time_s: float = 0.0) -> None:
        current = self._offline_from.get(device_id)
        due = float(at_sim_time_s)
        self._offline_from[device_id] = due if current is None else min(current, due)

    def is_offline(self, device_id: str, sim_time_s: float) -> bool:
        due = self._offline_from.get(device_id)
        return due is not None and sim_time_s >= due

    def inject_feedback_loss(
        self, device_id: str, *, drop_count: int = 1, at_sim_time_s: float = 0.0
    ) -> None:
        if drop_count < 1:
            raise ValueError("drop_count must be positive")
        self._feedback_loss[device_id].append((float(at_sim_time_s), int(drop_count)))
        self._feedback_loss[device_id].sort(key=lambda item: item[0])

    def consume_feedback_loss(self, device_id: str, sim_time_s: float) -> bool:
        plans = self._feedback_loss.get(device_id, [])
        for index, (due, remaining) in enumerate(plans):
            if due > sim_time_s or remaining <= 0:
                continue
            plans[index] = (due, remaining - 1)
            return True
        return False
