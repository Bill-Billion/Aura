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
        """纯事件模式下，step 返回行为变化事件，但不直接修改世界状态。"""
        sim = UserBehaviorSimulator()
        world = _make_world("06:00")

        # Advance through the day in half-hour steps
        expected_transitions = [
            ("06:30", "bedroom", "waking_up"),
            ("07:00", "bathroom", "getting_ready"),
            ("07:30", "kitchen", "breakfast"),
            ("08:30", "outside", "away"),
            ("12:30", "kitchen", "lunch"),
            ("13:30", "outside", "away"),
            ("18:30", "living_room", "arrive_home"),
            ("19:30", "kitchen", "cooking"),
            ("20:00", "living_room", "relaxing"),
            ("22:30", "bedroom", "sleeping"),
        ]

        for time_of_day, expected_room, expected_activity in expected_transitions:
            world.environment.time_of_day = time_of_day
            events = sim.step(world)

            user = world.users["user_01"]
            assert user.location.room == "bedroom"
            assert user.activity == "sleeping"
            if expected_room == "bedroom" and expected_activity == "sleeping":
                assert len(events) == 0
            else:
                assert len(events) > 0, f"Expected event at {time_of_day}"
                assert events[0].event_type == "user.activity_change"
                assert events[0].data["to_room"] == expected_room
                assert events[0].data["activity"] == expected_activity

            # Verify world state has not been mutated yet
            assert world.rooms["bedroom"].occupancy is False

    def test_no_event_when_hour_unchanged(self):
        """No events should fire when the hour stays the same."""
        sim = UserBehaviorSimulator()
        world = _make_world("09:00")

        events1 = sim.step(world)
        assert len(events1) > 0  # first call triggers

        events2 = sim.step(world)
        assert len(events2) == 0  # same hour, no change
