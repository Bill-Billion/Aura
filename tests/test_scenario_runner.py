"""ScenarioRunner：把一份 ScenarioSpec headless 跑到底（S2-T6）。

它是 S2-T9 确定性门、S4 suite runner 与 §15-2 验收共同的驱动入口，因此这里钉死四件事：
  1. headless 跑完整个 duration 不睡任何墙钟（否则 8 场景 × 2 遍的确定性门要跑 10 分钟）；
  2. 富根事件（user.arrives_home）真的能触发 agent episode——否则场景根事件全是哑弹；
  3. timeline 的设备变更走 CommandExecutor 且 source=scenario（critic 修正①）；
  4. tick 抛异常时引擎**停下并报错**，而不是 is_running=True 的假活（critic 修正③）；
     场景 runner 在这种情况下必须结构化失败，不能交出一份被截断却看起来正常的 trace。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.agents.llm import LLMProvider, LLMProviderError
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus
from backend.engine.event_log import read_run_events, read_run_metadata
from backend.engine.simulation import ENGINE_ERROR_WS_TYPE, SimulationEngine
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES, ENGINE_ERROR_EVENT_TYPE
from backend.engine.state import Location3D, RoomState, WorldState
from backend.engine.state_manager import StateManager
from backend.execution.command import LIFECYCLE_EVENT_TYPE
from backend.models.schemas import WSMessage
from backend.scenarios.loader import get_scenario
from backend.scenarios.runner import (
    ScenarioRunError,
    ScenarioRunErrorCode,
    ScenarioRunner,
    run_scenario,
)

SCENARIO_ID = "user_arrives_home_evening"


class _RecordingConnectionManager(ConnectionManager):
    """记录全部广播消息的连接管理器（没有真实 socket 时的观测口）。"""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[WSMessage] = []

    async def broadcast(self, msg: WSMessage) -> None:  # type: ignore[override]
        self.messages.append(msg)
        await super().broadcast(msg)


class _DisabledProvider(LLMProvider):
    provider_name = "disabled"
    model = "rule_based"

    async def generate_decision(self, request):  # type: ignore[override]
        raise LLMProviderError("provider_error", "LLM provider is disabled")


class _HangingProvider(LLMProvider):
    provider_name = "hanging_test"
    model = "never_returns"
    api_key = "test-only"
    max_output_tokens = 1200

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def generate_decision(self, request):  # type: ignore[override]
        del request
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _spec():
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None, "库场景 arrive_home_evening.yaml 必须可加载"
    return spec


# --------------------------------------------------------------- headless 驱动


@pytest.mark.anyio
async def test_headless_run_completes_within_duration_without_wall_clock_sleep():
    spec = _spec()
    started = time.monotonic()
    result = await run_scenario(SCENARIO_ID)
    elapsed = time.monotonic() - started

    assert result.scenario_id == SCENARIO_ID
    assert result.seed == spec.seed
    assert result.run_id
    assert result.completed is True
    # 模拟时间覆盖整段 duration，墙钟却几乎没走（headless tick_once 绕开 2s 节拍）
    assert result.sim_time_s >= spec.duration_seconds
    assert elapsed < 10.0
    assert result.ticks == int(spec.duration_seconds // 10) + 1
    # timeline 全部命中
    assert result.fired_timeline_event_types == ("user.arrives_home", "user.command")


@pytest.mark.anyio
async def test_run_result_answers_which_scenario_and_seed():
    """§18 Q1：一份事件流必须能回答"哪个场景、哪个 seed"。"""

    result = await run_scenario(SCENARIO_ID)
    assert result.run_metadata.scenario_id == SCENARIO_ID
    assert result.run_metadata.seed == result.seed
    assert all(event.run_id == result.run_id for event in result.events)
    assert {event.scenario_id for event in result.events} == {SCENARIO_ID}


@pytest.mark.anyio
async def test_rich_root_event_triggers_agent_episode():
    """user.arrives_home（富分类学）必须真的开出一条 agent episode。"""

    result = await run_scenario(SCENARIO_ID)
    arrivals = [e for e in result.events if e.event_type == "user.arrives_home"]
    assert len(arrivals) == 1
    correlation_id = arrivals[0].correlation_id

    episode_events = [
        e
        for e in result.events
        if e.correlation_id == correlation_id
        and e.event_type.startswith("reasoning.")
    ]
    assert any(e.event_type == "reasoning.perception_snapshot" for e in episode_events)
    assert any(e.event_type == "reasoning.intent_recognized" for e in episode_events)


@pytest.mark.anyio
async def test_every_real_agent_episode_has_one_causal_none_rich_root():
    """Generated schedule events remain episode roots, not timer children."""

    result = await run_scenario("cooking_dinner")
    reasoning_correlations = {
        event.correlation_id
        for event in result.events
        if event.event_type.startswith("reasoning.")
    }
    for correlation_id in reasoning_correlations:
        roots = [
            event
            for event in result.events
            if event.correlation_id == correlation_id
            and event.event_type in ALL_ROOT_EVENT_TYPES
            and event.causal_parent is None
        ]
        assert len(roots) == 1, correlation_id


@pytest.mark.anyio
async def test_scenario_timeline_command_traverses_executor_with_scenario_source():
    """critic 修正①：脚本设备变更 = DeviceCommand(source=scenario) 的完整生命周期链。"""

    result = await run_scenario(SCENARIO_ID)
    lifecycle = [
        e
        for e in result.events
        if e.event_type == LIFECYCLE_EVENT_TYPE and e.data["source"] == "scenario"
    ]
    assert lifecycle, "库场景的 timeline 设备命令必须产生 scenario 来源的生命周期事件"
    statuses = [e.data["to_status"] for e in lifecycle]
    assert statuses == ["proposed", "approved", "validated", "executing", "succeeded"]
    assert {e.data["device_id"] for e in lifecycle} == {"curtain_living_01"}
    # 命令确实改了世界
    action = [
        e
        for e in result.events
        if e.event_type == "action.device_control" and e.data["source"] == "scenario"
    ]
    assert len(action) == 1
    assert action[0].data["capability"] == "open_percent"


@pytest.mark.anyio
async def test_generated_events_are_inspectable_by_generation_mode():
    """§4.5 末段：研究者要能逐条判断一条行为来自脚本、规则还是噪声。"""

    result = await run_scenario(SCENARIO_ID)
    modes = {
        event.event_generation_mode
        for event in result.events
        if event.event_generation_mode is not None
    }
    assert "scripted" in modes
    assert "system" in modes  # timer/reset 这类生命周期事件


@pytest.mark.anyio
async def test_stochastic_device_offline_marks_world_and_is_attributable():
    """随机故障注入经引擎落到世界上：事件带 rng_stream，delta 归因到 failure_injector。

    ``online`` 是 §3.2 声明的**不可写**能力，因此这条写入刻意不走 CommandExecutor
    （走了只会拿到一条 read_only_capability 失败）——但它依然必须可归因。
    """

    conn = _RecordingConnectionManager()
    runner = ScenarioRunner(
        _spec(),
        llm_provider=_DisabledProvider(),
        connection_manager=conn,
        stochastic_overrides={"probability": 1.0, "device_ids": ["camera_entry_01"]},
    )
    result = await runner.run()

    offline = result.events_of_type("device.offline")
    assert offline, "probability=1.0 时第一拍就该掉线"
    assert offline[0].event_generation_mode == "stochastic"
    assert offline[0].rng_stream == "stochastic_events"
    assert offline[0].data["device_id"] == "camera_entry_01"

    # 世界真的变了：最后一条可用性事件与设备当前 online 位一致
    availability = [
        event
        for event in result.events
        if event.event_type in {"device.offline", "device.recovered"}
    ]
    device = runner.engine.state_manager.world.devices["camera_entry_01"]
    assert device.state.extra["online"] is availability[-1].data["online"]

    # 这条写入可归因：delta 的 caused_by 是故障注入器，不是环境仿真、更不是某条命令
    online_deltas = [
        delta
        for message in conn.messages
        if message.type == "STATE_DELTA"
        for delta in message.payload["deltas"]
        if delta["path"] == "devices[camera_entry_01].state.extra.online"
    ]
    assert online_deltas
    assert {delta["caused_by"] for delta in online_deltas} == {"failure_injector"}
    assert all(delta["caused_by_event_id"] for delta in online_deltas)


# ---------------------------------------------------- 双模式一致性（风险条款）


@pytest.mark.anyio
async def test_headless_and_live_modes_produce_same_generated_event_sequence():
    """headless tick_once 与墙钟 live 模式必须产出同一条**生成事件**序列。"""

    headless = await run_scenario(SCENARIO_ID)
    live = await run_scenario(SCENARIO_ID, live=True, tick_interval=0.005)

    def generated(result) -> list[str]:
        return [
            event.event_type
            for event in result.events
            if event.event_generation_mode in {"scripted", "rule_based", "stochastic"}
        ]

    assert generated(headless) == generated(live)
    assert headless.ticks == live.ticks


@pytest.mark.anyio
async def test_non_integral_duration_stops_both_drivers_after_first_covering_tick():
    """duration=25/dt=10 means complete t=0,10,20,30 fan-out, never tick 5."""

    spec = _spec().model_copy(update={"duration_seconds": 25.0})
    headless = await run_scenario(spec, seed=4242)
    live = await run_scenario(spec, seed=4242, live=True, tick_interval=0.005)

    def tick_events(result):
        return sorted(
            result.events_of_type("system.timer_tick"),
            key=lambda event: int(event.seq or 0),
        )

    def generated_signature(result):
        return [
            (event.event_type, event.sim_time_s, event.generation_rule_id, event.rng_stream)
            for event in result.events
            if event.event_generation_mode in {"scripted", "rule_based", "stochastic"}
        ]

    for result in (headless, live):
        ticks = tick_events(result)
        assert result.seed == 4242
        assert result.ticks == 4
        assert result.sim_time_s == 30.0
        assert [event.data["tick"] for event in ticks] == [1, 2, 3, 4]
        assert [event.sim_time_s for event in ticks] == [0.0, 10.0, 20.0, 30.0]
        final_tick = ticks[-1]
        assert any(
            event.event_type == "environment.state_refresh"
            and event.causal_parent == final_tick.event_id
            for event in result.events
        )

    assert generated_signature(headless) == generated_signature(live)


@pytest.mark.anyio
async def test_tick_budget_cannot_silently_finalize_before_duration_boundary():
    spec = _spec().model_copy(update={"duration_seconds": 25.0})
    runner = ScenarioRunner(spec, max_ticks=3)

    with pytest.raises(ScenarioRunError) as excinfo:
        await runner.run()

    assert excinfo.value.code is ScenarioRunErrorCode.TICK_BUDGET_EXCEEDED
    assert excinfo.value.details["required_ticks"] == 4
    assert runner.engine.run_manager.finished[-1].end_reason == "tick_budget_exceeded"


# ------------------------------------------- critic 修正③：引擎假活（fake-alive）


def _error_engine() -> tuple[SimulationEngine, _RecordingConnectionManager]:
    world = WorldState(scene_id="fake_alive")
    world.rooms = {"living_room": RoomState(id="living_room")}
    conn = _RecordingConnectionManager()
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(world),
        connection_manager=conn,
        llm_provider=_DisabledProvider(),
    )
    engine.timer.tick_interval = 0.01
    return engine, conn


@pytest.mark.anyio
async def test_tick_exception_stops_engine_with_engine_error_event_not_silent_fake_alive():
    engine, conn = _error_engine()

    def boom(*args, **kwargs):
        raise RuntimeError("tick body exploded")

    engine.env_sim.step = boom  # type: ignore[method-assign]

    await engine.start()
    for _ in range(200):
        if not engine.is_running:
            break
        await asyncio.sleep(0.01)

    # 引擎必须**停下**：旧实现只吞 CancelledError，循环死掉但 is_running 仍为 True
    assert engine.is_running is False
    assert engine.timer.is_running is False
    assert engine.state_manager.world.is_running is False

    errors = engine.event_bus.get_history(event_type=ENGINE_ERROR_EVENT_TYPE)
    assert len(errors) == 1
    assert "tick body exploded" in errors[0].data["error"]
    assert errors[0].data["phase"] == "timer_tick"
    assert any(msg.type == ENGINE_ERROR_WS_TYPE for msg in conn.messages)
    assert engine.last_engine_error is not None

    # 报错之后必须还能重新起来（旧实现里 start() 会因为 is_running=True 直接 return）
    engine.env_sim.step = lambda *a, **kw: {}  # type: ignore[method-assign]
    await engine.start()
    assert engine.is_running is True
    assert engine.last_engine_error is None
    await engine.close()


@pytest.mark.anyio
async def test_scenario_runner_fails_fast_when_engine_dies_mid_run():
    runner = ScenarioRunner(_spec(), llm_provider=_DisabledProvider())
    calls = {"n": 0}
    original = runner.engine.env_sim.step

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("env sim died mid run")
        return original(*args, **kwargs)

    runner.engine.env_sim.step = flaky  # type: ignore[method-assign]

    with pytest.raises(ScenarioRunError) as excinfo:
        await runner.run()

    error = excinfo.value
    assert error.code is ScenarioRunErrorCode.ENGINE_ERROR
    assert error.details["scenario_id"] == SCENARIO_ID
    assert "env sim died mid run" in error.details["error"]
    assert runner.engine.is_running is False


@pytest.mark.anyio
async def test_scenario_runner_rejects_and_seals_hung_episode_as_incomplete(
    monkeypatch,
):
    """A settle timeout must cancel the episode before sealing a non-completed run."""

    monkeypatch.setenv("LLM_MODE", "live")
    provider = _HangingProvider()
    runner = ScenarioRunner(
        _spec(),
        llm_provider=provider,
        episode_timeout_ms=60_000,
        episode_settle_timeout_s=0.02,
    )

    with pytest.raises(ScenarioRunError) as excinfo:
        await runner.run()

    assert excinfo.value.code is ScenarioRunErrorCode.EPISODE_SETTLE_TIMEOUT
    assert provider.started.is_set()
    assert provider.cancelled.is_set()
    assert runner.engine.is_running is False

    metadata = runner.engine.run_manager.finished[-1]
    assert metadata.end_reason == "episode_settle_timeout"
    persisted = read_run_metadata(metadata.run_id)
    assert persisted["end_reason"] == "episode_settle_timeout"
    assert persisted["events_integrity"] is not None

    events, _ = read_run_events(metadata.run_id)
    event_types = [event["event_type"] for event in events]
    assert "system.episode_cancelled" in event_types
    assert "system.episode_cancelled" in [event.event_type for event in runner._collected]


# ------------------------------------- S1 遗留：真实的在飞窗口（不再靠 _propose 造）


@pytest.mark.anyio
async def test_in_flight_scenario_command_is_cancelled_with_lifecycle_events():
    """S1 时"在飞命令"只能用私有 ``executor._propose`` 造出来（同步 apply 没有窗口）。

    S2 的异步 episode/场景驱动让窗口变成真的：命令停在 executing 上等一次事件外发时，
    reset 的 ``cancel_pending`` 必须**带生命周期事件**地把它取消——静默从注册表删掉一条
    活命令等于零可观测性（S1 review2 finding-1 的同类）。
    """

    from backend.engine.state import DeviceState, DeviceStateValues
    from backend.execution.command import CommandSource, CommandStatus, DeviceCommand
    from backend.execution.executor import ACTION_EVENT_TYPE, CommandExecutor

    world = WorldState(scene_id="inflight")
    world.rooms = {"living_room": RoomState(id="living_room")}
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(power=False, extra={"brightness": 0}),
        )
    }
    state_manager = StateManager(world)
    events = []
    reached_action = asyncio.Event()
    release = asyncio.Event()

    async def slow_publish(event):
        events.append(event)
        if event.event_type == ACTION_EVENT_TYPE:
            # 真实的 await 窗口：动作已下发、世界尚未变更，命令停在 executing 上。
            reached_action.set()
            await release.wait()
        return event

    executor = CommandExecutor(state_manager, slow_publish)
    task = asyncio.create_task(
        executor.submit(
            DeviceCommand(
                source=CommandSource.SCENARIO,
                device_id="light_living_01",
                capability="power",
                value=True,
                reason="scenario timeline step",
            )
        )
    )
    await asyncio.wait_for(reached_action.wait(), timeout=5.0)

    assert list(executor.pending), "在飞注册表里必须真的有一条未终态命令"
    cancelled = await executor.cancel_pending("simulation_reset")
    release.set()
    record = await asyncio.wait_for(task, timeout=5.0)

    assert [item.status for item in cancelled] == [CommandStatus.CANCELLED]
    assert record.status is CommandStatus.CANCELLED
    lifecycle = [e.data["to_status"] for e in events if e.event_type == LIFECYCLE_EVENT_TYPE]
    assert lifecycle == ["proposed", "approved", "validated", "executing", "cancelled"]
    assert {e.data["source"] for e in events if e.event_type == LIFECYCLE_EVENT_TYPE} == {"scenario"}
    # 取消发生在 apply 之前：世界零变更
    assert world.devices["light_living_01"].state.power is False


# --------------------------------------------------- 初始状态 / 世界一致性


@pytest.mark.anyio
async def test_runner_applies_scenario_initial_state_before_first_tick():
    runner = ScenarioRunner(_spec(), llm_provider=_DisabledProvider())
    world = runner.engine.state_manager.world
    # initial_state 在构造期就已落到世界上（run 起点必须是场景声明的世界）
    assert world.environment.time_of_day == "18:30"
    assert world.rooms["living_room"].occupancy is False
    assert world.users["user_01"].location is None

    result = await runner.run()
    assert result.initial_state.deltas, "initial_state 必须留下可归因的 delta"
    assert runner.engine.state_manager.world.users["user_01"].location == Location3D(
        room="living_room"
    )
