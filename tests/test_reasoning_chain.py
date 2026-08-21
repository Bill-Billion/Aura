"""S3-T6：§4.3 六环推理事件对齐 + §4.4 因果深度上限。

这份测试盯死两件事：

1. **一条根事件 = 一棵 episode 树**（§4.4 + §15 验收 4）。
   审计 §六记下的症状是"episode 被稀释"：编排器有自己的感知/意图/任务拆分三环，
   每个域 agent 又各发一套同名事件，于是一条 correlation 下挂着 N 棵平行小树，
   "六环"看起来齐了，实际上没有一棵树能读出「感知 → 意图 → 拆分 → 协调 → 执行 →
   反馈」这条完整链路。编排器接管前三环之后，域 agent 的贡献落在
   ``reasoning.execution_plan``（每 agent 一条）与那唯一一条
   ``reasoning.coordination_decision`` 里——**信息不丢，树只有一棵**。

2. **事件风暴要有上限，而且上限触发时必须可见**（演进评审风险项）。
   tick 循环被事件驱动取代之后，天然的限速也一起没了：一条反馈回环可以无限自我派生。
   总线因此按 ``causal_parent`` 记深度并在 ``AGENT_MAX_CAUSAL_DEPTH`` 处刹车——
   刹车不是静默丢弃，而是发一条 ``system.event_storm_suppressed``（每条 correlation
   只发一条，否则"防风暴"本身就是新的风暴）。
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock

import pytest

from backend.agents.orchestrator import DEFAULT_ORCHESTRATOR_ID
from backend.agents.runtime import DisabledLLMProvider
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import (
    DEFAULT_MAX_CAUSAL_DEPTH,
    EVENT_STORM_SUPPRESSED_EVENT_TYPE,
    MAX_CAUSAL_DEPTH_ENV,
    EventBus,
    SimEvent,
)
from backend.engine.simulation import SimulationEngine
from backend.main import _init_default_state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------- 夹具


def _engine(provider=None):
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=provider or DisabledLLMProvider(),
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


async def _run_one_episode(engine, root: SimEvent) -> list[SimEvent]:
    await engine.event_bus.publish(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()
    return sorted(
        [e for e in engine.event_bus.get_history() if e.correlation_id == root.correlation_id],
        key=lambda e: e.seq if e.seq is not None else -1,
    )


def _arrives_home() -> SimEvent:
    return SimEvent(
        event_type="user.arrives_home",
        source="test",
        timestamp=1.0,
        data={"user_id": "user_01", "room_id": "living_room", "to_room": "living_room"},
    )


def _tree_depth(events: list[SimEvent], event: SimEvent) -> int:
    by_id = {item.event_id: item for item in events}
    depth = 0
    parent = event.causal_parent
    while parent in by_id:
        depth += 1
        parent = by_id[parent].causal_parent
    return depth


# ------------------------------------------------- 1. 一条根事件 = 一棵 episode 树


async def test_single_episode_tree_per_root_event(monkeypatch):
    """一条根事件下：感知 / 意图 / 任务拆分 / 协调决策各恰好一条，无论几个域 agent 参与。"""

    engine = _engine()
    root = _arrives_home()
    events = await _run_one_episode(engine, root)

    counts: dict[str, int] = {}
    for event in events:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1

    assert counts["reasoning.perception_snapshot"] == 1
    assert counts["reasoning.intent_recognized"] == 1
    assert counts["reasoning.task_decomposition"] == 1
    assert counts["reasoning.coordination_decision"] == 1
    # 执行计划仍然是 per-agent：域 agent 的贡献落在这一环上（而不是各自另起一棵树）。
    assert counts["reasoning.execution_plan"] >= 2

    # 前三环全部由编排器发出，域 agent 一条都不发（它们不再拥有自己的感知/意图/拆分）。
    for event in events:
        if event.event_type in {
            "reasoning.perception_snapshot",
            "reasoning.intent_recognized",
            "reasoning.task_decomposition",
        }:
            assert event.source == DEFAULT_ORCHESTRATOR_ID

    # 树是连通的：除根事件外，每条事件的 causal_parent 都能在同一条 correlation 内找到。
    by_id = {event.event_id: event for event in events}
    for event in events:
        if event.event_id == root.event_id:
            continue
        assert event.causal_parent in by_id, f"{event.event_type} 的父事件不在本 episode 内"


async def test_six_rings_present_under_one_correlation_id(monkeypatch):
    """§15 验收 4：一条 correlation 下六环齐全，且顺着 causal_parent 能一路串起来。"""

    engine = _engine()
    root = _arrives_home()
    events = await _run_one_episode(engine, root)
    types = {event.event_type for event in events}

    for ring in (
        "reasoning.perception_snapshot",
        "reasoning.intent_recognized",
        "reasoning.task_decomposition",
        "reasoning.coordination_decision",
        "reasoning.execution_plan",
        "action.device_control",
        "feedback.state_delta",
    ):
        assert ring in types, f"缺环：{ring}"

    by_id = {event.event_id: event for event in events}

    def ancestors(event: SimEvent) -> list[str]:
        chain: list[str] = []
        parent = event.causal_parent
        while parent in by_id:
            chain.append(by_id[parent].event_type)
            parent = by_id[parent].causal_parent
        return chain

    feedback = next(e for e in events if e.event_type == "feedback.state_delta")
    lineage = set(ancestors(feedback))
    assert {
        "action.device_control",
        "reasoning.execution_plan",
        "reasoning.coordination_decision",
        "reasoning.task_decomposition",
        "reasoning.intent_recognized",
        "reasoning.perception_snapshot",
    } <= lineage, "反馈事件必须能沿 causal_parent 追到六环的每一环"


async def test_execution_plan_carries_domain_agent_reasoning_contribution(monkeypatch):
    """域 agent 不再自己开树，但它的感知/意图/置信度不能因此消失。"""

    engine = _engine()
    events = await _run_one_episode(engine, _arrives_home())

    plans = [e for e in events if e.event_type == "reasoning.execution_plan"]
    assert plans
    for plan in plans:
        data = plan.data
        assert data["agent_id"]
        assert data["agent_role"]
        assert data["intent"]
        assert isinstance(data["confidence"], float)
        assert "explanation" in data
        assert isinstance(data["task_steps"], list)
        perception = data["perception"]
        assert perception["world_summary"]
        assert isinstance(perception["relevant_devices"], list)
        assert isinstance(perception["relevant_rooms"], list)
        # 编排器派给它的那件事也要在场：没有它就没法回答"这条执行计划是谁点的名"。
        assert "domain_task" in data


async def test_task_decomposition_data_is_structured_domain_tasks_not_free_text(monkeypatch):
    """审计坑：task_decomposition 里装的是 LLM 散文。事件流上必须是编排契约。"""

    engine = _engine()
    events = await _run_one_episode(engine, _arrives_home())

    decompositions = [e for e in events if e.event_type == "reasoning.task_decomposition"]
    assert len(decompositions) == 1
    data = decompositions[0].data

    assert isinstance(data["domain_tasks"], list) and data["domain_tasks"]
    for task in data["domain_tasks"]:
        assert set(task) >= {
            "agent_role",
            "task",
            "relevant_device_ids",
            "relevant_room_ids",
            "priority",
        }
        assert isinstance(task["relevant_device_ids"], list)
    assert data["agent_ids"] and data["agent_roles"]

    # event schema 1.0 persisted-run compatibility.
    assert len(data["task_steps"]) == len(data["domain_tasks"])
    for step, task in zip(data["task_steps"], data["domain_tasks"], strict=True):
        assert step.startswith(f"{task['agent_role']}: ")

# ----------------------------------------------------------- 2. §15 事件顺序


async def test_event_ordering_root_before_reasoning_before_action_before_feedback(monkeypatch):
    """§4.4 / §15：根事件先于推理，推理先于动作，动作先于反馈——按发布序断言。"""

    engine = _engine()
    root = _arrives_home()
    events = await _run_one_episode(engine, root)

    seq_of = {event.event_id: event.seq for event in events}
    assert events[0].event_id == root.event_id, "根事件必须是本 correlation 的第一条"

    first_reasoning = min(e.seq for e in events if e.event_type.startswith("reasoning."))
    first_action = min(e.seq for e in events if e.event_type == "action.device_control")
    first_feedback = min(e.seq for e in events if e.event_type == "feedback.state_delta")

    assert seq_of[root.event_id] < first_reasoning < first_action < first_feedback

    # 逐条断言"父先于子"：这条比"类别先后"更强，也是 S5 因果树按 seq 排序的前提。
    by_id = {event.event_id: event for event in events}
    for event in events:
        parent = by_id.get(event.causal_parent) if event.causal_parent else None
        if parent is not None:
            assert parent.seq < event.seq, f"{event.event_type} 排在了它的父事件之前"

    # 每一条 feedback 都能追到一条 action（§4.4「状态变更必须归因于先前的动作」）。
    for feedback in [e for e in events if e.event_type == "feedback.state_delta"]:
        chain_types = []
        parent = feedback.causal_parent
        while parent in by_id:
            chain_types.append(by_id[parent].event_type)
            parent = by_id[parent].causal_parent
        assert "action.device_control" in chain_types


# ------------------------------------------------------------- 3. 因果深度上限


async def test_depth_is_stamped_from_causal_parent():
    bus = EventBus()
    root = SimEvent(event_type="user.command", source="test", timestamp=0.0)
    await bus.publish(root)
    assert root.depth == 0

    parent_id = root.event_id
    for expected in range(1, 5):
        child = SimEvent(
            event_type="reasoning.intent_recognized",
            source="test",
            timestamp=0.0,
            correlation_id=root.correlation_id,
            causal_parent=parent_id,
        )
        await bus.publish(child)
        assert child.depth == expected
        parent_id = child.event_id


async def test_depth_cap_suppresses_runaway_chain():
    """20 层的失控自派生链：第 17 层被拒，并发一条（且只发一条）风暴抑制事件。"""

    bus = EventBus()
    assert bus.max_causal_depth == DEFAULT_MAX_CAUSAL_DEPTH == 16

    root = SimEvent(event_type="user.command", source="test", timestamp=0.0)
    await bus.publish(root)

    parent_id = root.event_id
    refused: list[SimEvent] = []
    for _ in range(20):
        child = SimEvent(
            event_type="feedback.state_delta",
            source="runaway",
            timestamp=0.0,
            correlation_id=root.correlation_id,
            causal_parent=parent_id,
        )
        handlers = await bus.publish(child)
        if child.event_id not in {e.event_id for e in bus.get_history()}:
            refused.append(child)
        else:
            parent_id = child.event_id
        del handlers

    history = bus.get_history(correlation_id=root.correlation_id)
    chain = [e for e in history if e.source == "runaway"]
    # 落地的最深一条正好在上限上，再深的全部被拒。
    assert max(e.depth for e in chain) == DEFAULT_MAX_CAUSAL_DEPTH
    assert len(chain) == DEFAULT_MAX_CAUSAL_DEPTH
    assert len(refused) == 20 - DEFAULT_MAX_CAUSAL_DEPTH

    # 抑制不是静默丢弃：事件流上留下一条可见记录，且每条 correlation 只留一条。
    storms = [e for e in history if e.event_type == EVENT_STORM_SUPPRESSED_EVENT_TYPE]
    assert len(storms) == 1
    storm = storms[0]
    assert storm.correlation_id == root.correlation_id
    assert storm.data["reason"] == "causal_depth_exceeded"
    assert storm.data["max_depth"] == DEFAULT_MAX_CAUSAL_DEPTH
    assert storm.data["depth"] == DEFAULT_MAX_CAUSAL_DEPTH + 1
    assert storm.data["suppressed_event_type"] == "feedback.state_delta"
    assert storm.data["suppressed_count"] >= 1


async def test_depth_cap_is_per_correlation():
    """另一条 correlation 有自己的额度：一次风暴不该让全世界闭嘴。"""

    bus = EventBus(max_causal_depth=2)

    ids: list[str] = []
    for _ in range(2):
        root = SimEvent(event_type="user.command", source="test", timestamp=0.0)
        await bus.publish(root)
        parent_id = root.event_id
        for _ in range(5):
            child = SimEvent(
                event_type="feedback.state_delta",
                source="runaway",
                timestamp=0.0,
                correlation_id=root.correlation_id,
                causal_parent=parent_id,
            )
            await bus.publish(child)
            if child.event_id in {e.event_id for e in bus.get_history()}:
                parent_id = child.event_id
        ids.append(root.correlation_id)

    for correlation_id in ids:
        storms = [
            e
            for e in bus.get_history(correlation_id=correlation_id)
            if e.event_type == EVENT_STORM_SUPPRESSED_EVENT_TYPE
        ]
        assert len(storms) == 1


async def test_depth_cap_can_be_disabled(monkeypatch):
    monkeypatch.setenv(MAX_CAUSAL_DEPTH_ENV, "0")
    bus = EventBus()
    assert bus.max_causal_depth == 0

    root = SimEvent(event_type="user.command", source="test", timestamp=0.0)
    await bus.publish(root)
    parent_id = root.event_id
    for _ in range(30):
        child = SimEvent(
            event_type="feedback.state_delta",
            source="runaway",
            timestamp=0.0,
            correlation_id=root.correlation_id,
            causal_parent=parent_id,
        )
        await bus.publish(child)
        parent_id = child.event_id

    history = bus.get_history(correlation_id=root.correlation_id)
    assert not [e for e in history if e.event_type == EVENT_STORM_SUPPRESSED_EVENT_TYPE]
    assert max(e.depth for e in history) == 30


async def test_real_episode_stays_far_below_the_depth_cap(monkeypatch):
    """真实 episode 的树深必须离上限有余量，否则这道闸会误伤正常链路。"""

    engine = _engine()
    events = await _run_one_episode(engine, _arrives_home())

    assert not [e for e in events if e.event_type == EVENT_STORM_SUPPRESSED_EVENT_TYPE]
    deepest = max(_tree_depth(events, event) for event in events)
    assert deepest <= DEFAULT_MAX_CAUSAL_DEPTH // 2
    # 总线盖的 depth 与按 causal_parent 现算的树深一致。
    for event in events:
        assert event.depth == _tree_depth(events, event)


def test_depth_cap_env_is_read_at_construction(monkeypatch):
    monkeypatch.setenv(MAX_CAUSAL_DEPTH_ENV, "5")
    assert EventBus().max_causal_depth == 5
    monkeypatch.delenv(MAX_CAUSAL_DEPTH_ENV, raising=False)
    assert EventBus().max_causal_depth == DEFAULT_MAX_CAUSAL_DEPTH
    assert os.getenv(MAX_CAUSAL_DEPTH_ENV) is None
