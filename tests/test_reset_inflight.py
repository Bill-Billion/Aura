"""Reset must cancel in-flight agent episodes before swapping the world.

审计必修②：SimulationEngine.reset() 曾只清 memory_store，挂在 LLM 上的
在飞 episode 恢复后会把旧世界的命令写进重置后的新世界（spec §15
"Reset cancels or invalidates old in-flight agent episodes"，§11 run
hygiene）。取消必须发生在世界替换之前（cancel-before-swap），并对每条
被取消的 episode 发布 system.episode_cancelled，前端因果链才不会凭空
消失；受影响 agent 不得停留在 thinking。

S2-T3 在其上加 run 层（§2.2 "A mutation from an old run_id must not be
applied to the active run"）：取消是"尽力"防线（任务被砍），run_id 门是
"兜底"防线（任务没被砍也不许落地）。两条都要有——只靠取消时，任何一条
不经 cancel_active_episodes 的换 run 路径（场景连跑、S2-T6 runner）都会
让旧世界的命令写进新 run 且无人察觉。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.agents.llm import LLMProvider
from backend.agents.runtime import (
    STALE_RUN_DISCARD_EVENT_TYPE,
    STALE_RUN_DISCARD_REASON,
)
from backend.agents.types import (
    AgentCommandProposal,
    AgentLLMDecision,
    LLMDecisionRequest,
)
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.run_manager import (
    RunProvenanceError,
    RunProvenanceErrorCode,
    compute_initial_state_hash,
)
from backend.engine.simulation import SimulationEngine
from backend.engine.state import (
    AgentRuntimeState,
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    UserState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.scenarios.loader import get_scenario

# 库里真实存在的场景 id（backend/scenarios/library/arrive_home_evening.yaml 的 `id:`）。
LIBRARY_SCENARIO_ID = "user_arrives_home_evening"


class GatedProvider(LLMProvider):
    """Park every decision on an asyncio gate so the episode stays in flight."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.entered.set()
        await self.gate.wait()
        if request.agent_id == "lighting_agent":
            return AgentLLMDecision(
                intent="light occupied room",
                confidence=0.9,
                task_steps=["raise brightness"],
                proposed_commands=[
                    AgentCommandProposal(
                        device_id="light_living_01",
                        property="extra.brightness",
                        value=70,
                        reason="stale command from the pre-reset world",
                    )
                ],
                explanation="Lighting reacts to occupancy",
                needs_coordination=False,
            )
        return AgentLLMDecision(
            intent="hold hvac steady",
            confidence=0.5,
            task_steps=["no change"],
            proposed_commands=[],
            explanation="No HVAC action needed",
            needs_coordination=False,
        )


def _make_world() -> WorldState:
    world = WorldState(scene_id="test")
    world.environment.time_of_day = "19:00"
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=28.0,
            occupancy=False,
            persons=[],
        )
    }
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness", "color_temp"],
            state=DeviceStateValues(
                power=True,
                extra={"brightness": 0, "color_temp": 4000},
            ),
        ),
        "ac_living_01": DeviceState(
            id="ac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            capabilities=["power", "target_temp", "mode", "speed"],
            state=DeviceStateValues(
                power=True,
                extra={"target_temp": 24.0, "mode": "cool"},
            ),
        ),
    }
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="entry"),
            activity="arriving",
        )
    }
    world.agents = {
        "lighting_agent": AgentRuntimeState(id="lighting_agent", name="Lighting Agent"),
        "hvac_agent": AgentRuntimeState(id="hvac_agent", name="HVAC Agent"),
    }
    return world


def _make_engine(provider: LLMProvider) -> SimulationEngine:
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(_make_world()),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


def _make_default_world_engine(provider: LLMProvider) -> SimulationEngine:
    """带**默认公寓世界**的引擎：库场景的 initial_state 引用的是这套注册表。

    上面那个 ``_make_world()`` 只有一间房两台设备，摆不下一份真实场景的起始状态；
    凡是要走 ``reset(scenario=...)`` 的用例都从这里造引擎。
    """

    from backend.main import _init_default_state

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


def _root_event(suffix: str) -> SimEvent:
    return SimEvent(
        event_id=f"root-{suffix}",
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=5.0,
        wall_time=5.0,
        correlation_id=f"corr-{suffix}",
        priority=2,
        data={
            "user_id": "user_01",
            "from_room": "entry",
            "to_room": "living_room",
            "activity": "watching_tv",
        },
    )


def _sim_events_from(engine: SimulationEngine) -> list[dict]:
    return [
        call.args[0].payload
        for call in engine.conn.broadcast.call_args_list
        if call.args[0].type == "SIM_EVENT"
    ]


async def _park_episode(
    engine: SimulationEngine, provider: GatedProvider, root_event: SimEvent
) -> asyncio.Task:
    """Publish the root event and wait until the episode is parked on the LLM gate."""
    await engine._publish_sim_event(root_event)
    await asyncio.wait_for(provider.entered.wait(), timeout=1.0)
    # 给并发的第二个 agent 评估任务一个调度间隙，让它同样挂上 gate。
    await asyncio.sleep(0.02)
    tasks = [task for task in engine.agent_runtime._background_tasks if not task.done()]
    assert len(tasks) == 1, "expected exactly one in-flight episode task"
    return tasks[0]


async def _drain_tasks(engine: SimulationEngine) -> None:
    for _ in range(300):
        pending = [
            task
            for task in engine.agent_runtime._background_tasks
            if not task.done()
        ]
        if not pending:
            await asyncio.sleep(0.02)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("agent tasks did not settle in time")


def _mutation_snapshot(state_manager: StateManager) -> dict:
    return state_manager.world.model_dump(include={"devices", "rooms"})


@pytest.mark.anyio
async def test_reset_cancels_inflight_episode():
    provider = GatedProvider()
    engine = _make_engine(provider)
    stale_task = await _park_episode(engine, provider, _root_event("stale"))

    new_state_manager = StateManager(_make_world())
    await engine.reset(new_state_manager=new_state_manager)
    before = _mutation_snapshot(new_state_manager)

    provider.gate.set()
    await _drain_tasks(engine)

    # 旧 episode 的命令绝不能落进重置后的新世界。
    assert _mutation_snapshot(new_state_manager) == before
    light = new_state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 0
    assert light.state.last_changed_by == "system"  # 模型默认值，未被任何 agent 改写

    assert stale_task.cancelled()
    assert engine.agent_runtime._active_tasks == {}
    assert engine.agent_runtime._background_tasks == set()


@pytest.mark.anyio
async def test_reset_emits_episode_cancelled_event():
    provider = GatedProvider()
    engine = _make_engine(provider)
    root_event = _root_event("cancelled")
    await _park_episode(engine, provider, root_event)
    cancelled_run_id = engine.run_id

    await engine.reset(new_state_manager=StateManager(_make_world()))

    cancelled = [
        event
        for event in _sim_events_from(engine)
        if event["event_type"] == "system.episode_cancelled"
    ]
    assert len(cancelled) == 1
    event = cancelled[0]
    assert event["correlation_id"] == root_event.correlation_id
    assert event["data"]["correlation_id"] == root_event.correlation_id
    assert event["data"]["reason"] == "simulation_reset"
    assert set(event["data"]["agent_ids"]) == {"lighting_agent", "hvac_agent"}
    # 取消发生在换 run 之前：这条收尾事件仍归旧 run，且 data 里指名被取消的是哪个 run
    # （reset 会清空事件历史，此后它是唯一还能指认旧 run 的线索）。
    assert event["data"]["run_id"] == cancelled_run_id
    assert event["run_id"] == cancelled_run_id
    assert engine.run_id != cancelled_run_id


@pytest.mark.anyio
async def test_reset_leaves_agents_idle():
    provider = GatedProvider()
    engine = _make_engine(provider)
    await _park_episode(engine, provider, _root_event("thinking"))

    # 原地 reset：同一个世界继续用，卡在 thinking 的 agent 必须回到 idle。
    await engine.reset()

    for agent in engine.state_manager.world.agents.values():
        assert agent.status == "idle"
        assert agent.active_correlation_id is None

    # reset 后新的根事件要能开启一条干净的 episode。
    provider.gate.set()
    fresh_event = _root_event("fresh")
    await engine._publish_sim_event(fresh_event)
    await _drain_tasks(engine)

    light = engine.state_manager.world.devices["light_living_01"]
    assert light.state.extra["brightness"] == 70

    events = _sim_events_from(engine)
    fresh_actions = [
        event
        for event in events
        if event["event_type"] == "action.device_control"
        and event["correlation_id"] == fresh_event.correlation_id
    ]
    assert len(fresh_actions) == 1
    for agent in engine.state_manager.world.agents.values():
        assert agent.status == "idle"


@pytest.mark.anyio
async def test_reset_without_inflight_is_noop():
    provider = GatedProvider()
    engine = _make_engine(provider)
    engine.state_manager.world.simulation_tick = 42
    engine.state_manager.world.environment.time_of_day = "18:30"
    engine.timer.current_tick = 42

    await engine.reset()

    assert engine.state_manager.world.simulation_tick == 0
    assert engine.state_manager.world.environment.time_of_day == "12:00"
    assert engine.timer.current_tick == 0

    event_types = [event["event_type"] for event in _sim_events_from(engine)]
    assert "system.episode_cancelled" not in event_types
    assert "system.simulation_reset" in event_types


# ---------------------------------------------------------------------------
# S2-T3：run 层（§11 run 模型 + §2.2 旧 run 变更不得落进活跃 run）
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_old_run_delta_applied_to_new_world():
    """门测试：episode 没被取消、但 run 换了——它的命令依然一条都不许落地。

    这里刻意**不**调 reset：直接换 run，模拟场景连跑 / S2-T6 runner 那条不经
    cancel_active_episodes 的换 run 路径。只靠"取消在飞任务"防不住它。
    """

    provider = GatedProvider()
    engine = _make_engine(provider)
    stale_run_id = engine.run_id
    assert stale_run_id is not None

    root_event = _root_event("stale-run")
    await _park_episode(engine, provider, root_event)
    before = _mutation_snapshot(engine.state_manager)

    new_run = engine.run_manager.start_run(world=engine.state_manager.world)
    assert new_run.run_id != stale_run_id

    provider.gate.set()
    await _drain_tasks(engine)

    assert _mutation_snapshot(engine.state_manager) == before
    assert engine.state_manager.world.devices["light_living_01"].state.extra["brightness"] == 0

    discarded = [
        event
        for event in _sim_events_from(engine)
        if event["event_type"] == STALE_RUN_DISCARD_EVENT_TYPE
    ]
    assert len(discarded) == 1
    data = discarded[0]["data"]
    assert data["reason"] == STALE_RUN_DISCARD_REASON
    assert data["stale_run_id"] == stale_run_id
    assert data["active_run_id"] == new_run.run_id
    assert data["correlation_id"] == root_event.correlation_id
    assert set(data["agent_ids"]) == {"lighting_agent", "hvac_agent"}
    # 丢弃事件本身属于活跃 run（它发生在活跃 run 里），否则新 run 的事件流里
    # 根本看不到"上一 run 有东西被扔掉了"。
    assert discarded[0]["run_id"] == new_run.run_id

    # 被丢弃的 episode 不得把 agent 永久留在 thinking。
    for agent in engine.state_manager.world.agents.values():
        assert agent.status == "idle"
        assert agent.active_correlation_id is None


def test_runtime_without_run_model_never_judges_stale():
    """没接 RunManager 的 runtime（独立构造 / 老单测）不得因此开始丢命令。"""

    from backend.agents.runtime import AgentRuntime

    runtime = AgentRuntime(llm_provider=GatedProvider())
    assert runtime.current_run_id() is None
    assert runtime._is_stale_run(None) is False

    active = {"run_id": "run-a"}
    runtime.run_id_source = lambda: active["run_id"]
    assert runtime._is_stale_run("run-a") is False
    active["run_id"] = "run-b"
    assert runtime._is_stale_run("run-a") is True


@pytest.mark.anyio
async def test_event_bus_history_empty_of_old_run_after_reset():
    provider = GatedProvider()
    engine = _make_engine(provider)
    old_run_id = engine.run_id
    assert old_run_id is not None

    await engine._publish_sim_event(
        SimEvent(event_type="observability.note", source="test", timestamp=0.0)
    )
    assert [event.run_id for event in engine.event_bus.get_history()] == [old_run_id]

    await engine.reset(new_state_manager=StateManager(_make_world()))

    new_run_id = engine.run_id
    assert new_run_id is not None and new_run_id != old_run_id
    # 审计发现③：1000 条环形历史 reset 从不清空 → get_causal_chain 跨 run 穿插。
    assert engine.event_bus.get_history(run_id=old_run_id) == []
    assert [event.event_type for event in engine.event_bus.get_history()] == [
        "system.simulation_reset"
    ]
    assert engine.event_bus.get_history()[0].run_id == new_run_id
    assert engine.event_bus.next_seq == 1


@pytest.mark.anyio
async def test_reset_starts_new_run_with_spec11_metadata():
    """§11 九字段 + provenance：scenario_id 必须是一个真被加载、真被应用的场景。

    这个用例原来传的是 ``"arrive_home_evening"``——那是**文件名**，库里的 id 是
    ``user_arrives_home_evening``，而它照样通过，正是 S2 review major-2 抓到的那个洞。
    """

    from backend.main import _init_default_state

    provider = GatedProvider()
    engine = _make_default_world_engine(provider)
    first = engine.run_manager.current
    assert first is not None

    spec = get_scenario(LIBRARY_SCENARIO_ID)
    assert spec is not None
    new_state_manager = _init_default_state()
    await engine.reset(new_state_manager=new_state_manager, scenario=LIBRARY_SCENARIO_ID)

    current = engine.run_manager.current
    assert current is not None
    assert current.run_id != first.run_id
    assert current.scenario_id == LIBRARY_SCENARIO_ID
    assert current.seed == spec.seed  # 没显式给 seed 时取场景声明的那个
    assert current.initial_state_hash == compute_initial_state_hash(new_state_manager.world)
    assert first.ended_at is not None
    # run 上下文换过之后，事件盖的是新 run 的章（§4.5 元数据锚在 run 上）。
    assert engine.event_bus.run_id == current.run_id
    assert engine.event_bus.scenario_id == LIBRARY_SCENARIO_ID

    # 元数据说自己是这个场景，世界就必须真的是这个场景摆出来的：
    # initial_state_hash 是对**应用之后**的世界取的，两者不能各说各话。
    world = new_state_manager.world
    assert world.environment.time_of_day == spec.initial_state.time_of_day
    assert world.environment.weather == spec.initial_state.weather
    assert world.users["user_01"].location is None  # 场景声明 presence_state: away
    assert world.rooms["living_room"].persons == []


@pytest.mark.anyio
async def test_reset_rejects_unknown_scenario_id():
    """未知 scenario_id 必须结构化拒绝——一份工件宁可没有场景标签，也不能撒谎。"""

    provider = GatedProvider()
    engine = _make_default_world_engine(provider)
    run_id_before = engine.run_id
    time_of_day_before = engine.state_manager.world.environment.time_of_day

    with pytest.raises(RunProvenanceError) as excinfo:
        # 文件名不是场景 id：review 里正是这个字符串静默进了 run.json。
        await engine.reset(scenario="arrive_home_evening")

    error = excinfo.value
    assert error.code is RunProvenanceErrorCode.SCENARIO_NOT_FOUND
    assert LIBRARY_SCENARIO_ID in error.details["known_ids"]
    assert error.to_dict()["code"] == "scenario_not_found"

    # 拒绝发生在动世界之前：run 没换，世界没动。
    assert engine.run_id == run_id_before
    assert engine.state_manager.world.environment.time_of_day == time_of_day_before


@pytest.mark.anyio
async def test_reset_to_scenario_cancels_inflight_and_installs_generation_sources():
    """场景 run 中途换实验：旧 episode 的命令一条不许落地，新 run 装上三条产线。

    S2 手动门 1 的引擎侧对应物（HTTP/WS 侧见 tests/test_scenario_launch.py）。
    刻意复用本文件既有的 GatedProvider 机制，不另起一套在飞窗口。
    """

    from backend.main import _init_default_state

    provider = GatedProvider()
    engine = _make_default_world_engine(provider)
    stale_run_id = engine.run_id

    stale_task = await _park_episode(engine, provider, _root_event("mid-run"))

    new_state_manager = _init_default_state()
    await engine.reset(
        new_state_manager=new_state_manager, scenario=LIBRARY_SCENARIO_ID, seed=4242
    )
    before = _mutation_snapshot(new_state_manager)

    provider.gate.set()
    await _drain_tasks(engine)

    assert stale_task.cancelled()
    assert _mutation_snapshot(new_state_manager) == before
    # 场景把这盏灯摆成 power=false/brightness=0；旧 episode 想写 70。
    assert new_state_manager.world.devices["light_living_01"].state.extra["brightness"] == 0

    assert engine.run_id != stale_run_id
    assert engine.run_manager.current is not None
    assert engine.run_manager.current.seed == 4242
    sources = engine.generation_sources
    assert sources is not None, "reset(scenario=...) 必须把 §4.5 三条产线装上"
    assert sources.context.run_id == engine.run_id
    assert sources.context.scenario_id == LIBRARY_SCENARIO_ID


@pytest.mark.anyio
async def test_anonymous_reset_drops_scenario_generation_sources():
    """匿名 reset（前端那颗 Reset 按钮）＝换实验：上一个场景的产线必须摘掉。

    留着它，新 run 会继续按旧场景的 timeline 发事件，而 run.json 里 scenario_id 是空的。
    """

    from backend.main import _init_default_state

    engine = _make_default_world_engine(GatedProvider())
    await engine.reset(new_state_manager=_init_default_state(), scenario=LIBRARY_SCENARIO_ID)
    assert engine.generation_sources is not None

    await engine.reset(new_state_manager=_init_default_state())

    assert engine.generation_sources is None
    assert engine.scenario_id is None
    assert engine.run_manager.current is not None
    assert engine.run_manager.current.scenario_id is None
