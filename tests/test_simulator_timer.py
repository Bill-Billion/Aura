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
