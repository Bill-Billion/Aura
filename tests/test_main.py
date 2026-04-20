"""Tests for main.py WebSocket handler and lifespan."""

import time

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


def _receive_until_event_types(
    ws,
    expected_event_types: set[str],
    max_messages: int = 24,
) -> list[dict]:
    messages: list[dict] = []
    seen: set[str] = set()
    for _ in range(max_messages):
        data = ws.receive_json()
        messages.append(data)
        if data["type"] == "SIM_EVENT":
            seen.add(data["payload"]["event_type"])
        if expected_event_types.issubset(seen):
            break
    return messages


def _receive_until_message_type(
    ws,
    expected_type: str,
    max_messages: int = 24,
) -> dict:
    last = {}
    for _ in range(max_messages):
        last = ws.receive_json()
        if last["type"] == expected_type:
            return last
    return last


def test_ws_receives_full_state_on_connect(client):
    with _connect(client) as ws:
        data = ws.receive_json()
        assert data["type"] == "STATE_FULL"
        assert "rooms" in data["payload"]


def test_ws_stays_open_without_client_messages_for_five_seconds(client):
    with _connect(client) as ws:
        data = ws.receive_json()
        assert data["type"] == "STATE_FULL"

        time.sleep(5)

        ws.send_json({"type": "UNKNOWN_TYPE", "payload": {}})
        ws.close()


def test_ws_full_state_exposes_registered_device_metadata(client):
    with _connect(client) as ws:
        data = ws.receive_json()
        assert data["type"] == "STATE_FULL"

        devices = data["payload"]["devices"]
        assert "fan_living_01" in devices
        assert "camera_entry_01" in devices
        assert "sensor_living_temp_01" in devices

        fan = devices["fan_living_01"]
        camera = devices["camera_entry_01"]
        sensor = devices["sensor_living_temp_01"]

        assert fan["type"] == "fan"
        assert fan["display_name"] == "客厅风扇"
        assert fan["floor_id"] == "F1"
        assert "speed" in fan["capabilities"]

        assert camera["type"] == "camera"
        assert camera["ui_group"] == "security"
        assert "view" in camera["capabilities"]

        assert sensor["type"] == "sensor"
        assert sensor["ui_group"] == "environment"
        assert "read" in sensor["capabilities"]


def test_ws_cmd_sim_start(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_START"})
        data = _receive_until_message_type(ws, "SIMULATION_STATUS")
        assert data["type"] == "SIMULATION_STATUS"
        assert data["payload"]["is_running"] is True


def test_ws_cmd_sim_start_streams_timer_and_environment_events(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_START"})
        _receive_until_message_type(ws, "SIMULATION_STATUS")

        messages = _receive_until_event_types(
            ws,
            {"system.timer_tick", "environment.state_refresh"},
        )

        sim_events = [message["payload"]["event_type"] for message in messages if message["type"] == "SIM_EVENT"]
        assert "system.timer_tick" in sim_events
        assert "environment.state_refresh" in sim_events


def test_ws_cmd_sim_pause(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_START"})
        _receive_until_message_type(ws, "SIMULATION_STATUS")
        ws.send_json({"type": "CMD_SIM_PAUSE"})
        data = _receive_until_message_type(ws, "SIMULATION_STATUS")
        assert data["type"] == "SIMULATION_STATUS"
        assert data["payload"]["is_running"] is False


def test_ws_cmd_sim_reset(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SIM_RESET"})
        data = _receive_until_message_type(ws, "STATE_FULL")
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


def test_ws_cmd_device_control_updates_fan_state(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {
                "device_id": "fan_living_01",
                "action": "set_state",
                "params": {"speed": "high", "shake": True},
            },
        })

        data = ws.receive_json()
        assert data["type"] == "STATE_DELTA"
        paths = {delta["path"] for delta in data["payload"]["deltas"]}
        assert "devices[fan_living_01].state.extra.speed" in paths
        assert "devices[fan_living_01].state.extra.shake" in paths


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


def test_ws_cmd_device_control_rejects_sensor_write(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {
                "device_id": "sensor_living_temp_01",
                "action": "set_state",
                "params": {"value": 18},
            },
        })

        data = ws.receive_json()
        assert data["type"] == "ERROR"
        assert data["payload"]["code"] == "DEVICE_NOT_CONTROLLABLE"
        assert data["payload"]["message"]
        assert isinstance(data["payload"]["details"], dict)
        assert data["payload"]["details"]["device_id"] == "sensor_living_temp_01"
