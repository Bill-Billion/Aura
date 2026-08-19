"""Tests for WebSocket ConnectionManager."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from backend.api.ws import ConnectionManager
from backend.models.schemas import WSMessage


@pytest.fixture
def manager():
    return ConnectionManager()


def _make_ws():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.mark.anyio
async def test_connect_adds_to_active(manager):
    ws = _make_ws()
    await manager.connect(ws)
    assert ws in manager.active
    assert len(manager.active) == 1


@pytest.mark.anyio
async def test_connect_sends_full_state(manager):
    ws = _make_ws()
    state = {"scene_id": "test"}
    await manager.connect(ws, full_state=state)
    ws.send_json.assert_awaited_once()
    sent = ws.send_json.call_args[0][0]
    assert sent["type"] == "STATE_FULL"
    assert sent["payload"] == state


@pytest.mark.anyio
async def test_connect_no_state_no_send(manager):
    ws = _make_ws()
    await manager.connect(ws)
    ws.send_json.assert_not_awaited()


@pytest.mark.anyio
async def test_initial_batch_precedes_broadcasts_after_registration(manager):
    ws = _make_ws()
    first_send_started = asyncio.Event()
    release_first_send = asyncio.Event()
    delivered: list[str] = []

    async def send_json(message):
        delivered.append(message["type"])
        if message["type"] == "STATE_FULL":
            first_send_started.set()
            await release_first_send.wait()

    ws.send_json.side_effect = send_json

    def initial_messages():
        assert ws in manager.active
        return [
            WSMessage(type="STATE_FULL", payload={"tick": 1}),
            WSMessage(type="SIMULATION_STATUS", payload={"run_id": "run-1"}),
        ]

    initialize_task = asyncio.create_task(
        manager.initialize(ws, initial_messages_factory=initial_messages)
    )
    await first_send_started.wait()
    broadcast_task = asyncio.create_task(
        manager.broadcast(WSMessage(type="STATE_DELTA", payload={"tick": 2}))
    )
    await asyncio.sleep(0)

    assert delivered == ["STATE_FULL"]

    release_first_send.set()
    await asyncio.gather(initialize_task, broadcast_task)
    assert delivered == ["STATE_FULL", "SIMULATION_STATUS", "STATE_DELTA"]


def test_disconnect_removes(manager):
    ws = _make_ws()
    manager.active.append(ws)
    manager.disconnect(ws)
    assert ws not in manager.active


def test_disconnect_idempotent(manager):
    ws = _make_ws()
    manager.disconnect(ws)  # not in list, should not raise
    assert ws not in manager.active


@pytest.mark.anyio
async def test_send_succeeds(manager):
    ws = _make_ws()
    await manager.initialize(ws)
    msg = WSMessage(type="STATE_DELTA", payload={"deltas": []})
    assert await manager.send(ws, msg) is True
    ws.send_json.assert_awaited_once()


@pytest.mark.anyio
async def test_send_error_disconnects(manager):
    ws = _make_ws()
    await manager.initialize(ws)
    ws.send_json.side_effect = RuntimeError("boom")
    msg = WSMessage(type="STATE_DELTA", payload={"deltas": []})
    assert await manager.send(ws, msg) is False
    assert ws not in manager.active


@pytest.mark.anyio
async def test_broadcast_sends_to_all(manager):
    ws1, ws2 = _make_ws(), _make_ws()
    await manager.initialize(ws1)
    await manager.initialize(ws2)
    msg = WSMessage(type="SIMULATION_STATUS", payload={"is_running": True})
    await manager.broadcast(msg)
    ws1.send_json.assert_awaited_once()
    ws2.send_json.assert_awaited_once()


@pytest.mark.anyio
async def test_broadcast_removes_dead(manager):
    ws_ok = _make_ws()
    ws_dead = _make_ws()
    await manager.initialize(ws_ok)
    await manager.initialize(ws_dead)
    ws_dead.send_json.side_effect = RuntimeError("dead")
    msg = WSMessage(type="STATE_FULL", payload={})
    await manager.broadcast(msg)
    assert ws_ok in manager.active
    assert ws_dead not in manager.active


@pytest.mark.anyio
async def test_slow_socket_is_evicted_after_bounded_send_timeout(manager):
    ws = _make_ws()
    await manager.initialize(ws)
    never_returns = asyncio.Event()

    async def send_json(_message):
        await never_returns.wait()

    ws.send_json.side_effect = send_json
    manager.SEND_TIMEOUT_SECONDS = 0.01

    await asyncio.wait_for(
        manager.broadcast(WSMessage(type="STATE_DELTA", payload={})),
        timeout=0.2,
    )

    assert ws not in manager.active


@pytest.mark.anyio
async def test_send_failure_terminates_receive_loop_and_socket_cannot_revive(manager):
    ws = _make_ws()
    await manager.initialize(ws)
    receive_started = asyncio.Event()
    never_receives = asyncio.Event()
    never_sends = asyncio.Event()

    async def receive_json():
        receive_started.set()
        await never_receives.wait()

    async def send_json(_message):
        await never_sends.wait()

    ws.receive_json.side_effect = receive_json
    ws.send_json.side_effect = send_json
    manager.SEND_TIMEOUT_SECONDS = 0.01

    receive_task = asyncio.create_task(manager.receive_json(ws))
    await receive_started.wait()
    await manager.broadcast(WSMessage(type="STATE_DELTA", payload={}))

    with pytest.raises(WebSocketDisconnect) as disconnected:
        await asyncio.wait_for(receive_task, timeout=0.2)
    assert disconnected.value.code == 1011

    sends_after_failure = ws.send_json.await_count
    ws.send_json.side_effect = None
    assert await manager.send(ws, WSMessage(type="ERROR", payload={})) is False
    assert ws.send_json.await_count == sends_after_failure

    await manager.close(ws, code=1011)
    ws.close.assert_awaited_once_with(code=1011)


@pytest.mark.anyio
async def test_initial_snapshot_is_captured_under_global_lock_but_sent_outside_it(
    manager,
):
    ws = _make_ws()
    registration_lock = asyncio.Lock()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    def initial_messages():
        assert registration_lock.locked()
        return [WSMessage(type="STATE_FULL", payload={"tick": 1})]

    async def send_json(_message):
        assert not registration_lock.locked()
        send_started.set()
        await release_send.wait()

    ws.send_json.side_effect = send_json
    initialize_task = asyncio.create_task(
        manager.initialize(
            ws,
            initial_messages_factory=initial_messages,
            registration_lock=registration_lock,
        )
    )

    await send_started.wait()
    await asyncio.wait_for(registration_lock.acquire(), timeout=0.1)
    registration_lock.release()
    release_send.set()
    assert await initialize_task is True
