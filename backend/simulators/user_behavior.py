"""Schedule-based user behavior simulator.

Moves the simulated user between rooms based on a fixed daily schedule.
Emits ``user.activity_change`` events when the hour changes.
"""

from __future__ import annotations

import time as _time

from backend.engine.event_bus import WorldEvent
from backend.engine.state import WorldState


# ---------------------------------------------------------------------------
# Daily schedule: hour -> (room, activity)
# ---------------------------------------------------------------------------

SCHEDULE: dict[int, tuple[str, str]] = {
    7: ("bedroom", "waking_up"),
    8: ("kitchen", "breakfast"),
    9: ("living_room", "idle"),
    12: ("kitchen", "lunch"),
    14: ("living_room", "working"),
    18: ("kitchen", "dinner"),
    19: ("living_room", "watching_tv"),
    22: ("bedroom", "sleeping"),
}


def _parse_hour(time_of_day: str) -> int:
    """Return the integer hour from a 'HH:MM' string."""
    return int(time_of_day.split(":")[0])


def _current_schedule_entry(hour: int) -> tuple[str, str]:
    """Return (room, activity) for the given hour using the most recent
    schedule entry that is <= *hour*."""
    # Find the latest schedule hour <= current hour
    candidates = [h for h in SCHEDULE if h <= hour]
    if not candidates:
        # Before first schedule entry (e.g. 0:00-6:59) -> sleeping in bedroom
        return ("bedroom", "sleeping")
    return SCHEDULE[max(candidates)]


class UserBehaviorSimulator:
    """Moves users between rooms based on a daily schedule."""

    def __init__(self) -> None:
        self._last_hour: int | None = None

    def step(self, state: WorldState) -> list[WorldEvent]:
        """Check if the simulated hour changed and move users accordingly.

        Returns a (possibly empty) list of ``WorldEvent`` objects.
        """
        events: list[WorldEvent] = []
        hour = _parse_hour(state.environment.time_of_day)

        # Only act when the hour changes
        if hour == self._last_hour:
            return events

        self._last_hour = hour
        target_room, activity = _current_schedule_entry(hour)

        for user_id, user in state.users.items():
            old_room = user.location.room if user.location else ""
            if old_room == target_room and user.activity == activity:
                continue  # no change needed

            # Remove user from old room
            if old_room and old_room in state.rooms:
                room = state.rooms[old_room]
                if user_id in room.persons:
                    room.persons.remove(user_id)
                if not room.persons:
                    room.occupancy = False

            # Move user to new room
            if user.location:
                user.location.room = target_room
            else:
                from backend.engine.state import Location3D
                user.location = Location3D(room=target_room)
            user.activity = activity

            # Update new room occupancy
            if target_room in state.rooms:
                room = state.rooms[target_room]
                if user_id not in room.persons:
                    room.persons.append(user_id)
                room.occupancy = True

            events.append(
                WorldEvent(
                    event_type="user.activity_change",
                    source="user_behavior_sim",
                    timestamp=_time.time(),
                    data={
                        "user_id": user_id,
                        "from_room": old_room,
                        "to_room": target_room,
                        "activity": activity,
                    },
                )
            )

        return events
