"""S1-T4 CommandExecutor：四来源唯一入口（spec §10 执行序列 5–8 步）。

executor 是所有设备变更的唯一路径：§3.3 六级校验（复用 S1-T2 validate_command）→
§10.1 十态生命周期（每次迁移发 command.lifecycle）→ 唯一调用 state_manager.apply_action →
房间光照统一 effect 钩子 → 逐 delta 发 feedback.state_delta。这里只测 executor 自身，
不改 main.py / runtime.py（那是 S1-T5）。
"""

import pytest

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
    LIFECYCLE_EVENT_SOURCE,
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
    PreSubmitDecision,
)
from backend.simulators.environment import calculate_room_light_level


def _make_world() -> WorldState:
    world = WorldState()
    world.rooms = {"living_room": RoomState(id="living_room", light_level=300.0)}
    world.devices = {
        "light_living": DeviceState(
            id="light_living",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness", "color_temp"],
            state=DeviceStateValues(power=False, extra={"brightness": 80, "color_temp": 3000}),
        ),
        "curtain_living": DeviceState(
            id="curtain_living",
            type="curtain",
            location=Location3D(room="living_room"),
            capabilities=["open_percent"],
            state=DeviceStateValues(extra={"open_percent": 50}),
        ),
        "camera_living": DeviceState(
            id="camera_living",
            type="camera",
            location=Location3D(room="living_room"),
            capabilities=["view"],
            state=DeviceStateValues(extra={"online": True}),
        ),
    }
    return world


def _collector():
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    return events, publish


def _cmd(**overrides) -> DeviceCommand:
    params = dict(
        source=CommandSource.UI,
        device_id="light_living",
        capability="power",
        value=True,
        correlation_id="corr-1",
        causal_parent="root-1",
        issued_tick=7,
    )
    params.update(overrides)
    return DeviceCommand(**params)


def _lifecycle_to_status(events) -> list[str]:
    return [e.data["to_status"] for e in events if e.event_type == LIFECYCLE_EVENT_TYPE]


@pytest.mark.anyio
async def test_success_path_event_order_action_before_feedback():
    """§15 ordering：一条成功命令的事件流严格 root → action → feedback。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    rec = await ex.submit(_cmd())

    assert rec.status == CommandStatus.SUCCEEDED
    assert rec.is_terminal
    # 生命周期五态齐全
    assert _lifecycle_to_status(events) == [
        "proposed",
        "approved",
        "validated",
        "executing",
        "succeeded",
    ]

    types = [e.event_type for e in events]
    executing_i = next(
        i
        for i, e in enumerate(events)
        if e.event_type == LIFECYCLE_EVENT_TYPE and e.data["to_status"] == "executing"
    )
    action_i = types.index(ACTION_EVENT_TYPE)
    feedback_i = types.index(FEEDBACK_EVENT_TYPE)
    # 执行态 → action → feedback，顺序恒定
    assert executing_i < action_i < feedback_i

    action_ev = events[action_i]
    feedback_ev = events[feedback_i]
    # 因果链：action 挂在命令根事件下，feedback 挂在 action 下
    assert action_ev.causal_parent == "root-1"
    assert action_ev.correlation_id == "corr-1"
    assert feedback_ev.causal_parent == action_ev.event_id

    # executor 是唯一变更方，世界真的被改
    assert sm.world.devices["light_living"].state.power is True


@pytest.mark.anyio
async def test_invalid_command_emits_failed_and_command_failed_and_zero_deltas():
    """非法命令：approved→failed + 派生 device.command_failed + 世界零变更、零反馈。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    before = sm.world.devices["light_living"].state.extra["brightness"]
    rec = await ex.submit(_cmd(capability="brightness", value=999))  # 越界

    assert rec.status == CommandStatus.FAILED
    assert rec.failure_code == "invalid_value_range"
    assert _lifecycle_to_status(events) == ["proposed", "approved", "failed"]

    failed_ev = next(e for e in events if e.event_type == COMMAND_FAILED_EVENT_TYPE)
    assert failed_ev.data["error_code"] == "invalid_value_range"
    assert failed_ev.data["device_id"] == "light_living"

    # 校验失败绝不下发 action、绝不动世界
    assert not any(e.event_type == ACTION_EVENT_TYPE for e in events)
    assert not any(e.event_type == FEEDBACK_EVENT_TYPE for e in events)
    assert sm.world.devices["light_living"].state.extra["brightness"] == before


@pytest.mark.anyio
async def test_newer_command_supersedes_pending_same_device_capability():
    """同批更新命令取代旧命令：旧命令 proposed→superseded 且不落地。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    first = _cmd(capability="brightness", value=30, command_id="cmd-a")
    second = _cmd(capability="brightness", value=70, command_id="cmd-b")
    records = await ex.submit_batch([first, second])

    by_id = {r.command.command_id: r for r in records}
    assert by_id["cmd-a"].status == CommandStatus.SUPERSEDED
    assert by_id["cmd-a"].failure_code == "superseded_by_newer_command"
    assert by_id["cmd-b"].status == CommandStatus.SUCCEEDED
    # 只有胜出的命令落地
    assert sm.world.devices["light_living"].state.extra["brightness"] == 70

    superseded = [
        e
        for e in events
        if e.event_type == LIFECYCLE_EVENT_TYPE and e.data["to_status"] == "superseded"
    ]
    assert len(superseded) == 1
    assert superseded[0].data["command_id"] == "cmd-a"
    assert superseded[0].data["failure_code"] == "superseded_by_newer_command"


@pytest.mark.anyio
async def test_all_four_sources_traverse_identical_pipeline(monkeypatch):
    """ui/agent/scenario/rule_fallback 走同一 validate + apply 调用链、同一生命周期序列。"""
    import backend.execution.executor as ex_mod

    validate_count = {"n": 0}
    orig_validate = ex_mod.validate_command

    def spy_validate(*args, **kwargs):
        validate_count["n"] += 1
        return orig_validate(*args, **kwargs)

    monkeypatch.setattr(ex_mod, "validate_command", spy_validate)

    apply_order: list[str] = []
    sequences: dict[str, list[str]] = {}

    for source in (
        CommandSource.UI,
        CommandSource.AGENT,
        CommandSource.SCENARIO,
        CommandSource.RULE_FALLBACK,
    ):
        events, publish = _collector()
        sm = StateManager(_make_world())
        orig_apply = sm.apply_action

        def spy_apply(*args, _orig=orig_apply, _src=source.value, **kwargs):
            apply_order.append(_src)
            return _orig(*args, **kwargs)

        monkeypatch.setattr(sm, "apply_action", spy_apply)
        ex = CommandExecutor(sm, publish)

        rec = await ex.submit(_cmd(source=source, value=True))
        assert rec.status == CommandStatus.SUCCEEDED
        sequences[source.value] = _lifecycle_to_status(events)
        # source 贯穿生命周期与 action 事件
        for e in events:
            if e.event_type in (LIFECYCLE_EVENT_TYPE, ACTION_EVENT_TYPE):
                assert e.data["source"] == source.value

    seqs = list(sequences.values())
    assert all(s == seqs[0] for s in seqs)
    assert seqs[0] == ["proposed", "approved", "validated", "executing", "succeeded"]
    # 四来源各恰好一次校验 + 一次 apply
    assert validate_count["n"] == 4
    assert apply_order == ["ui", "agent", "scenario", "rule_fallback"]


@pytest.mark.anyio
async def test_light_and_curtain_commands_recompute_room_light_level_once():
    """灯与窗帘命令各触发恰好一次房间光照重算（两处重复逻辑已收拢到 executor）。"""
    # 灯功率命令
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)
    await ex.submit(_cmd(capability="power", value=True))

    light_recompute = [
        e
        for e in events
        if e.event_type == FEEDBACK_EVENT_TYPE
        and e.data["path"] == "rooms[living_room].light_level"
    ]
    assert len(light_recompute) == 1
    assert sm.world.rooms["living_room"].light_level == calculate_room_light_level(
        sm.world, "living_room"
    )

    # 窗帘命令
    events2, publish2 = _collector()
    sm2 = StateManager(_make_world())
    ex2 = CommandExecutor(sm2, publish2)
    await ex2.submit(_cmd(device_id="curtain_living", capability="open_percent", value=100))

    light_recompute2 = [
        e
        for e in events2
        if e.event_type == FEEDBACK_EVENT_TYPE
        and e.data["path"] == "rooms[living_room].light_level"
    ]
    assert len(light_recompute2) == 1


@pytest.mark.anyio
async def test_feedback_timeout_with_injected_clock_yields_timed_out():
    """注入慢时钟：动作已下发但反馈超出预算窗口 → executing→timed_out（execution_timeout）。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    stamps = iter([0.0, 10.0, 20.0, 30.0])
    ex = CommandExecutor(
        sm, publish, clock=lambda: next(stamps), feedback_timeout=1.0
    )

    rec = await ex.submit(_cmd(capability="power", value=True))

    assert rec.status == CommandStatus.TIMED_OUT
    assert rec.failure_code == "execution_timeout"
    assert _lifecycle_to_status(events) == [
        "proposed",
        "approved",
        "validated",
        "executing",
        "timed_out",
    ]
    failed_ev = next(e for e in events if e.event_type == COMMAND_FAILED_EVENT_TYPE)
    assert failed_ev.data["error_code"] == "execution_timeout"
    # §10.2「失败命令不得改动世界」：超时同不变式违规一样回滚，并把 reverted_paths 留痕。
    assert failed_ev.data["reverted_paths"] == [
        "devices[light_living].state.power",
        "devices[light_living].state.last_changed_by",
    ]
    # 反馈未在窗口内到达，不发 feedback 事件
    assert not any(e.event_type == FEEDBACK_EVENT_TYPE for e in events)
    # 世界与事件流一致：既然没有任何 feedback 外发，世界也必须回到命令之前
    assert sm.world.devices["light_living"].state.power is False
    assert sm.world.devices["light_living"].state.last_changed_by == "system"
    assert sm.world.rooms["living_room"].light_level == 300.0


@pytest.mark.anyio
async def test_agent_sourced_command_keeps_actor_identity_end_to_end():
    """finding-1 回归闸：agent 命令的执行者身份（agent_id）必须贯穿事件、delta 与设备。

    可观测性产品的卖点就是「哪个 agent 干的」；退化成四值 source 枚举（'agent'）即产品回归。
    """
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    rec = await ex.submit(
        _cmd(
            source=CommandSource.AGENT,
            actor="comfort_agent",
            actor_name="Comfort Agent",
            capability="power",
            value=True,
        )
    )
    assert rec.status == CommandStatus.SUCCEEDED

    action_ev = next(e for e in events if e.event_type == ACTION_EVENT_TYPE)
    # 前端 extractEventAgentId 依赖 source/agent_id，observability 面板依赖 agent_name
    assert action_ev.source == "comfort_agent"
    assert action_ev.data["agent_id"] == "comfort_agent"
    assert action_ev.data["agent_name"] == "Comfort Agent"
    # 四值来源不丢：source 字段仍是审计口径的 'agent'
    assert action_ev.data["source"] == "agent"

    feedbacks = [e for e in events if e.event_type == FEEDBACK_EVENT_TYPE]
    assert feedbacks
    # 直接 delta 与 effect 派生 delta（房间光照）都归因到 agent 本人
    assert {e.data["caused_by"] for e in feedbacks} == {"comfort_agent"}
    assert sm.world.devices["light_living"].state.last_changed_by == "comfort_agent"

    lifecycle = [e for e in events if e.event_type == LIFECYCLE_EVENT_TYPE]
    assert all(e.data["agent_id"] == "comfort_agent" for e in lifecycle)


@pytest.mark.anyio
async def test_ui_command_without_actor_attributes_to_source():
    """无 actor 的 UI 命令：归因回落到四值来源，且不伪造 agent 身份字段。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    rec = await ex.submit(_cmd(source=CommandSource.UI, capability="power", value=True))
    assert rec.status == CommandStatus.SUCCEEDED

    action_ev = next(e for e in events if e.event_type == ACTION_EVENT_TYPE)
    assert action_ev.source == LIFECYCLE_EVENT_SOURCE
    assert action_ev.data["source"] == "ui"
    assert "agent_id" not in action_ev.data
    assert "agent_name" not in action_ev.data

    feedbacks = [e for e in events if e.event_type == FEEDBACK_EVENT_TYPE]
    assert {e.data["caused_by"] for e in feedbacks} == {"ui"}
    assert sm.world.devices["light_living"].state.last_changed_by == "ui"


@pytest.mark.anyio
async def test_pre_submit_hook_is_invoked_between_validation_and_executing_noop():
    """接缝（S1→S3）：pre_submit 在校验之后、执行之前被调用；S1 no-op 直通、命令照常成功。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    seen: list[str] = []

    def hook(command: DeviceCommand):
        seen.append(command.command_id)
        # 调用点：已 validated，但尚未 executing、尚无 action 事件
        assert any(
            e.event_type == LIFECYCLE_EVENT_TYPE and e.data["to_status"] == "validated"
            for e in events
        )
        assert not any(e.event_type == ACTION_EVENT_TYPE for e in events)
        return None  # no-op passthrough

    rec = await ex.submit(_cmd(command_id="cmd-hook"), pre_submit=hook)

    assert seen == ["cmd-hook"]
    assert rec.status == CommandStatus.SUCCEEDED
    assert _lifecycle_to_status(events) == [
        "proposed",
        "approved",
        "validated",
        "executing",
        "succeeded",
    ]


@pytest.mark.anyio
async def test_pre_submit_decision_supersede_list_marks_other_pending_superseded():
    """接缝续：pre_submit 返回 supersede-list 时取代在飞的其他命令（S3 装仲裁逻辑于此）。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    # 白盒留一条在飞的旧命令（proposed，未执行）
    victim = await ex._propose(_cmd(capability="brightness", value=20, command_id="old"), None)
    assert ex.pending

    def hook(command: DeviceCommand):
        return PreSubmitDecision(superseded_targets=(("light_living", "brightness"),))

    rec = await ex.submit(
        _cmd(capability="color_temp", value=4000, command_id="new"), pre_submit=hook
    )

    assert rec.status == CommandStatus.SUCCEEDED
    assert victim.status == CommandStatus.SUPERSEDED
    assert victim.failure_code == "superseded_by_newer_command"


@pytest.mark.anyio
async def test_submit_source_override_is_threaded_into_events():
    """接缝（S2）：submit(source=...) 覆盖命令来源并贯穿全部事件，供 executor.submit(source='scenario')。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    rec = await ex.submit(_cmd(source=CommandSource.UI), source="scenario")

    assert rec.command.source == CommandSource.SCENARIO
    for e in events:
        if e.event_type in (LIFECYCLE_EVENT_TYPE, ACTION_EVENT_TYPE, FEEDBACK_EVENT_TYPE):
            assert e.data["source"] == "scenario"


@pytest.mark.anyio
async def test_device_vanishing_after_validation_fails_not_silently_swallowed():
    """校验通过后设备并发消失（reset）：executing→failed + command_failed，绝不静默吞（根治缺陷①）。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    orig_apply = sm.apply_action

    def vanishing_apply(*args, **kwargs):
        # 模拟 apply 前设备被移除
        sm.world.devices.pop("light_living", None)
        return orig_apply(*args, **kwargs)

    sm.apply_action = vanishing_apply  # type: ignore[method-assign]

    rec = await ex.submit(_cmd(capability="power", value=True))

    assert rec.status == CommandStatus.FAILED
    assert rec.failure_code == "unknown_device"
    assert any(
        e.event_type == COMMAND_FAILED_EVENT_TYPE
        and e.data["error_code"] == "unknown_device"
        for e in events
    )


@pytest.mark.anyio
async def test_cancel_pending_cancels_inflight_and_clears_after_normal_submit():
    """cancel_pending（S2 reset 取消在飞用）：正常提交后注册表清空；显式在飞命令被迁到 cancelled。"""
    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    await ex.submit(_cmd(value=True))
    assert ex.pending == {}
    assert await ex.cancel_pending("nothing pending") == []

    rec = await ex._propose(_cmd(command_id="cmd-pending"), None)
    assert ex.pending
    cancelled = await ex.cancel_pending("reset scenario")
    assert [r.command.command_id for r in cancelled] == ["cmd-pending"]
    assert rec.status == CommandStatus.CANCELLED
    assert ex.pending == {}


@pytest.mark.anyio
async def test_bind_state_manager_cancels_pending_with_lifecycle_events():
    """S1 遗留缺陷（S2-T3 修）：换绑世界时 ``_pending.clear()`` 是一次无事件的静默丢弃。

    S1 时这条路走不到（注册表只在同步调用内瞬态存在），但 S2 的 run 模型让它可达：
    reset 换世界、runtime 发现世界被换过都会调它。静默丢弃正是 S1 到处根治的那一类
    缺陷——命令消失、生命周期停在非终态、可观测性面板永远等一个不会来的收尾。
    """

    events, publish = _collector()
    sm = StateManager(_make_world())
    ex = CommandExecutor(sm, publish)

    record = await ex._propose(_cmd(command_id="cmd-inflight"), None, publish=publish)
    events.clear()

    new_sm = StateManager(_make_world())
    cancelled = await ex.bind_state_manager(new_sm, reason="simulation_reset")

    assert [rec.command.command_id for rec in cancelled] == ["cmd-inflight"]
    assert record.status == CommandStatus.CANCELLED
    assert record.detail == "simulation_reset"
    assert ex.pending == {}
    assert ex.state_manager is new_sm

    lifecycle = [
        event
        for event in events
        if event.event_type == LIFECYCLE_EVENT_TYPE
        and event.data["to_status"] == CommandStatus.CANCELLED.value
    ]
    assert len(lifecycle) == 1
    assert lifecycle[0].data["command_id"] == "cmd-inflight"
    assert lifecycle[0].data["detail"] == "simulation_reset"


@pytest.mark.anyio
async def test_bind_state_manager_without_pending_emits_nothing():
    events, publish = _collector()
    ex = CommandExecutor(StateManager(_make_world()), publish)

    assert await ex.bind_state_manager(StateManager(_make_world())) == []
    assert events == []
