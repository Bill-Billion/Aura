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
    max_messages: int = 24,
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
        assert "value" in sensor["capabilities"]


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
        # 命令经 executor 后先发根事件与生命周期 SIM_EVENT，STATE_DELTA 在其后广播
        data = _receive_until_message_type(ws, "STATE_DELTA")
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
        data = _receive_until_message_type(ws, "STATE_DELTA")
        assert data["type"] == "STATE_DELTA"


def test_ws_light_control_updates_room_light_feedback_immediately(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_loft_01", "action": "turn_on"},
        })

        # 单条命令的所有 delta（设备功率 + 房间光照重算）聚合在同一条 STATE_DELTA 中
        data = _receive_until_message_type(ws, "STATE_DELTA")

        assert data["type"] == "STATE_DELTA"
        paths = {delta["path"] for delta in data["payload"]["deltas"]}
        assert "devices[light_loft_01].state.power" in paths
        assert "rooms[loft].light_level" in paths


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

        # set_state 多参数归一为多条 DeviceCommand，其 delta 聚合在同一 STATE_DELTA
        messages = _receive_until_types(ws, {"STATE_DELTA"})
        paths = {
            delta["path"]
            for message in messages
            if message["type"] == "STATE_DELTA"
            for delta in message["payload"]["deltas"]
        }
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
        data = _receive_until_message_type(ws, "STATE_DELTA")
        assert data["type"] == "STATE_DELTA"


def test_ws_cmd_device_control_no_delta(client):
    # Turn off first (produces delta), then turn off again (no delta, no broadcast)
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        _receive_until_message_type(ws, "STATE_DELTA")  # STATE_DELTA (turn off)
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        # turning off an already-off device produces no delta, so no STATE_DELTA broadcast
        # (生命周期事件仍会外发)；连接保持打开，关闭以结束测试
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

        # 经 executor 六级校验：sensor.value 是只读能力位 → §10.2 read_only_capability
        data = _receive_until_message_type(ws, "ERROR")
        assert data["type"] == "ERROR"
        assert data["payload"]["code"] == "read_only_capability"
        assert data["payload"]["message"]
        assert isinstance(data["payload"]["details"], dict)
        assert data["payload"]["details"]["device_id"] == "sensor_living_temp_01"


# ---------------------------------------------------------------------------
# S1-T5: UI 直控经 CommandExecutor（根事件先于 STATE_DELTA、完整生命周期、
# 坏消息回结构化 ERROR 不杀连接）
# ---------------------------------------------------------------------------


def test_ui_command_emits_root_event_before_state_delta(client):
    # 修审计§六⑤：根 user.command SIM_EVENT 必须先于 STATE_DELTA（旧实现顺序倒置）
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        messages = _receive_until_types(ws, {"STATE_DELTA"})
        types = [message["type"] for message in messages]
        state_delta_index = types.index("STATE_DELTA")
        root_index = next(
            i
            for i, message in enumerate(messages)
            if message["type"] == "SIM_EVENT"
            and message["payload"]["event_type"] == "user.command"
        )
        assert root_index < state_delta_index


def test_ui_command_full_lifecycle_visible_over_ws(client):
    # spec §15 验收：一条 UI 命令的完整生命周期 proposed→…→succeeded 经 SIM_EVENT 外发
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        messages = _receive_until_types(ws, {"STATE_DELTA"})
        statuses = [
            message["payload"]["data"]["to_status"]
            for message in messages
            if message["type"] == "SIM_EVENT"
            and message["payload"]["event_type"] == "command.lifecycle"
        ]
        for expected in ("proposed", "approved", "validated", "executing", "succeeded"):
            assert expected in statuses
        # 生命周期事件来源固定为 command_executor
        lifecycle_sources = {
            message["payload"]["source"]
            for message in messages
            if message["type"] == "SIM_EVENT"
            and message["payload"]["event_type"] == "command.lifecycle"
        }
        assert lifecycle_sources == {"command_executor"}


def test_invalid_ui_command_returns_error_with_spec_code_and_connection_survives(client):
    # 治『一条坏消息杀连接』：非法命令回 §10.2 码 ERROR 后，同一连接继续发合法命令仍成功
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "ghost_device", "action": "turn_on"},
        })
        err = _receive_until_message_type(ws, "ERROR")
        assert err["type"] == "ERROR"
        assert err["payload"]["code"] == "unknown_device"
        assert err["payload"]["details"]["device_id"] == "ghost_device"

        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        delta = _receive_until_message_type(ws, "STATE_DELTA")
        assert delta["type"] == "STATE_DELTA"
        assert len(delta["payload"]["deltas"]) > 0


def test_malformed_json_message_gets_error_not_disconnect(client):
    # 逐消息 try/except：非法 JSON 回结构化 ERROR，不落入外层 except 杀连接
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_text("this is not json {{{")
        err = _receive_until_message_type(ws, "ERROR")
        assert err["type"] == "ERROR"
        assert err["payload"]["code"]

        # 坏消息后连接仍在：合法命令继续成功
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        delta = _receive_until_message_type(ws, "STATE_DELTA")
        assert delta["type"] == "STATE_DELTA"


def test_structurally_invalid_device_payload_returns_invalid_payload(client):
    # CmdDeviceControlPayload 结构守卫：device_id 缺失是消息层问题，不是 §10.2 命令失败
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_DEVICE_CONTROL", "payload": {"action": "turn_on"}})
        err = _receive_until_message_type(ws, "ERROR")
        assert err["payload"]["code"] == "invalid_payload"
        assert err["payload"]["details"]["issues"]


def test_unsupported_message_type_returns_error_and_connection_survives(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_NOT_A_THING", "payload": {}})
        err = _receive_until_message_type(ws, "ERROR")
        assert err["payload"]["code"] == "unsupported_message_type"
        assert err["payload"]["details"]["type"] == "CMD_NOT_A_THING"

        ws.send_json({"type": "CMD_SIM_SPEED", "payload": {"speed": 2.0}})
        status = _receive_until_message_type(ws, "SIMULATION_STATUS")
        assert status["payload"]["speed"] == 2.0


def test_heartbeat_pong_is_accepted_without_error(client):
    # 前端心跳应答不是未知类型：收到后必须静默，否则前端会被自己的心跳刷屏
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "HEARTBEAT_PONG", "payload": {"timestamp": 1}})
        ws.send_json({"type": "CMD_SIM_SPEED", "payload": {"speed": 1.5}})
        data = ws.receive_json()
        assert data["type"] == "SIMULATION_STATUS"
        assert data["payload"]["speed"] == 1.5
