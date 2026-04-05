import asyncio
import pytest
from backend.engine.event_bus import EventBus, WorldEvent


@pytest.mark.anyio
async def test_publish_notifies_subscriber():
    bus = EventBus()
    received: list[WorldEvent] = []

    async def handler(event: WorldEvent):
        received.append(event)

    bus.subscribe("device.changed", handler)

    event = WorldEvent(
        event_type="device.changed",
        source="agent-1",
        timestamp=100.0,
        data={"device_id": "light-001"},
    )
    count = await bus.publish(event)

    assert count == 1
    assert len(received) == 1
    assert received[0].source == "agent-1"
    assert received[0].data["device_id"] == "light-001"


@pytest.mark.anyio
async def test_wildcard():
    bus = EventBus()
    wildcard_events: list[WorldEvent] = []

    async def wildcard_handler(event: WorldEvent):
        wildcard_events.append(event)

    bus.subscribe("*", wildcard_handler)

    e1 = WorldEvent(event_type="device.on", source="a", timestamp=1.0, data={})
    e2 = WorldEvent(event_type="room.temp", source="b", timestamp=2.0, data={})

    c1 = await bus.publish(e1)
    c2 = await bus.publish(e2)

    assert c1 == 1
    assert c2 == 1
    assert len(wildcard_events) == 2
    assert wildcard_events[0].event_type == "device.on"
    assert wildcard_events[1].event_type == "room.temp"


@pytest.mark.anyio
async def test_unsubscribe():
    bus = EventBus()
    received: list[WorldEvent] = []

    async def handler(event: WorldEvent):
        received.append(event)

    bus.subscribe("test.event", handler)
    bus.unsubscribe("test.event", handler)

    event = WorldEvent(event_type="test.event", source="x", timestamp=1.0, data={})
    count = await bus.publish(event)

    assert count == 0
    assert len(received) == 0


@pytest.mark.anyio
async def test_history():
    bus = EventBus()

    events = [
        WorldEvent(event_type="device.on", source="a", timestamp=10.0, data={}),
        WorldEvent(event_type="device.off", source="a", timestamp=20.0, data={}),
        WorldEvent(event_type="device.on", source="b", timestamp=30.0, data={}),
    ]
    for e in events:
        await bus.publish(e)

    # All history
    all_hist = bus.get_history()
    assert len(all_hist) == 3

    # Filtered by type
    on_hist = bus.get_history(event_type="device.on")
    assert len(on_hist) == 2

    # Filtered by timestamp
    recent = bus.get_history(since=25.0)
    assert len(recent) == 1
    assert recent[0].timestamp == 30.0

    # Filtered by both
    combined = bus.get_history(event_type="device.on", since=15.0)
    assert len(combined) == 1
