"""Tests for main.py WebSocket handler and lifespan."""

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _connect(client):
    return client.websocket_connect("/ws/simulation")


def _receive_until_types(
    ws,
    expected: set[str],
    max_messages: int = 8,
    min_sim_events: int = 1,
) -> list[dict]:
    messages: list[dict] = []
    seen: set[str] = set()
    for _ in range(max_messages):
        data = ws.receive_json()
        messages.append(data)
        seen.add(data["type"])
        sim_event_count = sum(1 for message in messages if message["type"] == "SIM_EVENT")
        if expected.issubset(seen) and sim_event_count >= min_sim_events:
            break
    return messages


def test_ws_receives_full_state_on_connect(client):
    with _connect(client) as ws:
        data = ws.receive_json()
        assert data["type"] == "STATE_FULL"
        assert "rooms" in data["payload"]


def test_ws_cmd_sim_start(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_START"})
        data = ws.receive_json()
        assert data["type"] == "SIMULATION_STATUS"
        assert data["payload"]["is_running"] is True


def test_ws_cmd_sim_pause(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_START"})
        ws.receive_json()  # SIMULATION_STATUS
        ws.send_json({"type": "CMD_SIM_PAUSE"})
        # simulation_engine.pause() may broadcast multiple messages before SIMULATION_STATUS
        # (STATE_DELTA, AGENT_STATUS, etc.)
        data = ws.receive_json()
        while data["type"] != "SIMULATION_STATUS":
            data = ws.receive_json()
        assert data["type"] == "SIMULATION_STATUS"
        assert data["payload"]["is_running"] is False


def test_ws_cmd_sim_reset(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_RESET"})
        data = ws.receive_json()
        assert data["type"] == "STATE_FULL"
        assert "rooms" in data["payload"]


def test_ws_cmd_sim_speed(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_SPEED", "payload": {"speed": 2.0}})
        data = ws.receive_json()
        assert data["type"] == "SIMULATION_STATUS"
        assert data["payload"]["speed"] == 2.0


def test_ws_cmd_device_control_turn_on(client):
    # light_living_01 defaults to power=True, so turn_off first to create a real state change
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        _receive_until_types(ws, {"STATE_DELTA", "SIM_EVENT"}, min_sim_events=2)
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_on"},
        })
        messages = _receive_until_types(ws, {"STATE_DELTA"})
        delta_message = next(message for message in messages if message["type"] == "STATE_DELTA")
        assert len(delta_message["payload"]["deltas"]) > 0


def test_ws_cmd_device_control_turn_off(client):
    # light_living_01 defaults to power=True, so turn_off directly produces a delta
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        data = ws.receive_json()
        assert data["type"] == "STATE_DELTA"
        assert len(data["payload"]["deltas"]) > 0


def test_ws_cmd_device_control_set_state(client):
    # light_living_01 defaults to brightness=80, so use a different value
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {
                "device_id": "light_living_01",
                "action": "set_state",
                "params": {"brightness": 50},
            },
        })
        data = ws.receive_json()
        assert data["type"] == "STATE_DELTA"


def test_ws_cmd_device_control_legacy_property(client):
    # light_living_01 defaults to power=True, so set to False to produce a delta
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {
                "device_id": "light_living_01",
                "property": "power",
                "value": False,
            },
        })
        data = ws.receive_json()
        assert data["type"] == "STATE_DELTA"


def test_ws_cmd_device_control_no_delta(client):
    # Turn off first (produces delta), then turn off again (no delta, no broadcast)
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        ws.receive_json()  # STATE_DELTA (turn off)
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        # turning off an already-off device produces no delta, so no broadcast
        # the connection stays open, close it to end the test
        ws.close()


def test_ws_cmd_device_control_emits_structured_events(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })

        messages = _receive_until_types(
            ws,
            {"STATE_DELTA", "SIM_EVENT"},
            min_sim_events=2,
        )
        event_messages = [message for message in messages if message["type"] == "SIM_EVENT"]

        assert any(message["type"] == "STATE_DELTA" for message in messages)
        assert len(event_messages) >= 2
        assert event_messages[0]["payload"]["event_id"]
        assert event_messages[0]["payload"]["correlation_id"]
        assert event_messages[1]["payload"]["causal_parent"] == event_messages[0]["payload"]["event_id"]


def test_ws_unknown_message_type(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "UNKNOWN_TYPE", "payload": {}})
        ws.close()
