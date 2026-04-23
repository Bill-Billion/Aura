"""Schedule-based user behavior simulator."""

from __future__ import annotations

import time as _time
from dataclasses import dataclass

from backend.engine.event_bus import WorldEvent
from backend.engine.state import WorldState


@dataclass(frozen=True)
class ScheduleEntry:
    minute_of_day: int
    room: str
    activity: str


SCHEDULE: tuple[ScheduleEntry, ...] = (
    ScheduleEntry(0, "bedroom", "sleeping"),
    ScheduleEntry(390, "bedroom", "waking_up"),
    ScheduleEntry(420, "bathroom", "getting_ready"),
    ScheduleEntry(450, "kitchen", "breakfast"),
    ScheduleEntry(510, "outside", "away"),
    ScheduleEntry(750, "kitchen", "lunch"),
    ScheduleEntry(810, "outside", "away"),
    ScheduleEntry(1110, "living_room", "arrive_home"),
    ScheduleEntry(1170, "kitchen", "cooking"),
    ScheduleEntry(1200, "living_room", "relaxing"),
    ScheduleEntry(1350, "bedroom", "sleeping"),
)


def _parse_minutes(time_of_day: str) -> int:
    hour, minute = time_of_day.split(":")
    return int(hour) * 60 + int(minute)


def _half_hour_slot(minute_of_day: int) -> int:
    return minute_of_day // 30


def _current_schedule_entry(minute_of_day: int) -> ScheduleEntry:
    candidates = [entry for entry in SCHEDULE if entry.minute_of_day <= minute_of_day]
    if not candidates:
        return SCHEDULE[0]
    return candidates[-1]


class UserBehaviorSimulator:
    """按半小时节拍产出用户活动事件。"""

    def __init__(self) -> None:
        self._last_slot: int | None = None

    def step(self, state: WorldState) -> list[WorldEvent]:
        events: list[WorldEvent] = []
        minute_of_day = _parse_minutes(state.environment.time_of_day)
        slot = _half_hour_slot(minute_of_day)

        if slot == self._last_slot:
            return events

        self._last_slot = slot
        target = _current_schedule_entry(minute_of_day)

        # 设计意图：用户活动脚本只负责“宣布变化”，真正的房间占用与用户位置写回由 SimulationEngine 统一处理。
        for user_id, user in state.users.items():
            old_room = user.location.room if user.location else ""
            if old_room == target.room and user.activity == target.activity:
                continue

            events.append(
                WorldEvent(
                    event_type="user.activity_change",
                    source="user_behavior_sim",
                    timestamp=_time.time(),
                    data={
                        "user_id": user_id,
                        "from_room": old_room,
                        "to_room": target.room,
                        "activity": target.activity,
                    },
                )
            )

        return events
