import pytest
from backend.engine.event_bus import EventBus, SimEvent, WorldEvent


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


@pytest.mark.anyio
async def test_publish_world_event_upgrades_to_sim_event():
    bus = EventBus()

    event = WorldEvent(
        event_type="device.changed",
        source="user",
        timestamp=12.0,
        data={"device_id": "light_living_01"},
    )

    await bus.publish(event)
    history = bus.get_history()

    assert len(history) == 1
    assert isinstance(history[0], SimEvent)
    assert history[0].event_id
    assert history[0].correlation_id
    assert history[0].priority == 1
    assert history[0].timestamp == 12.0


@pytest.mark.anyio
async def test_history_supports_correlation_and_priority_filters():
    bus = EventBus()

    low_priority = SimEvent(
        event_type="feedback",
        source="lighting_agent",
        timestamp=10.0,
        wall_time=10.0,
        correlation_id="corr-a",
        priority=0,
        data={"path": "devices[light_living_01].state.power"},
    )
    high_priority = SimEvent(
        event_type="action",
        source="hvac_agent",
        timestamp=11.0,
        wall_time=11.0,
        correlation_id="corr-a",
        priority=3,
        data={"device_id": "ac_living_01"},
    )
    unrelated = SimEvent(
        event_type="user",
        source="user_sim",
        timestamp=12.0,
        wall_time=12.0,
        correlation_id="corr-b",
        priority=1,
        data={"activity": "breakfast"},
    )

    await bus.publish(low_priority)
    await bus.publish(high_priority)
    await bus.publish(unrelated)

    correlation_history = bus.get_history(correlation_id="corr-a")
    assert [event.event_type for event in correlation_history] == ["feedback", "action"]

    urgent_events = bus.get_history(min_priority=2)
    assert [event.event_type for event in urgent_events] == ["action"]


@pytest.mark.anyio
async def test_get_causal_chain_returns_root_first():
    bus = EventBus()

    root = SimEvent(
        event_id="root-event",
        event_type="user",
        source="user_sim",
        timestamp=100.0,
        wall_time=100.0,
        correlation_id="corr-root",
        priority=1,
        data={"activity": "arrive_home"},
    )
    child = SimEvent(
        event_id="child-event",
        event_type="action",
        source="lighting_agent",
        timestamp=101.0,
        wall_time=101.0,
        correlation_id="corr-root",
        causal_parent="root-event",
        priority=2,
        data={"device_id": "light_living_01"},
    )
    grandchild = SimEvent(
        event_id="grandchild-event",
        event_type="feedback",
        source="state_manager",
        timestamp=102.0,
        wall_time=102.0,
        correlation_id="corr-root",
        causal_parent="child-event",
        priority=1,
        data={"path": "devices[light_living_01].state.power"},
    )

    await bus.publish(child)
    await bus.publish(grandchild)
    await bus.publish(root)

    chain = bus.get_causal_chain("root-event")

    assert [event.event_id for event in chain] == [
        "root-event",
        "child-event",
        "grandchild-event",
    ]
