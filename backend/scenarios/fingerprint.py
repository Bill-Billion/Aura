"""Deterministic fingerprint for a scenario's persisted evaluation contract."""

from __future__ import annotations

import hashlib
import json

from backend.config.device_registry import build_default_rooms, get_default_device_registry
from backend.scenarios.spec import ScenarioSpec

SCENARIO_CONTRACT_FINGERPRINT_VERSION = "1"


def scenario_contract_fingerprint(spec: ScenarioSpec) -> str:
    """Hash the scenario and registry content used to evaluate its trace.

    Schema versions alone cannot detect an edited YAML file or registry entry.
    Persisting this digest makes re-evaluation fail explicitly instead of
    silently applying today's expectations to yesterday's events.
    """

    payload = {
        "fingerprint_version": SCENARIO_CONTRACT_FINGERPRINT_VERSION,
        "scenario": spec.model_dump(mode="json"),
        "device_registry": [
            entry.model_dump(mode="json")
            for entry in sorted(get_default_device_registry(), key=lambda item: item.id)
        ],
        "rooms": {
            room_id: room.model_dump(mode="json")
            for room_id, room in sorted(build_default_rooms().items())
        },
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["SCENARIO_CONTRACT_FINGERPRINT_VERSION", "scenario_contract_fingerprint"]
