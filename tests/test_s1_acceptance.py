"""spec §15 三条 S1 验收条款的固化测试（门禁，防散落）。

这三条是 S1 的出厂门：
  1. commands are validated by the capability schema —— 四来源任一非法命令都被
     §10.2 结构化错误码拒绝，世界零变更；
  2. UI commands and scenario commands use the same executor —— 两条来源产生逐字段
     一致的生命周期序列，且世界只有 CommandExecutor 一个落点；
  3. lifecycle exposes success / failure / cancellation / superseded —— 四种终态各自
     可达且经 SIM_EVENT 事件流可见。

边界声明：ScenarioSpec（S2）尚不存在，第 2 条的 scenario 腿以「脚本式直接 submit
source='scenario'」代表真实场景来源；S2-T6 落地 YAML 场景后把真场景接进同一条测试。
"""

import queue
import threading

import anyio
import pytest
from fastapi.testclient import TestClient

from backend.engine.event_bus import SimEvent
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.execution.command import (
    LIFECYCLE_EVENT_TYPE,
    CommandSource,
    CommandStatus,
    DeviceCommand,
)
from backend.execution.executor import (
    ACTION_EVENT_TYPE,
    COMMAND_FAILED_EVENT_TYPE,
    FEEDBACK_EVENT_TYPE,
    CommandExecutor,
)
from backend.main import _init_default_state, app

ALL_SOURCES = (
    CommandSource.UI,
    CommandSource.AGENT,
    CommandSource.SCENARIO,
    CommandSource.RULE_FALLBACK,
)

# UI 腿与 scenario 腿使用同一控制点，两条链路才可逐字段对照。
SHARED_DEVICE_ID = "light_living_01"
SHARED_CAPABILITY = "power"
SHARED_VALUE = False


def _acceptance_world() -> WorldState:
    """含合法/只读/离线三类设备的最小世界，覆盖五种非法命令形态。"""

    world = WorldState()
    world.rooms = {"living_room": RoomState(id="living_room", light_level=300.0)}
    world.devices = {
        "light_living": DeviceState(
            id="light_living",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness", "color_temp"],
            state=DeviceStateValues(power=False, extra={"brightness": 40, "color_temp": 3000}),
        ),
        "hvac_living": DeviceState(
            id="hvac_living",
            type="hvac",
            location=Location3D(room="living_room"),
            capabilities=["power", "target_temp", "mode"],
            state=DeviceStateValues(power=True, extra={"target_temp": 24.0, "mode": "cool"}),
        ),
        "sensor_living": DeviceState(
            id="sensor_living",
            type="sensor",
            location=Location3D(room="living_room"),
            capabilities=["value"],
            state=DeviceStateValues(extra={"value": 25.0}),
        ),
        "light_offline": DeviceState(
            id="light_offline",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(power=False, extra={"brightness": 10, "online": False}),
        ),
    }
    return world


def _collector():
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    return events, publish


_WS_RECEIVE_TIMEOUT = 5.0


def _receive_json(ws, timeout: float = _WS_RECEIVE_TIMEOUT) -> dict:
    """带超时的 WS 收包（守门测试必须变红而不是挂死）。

    TestClient 的 ``receive_json`` 无限阻塞：一旦实现回归少发一条消息，整条测试套会永久
    挂起而不是失败。把阻塞收包丢到 daemon 线程并加超时，缺消息即 ``queue.Empty``。
    """

    box: queue.Queue = queue.Queue(maxsize=1)

    def _pump() -> None:
        try:
            box.put(("ok", ws.receive_json()))
        except BaseException as exc:  # noqa: BLE001 - 转交主线程原样抛出
            box.put(("error", exc))

    threading.Thread(target=_pump, daemon=True).start()
    kind, value = box.get(timeout=timeout)
    if kind == "error":
        raise value
    return value


def _receive_until(ws, predicate, max_messages: int = 24) -> list[dict]:
    """收包直到 predicate 命中或达到消息上限；返回全部已收消息。"""

    messages: list[dict] = []
    for _ in range(max_messages):
        message = _receive_json(ws)
        messages.append(message)
        if predicate(message):
            break
    return messages


def _sim_events_of_type(messages: list[dict], event_type: str) -> list[dict]:
    return [
        message["payload"]
        for message in messages
        if message["type"] == "SIM_EVENT"
        and message["payload"]["event_type"] == event_type
    ]


def _lifecycle_statuses(events) -> list[str]:
    return [e.data["to_status"] for e in events if e.event_type == LIFECYCLE_EVENT_TYPE]


def _lifecycle_events(events) -> list[SimEvent]:
    return [e for e in events if e.event_type == LIFECYCLE_EVENT_TYPE]


# ---------------------------------------------------------------------------
# §15 验收 1：commands are validated by the capability schema
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_commands_are_validated_by_capability_schema():
    """五类非法命令（类型/值域/枚举/只读/离线）× 四来源，全部以 §10.2 码拒绝且世界零变更。"""

    illegal_cases = [
        # (device_id, capability, value, 期望 §10.2 错误码)
        ("light_living", "brightness", "很亮", "invalid_value_type"),
        ("light_living", "brightness", 999, "invalid_value_range"),
        ("hvac_living", "mode", "turbo", "invalid_value_range"),
        ("sensor_living", "value", 18.0, "read_only_capability"),
        ("light_offline", "power", True, "device_offline"),
    ]

    for source in ALL_SOURCES:
        for device_id, capability, value, expected_code in illegal_cases:
            events, publish = _collector()
            sm = StateManager(_acceptance_world())
            before = sm.world.model_dump()
            ex = CommandExecutor(sm, publish)

            record = await ex.submit(
                DeviceCommand(
                    source=source,
                    device_id=device_id,
                    capability=capability,
                    value=value,
                )
            )

            assert record.status == CommandStatus.FAILED, (source, device_id, capability)
            assert record.failure_code == expected_code, (source, device_id, capability)
            # 失败经生命周期事件与 device.command_failed 双通道可见，码一致
            assert _lifecycle_statuses(events) == ["proposed", "approved", "failed"]
            failed_events = [e for e in events if e.event_type == COMMAND_FAILED_EVENT_TYPE]
            assert len(failed_events) == 1
            assert failed_events[0].data["error_code"] == expected_code
            assert failed_events[0].data["source"] == source.value
            # 校验失败绝不下发动作、绝不产生反馈、绝不改世界
            assert not any(e.event_type == ACTION_EVENT_TYPE for e in events)
            assert not any(e.event_type == FEEDBACK_EVENT_TYPE for e in events)
            assert sm.world.model_dump() == before


def test_illegal_ui_command_over_websocket_is_rejected_with_spec_code():
    """同一条校验在真实 WS 入口同样生效：非法 UI 命令回 §10.2 码 ERROR，连接不死。"""

    with TestClient(app) as client:
        with client.websocket_connect("/ws/simulation") as ws:
            _receive_json(ws)  # initial STATE_FULL
            ws.send_json(
                {
                    "type": "CMD_DEVICE_CONTROL",
                    "payload": {
                        "device_id": "light_living_01",
                        "action": "set_state",
                        "params": {"brightness": 999},
                    },
                }
            )
            messages = _receive_until(ws, lambda m: m["type"] == "ERROR")
            errors = [m for m in messages if m["type"] == "ERROR"]
            assert errors, "非法 UI 命令必须回 ERROR"
            assert errors[0]["payload"]["code"] == "invalid_value_range"
            assert errors[0]["payload"]["details"]["device_id"] == "light_living_01"


# ---------------------------------------------------------------------------
# §15 验收 2：UI commands and scenario commands use the same executor
# ---------------------------------------------------------------------------


def _lifecycle_projection(payloads: list[dict]) -> list[dict]:
    """把生命周期事件投影成可跨来源逐字段对照的形状（去掉 id/来源等天然差异字段）。"""

    return [
        {
            "device_id": payload["device_id"],
            "capability": payload["capability"],
            "from_status": payload["from_status"],
            "to_status": payload["to_status"],
        }
        for payload in payloads
    ]


def test_ui_and_scenario_commands_use_same_executor(monkeypatch):
    """UI（真实 WS）与 scenario（脚本式 submit）走同一台 executor：逐字段一致的生命周期 + 唯一落点。

    S2 边界：ScenarioSpec 未落地，scenario 腿以直接 submit(source='scenario') 代表；
    S2-T6 会把真 YAML 场景接进这条测试而不改断言形状。
    """

    # 唯一落点探针：apply_action 只允许在 CommandExecutor._run 内被调用。
    run_depth = {"n": 0}
    applied: list[str] = []
    orig_run = CommandExecutor._run
    orig_apply = StateManager.apply_action

    async def spy_run(self, command, **kwargs):
        run_depth["n"] += 1
        try:
            return await orig_run(self, command, **kwargs)
        finally:
            run_depth["n"] -= 1

    def spy_apply(self, *args, **kwargs):
        assert run_depth["n"] > 0, "世界变更必须来自 CommandExecutor（唯一落点）"
        applied.append(kwargs.get("device_id") or args[1])
        return orig_apply(self, *args, **kwargs)

    monkeypatch.setattr(CommandExecutor, "_run", spy_run)
    monkeypatch.setattr(StateManager, "apply_action", spy_apply)

    # —— UI 腿：真实 WS TestClient ——
    with TestClient(app) as client:
        with client.websocket_connect("/ws/simulation") as ws:
            _receive_json(ws)  # initial STATE_FULL
            applied.clear()
            ws.send_json(
                {
                    "type": "CMD_DEVICE_CONTROL",
                    "payload": {"device_id": SHARED_DEVICE_ID, "action": "turn_off"},
                }
            )
            messages = _receive_until(ws, lambda m: m["type"] == "STATE_DELTA")
            ui_lifecycle = [
                payload["data"]
                for payload in _sim_events_of_type(messages, LIFECYCLE_EVENT_TYPE)
            ]
    ui_applied = list(applied)

    # —— scenario 腿：脚本式直接 submit（S2 ScenarioSpec 的替身）——
    applied.clear()
    events, publish = _collector()
    sm = _init_default_state()
    executor = CommandExecutor(sm, publish)
    record = anyio.run(
        lambda: executor.submit(
            DeviceCommand(
                source=CommandSource.SCENARIO,
                device_id=SHARED_DEVICE_ID,
                capability=SHARED_CAPABILITY,
                value=SHARED_VALUE,
                reason="scenario script step",
            )
        )
    )
    scenario_applied = list(applied)
    scenario_lifecycle = [e.data for e in _lifecycle_events(events)]

    # 生命周期序列逐字段一致（仅 command_id / source 天然不同）
    assert record.status == CommandStatus.SUCCEEDED
    assert _lifecycle_projection(ui_lifecycle) == _lifecycle_projection(scenario_lifecycle)
    assert [p["to_status"] for p in ui_lifecycle] == [
        "proposed",
        "approved",
        "validated",
        "executing",
        "succeeded",
    ]
    # 两腿的 source 各自贯穿全部生命周期事件
    assert {p["source"] for p in ui_lifecycle} == {"ui"}
    assert {p["source"] for p in scenario_lifecycle} == {"scenario"}
    # 唯一落点：两腿都恰好经 executor 触达 apply_action 一次，同一设备
    assert ui_applied == [SHARED_DEVICE_ID]
    assert scenario_applied == [SHARED_DEVICE_ID]
    # 校验结果一致：两腿都通过六级校验、都无失败事件
    assert not any(e.event_type == COMMAND_FAILED_EVENT_TYPE for e in events)
    assert record.failure_code is None


# ---------------------------------------------------------------------------
# §15 验收 3：lifecycle exposes success / failure / cancellation / superseded
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_lifecycle_exposes_success_failure_cancellation_superseded():
    """四种终态各一实例：状态可达 + 经事件流可见 + 成功路径 root→action→feedback 顺序稳定。"""

    events, publish = _collector()
    sm = StateManager(_acceptance_world())
    ex = CommandExecutor(sm, publish)

    def cmd(**overrides) -> DeviceCommand:
        params = dict(
            source=CommandSource.UI,
            device_id="light_living",
            capability="power",
            value=True,
            correlation_id="corr-accept",
            causal_parent="root-accept",
        )
        params.update(overrides)
        return DeviceCommand(**params)

    # success
    success = await ex.submit(cmd(command_id="cmd-success"))
    # failure（越界值 → §10.2 invalid_value_range）
    failure = await ex.submit(
        cmd(command_id="cmd-failure", capability="brightness", value=999)
    )
    # superseded（同批同控制点，旧命令被最新命令取代）
    batch = await ex.submit_batch(
        [
            cmd(command_id="cmd-superseded", capability="brightness", value=20),
            cmd(command_id="cmd-winner", capability="brightness", value=60),
        ]
    )
    superseded = next(r for r in batch if r.command.command_id == "cmd-superseded")
    # cancellation（在飞命令被 reset 取消；同步 StateManager 下需显式挂起一条在飞命令，
    # S2 异步 episode 落地后这里换成真实在飞窗口）
    cancelled = await ex._propose(cmd(command_id="cmd-cancelled", capability="color_temp"), None)
    await ex.cancel_pending("scenario reset")

    assert success.status == CommandStatus.SUCCEEDED
    assert failure.status == CommandStatus.FAILED
    assert failure.failure_code == "invalid_value_range"
    assert superseded.status == CommandStatus.SUPERSEDED
    assert superseded.failure_code == "superseded_by_newer_command"
    assert cancelled.status == CommandStatus.CANCELLED

    # 四种终态经 command.lifecycle 事件流可见，且能定位到具体命令
    terminal_by_command = {
        e.data["command_id"]: e.data
        for e in _lifecycle_events(events)
        if e.data["to_status"]
        in {"succeeded", "failed", "superseded", "cancelled"}
    }
    assert {
        "cmd-success",
        "cmd-failure",
        "cmd-superseded",
        "cmd-cancelled",
    } <= set(terminal_by_command)
    assert terminal_by_command["cmd-success"]["to_status"] == "succeeded"
    assert terminal_by_command["cmd-failure"]["to_status"] == "failed"
    assert terminal_by_command["cmd-failure"]["failure_code"] == "invalid_value_range"
    assert terminal_by_command["cmd-superseded"]["to_status"] == "superseded"
    assert terminal_by_command["cmd-cancelled"]["to_status"] == "cancelled"
    # 每条终态事件都带足可观测性定位字段
    for data in terminal_by_command.values():
        assert data["device_id"] and data["capability"] and data["source"]
        assert data["from_status"] is not None

    # 成功命令的因果顺序恒定：executing → action.device_control → feedback.state_delta
    action_event = next(
        e
        for e in events
        if e.event_type == ACTION_EVENT_TYPE and e.data["command_id"] == "cmd-success"
    )
    action_index = events.index(action_event)
    executing_index = next(
        i
        for i, e in enumerate(events)
        if e.event_type == LIFECYCLE_EVENT_TYPE
        and e.data["command_id"] == "cmd-success"
        and e.data["to_status"] == "executing"
    )
    feedback_index = next(
        i
        for i, e in enumerate(events)
        if e.event_type == FEEDBACK_EVENT_TYPE
        and e.causal_parent == action_event.event_id
    )
    assert executing_index < action_index < feedback_index
    # 根事件先于派生事件：action 挂在命令根事件下，feedback 挂在 action 下
    assert action_event.causal_parent == "root-accept"
    assert action_event.correlation_id == "corr-accept"
