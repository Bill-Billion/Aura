"""§4.5 三种事件生成模式 + §4.1 富根事件分类学（S2-T6）。

三条不可退让的断言：
  1. 每条**被生成**的事件都带齐 §4.5 五项元数据（run_id / scenario_id /
     event_generation_mode，rule 事件另有 generation_rule_id、stochastic 事件另有 rng_stream）；
  2. timeline 里的设备变更必须构造 ``DeviceCommand(source="scenario")`` 走 CommandExecutor
     ——没有 state_manager 兜底路径（critic 修正①）；
  3. 富根事件的 causal_parent 为空，具体物理触发者记录在 data.trigger_event_id，绝不是
     "最近一条用户事件"（critic 修正②：两个用户先后动作时那条启发式会张冠李戴）。
"""

from __future__ import annotations

import pytest

from backend.engine.event_bus import EventBus, SimEvent
from backend.engine import event_types
from backend.engine.rng import RngStream, SimRandom
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    UserState,
    WorldState,
)
from backend.execution.command import CommandSource
from backend.scenarios.generator import (
    RuleBasedEventSource,
    ScriptedEventSource,
    StochasticEventSource,
    GenerationContext,
    build_generation_sources,
    scenario_timeline_device_entries,
)
from backend.scenarios.loader import parse_scenario_mapping
from backend.scenarios.spec import (
    ALLOWED_TIMELINE_EVENT_TYPES,
    COMPAT_ROOT_EVENT_TYPES,
    ROOT_EVENT_TYPES,
    ScenarioSpec,
)


# --------------------------------------------------------------------- 夹具


def _world() -> WorldState:
    world = WorldState(scene_id="gen_test")
    world.rooms = {
        "living_room": RoomState(id="living_room", temperature=24.0, light_level=10.0),
        "kitchen": RoomState(id="kitchen", temperature=24.0, light_level=400.0),
    }
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(power=False, extra={"brightness": 0}),
        ),
        "camera_entry_01": DeviceState(
            id="camera_entry_01",
            type="camera",
            location=Location3D(room="living_room"),
            capabilities=["view", "online"],
            state=DeviceStateValues(power=True, extra={"online": True}),
        ),
    }
    world.users = {
        "user_01": UserState(id="user_01", name="A", location=None, activity="commuting"),
        "user_02": UserState(id="user_02", name="B", location=None, activity="commuting"),
    }
    return world


def _spec(**overrides) -> ScenarioSpec:
    data: dict = {
        "id": "gen_test",
        "name": "生成模式测试场景",
        "description": "unit fixture",
        "seed": 4242,
        "duration_seconds": 60,
        "initial_state": {"time_of_day": "18:30"},
        "timeline": [
            {"at": 0, "type": "user.arrives_home", "user_id": "user_01", "room_id": "living_room"},
            {
                "at": 10,
                "type": "user.command",
                "user_id": "user_01",
                "device_id": "light_living_01",
                "payload": {"capability": "power", "value": True, "reason": "剧本直控"},
            },
        ],
        "expected_device_effects": [
            {"device_id": "light_living_01", "expected": {"power": True}}
        ],
        "involved_agents": ["lighting_agent"],
        "success_criteria": {},
    }
    data.update(overrides)
    return parse_scenario_mapping(data, check_registry=False)


def _tick_event(tick: int = 1) -> SimEvent:
    return SimEvent(
        event_type="system.timer_tick",
        source="simulator_timer",
        timestamp=float(tick),
        data={"tick": tick, "simulated_dt": 10.0},
    )


def _context() -> GenerationContext:
    return GenerationContext(run_id="run-test", scenario_id="gen_test")


# ------------------------------------------------------- §4.1 分类学单一来源


def test_root_event_taxonomy_has_single_source_of_truth():
    """spec.py 与 engine/event_types.py 必须是同一份分类学对象，不是两份内容相同的枚举。"""

    assert ROOT_EVENT_TYPES is event_types.ROOT_EVENT_TYPES
    assert COMPAT_ROOT_EVENT_TYPES is event_types.COMPAT_ROOT_EVENT_TYPES
    assert ALLOWED_TIMELINE_EVENT_TYPES is event_types.ALLOWED_TIMELINE_EVENT_TYPES
    assert len(event_types.ROOT_EVENT_TYPES) == 14


# -------------------------------------------------------------- scripted 模式


def test_scripted_same_scenario_seed_same_root_event_order():
    """§4.5 原文：同场景同 seed 必须产出同样的根事件顺序（同刻按 timeline 索引）。"""

    spec = _spec(
        timeline=[
            {"at": 0, "type": "user.arrives_home", "user_id": "user_01", "room_id": "living_room"},
            {"at": 10, "type": "user.starts_activity", "user_id": "user_01", "activity": "cooking", "room_id": "kitchen"},
            {"at": 10, "type": "environment.weather_change", "payload": {"weather": "rainy"}},
            {"at": 30, "type": "user.leaves_home", "user_id": "user_01"},
        ]
    )

    def once() -> list[str]:
        source = ScriptedEventSource(spec, context=_context())
        world = _world()
        emitted: list[str] = []
        for sim_time in (0.0, 10.0, 20.0, 30.0):
            for generated in source.emit(world, trigger=_tick_event(), sim_time_s=sim_time):
                emitted.append(generated.event.event_type)
        return emitted

    first, second = once(), once()
    assert first == second
    assert first == [
        "user.arrives_home",
        "user.starts_activity",
        "environment.weather_change",
        "user.leaves_home",
    ]


def test_scripted_events_are_root_events_with_new_correlation_id():
    """§4.4：每条根事件开一条新因果链（correlation 各不相同、causal_parent 为空）。"""

    source = ScriptedEventSource(_spec(), context=_context())
    generated = source.emit(_world(), trigger=_tick_event(), sim_time_s=0.0)
    assert [g.event.event_type for g in generated] == ["user.arrives_home"]
    root = generated[0].event
    assert root.causal_parent is None
    assert root.correlation_id


def test_timeline_device_change_builds_scenario_sourced_command():
    """critic 修正①：timeline 的设备变更必须变成 DeviceCommand(source=scenario)，无兜底路径。"""

    source = ScriptedEventSource(_spec(), context=_context())
    source.emit(_world(), trigger=_tick_event(), sim_time_s=0.0)
    generated = source.emit(_world(), trigger=_tick_event(2), sim_time_s=10.0)

    assert len(generated) == 1
    item = generated[0]
    assert item.event.event_type == "user.command"
    assert item.device_command is not None
    command = item.device_command
    assert command.source is CommandSource.SCENARIO
    assert command.device_id == "light_living_01"
    assert command.capability == "power"
    assert command.value is True
    # 命令挂在它自己的根事件之下（§4.4 causal_parent 指向直接父事件）
    assert command.causal_parent == item.event.event_id
    assert command.correlation_id == item.event.correlation_id


def test_scenario_timeline_device_entries_exposes_scripted_commands():
    """S2-T8 的 YAML 作者与 §15-2 验收共用同一个"哪些 timeline 项是设备命令"的判定。"""

    entries = scenario_timeline_device_entries(_spec())
    assert [entry.device_id for entry in entries] == ["light_living_01"]


# ------------------------------------------------------------ rule_based 模式


def test_rule_based_threshold_emits_environment_temperature_threshold_with_rule_id():
    world = _world()
    world.rooms["living_room"].temperature = 31.0
    world.rooms["living_room"].occupancy = True
    world.rooms["living_room"].persons = ["user_01"]
    world.users["user_01"].location = Location3D(room="living_room")

    source = RuleBasedEventSource(context=_context(), emit_user_events=False)
    tick = _tick_event()
    generated = source.emit(world, trigger=tick, sim_time_s=10.0)

    events = [item.event for item in generated]
    threshold = [e for e in events if e.event_type == "environment.temperature_threshold"]
    assert len(threshold) == 1
    event = threshold[0]
    assert event.event_generation_mode == "rule_based"
    assert event.generation_rule_id == "temperature_threshold.high"
    assert event.data["room_id"] == "living_room"
    assert event.data["value"] == pytest.approx(31.0)
    assert event.causal_parent is None
    assert event.data["trigger_event_id"] == tick.event_id

    # 迟滞：同一状态不重复发（否则每 tick 一条会淹没事件流）
    assert source.emit(world, trigger=_tick_event(2), sim_time_s=20.0) == []


def test_rule_based_user_schedule_emits_rich_root_events_with_rule_id():
    """UserBehaviorSimulator 的硬编码 SCHEDULE 被收编成 rule_based 富根事件。"""

    world = _world()
    world.environment.time_of_day = "18:30"
    source = RuleBasedEventSource(context=_context())
    generated = source.emit(world, trigger=_tick_event(), sim_time_s=0.0)

    user_events = [item.event for item in generated if item.event.event_type.startswith("user.")]
    assert user_events, "18:30 的日程条目必须产出用户根事件"
    for event in user_events:
        assert event.event_type in ROOT_EVENT_TYPES  # 富分类学，不是 user.activity_change
        assert event.event_generation_mode == "rule_based"
        assert event.generation_rule_id.startswith("user_schedule")
        assert event.causal_parent is None
        assert event.data["trigger_event_id"]
        # 世界写回所需的三个键与旧兼容事件同名（迁移期兼容）
        assert {"user_id", "from_room", "to_room", "activity"} <= set(event.data)


def test_env_threshold_records_actual_trigger_without_demoting_episode_root():
    """两个用户先后动作时，阈值根单独记录真正改变读数的触发事件。

    用户 A 走进昏暗的客厅 → 触发 light_level_threshold；随后用户 B 走进明亮的厨房 → 不触发。
    "取最近一条用户事件"的旧启发式会把父指向 B，因果链从此撒谎。
    """

    world = _world()
    source = RuleBasedEventSource(context=_context(), emit_user_events=False)

    user_a = SimEvent(
        event_type="user.enters_room",
        source="rule_engine",
        timestamp=1.0,
        data={"user_id": "user_01", "from_room": "", "to_room": "living_room", "activity": "relaxing"},
    )
    # A 进入客厅（昏暗且有人）→ 阈值成立
    world.rooms["living_room"].occupancy = True
    world.rooms["living_room"].persons = ["user_01"]
    world.users["user_01"].location = Location3D(room="living_room")
    from_a = source.emit_threshold_events(world, trigger=user_a, sim_time_s=1.0)

    user_b = SimEvent(
        event_type="user.enters_room",
        source="rule_engine",
        timestamp=1.0,
        data={"user_id": "user_02", "from_room": "", "to_room": "kitchen", "activity": "cooking"},
    )
    world.rooms["kitchen"].occupancy = True
    world.rooms["kitchen"].persons = ["user_02"]
    world.users["user_02"].location = Location3D(room="kitchen")
    from_b = source.emit_threshold_events(world, trigger=user_b, sim_time_s=2.0)

    light_events = [
        item.event
        for item in (*from_a, *from_b)
        if item.event.event_type == "environment.light_level_threshold"
    ]
    assert len(light_events) == 1
    assert light_events[0].causal_parent is None
    assert light_events[0].data["trigger_event_id"] == user_a.event_id
    assert light_events[0].generation_rule_id == "light_level_threshold.low"


# ------------------------------------------------------------ stochastic 模式


def _stochastic_source(seed: int) -> StochasticEventSource:
    rng = SimRandom(seed)
    return StochasticEventSource(
        context=_context(),
        stream=rng.stream(RngStream.STOCHASTIC_EVENTS),
        device_ids=("light_living_01", "camera_entry_01"),
        offline_probability=0.5,
        recovery_probability=0.5,
    )


def _offline_schedule(seed: int) -> list[tuple[int, str, str]]:
    world = _world()
    source = _stochastic_source(seed)
    schedule: list[tuple[int, str, str]] = []
    for tick in range(1, 25):
        for item in source.emit(world, trigger=_tick_event(tick), sim_time_s=float(tick * 10)):
            schedule.append((tick, item.event.event_type, item.event.data["device_id"]))
            # 世界跟着改，否则 offline/recovered 的状态机永远停在原地
            assert item.availability_write is not None
            device = world.devices[item.availability_write.device_id]
            device.state.extra["online"] = item.availability_write.online
    return schedule


def test_stochastic_same_seed_identical_offline_schedule_diff_seed_differs():
    assert _offline_schedule(2026) == _offline_schedule(2026)
    assert _offline_schedule(2026) != _offline_schedule(9)
    assert _offline_schedule(2026), "阴性对照失效：这颗 seed 下根本没抽到过离线事件"


def test_stochastic_scope_is_device_offline_only():
    """MVP 收缩（critic 修正④）：stochastic 源只覆盖 device.offline/recovered。"""

    types = {entry[1] for entry in _offline_schedule(2026)}
    assert types <= {"device.offline", "device.recovered"}


# ------------------------------------------------- §4.5 五项元数据（全模式）


def test_every_generated_event_carries_run_scenario_and_mode_metadata():
    spec = _spec()
    rng = SimRandom(spec.seed)
    context = GenerationContext(run_id="run-meta", scenario_id=spec.id)
    sources = build_generation_sources(
        spec,
        context=context,
        rng=rng,
        stochastic_overrides={"probability": 1.0, "device_ids": ["light_living_01"]},
    )

    world = _world()
    world.environment.time_of_day = "18:30"
    world.rooms["living_room"].temperature = 31.0
    world.rooms["living_room"].occupancy = True
    world.rooms["living_room"].persons = ["user_01"]
    world.users["user_01"].location = Location3D(room="living_room")

    generated = []
    for source in sources.all():
        generated.extend(source.emit(world, trigger=_tick_event(), sim_time_s=0.0))

    assert generated, "三种源在本夹具下至少各产出一条事件"
    modes = set()
    for item in generated:
        event = item.event
        assert event.run_id == "run-meta"
        assert event.scenario_id == "gen_test"
        assert event.event_generation_mode in {"scripted", "rule_based", "stochastic"}
        modes.add(event.event_generation_mode)
        if event.event_generation_mode == "rule_based":
            assert event.generation_rule_id, "rule 事件必须记命中的规则 id"
        if event.event_generation_mode == "stochastic":
            assert event.rng_stream == RngStream.STOCHASTIC_EVENTS.value
            # seed 本身留在 run 元数据里，不逐事件复制（§4.5 + §11 分工）
            assert "seed" not in event.data
    assert modes == {"scripted", "rule_based", "stochastic"}


@pytest.mark.anyio
async def test_engine_env_refresh_parent_is_the_tick_not_the_last_user_event():
    """审计§六⑤ / critic 修正②的引擎级回归：两个用户同拍动作时环境刷新的父是谁。

    旧实现把 ``environment.state_refresh`` 的 causal_parent 指向"本 tick 最后一条用户
    事件"。环境刷新的物理成因是时钟推进了 simulated_dt，与谁最后挪了窝毫无关系——
    多用户同拍时那条边直接把因果图写错。
    """

    from backend.agents.llm import LLMProvider, LLMProviderError
    from backend.api.ws import ConnectionManager
    from backend.engine.simulation import SimulationEngine
    from backend.engine.state_manager import StateManager

    class _Disabled(LLMProvider):
        provider_name = "disabled"
        model = "rule_based"

        async def generate_decision(self, request):  # type: ignore[override]
            raise LLMProviderError("provider_error", "disabled")

    world = _world()
    # 两个用户同处一室：user_sim 在同一拍会为两人各发一条活动事件。
    world.rooms["living_room"].occupancy = True
    world.rooms["living_room"].persons = ["user_01", "user_02"]
    world.users["user_01"].location = Location3D(room="living_room")
    world.users["user_02"].location = Location3D(room="living_room")

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(world),
        connection_manager=ConnectionManager(),
        llm_provider=_Disabled(),
    )
    await engine.start(drive_timer=False)
    tick_event = await engine.timer.tick_once()
    await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    history = engine.event_bus.get_history()
    user_events = [e for e in history if e.event_type == "user.activity_change"]
    assert len(user_events) >= 2, "夹具必须真的产生两条先后发生的用户事件"

    refresh = [e for e in history if e.event_type == "environment.state_refresh"]
    assert len(refresh) == 1
    assert refresh[0].causal_parent == tick_event.event_id
    assert refresh[0].causal_parent != user_events[-1].event_id


@pytest.mark.anyio
async def test_generated_events_survive_event_bus_stamping():
    """总线的"缺失才填"盖章不得覆盖生成方已声明的 run/scenario 归属（§2.2 stale 判定前提）。"""

    bus = EventBus()
    bus.set_run_context("run-active", "other_scenario")
    source = ScriptedEventSource(_spec(), context=GenerationContext(run_id="run-old", scenario_id="gen_test"))
    generated = source.emit(_world(), trigger=_tick_event(), sim_time_s=0.0)
    await bus.publish(generated[0].event)

    stored = bus.get_history()[-1]
    assert stored.run_id == "run-old"
    assert stored.scenario_id == "gen_test"
    assert stored.seq == 0
