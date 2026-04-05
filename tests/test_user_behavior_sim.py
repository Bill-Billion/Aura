"""Tests for UserBehaviorSimulator."""

from backend.engine.state import (
    EnvironmentState,
    Location3D,
    RoomState,
    UserState,
    WorldState,
)
from backend.simulators.user_behavior import UserBehaviorSimulator


def _make_world(time_of_day: str = "06:00") -> WorldState:
    world = WorldState(environment=EnvironmentState(time_of_day=time_of_day))
    world.rooms = {
        "living_room": RoomState(id="living_room"),
        "bedroom": RoomState(id="bedroom"),
        "kitchen": RoomState(id="kitchen"),
        "bathroom": RoomState(id="bathroom"),
    }
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="bedroom"),
            activity="sleeping",
        ),
    }
    return world


class TestUserBehavior:
    def test_user_moves_on_schedule(self):
        """Simulate a full day and check user transitions."""
        sim = UserBehaviorSimulator()
        world = _make_world("06:00")

        # Advance through the day hour by hour
        expected_transitions = [
            (7, "bedroom", "waking_up"),
            (8, "kitchen", "breakfast"),
            (9, "living_room", "idle"),
            (12, "kitchen", "lunch"),
            (14, "living_room", "working"),
            (18, "kitchen", "dinner"),
            (19, "living_room", "watching_tv"),
            (22, "bedroom", "sleeping"),
        ]

        for hour, expected_room, expected_activity in expected_transitions:
            world.environment.time_of_day = f"{hour:02d}:00"
            events = sim.step(world)

            user = world.users["user_01"]
            assert user.location.room == expected_room, (
                f"At {hour}:00 expected room={expected_room}, got {user.location.room}"
            )
            assert user.activity == expected_activity, (
                f"At {hour}:00 expected activity={expected_activity}, got {user.activity}"
            )
            assert len(events) > 0, f"Expected event at {hour}:00"
            assert events[0].event_type == "user.activity_change"

            # Verify room occupancy
            assert world.rooms[expected_room].occupancy is True
            assert "user_01" in world.rooms[expected_room].persons

    def test_no_event_when_hour_unchanged(self):
        """No events should fire when the hour stays the same."""
        sim = UserBehaviorSimulator()
        world = _make_world("09:00")

        events1 = sim.step(world)
        assert len(events1) > 0  # first call triggers

        events2 = sim.step(world)
        assert len(events2) == 0  # same hour, no change
