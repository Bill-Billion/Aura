"""S1 review2 finding-1：共用一台 executor 之后的并发取代语义。

finding-8 的修复让 UI 腿与 agent 腿共用引擎持有的那台 executor，``_pending`` 注册表
第一次有了真实生产寿命——「同一控制点的旧命令被新命令取代」不再只发生在单次
``submit_batch`` 内部，而会跨调用、跨腿地在**任意 await 期间**发生（每条命令的流水线里
至少有 4 次广播 await，agent episode 又是后台任务）。本文件锁住这条并发语义：

  1. 中途被取代是普通的、可观测的结局：绝不从 ``submit()`` 抛 IllegalTransitionError；
  2. apply 之前被取代 → 世界零变更、不发 action 之后的任何东西；
  3. apply 之后被取代 → 保留已落地的变更并照常发 feedback（**不回滚**：取代者写的是同一个
     控制点且通常已经落地，回滚等于用旧值覆盖一条更新的合法写入，比 timed_out 分支危险——
     那里 apply 与回滚之间没有任何 await，没有第二个写入方插得进来）；
  4. VALIDATED 的受害者也必须发 superseded 生命周期，不能被注册表静默丢弃；
  5. 被取消的提交不留幽灵记录（否则下一条命令会发一条它其实没经历过的 superseded）；
  6. agent episode 与 UI 命令撞同一控制点时：episode 不中断、不留未取回的任务异常、
     agent 不卡在 thinking。
"""

from __future__ import annotations

import asyncio
from typing import Callable

import pytest

from backend.engine.event_bus import SimEvent
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    UserState,
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
    FEEDBACK_EVENT_TYPE,
    CommandExecutor,
)

# agent 腿的真实引擎夹具复用 finding-8 的所有权测试，避免第二份 SimulationEngine 夹具漂移。
from tests.test_executor_ownership import StubProvider, _drain_tasks, _make_engine

# 所有等待都带超时：WS/事件驱动测试里漏发一条消息会挂死整条测试套，超时即失败。
WAIT_TIMEOUT = 2.0


def _make_world() -> WorldState:
    world = WorldState()
    world.rooms = {
        "living_room": RoomState(
            id="living_room", light_level=300.0, occupancy=True, persons=["user_01"]
        )
    }
    world.users = {
        "user_01": UserState(
            id="user_01", name="User", location=Location3D(room="living_room")
        )
    }
    world.devices = {
        "light_living": DeviceState(
            id="light_living",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(power=True, extra={"brightness": 80}),
        )
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
        capability="brightness",
        value=99,
        correlation_id="corr-race",
        causal_parent="root-race",
    )
    params.update(overrides)
    return DeviceCommand(**params)


class _GatedPublish:
    """事件外发包装：在第一个命中 gate 的事件上挂起，把事件循环让给另一条命令。

    这就是生产里真实存在的 await 点（每条事件都要 ``manager.broadcast`` 逐 socket 发送），
    只是把"什么时候被别的命令插队"变成确定性的，而不是靠 sleep 赌调度顺序。
    """

    def __init__(self, gate: Callable[[SimEvent], bool]) -> None:
        self.events: list[SimEvent] = []
        self._gate = gate
        self._armed = True
        self.reached = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, event: SimEvent) -> SimEvent:
        self.events.append(event)
        if self._armed and self._gate(event):
            self._armed = False
            self.reached.set()
            await self.release.wait()
        return event

    @property
    def lifecycle(self) -> list[str]:
        return [
            event.data["to_status"]
            for event in self.events
            if event.event_type == LIFECYCLE_EVENT_TYPE
        ]

    def of_type(self, event_type: str) -> list[SimEvent]:
        return [event for event in self.events if event.event_type == event_type]


def _on_action(event: SimEvent) -> bool:
    return event.event_type == ACTION_EVENT_TYPE


def _on_feedback(event: SimEvent) -> bool:
    return event.event_type == FEEDBACK_EVENT_TYPE


def _on_status(status: str) -> Callable[[SimEvent], bool]:
    def gate(event: SimEvent) -> bool:
        return (
            event.event_type == LIFECYCLE_EVENT_TYPE
            and event.data["to_status"] == status
        )

    return gate


async def _wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), WAIT_TIMEOUT)


# ---------------------------------------------------------------------------
# 1. 中途被取代绝不抛异常
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_supersede_before_apply_is_an_ordinary_outcome_not_an_exception():
    """卡在 action 广播上的命令被同控制点新命令取代 → superseded 收工，世界零变更。"""

    sm = StateManager(_make_world())
    ex = CommandExecutor(sm)
    gate = _GatedPublish(_on_action)

    victim_task = asyncio.create_task(ex.submit(_cmd(value=99), publish=gate))
    await _wait(gate.reached)
    # action 已发但 apply 还没跑：世界仍是初值。
    assert sm.world.devices["light_living"].state.extra["brightness"] == 80

    events, publish = _collector()
    winner = await ex.submit(_cmd(value=60), publish=publish)
    gate.release.set()
    victim = await asyncio.wait_for(victim_task, WAIT_TIMEOUT)

    assert winner.status is CommandStatus.SUCCEEDED
    assert victim.status is CommandStatus.SUPERSEDED
    assert victim.failure_code == "superseded_by_newer_command"
    # 被取代者按普通生命周期收场：有 superseded 事件，没有 succeeded。
    assert "superseded" in gate.lifecycle
    assert "succeeded" not in gate.lifecycle
    # apply 之前就出局 → 一条 feedback 都没有，世界只留取代者的值。
    assert gate.of_type(FEEDBACK_EVENT_TYPE) == []
    assert sm.world.devices["light_living"].state.extra["brightness"] == 60
    assert ex.pending == {}


@pytest.mark.anyio
async def test_supersede_after_apply_keeps_the_landed_change_and_still_reports_it():
    """apply 之后才被取代：保留已落地的变更 + 照发 feedback，**不回滚**。

    回滚在这里是错的：取代者写的是同一个控制点且已经落地，回滚会用受害者的 old_value(80)
    覆盖取代者刚写的 60。timed_out 分支能回滚，是因为它的 apply 与回滚之间没有 await，
    没有第二个写入方插得进来——两条分支的差别是有原则的，不是不一致。
    """

    sm = StateManager(_make_world())
    ex = CommandExecutor(sm)
    gate = _GatedPublish(_on_feedback)

    victim_task = asyncio.create_task(ex.submit(_cmd(value=99), publish=gate))
    await _wait(gate.reached)
    assert sm.world.devices["light_living"].state.extra["brightness"] == 99

    events, publish = _collector()
    winner = await ex.submit(_cmd(value=60), publish=publish)
    gate.release.set()
    victim = await asyncio.wait_for(victim_task, WAIT_TIMEOUT)

    assert winner.status is CommandStatus.SUCCEEDED
    assert victim.status is CommandStatus.SUPERSEDED
    # 已落地的变更如实上报：世界与事件流不分叉。
    assert gate.of_type(FEEDBACK_EVENT_TYPE)
    # 回滚会把这里变回 80——那才是把世界改坏。
    assert sm.world.devices["light_living"].state.extra["brightness"] == 60
    assert "succeeded" not in gate.lifecycle
    assert ex.pending == {}


@pytest.mark.anyio
async def test_validated_victim_is_superseded_observably_not_dropped_silently():
    """VALIDATED 的在飞命令被取代：必须发 superseded 生命周期，且此后不再下发。"""

    sm = StateManager(_make_world())
    ex = CommandExecutor(sm)
    gate = _GatedPublish(_on_status("validated"))

    victim_task = asyncio.create_task(ex.submit(_cmd(value=99), publish=gate))
    await _wait(gate.reached)

    events, publish = _collector()
    winner = await ex.submit(_cmd(value=60), publish=publish)
    gate.release.set()
    victim = await asyncio.wait_for(victim_task, WAIT_TIMEOUT)

    assert winner.status is CommandStatus.SUCCEEDED
    assert victim.status is CommandStatus.SUPERSEDED
    assert "superseded" in gate.lifecycle
    # 校验通过但尚未执行就被取代 → 绝不下发 action，世界只留取代者的值。
    assert gate.of_type(ACTION_EVENT_TYPE) == []
    assert sm.world.devices["light_living"].state.extra["brightness"] == 60
    assert ex.pending == {}


@pytest.mark.anyio
async def test_cancelled_submit_leaves_no_ghost_record_in_the_registry():
    """提交任务被取消（runtime 砍上一轮 episode）不能在共享注册表里留幽灵记录。"""

    sm = StateManager(_make_world())
    ex = CommandExecutor(sm)
    gate = _GatedPublish(_on_action)

    task = asyncio.create_task(ex.submit(_cmd(value=99), publish=gate))
    await _wait(gate.reached)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT_TIMEOUT)

    assert ex.pending == {}
    # 取消也要落账，而不是无声消失。
    assert "cancelled" in gate.lifecycle

    # 下一条同控制点命令干净：不会替一条其实被取消的命令发 superseded。
    events, publish = _collector()
    later = await ex.submit(_cmd(value=60), publish=publish)
    assert later.status is CommandStatus.SUCCEEDED
    assert "superseded" not in [
        event.data["to_status"]
        for event in events
        if event.event_type == LIFECYCLE_EVENT_TYPE
    ]


# ---------------------------------------------------------------------------
# 2. agent 腿：episode 不因并发取代中断
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ui_command_during_agent_episode_does_not_abort_the_episode():
    """reviewer 复现的现实场景：agent episode 在飞时，UI 点了同一台设备的同一条能力。

    旧行为：executor 抛 IllegalTransitionError 冲出 submit → episode 任务带着未取回的
    异常中断 → agent 卡在 thinking、pending_deltas 永不广播（S1 要根治的静默失败类）。
    """

    engine = _make_engine(StubProvider())
    runtime = engine.agent_runtime
    original_publish = runtime.publish_event
    assert original_publish is not None
    gate = _GatedPublish(_on_action)

    async def gated(event: SimEvent) -> SimEvent:
        published = await original_publish(event)
        return await gate(published)

    runtime.publish_event = gated

    await engine._publish_sim_event(
        SimEvent(
            event_id="root-race",
            event_type="user.activity_change",
            source="user_behavior_sim",
            timestamp=5.0,
            wall_time=5.0,
            correlation_id="corr-race",
            priority=2,
            data={
                "user_id": "user_01",
                "from_room": "entry",
                "to_room": "living_room",
                "activity": "watching_tv",
            },
        )
    )
    # agent 的 action.device_control 卡在 gate 上 = 命令在飞。
    await _wait(gate.reached)
    episode_tasks = [task for task in runtime._background_tasks if not task.done()]
    assert episode_tasks

    events, publish = _collector()
    ui_records = await engine.command_executor.submit_batch(
        [
            DeviceCommand(
                source=CommandSource.UI,
                device_id="light_living_01",
                capability="brightness",
                value=10,
                reason="ui overrides the agent mid-episode",
            )
        ],
        publish=publish,
    )
    gate.release.set()
    await _drain_tasks(engine)

    episode = episode_tasks[0]
    assert episode.done()
    assert not episode.cancelled()
    # agent 腿不留未取回的任务异常。
    assert episode.exception() is None
    # agent 没有卡在 thinking：episode 走完了收尾。
    assert engine.state_manager.world.agents["lighting_agent"].status == "idle"
    assert ui_records[0].status is CommandStatus.SUCCEEDED
    assert engine.state_manager.world.devices["light_living_01"].state.extra["brightness"] == 10
    assert engine.command_executor.pending == {}
