import asyncio

import pytest

from backend.engine.simulator_timer import SimulatorTimer


@pytest.mark.anyio
async def test_timer_start_pause_emits_tick_events():
    received = []

    async def publish(event):
        received.append(event)
        return event

    timer = SimulatorTimer(
        publish_event=publish,
        tick_interval=0.01,
        simulated_dt=60.0,
    )

    await timer.start()
    await asyncio.sleep(0.035)
    await timer.pause()

    assert timer.is_running is False
    assert len(received) >= 2
    assert all(event.event_type == "system.timer_tick" for event in received)
    assert received[0].data["tick"] == 1
    assert received[-1].data["tick"] == len(received)


@pytest.mark.anyio
async def test_timer_tick_payload_reflects_speed_and_reset():
    received = []

    async def publish(event):
        received.append(event)
        return event

    timer = SimulatorTimer(
        publish_event=publish,
        tick_interval=0.01,
        simulated_dt=60.0,
    )
    timer.speed = 2.5

    await timer.start()
    await asyncio.sleep(0.015)
    await timer.pause()

    assert received
    assert received[-1].data["simulation_speed"] == 2.5
    assert received[-1].data["simulated_dt"] == 60.0

    timer.reset()
    assert timer.current_tick == 0


@pytest.mark.anyio
async def test_duration_stop_request_drains_current_publish_without_next_tick():
    entered = asyncio.Event()
    release = asyncio.Event()
    received = []
    timer = None

    async def publish(event):
        received.append(event)
        assert timer is not None
        timer.request_stop_after_current_tick()
        entered.set()
        await release.wait()
        return event

    timer = SimulatorTimer(publish_event=publish, tick_interval=0.005)
    await timer.start()
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    # The current publish remains alive, but the armed stop blocks tick 2.
    await asyncio.sleep(0.03)
    assert timer.current_tick == 1
    assert [event.data["tick"] for event in received] == [1]

    release.set()
    await timer.pause()
    assert [event.data["tick"] for event in received] == [1]


def test_timer_defaults_to_slower_observe_mode():
    async def publish(event):
        return event

    timer = SimulatorTimer(publish_event=publish)

    assert timer.tick_interval == 2.0
    assert timer.simulated_dt == 10.0
    timer.set_mode("demo")
    assert timer.simulated_dt == 60.0
