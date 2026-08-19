from typing import get_args

import pytest
from pydantic import ValidationError

from backend.engine.event_bus import (
    EVENT_STORM_SUPPRESSED_EVENT_TYPE,
    EventBus,
    EventGenerationMode,
    SimEvent,
    WorldEvent,
)
from backend.engine.event_log import (
    attach_run_artifacts,
    read_run_events,
    verify_finalized_event_log,
)
from backend.engine.run_manager import RunManager
from backend.engine.state import WorldState
from backend.evaluation.evaluator import EvalOutcome, evaluate_run
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import get_scenario
from backend.scenarios.trace import export_canonical_trace


@pytest.mark.anyio
async def test_publish_notifies_subscriber():
    bus = EventBus()
    received: list[WorldEvent] = []

    async def handler(event: WorldEvent):
        received.append(event)

    bus.subscribe("device.changed", handler)

    event = WorldEvent(
        event_type="device.changed",
        source="agent-1",
        timestamp=100.0,
        data={"device_id": "light-001"},
    )
    count = await bus.publish(event)

    assert count == 1
    assert len(received) == 1
    assert received[0].source == "agent-1"
    assert received[0].data["device_id"] == "light-001"


@pytest.mark.anyio
async def test_wildcard():
    bus = EventBus()
    wildcard_events: list[WorldEvent] = []

    async def wildcard_handler(event: WorldEvent):
        wildcard_events.append(event)

    bus.subscribe("*", wildcard_handler)

    e1 = WorldEvent(event_type="device.on", source="a", timestamp=1.0, data={})
    e2 = WorldEvent(event_type="room.temp", source="b", timestamp=2.0, data={})

    c1 = await bus.publish(e1)
    c2 = await bus.publish(e2)

    assert c1 == 1
    assert c2 == 1
    assert len(wildcard_events) == 2
    assert wildcard_events[0].event_type == "device.on"
    assert wildcard_events[1].event_type == "room.temp"


@pytest.mark.anyio
async def test_unsubscribe():
    bus = EventBus()
    received: list[WorldEvent] = []

    async def handler(event: WorldEvent):
        received.append(event)

    bus.subscribe("test.event", handler)
    bus.unsubscribe("test.event", handler)

    event = WorldEvent(event_type="test.event", source="x", timestamp=1.0, data={})
    count = await bus.publish(event)

    assert count == 0
    assert len(received) == 0


@pytest.mark.anyio
async def test_history():
    bus = EventBus()

    events = [
        WorldEvent(event_type="device.on", source="a", timestamp=10.0, data={}),
        WorldEvent(event_type="device.off", source="a", timestamp=20.0, data={}),
        WorldEvent(event_type="device.on", source="b", timestamp=30.0, data={}),
    ]
    for e in events:
        await bus.publish(e)

    # All history
    all_hist = bus.get_history()
    assert len(all_hist) == 3

    # Filtered by type
    on_hist = bus.get_history(event_type="device.on")
    assert len(on_hist) == 2

    # Filtered by timestamp
    recent = bus.get_history(since=25.0)
    assert len(recent) == 1
    assert recent[0].timestamp == 30.0

    # Filtered by both
    combined = bus.get_history(event_type="device.on", since=15.0)
    assert len(combined) == 1


@pytest.mark.anyio
async def test_publish_world_event_upgrades_to_sim_event():
    bus = EventBus()

    event = WorldEvent(
        event_type="device.changed",
        source="user",
        timestamp=12.0,
        data={"device_id": "light_living_01"},
    )

    await bus.publish(event)
    history = bus.get_history()

    assert len(history) == 1
    assert isinstance(history[0], SimEvent)
    assert history[0].event_id
    assert history[0].correlation_id
    assert history[0].priority == 1
    assert history[0].timestamp == 12.0


@pytest.mark.anyio
async def test_history_supports_correlation_and_priority_filters():
    bus = EventBus()

    low_priority = SimEvent(
        event_type="feedback",
        source="lighting_agent",
        timestamp=10.0,
        wall_time=10.0,
        correlation_id="corr-a",
        priority=0,
        data={"path": "devices[light_living_01].state.power"},
    )
    high_priority = SimEvent(
        event_type="action",
        source="hvac_agent",
        timestamp=11.0,
        wall_time=11.0,
        correlation_id="corr-a",
        priority=3,
        data={"device_id": "ac_living_01"},
    )
    unrelated = SimEvent(
        event_type="user",
        source="user_sim",
        timestamp=12.0,
        wall_time=12.0,
        correlation_id="corr-b",
        priority=1,
        data={"activity": "breakfast"},
    )

    await bus.publish(low_priority)
    await bus.publish(high_priority)
    await bus.publish(unrelated)

    correlation_history = bus.get_history(correlation_id="corr-a")
    assert [event.event_type for event in correlation_history] == ["feedback", "action"]

    urgent_events = bus.get_history(min_priority=2)
    assert [event.event_type for event in urgent_events] == ["action"]


@pytest.mark.anyio
async def test_get_causal_chain_returns_root_first():
    bus = EventBus()

    root = SimEvent(
        event_id="root-event",
        event_type="user",
        source="user_sim",
        timestamp=100.0,
        wall_time=100.0,
        correlation_id="corr-root",
        priority=1,
        data={"activity": "arrive_home"},
    )
    child = SimEvent(
        event_id="child-event",
        event_type="action",
        source="lighting_agent",
        timestamp=101.0,
        wall_time=101.0,
        correlation_id="corr-root",
        causal_parent="root-event",
        priority=2,
        data={"device_id": "light_living_01"},
    )
    grandchild = SimEvent(
        event_id="grandchild-event",
        event_type="feedback",
        source="state_manager",
        timestamp=102.0,
        wall_time=102.0,
        correlation_id="corr-root",
        causal_parent="child-event",
        priority=1,
        data={"path": "devices[light_living_01].state.power"},
    )

    await bus.publish(child)
    await bus.publish(grandchild)
    await bus.publish(root)

    chain = bus.get_causal_chain("root-event")

    assert [event.event_id for event in chain] == [
        "root-event",
        "child-event",
        "grandchild-event",
    ]


# ---------------------------------------------------------------------------
# S2-T2：SimEvent 可复现元数据（run_id / scenario_id / 生成模式 / seq / sim_time_s）
# ---------------------------------------------------------------------------


def _sim_event(**overrides) -> SimEvent:
    """构造一条最小合法 SimEvent，只覆盖测试关心的字段。"""
    payload: dict = {
        "event_type": "user.arrives_home",
        "source": "scenario_runner",
        "timestamp": 1.0,
    }
    payload.update(overrides)
    return SimEvent(**payload)


async def _publish(bus: EventBus, event: WorldEvent | SimEvent) -> SimEvent:
    """发布并取回被总线盖章后的那一条（publish 的返回值是订阅者数量，不是事件）。"""
    await bus.publish(event)
    return bus.get_history()[-1]


@pytest.mark.anyio
async def test_publish_stamps_run_context_and_monotonic_seq():
    bus = EventBus()
    bus.set_run_context(run_id="run-a", scenario_id="arrive_home_evening")

    first = await _publish(bus, _sim_event(timestamp=1.0))
    second = await _publish(
        bus, _sim_event(timestamp=1.0, event_type="action.device_control")
    )
    third = await _publish(
        bus,
        WorldEvent(event_type="feedback.state_delta", source="state_manager", timestamp=1.0),
    )

    assert [event.run_id for event in (first, second, third)] == ["run-a"] * 3
    assert [event.scenario_id for event in (first, second, third)] == [
        "arrive_home_evening"
    ] * 3
    # seq 从 0 起、每条 +1，且与 timestamp（tick 计数）无关——同 tick 内也严格有序。
    assert [event.seq for event in (first, second, third)] == [0, 1, 2]
    assert bus.next_seq == 3


@pytest.mark.anyio
@pytest.mark.parametrize("pre_stamp", [False, True])
async def test_depth_cap_rejection_does_not_leave_a_public_seq_gap(pre_stamp: bool):
    bus = EventBus(max_causal_depth=1)
    bus.set_run_context(run_id="run-a", scenario_id="storm")

    root = _sim_event(
        event_id="root",
        event_type="user.command",
        correlation_id="corr",
    )
    allowed = _sim_event(
        event_id="allowed",
        event_type="feedback.state_delta",
        correlation_id="corr",
        causal_parent=root.event_id,
    )
    refused = _sim_event(
        event_id="refused",
        event_type="feedback.state_delta",
        correlation_id="corr",
        causal_parent=allowed.event_id,
    )

    await bus.publish(root)
    await bus.publish(allowed)
    if pre_stamp:
        # 引擎在 WS 外发前会先 stamp；闸门在这条路径上也不能预占幽灵序号。
        assert bus.stamp(refused).seq is None
    await bus.publish(refused)

    visible = bus.get_history()
    assert refused.event_id not in {event.event_id for event in visible}
    assert refused.seq is None, "被 depth-cap 拒绝的事件不应占用公开序号"
    assert [event.seq for event in visible] == [0, 1, 2]
    assert bus.next_seq == 3

    notice = visible[-1]
    assert notice.event_type == EVENT_STORM_SUPPRESSED_EVENT_TYPE
    assert notice.run_id == "run-a"
    assert notice.scenario_id == "storm"
    assert notice.correlation_id == root.correlation_id
    assert notice.causal_parent == allowed.event_id
    assert notice.depth == 2


@pytest.mark.anyio
async def test_publish_visible_broadcasts_only_admitted_notice_and_keeps_descendants_blocked():
    bus = EventBus(max_causal_depth=2)
    broadcast: list[SimEvent] = []

    async def before_fan_out(event: SimEvent) -> None:
        broadcast.append(event)

    parent_id: str | None = None
    inputs: list[SimEvent] = []
    returned: list[SimEvent] = []
    for index in range(8):
        event = _sim_event(
            event_id=f"event-{index}",
            event_type=f"test.depth_{index}",
            correlation_id="corr",
            causal_parent=parent_id,
        )
        inputs.append(event)
        returned.append(
            await bus.publish_visible(event, before_fan_out=before_fan_out)
        )
        # Deliberately keep extending from the producer's refused ID.  The bus
        # tombstone must retain its depth instead of resetting the next child
        # to a fresh root.
        parent_id = event.event_id

    visible = bus.get_history()
    assert [event.event_id for event in visible[:3]] == [
        "event-0",
        "event-1",
        "event-2",
    ]
    assert visible[3].event_type == EVENT_STORM_SUPPRESSED_EVENT_TYPE
    assert [event.event_id for event in broadcast] == [
        event.event_id for event in visible
    ]
    assert [event.seq for event in visible] == [0, 1, 2, 3]
    assert all(event.seq is None for event in inputs[3:])
    assert all(event is visible[3] for event in returned[3:])
    assert bus.storm_suppressed_count("corr") == 5


@pytest.mark.anyio
async def test_depth_cap_trace_finalizes_and_remains_exportable_and_evaluable(tmp_path):
    """真实 recorder 不应因被拒事件的幽灵 seq 留下不可评估的 trace。"""

    bus = EventBus(max_causal_depth=1)
    bus.set_sim_time_source(lambda: 0.0)
    run_manager = RunManager(
        event_bus=bus,
        sim_version="test",
        source_revision="test",
    )
    recorder = attach_run_artifacts(run_manager, root=tmp_path, enabled=True)
    bus.subscribe("*", recorder.record)

    scenario = get_scenario("morning_wake_up")
    assert scenario is not None
    metadata = run_manager.start_run(
        world=WorldState(scene_id="test"),
        scenario_id=scenario.id,
        scenario_schema_version=scenario.scenario_schema_version,
        scenario_contract_hash=scenario_contract_fingerprint(scenario),
        seed=7,
    )

    root = _sim_event(
        event_id="root",
        event_type="user.command",
        correlation_id="corr",
    )
    allowed = _sim_event(
        event_id="allowed",
        event_type="feedback.state_delta",
        correlation_id="corr",
        causal_parent=root.event_id,
    )
    refused = _sim_event(
        event_id="refused",
        event_type="feedback.state_delta",
        correlation_id="corr",
        causal_parent=allowed.event_id,
    )
    await bus.publish(root)
    await bus.publish(allowed)
    await bus.publish(refused)
    run_manager.end_run("completed")

    events, total = read_run_events(metadata.run_id, root=tmp_path)
    assert total == 3
    assert [event["seq"] for event in events] == [0, 1, 2]
    assert verify_finalized_event_log(metadata.run_id, root=tmp_path)["final_seq"] == 2

    exported = export_canonical_trace(metadata.run_id, root=tmp_path)
    assert len(exported.splitlines()) == 3
    report = evaluate_run(metadata.run_id, data_root=tmp_path)
    assert report.outcome is not EvalOutcome.ERROR


@pytest.mark.anyio
async def test_publish_stamps_in_place_on_caller_object():
    """调用方（main.py/runtime.py）广播的是自己手里那条对象，盖章必须原地生效。"""
    bus = EventBus()
    bus.set_run_context(run_id="run-a", scenario_id="s")
    event = _sim_event()

    await bus.publish(event)

    assert event.run_id == "run-a"
    assert event.seq == 0


@pytest.mark.anyio
async def test_publish_preserves_explicit_run_id_and_scenario_id():
    """旧 run 的事件不得被新 run 的上下文改写——否则 S2-T3 的 stale_run 判定失效。"""
    bus = EventBus()
    bus.set_run_context(run_id="run-new", scenario_id="scenario-new")

    stale = await _publish(bus, _sim_event(run_id="run-old", scenario_id="scenario-old"))

    assert stale.run_id == "run-old"
    assert stale.scenario_id == "scenario-old"
    assert stale.seq == 0  # seq 仍由总线盖章：它描述的是发布顺序，不是生产方的主张


@pytest.mark.anyio
async def test_publish_without_run_context_leaves_run_fields_none():
    bus = EventBus()

    event = await _publish(bus, _sim_event())

    assert event.run_id is None
    assert event.scenario_id is None
    assert event.seq == 0
    assert bus.run_id is None
    assert bus.scenario_id is None


@pytest.mark.anyio
async def test_stamp_assigns_seq_before_publish_and_publish_does_not_renumber():
    """给"必须先拿到盖章副本再外发"的调用方（main.py 的 UI 根事件）用。

    修 S2 review：根事件此前在 publish 之前就广播出去，WS 上那份 seq=null，
    而它的子事件带 1..N——S5 的因果树按 seq 排序，无号的根节点排不进去。
    """
    bus = EventBus()
    bus.set_run_context(run_id="run-a", scenario_id="s")
    root = _sim_event()

    stamped = bus.stamp(root)

    assert stamped is root  # 原地盖章：调用方手里那条对象同步生效
    assert root.seq == 0
    assert root.run_id == "run-a"
    assert bus.next_seq == 1  # 盖章即占号，后来者不会撞号

    other = await _publish(bus, _sim_event())
    await bus.publish(root)

    assert other.seq == 1
    assert root.seq == 0  # 二次入总线不重编号：WS 副本与 events.jsonl 副本同号
    assert bus.get_history()[-1] is root


@pytest.mark.anyio
async def test_clear_empties_history_and_resets_seq():
    bus = EventBus()
    bus.set_run_context(run_id="run-a", scenario_id="s")
    await bus.publish(_sim_event())
    await bus.publish(_sim_event())
    assert bus.next_seq == 2

    bus.clear()

    assert bus.get_history() == []
    assert bus.next_seq == 0
    # clear() 只清历史与序号，不动 run 上下文与订阅（换 run 由 set_run_context 显式负责）。
    assert bus.run_id == "run-a"

    bus.set_run_context(run_id="run-b", scenario_id="s2")
    fresh = await _publish(bus, _sim_event())
    assert fresh.seq == 0
    assert fresh.run_id == "run-b"


@pytest.mark.anyio
async def test_clear_keeps_subscribers():
    bus = EventBus()
    received: list[SimEvent] = []

    async def handler(event: SimEvent) -> None:
        received.append(event)

    bus.subscribe("user.arrives_home", handler)
    bus.clear()

    await bus.publish(_sim_event())
    assert len(received) == 1


@pytest.mark.anyio
async def test_get_causal_chain_never_crosses_run_boundary():
    bus = EventBus()

    bus.set_run_context(run_id="run-a", scenario_id="s")
    await bus.publish(_sim_event(event_id="root-a", correlation_id="corr", timestamp=10.0))
    await bus.publish(
        _sim_event(
            event_id="child-a",
            event_type="action.device_control",
            correlation_id="corr",
            causal_parent="root-a",
            timestamp=11.0,
        )
    )

    # 同 correlation_id / 同 causal_parent 的另一 run 事件：reset 后未清历史的经典污染形态。
    bus.set_run_context(run_id="run-b", scenario_id="s")
    await bus.publish(
        _sim_event(
            event_id="child-b",
            event_type="action.device_control",
            correlation_id="corr",
            causal_parent="root-a",
            timestamp=11.0,
        )
    )

    chain = bus.get_causal_chain("root-a")

    assert [event.event_id for event in chain] == ["root-a", "child-a"]


@pytest.mark.anyio
async def test_same_timestamp_children_ordered_by_seq():
    """timestamp=tick 计数在同 tick 内并列，seq 是唯一稳定的次级排序锚。"""
    bus = EventBus()
    await bus.publish(_sim_event(event_id="root", timestamp=5.0, wall_time=1000.0))
    for index in range(3):
        await bus.publish(
            _sim_event(
                event_id=f"child-{index}",
                event_type="action.device_control",
                causal_parent="root",
                timestamp=5.0,
                wall_time=1000.0,
            )
        )

    chain = bus.get_causal_chain("root")

    assert [event.event_id for event in chain] == ["root", "child-0", "child-1", "child-2"]


@pytest.mark.anyio
async def test_get_history_filters_by_run_id_and_generation_mode():
    bus = EventBus()
    bus.set_run_context(run_id="run-a", scenario_id="s")
    await bus.publish(_sim_event(event_generation_mode="scripted"))
    await bus.publish(_sim_event(event_generation_mode="stochastic"))
    bus.set_run_context(run_id="run-b", scenario_id="s")
    await bus.publish(_sim_event(event_generation_mode="scripted"))

    assert len(bus.get_history(run_id="run-a")) == 2
    assert len(bus.get_history(event_generation_mode="scripted")) == 2
    assert len(bus.get_history(run_id="run-a", event_generation_mode="stochastic")) == 1


@pytest.mark.anyio
async def test_sim_time_source_stamps_sim_time_s():
    bus = EventBus()
    clock = {"t": 0.0}
    bus.set_sim_time_source(lambda: clock["t"])

    first = await _publish(bus, _sim_event())
    clock["t"] = 30.0
    second = await _publish(bus, _sim_event())
    # 生产方显式给了 sim_time_s 就不覆盖（scripted timeline 按 at 偏移自带模拟时刻）。
    third = await _publish(bus, _sim_event(sim_time_s=12.5))

    assert first.sim_time_s == 0.0
    assert second.sim_time_s == 30.0
    assert third.sim_time_s == 12.5


@pytest.mark.anyio
async def test_no_sim_time_source_leaves_sim_time_s_none():
    bus = EventBus()
    event = await _publish(bus, _sim_event())
    assert event.sim_time_s is None


def test_generation_metadata_fields_roundtrip():
    event = SimEvent(
        event_type="device.offline",
        source="stochastic_event_source",
        timestamp=7.0,
        run_id="run-a",
        scenario_id="device_offline",
        event_generation_mode="stochastic",
        generation_rule_id="device_offline_sampler",
        rng_stream="stochastic_events",
        seq=41,
        sim_time_s=70.0,
    )

    dumped = event.model_dump()
    assert dumped["run_id"] == "run-a"
    assert dumped["scenario_id"] == "device_offline"
    assert dumped["event_generation_mode"] == "stochastic"
    assert dumped["generation_rule_id"] == "device_offline_sampler"
    assert dumped["rng_stream"] == "stochastic_events"
    assert dumped["seq"] == 41
    assert dumped["sim_time_s"] == 70.0
    assert SimEvent(**dumped) == event


def test_unknown_generation_mode_rejected():
    with pytest.raises(ValidationError):
        SimEvent(
            event_type="user.arrives_home",
            source="x",
            timestamp=1.0,
            event_generation_mode="handcrafted",
        )


def test_generation_mode_enum_has_no_unwritten_member():
    """修 S2 review：枚举里不留没有生产方的成员。

    生成模式回答的是"这条**根**事件是怎么进世界的"：三条生成产线
    （scripted / rule_based / stochastic）加引擎自己的生命周期事件（system）。
    agent 的推理与动作是**派生**事件，来源由 causal_parent + source 表达，
    曾经的 'agent' 成员从未被任何生产方写入。
    """
    assert get_args(EventGenerationMode) == ("scripted", "rule_based", "stochastic", "system")


def test_agent_generation_mode_rejected():
    with pytest.raises(ValidationError):
        SimEvent(
            event_type="reasoning.intent_recognized",
            source="agent_runtime",
            timestamp=1.0,
            event_generation_mode="agent",
        )


def test_legacy_events_without_run_fields_still_valid():
    """迁移兼容：既有测试文件里的旧构造式必须零改动继续通过。"""
    legacy = SimEvent(
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=3.0,
        wall_time=1760762400.0,
        correlation_id="corr-legacy",
        causal_parent=None,
        priority=2,
        data={"user_id": "user_01"},
    )

    assert legacy.run_id is None
    assert legacy.scenario_id is None
    assert legacy.event_generation_mode is None
    assert legacy.generation_rule_id is None
    assert legacy.rng_stream is None
    assert legacy.seq is None
    assert legacy.sim_time_s is None


def test_from_world_event_carries_generation_overrides():
    world_event = WorldEvent(
        event_type="environment.temperature_threshold",
        source="environment_sim",
        timestamp=9.0,
        data={"room_id": "living_room"},
    )

    upgraded = SimEvent.from_world_event(
        world_event,
        event_generation_mode="rule_based",
        generation_rule_id="temp_above_comfort",
        sim_time_s=90.0,
    )

    assert upgraded.event_generation_mode == "rule_based"
    assert upgraded.generation_rule_id == "temp_above_comfort"
    assert upgraded.sim_time_s == 90.0
    assert upgraded.seq is None  # 未发布前没有序号
