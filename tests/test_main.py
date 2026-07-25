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


def test_ui_command_root_event_carries_seq_ahead_of_its_children(client):
    """修 S2 review：WS 上的根事件此前 seq=null，而它的子事件带 1..N。

    根事件在广播前先经 event_bus.stamp() 盖章，因此 WS 副本与 events.jsonl 副本同号，
    S5 的因果树按 seq 排序时根节点排在自己的子节点之前。
    """
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        messages = _receive_until_types(ws, {"STATE_DELTA"})
        sim_events = [
            message["payload"] for message in messages if message["type"] == "SIM_EVENT"
        ]
        root = next(event for event in sim_events if event["event_type"] == "user.command")

        assert root["seq"] is not None
        assert root["run_id"] is not None

        children = [
            event
            for event in sim_events
            if event["correlation_id"] == root["correlation_id"]
            and event["event_id"] != root["event_id"]
        ]
        assert children, "根事件必须有子事件（生命周期/动作/反馈）"
        assert all(child["seq"] is not None for child in children)
        assert all(child["seq"] > root["seq"] for child in children)


def test_ui_command_events_carry_no_generation_mode(client):
    """生成模式只描述"根事件怎么被生成的"，UI 命令与其派生事件都不带。

    与 docs/architecture/sim-event-schema.md §11.1 的表述对账：用户直发的命令不是
    平台生成的，agent/executor 的派生事件靠 causal_parent + source 表达来源。
    """
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        })
        messages = _receive_until_types(ws, {"STATE_DELTA"})
        sim_events = [
            message["payload"] for message in messages if message["type"] == "SIM_EVENT"
        ]
        root = next(event for event in sim_events if event["event_type"] == "user.command")
        family = [
            event
            for event in sim_events
            if event["correlation_id"] == root["correlation_id"]
        ]
        assert {event["event_generation_mode"] for event in family} == {None}


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



# ---------------------------------------------------------------------------
# S3-T4 review minor: CMD_SCENE_APPLY —— 场景切换的前门
#
# 被推翻的现状：SceneSelector.vue 在浏览器里循环发 2×N 条 CMD_DEVICE_CONTROL，
# 后端只看到 N 条互不相干的直控，拼不出"这是一次场景切换"这条因果链；SceneAgent 与
# scene_definitions.yaml 因此只有场景 YAML / 单测两个入口，产品里根本够不着。
# 现在一条 CMD_SCENE_APPLY = 一条 user.command 根事件 → 编排 → 仲裁 → CommandExecutor。
# ---------------------------------------------------------------------------


def test_ws_cmd_scene_apply_flows_through_arbitration_to_executor(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SCENE_APPLY", "payload": {"scene_id": "away"}})

        messages = _receive_until_event_types(
            ws,
            {"user.command", "reasoning.coordination_decision", "command.lifecycle"},
            max_messages=80,
        )
        events = [
            message["payload"] for message in messages if message["type"] == "SIM_EVENT"
        ]

        # 1) 根事件：一条消息 = 一次场景切换，带 scene_id，且先于任何子事件外发。
        roots = [
            event
            for event in events
            if event["event_type"] == "user.command"
            and event["data"].get("message_type") == "CMD_SCENE_APPLY"
        ]
        assert len(roots) == 1, "一条 CMD_SCENE_APPLY 只能开一条根事件"
        root = roots[0]
        assert root["data"]["scene_id"] == "away"
        assert root["seq"] is not None, "根事件必须先盖章再外发（S5 按 seq 排因果树）"
        assert events[0]["event_id"] == root["event_id"]

        # 2) 仲裁：SceneAgent 的提案真的进了同一台仲裁器（不是绕过去直接落地）。
        decisions = [
            event
            for event in events
            if event["event_type"] == "reasoning.coordination_decision"
            and event["correlation_id"] == root["correlation_id"]
        ]
        assert decisions, "场景切换必须留下一条 coordination_decision"
        scene_participation = [
            entry
            for decision in decisions
            for entry in decision["data"]["per_agent"]
            if entry["agent_id"] == "scene_agent"
        ]
        assert scene_participation, "SceneAgent 必须出现在仲裁的 per_agent 里"
        approved_by_scene = [
            item
            for decision in decisions
            for item in decision["data"]["approved_commands"]
            if item["agent_id"] == "scene_agent"
        ]
        assert approved_by_scene, "away 场景的命令必须有被批准的那几条"

        # 3) 执行：批准的命令走 CommandExecutor 的十态生命周期（同一条腿，无隐藏写入）。
        lifecycles = [
            event
            for event in events
            if event["event_type"] == "command.lifecycle"
            and event["correlation_id"] == root["correlation_id"]
        ]
        assert lifecycles, "场景命令必须经 CommandExecutor"
        assert {event["source"] for event in lifecycles} == {"command_executor"}


def test_ws_cmd_scene_apply_unknown_scene_returns_error_and_connection_survives(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SCENE_APPLY", "payload": {"scene_id": "ghost_scene"}})

        err = _receive_until_message_type(ws, "ERROR")
        assert err["payload"]["code"] == "unknown_scene"
        assert err["payload"]["details"]["scene_id"] == "ghost_scene"
        # 点名已知场景：前端拼错一个 id 时不必去翻后端 YAML。
        assert "away" in err["payload"]["details"]["known_scenes"]

        # 坏消息不杀连接
        ws.send_json({"type": "CMD_SIM_SPEED", "payload": {"speed": 1.25}})
        status = _receive_until_message_type(ws, "SIMULATION_STATUS")
        assert status["payload"]["speed"] == 1.25


def test_ws_cmd_scene_apply_missing_scene_id_returns_invalid_payload(client):
    with _connect(client) as ws:
        ws.receive_json()  # initial STATE_FULL
        ws.send_json({"type": "CMD_SCENE_APPLY", "payload": {}})
        err = _receive_until_message_type(ws, "ERROR")
        assert err["payload"]["code"] == "invalid_payload"
        assert err["payload"]["details"]["issues"]


# --------------------------------------------------------------- /api/health §11.1


# 会让 AgentRuntime._build_default_provider 挑到真 provider 的环境变量
# （开发机的 backend/.env.local 里就有它们）。"没有 LLM"这件事必须在测试里显式做到。
_PROVIDER_ENV_VARS = (
    "LLM_MODE",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_COMPAT_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)


def test_health_surfaces_the_resolved_llm_mode(client):
    """§11.1：健康检查必须回答"这台实例现在跑在哪种模式上"，而不只是 provider 名。

    只报 provider/model/configured 时，研究者（与 S5 面板）无法区分
    live / mocked / recorded / rule_based——而这正是模式系统存在的理由。
    """

    llm = client.get("/api/health").json()["llm"]

    assert set(llm) >= {"provider", "model", "configured", "mode", "benchmark_safe"}
    assert llm["mode"] in {"mocked", "recorded", "live", "rule_based"}
    # DECISION #7：只有非 live 的模式能拿去做 benchmark 声明，标志位必须与模式同源。
    assert llm["benchmark_safe"] is (llm["mode"] != "live")


def test_health_reports_rule_based_mode_when_no_llm_is_configured(monkeypatch):
    """拔掉全部 key 之后，模式必须是 rule_based——"没有 LLM"不是"罐头 LLM"。"""

    for name in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    with TestClient(app) as bare_client:
        llm = bare_client.get("/api/health").json()["llm"]

    assert llm["configured"] is False
    assert llm["provider"] == "disabled"
    assert llm["model"] == "rule_based"
    assert llm["mode"] == "rule_based"
    assert llm["benchmark_safe"] is True
