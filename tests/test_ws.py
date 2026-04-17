"""Tests for WebSocket ConnectionManager."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.api.ws import ConnectionManager
from backend.models.schemas import WSMessage


@pytest.fixture
def manager():
    return ConnectionManager()


def _make_ws():
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
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
    msg = WSMessage(type="STATE_DELTA", payload={"deltas": []})
    await manager.send(ws, msg)
    ws.send_json.assert_awaited_once()


@pytest.mark.anyio
async def test_send_error_disconnects(manager):
    ws = _make_ws()
    ws.send_json.side_effect = RuntimeError("boom")
    manager.active.append(ws)
    msg = WSMessage(type="STATE_DELTA", payload={"deltas": []})
    await manager.send(ws, msg)
    assert ws not in manager.active


@pytest.mark.anyio
async def test_broadcast_sends_to_all(manager):
    ws1, ws2 = _make_ws(), _make_ws()
    manager.active.extend([ws1, ws2])
    msg = WSMessage(type="SIMULATION_STATUS", payload={"is_running": True})
    await manager.broadcast(msg)
    ws1.send_json.assert_awaited_once()
    ws2.send_json.assert_awaited_once()


@pytest.mark.anyio
async def test_broadcast_removes_dead(manager):
    ws_ok = _make_ws()
    ws_dead = _make_ws()
    ws_dead.send_json.side_effect = RuntimeError("dead")
    manager.active.extend([ws_ok, ws_dead])
    msg = WSMessage(type="STATE_FULL", payload={})
    await manager.broadcast(msg)
    assert ws_ok in manager.active
    assert ws_dead not in manager.active
