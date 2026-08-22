"""Resident ground truth kept outside the agent-visible ``WorldState``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ResidentAuthority = Literal[
    "child", "guest", "adult", "owner", "administrator"
]


@dataclass(frozen=True, slots=True)
class ResidentPreferences:
    """Long-lived, latent preferences used by resident policies only."""

    preferred_brightness: int = 70
    reading_min_lux: float = 300.0
    sleeping_max_lux: float = 40.0
    temperature_min: float = 20.0
    temperature_max: float = 25.0
    correction_cooldown_s: float = 10.0
    max_corrections: int = 3
    regret_probability: float = 0.15


@dataclass(frozen=True, slots=True)
class ResidentProfile:
    profile_id: str
    preferences: ResidentPreferences = field(default_factory=ResidentPreferences)


@dataclass(slots=True)
class ResidentState:
    user_id: str
    profile_id: str
    authority_level: ResidentAuthority
    location: str | None
    activity: str
    goal: str
    satisfaction: float
    long_term_preferences: ResidentPreferences
    permissions: frozenset[str]
    satisfaction_band: Literal["dissatisfied", "neutral", "satisfied"] = "neutral"
    correction_count: int = 0
    last_correction_at_s: float | None = None


_READER = ResidentPreferences(
    preferred_brightness=75,
    reading_min_lux=350.0,
    temperature_min=20.0,
    temperature_max=25.0,
)


def resolve_profile(profile_id: str) -> ResidentProfile:
    """Resolve built-ins; unknown ids get deterministic neutral defaults."""

    preferences = _READER if profile_id == "resident_reader_v1" else ResidentPreferences()
    return ResidentProfile(profile_id=profile_id, preferences=preferences)


def permissions_for(authority: ResidentAuthority) -> frozenset[str]:
    permissions = {"request"}
    if authority in {"adult", "owner", "administrator"}:
        permissions.add("override")
    if authority in {"owner", "administrator"}:
        permissions.add("safety_control")
    return frozenset(permissions)


def goal_for_activity(activity: str) -> str:
    normalized = activity.strip().lower()
    if normalized == "reading":
        return "reading_comfort"
    if normalized in {"sleeping", "going_to_sleep"}:
        return "sleep_comfort"
    if normalized in {"away", "leaving"}:
        return "home_unoccupied"
    return "ambient_comfort"

