"""世界版本 + 陈旧决策丢弃（S3-T7，evolution-review 风险 #2）。

LLM 一轮决策要 1-5 秒，世界不会停下来等它。等决策回来时，它据以推理的那台设备可能
已经被别人（用户直控 / 另一个 agent / 仿真器）改过了——此时把命令照发下去，就是拿一份
过期世界去改现在的世界，而事后没有任何记录说明这次改动的前提早已不成立。

本文件断三件事：

1. 命中：快照之后被改过的设备，其命令必须被丢弃，且**零状态改动**；
2. 留痕：丢弃要发 ``reasoning.decision_discarded``（reason=stale），带上
   decided_at_version / current_version / discarded_commands；
3. 粒度：per-device，而不是全局计数器。环境每 tick 都在动，用全局版本判陈旧等于
   把所有决策都判死（plan_raw 明写的粒度要求）。
"""

from __future__ import annotations

import copy

import pytest

from backend.agents.llm_modes import (
    STALE_DECISION_EVENT_TYPE,
    STALE_DECISION_REASON,
    StaleDecisionCheck,
    VersionedDecision,
    WorldVersionTracker,
    build_stale_decision_event,
    check_stale_decision,
)
from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent
from backend.main import _init_default_state

LIGHT_ID = "light_living_01"


def _commands() -> list[AgentCommandProposal]:
    return [
        AgentCommandProposal(
            device_id=LIGHT_ID,
            property="extra.brightness",
            value=70,
            reason="user arrived home",
        )
    ]


def _root_event() -> SimEvent:
    return SimEvent(
        event_id="root-stale",
        event_type="user.arrives_home",
        source="user_behavior_sim",
        timestamp=12.0,
        correlation_id="corr-stale",
        data={"user_id": "user_01"},
    )


def test_tracker_tracks_global_and_per_device_versions():
    state_manager = _init_default_state()
    tracker = WorldVersionTracker.attach(state_manager)

    assert tracker.version == 0
    assert tracker.device_version(LIGHT_ID) == 0

    state_manager.apply_action("lighting_agent", LIGHT_ID, "extra.brightness", 42)
    assert tracker.version > 0
    assert tracker.device_version(LIGHT_ID) == tracker.version

    # 幂等：重复 attach 不会套两层 wrapper（否则版本号每次改动跳两格）
    assert WorldVersionTracker.attach(state_manager) is tracker
    before = tracker.version
    state_manager.apply_path_update("environment_sim", "environment.outdoor_temp", 31.5)
    assert tracker.version == before + 1

    # 相同值写入不产生 delta，也就不该动版本
    unchanged = tracker.version
    state_manager.apply_path_update("environment_sim", "environment.outdoor_temp", 31.5)
    assert tracker.version == unchanged


def test_device_mutated_between_snapshot_and_apply_discards_with_stale_event_and_no_mutation():
    state_manager = _init_default_state()
    tracker = WorldVersionTracker.attach(state_manager)

    decision = VersionedDecision.snapshot(
        tracker,
        agent_id="lighting_agent",
        commands=_commands(),
        correlation_id="corr-stale",
        root_event_id="root-stale",
    )
    assert decision.decided_at_version == tracker.version
    assert decision.device_versions[LIGHT_ID] == 0

    # 决策在飞期间用户直接把灯改了
    state_manager.apply_action("user_direct", LIGHT_ID, "power", False, reason="user override")

    world_before = copy.deepcopy(state_manager.world.model_dump())
    check = check_stale_decision(decision, tracker)
    assert isinstance(check, StaleDecisionCheck)
    assert check.is_stale is True
    assert check.fresh_commands == []
    assert [c.device_id for c in check.discarded_commands] == [LIGHT_ID]
    assert check.stale_device_ids == [LIGHT_ID]
    assert check.decided_at_version == decision.decided_at_version
    assert check.current_version == tracker.version

    # 判定本身零状态改动
    assert state_manager.world.model_dump() == world_before

    event = build_stale_decision_event(decision, check, root_event=_root_event())
    assert event.event_type == STALE_DECISION_EVENT_TYPE
    assert event.correlation_id == "corr-stale"
    assert event.causal_parent == "root-stale"
    assert event.data["reason"] == STALE_DECISION_REASON
    assert event.data["agent_id"] == "lighting_agent"
    assert event.data["decided_at_version"] == decision.decided_at_version
    assert event.data["current_version"] == tracker.version
    assert event.data["stale_device_ids"] == [LIGHT_ID]
    assert event.data["discarded_commands"] == [
        {
            "device_id": LIGHT_ID,
            "property": "extra.brightness",
            "value": 70,
            "reason": "user arrived home",
        }
    ]


def test_unrelated_env_change_does_not_discard():
    state_manager = _init_default_state()
    tracker = WorldVersionTracker.attach(state_manager)

    decision = VersionedDecision.snapshot(
        tracker, agent_id="lighting_agent", commands=_commands()
    )

    # 与决策无关的环境 tick：全局版本推进，但那台灯没动
    state_manager.apply_path_update("environment_sim", "environment.outdoor_temp", 33.0)
    state_manager.apply_action("hvac_agent", "ac_living_01", "power", True)

    check = check_stale_decision(decision, tracker)
    assert tracker.version > decision.decided_at_version  # 全局确实变了
    assert check.is_stale is False
    assert check.discarded_commands == []
    assert [c.device_id for c in check.fresh_commands] == [LIGHT_ID]
    assert check.stale_device_ids == []

    with pytest.raises(ValueError):
        build_stale_decision_event(decision, check, root_event=_root_event())


def test_partial_discard_keeps_fresh_commands_in_order():
    state_manager = _init_default_state()
    tracker = WorldVersionTracker.attach(state_manager)

    commands = [
        AgentCommandProposal(device_id=LIGHT_ID, property="power", value=True, reason="a"),
        AgentCommandProposal(device_id="light_bedroom_01", property="power", value=True, reason="b"),
    ]
    decision = VersionedDecision.snapshot(tracker, agent_id="lighting_agent", commands=commands)

    state_manager.apply_action("user_direct", LIGHT_ID, "power", False)

    check = check_stale_decision(decision, tracker)
    assert check.is_stale is True
    assert [c.device_id for c in check.fresh_commands] == ["light_bedroom_01"]
    assert [c.device_id for c in check.discarded_commands] == [LIGHT_ID]


# ---------------------------------------------------------------------------
# 生产接线（S3 review major-3）
#
# 上面四条测的是这台机器本身。审计原文说得很直白：``WorldVersionTracker`` /
# ``check_stale_decision`` / ``build_stale_decision_event`` 在 backend/ 里零调用点，
# 于是 ``reasoning.decision_discarded`` 在跑起来的系统里**永远不可能出现**，而阶段门
# 那一条却是绿的。所以本节跑一条真 episode，断这件事出现在**事件流与世界状态**里。
# S4 的失败注入场景要注入的就是这条路径——它必须先存在，才谈得上可注入。
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import AsyncMock

from backend.agents.llm import LLMProvider
from backend.agents.types import AgentLLMDecision
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus
from backend.engine.simulation import SimulationEngine


class _GatedProvider(LLMProvider):
    """在决策中途停住的 provider：把"LLM 想了 1-5 秒"变成测试可控的一段窗口。

    ``provider_name``/无 api_key ⇒ run 元数据判 mocked，编排器不打网（§11.1），
    只有域 agent 会走到这里——正好是我们要卡住的那一段。
    """

    provider_name = "gated"
    model = "gated-test"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate_decision(self, request):  # type: ignore[override]
        self.entered.set()
        await self.release.wait()
        return AgentLLMDecision(
            intent="brighten the living room for the arriving user",
            confidence=0.9,
            task_steps=["set living room brightness"],
            proposed_commands=[
                AgentCommandProposal(
                    device_id=LIGHT_ID,
                    property="extra.brightness",
                    value=70,
                    reason="user just arrived home",
                )
            ],
            explanation="Evening arrival calls for brighter light.",
        )


def _gated_engine(provider: LLMProvider) -> SimulationEngine:
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    # 灯先开着：这样"agent 想调亮度"是一条本来会成功的命令，被丢弃才有意义。
    light = engine.state_manager.world.devices[LIGHT_ID]
    light.state.power = True
    light.state.extra["brightness"] = 40
    return engine


def _arrive_home_root_event() -> SimEvent:
    return SimEvent(
        event_type="user.arrives_home",
        source="test",
        timestamp=1.0,
        data={"user_id": "user_01", "to_room": "living_room"},
    )


@pytest.mark.anyio
async def test_mid_episode_device_change_really_emits_decision_discarded_and_mutates_nothing():
    """真 episode：决策在飞期间别人改了那台灯 ⇒ 命令被丢弃 + 事件留痕 + 零状态改动。"""

    provider = _GatedProvider()
    engine = _gated_engine(provider)
    root = _arrive_home_root_event()

    await engine.event_bus.publish(root)
    await asyncio.wait_for(provider.entered.wait(), timeout=10.0)

    # —— 决策在飞期间，用户直接把这台灯调到 5 ——
    engine.state_manager.apply_action(
        "user_direct", LIGHT_ID, "extra.brightness", 5, reason="user override"
    )
    provider.release.set()
    assert await engine.agent_runtime.wait_for_idle(timeout=20.0)

    history = engine.event_bus.get_history(correlation_id=root.correlation_id)
    discarded = [event for event in history if event.event_type == STALE_DECISION_EVENT_TYPE]
    assert discarded, (
        "决策据以推理的设备在飞期间被改过，却没有一条 reasoning.decision_discarded——"
        "陈旧决策丢弃没有装在真实决策路径上"
    )
    payload = discarded[0].data
    assert payload["reason"] == STALE_DECISION_REASON
    assert LIGHT_ID in payload["stale_device_ids"]
    assert payload["current_version"] > payload["decided_at_version"]
    assert [item["device_id"] for item in payload["discarded_commands"]] == [LIGHT_ID]
    # 因果链：丢弃挂在本 episode 下，不是一条无根的孤儿事件
    assert discarded[0].correlation_id == root.correlation_id
    assert discarded[0].causal_parent

    # 零状态改动：用户那一次直控是这台灯的最后一次写入
    assert engine.state_manager.world.devices[LIGHT_ID].state.extra["brightness"] == 5
    # 被丢弃的命令绝不能走到执行环
    device_controls = [
        event
        for event in history
        if event.event_type == "action.device_control"
        and event.data.get("device_id") == LIGHT_ID
    ]
    assert device_controls == []


@pytest.mark.anyio
async def test_without_a_mid_episode_change_the_same_episode_executes_and_discards_nothing():
    """阴性对照：没人动这台灯时不该有任何丢弃，命令照常落地。

    没有这一条，上面那条门可以被"永远丢弃"糊弄过去——而那等于把 agent 关掉。
    """

    provider = _GatedProvider()
    engine = _gated_engine(provider)
    root = _arrive_home_root_event()

    await engine.event_bus.publish(root)
    await asyncio.wait_for(provider.entered.wait(), timeout=10.0)
    provider.release.set()
    assert await engine.agent_runtime.wait_for_idle(timeout=20.0)

    history = engine.event_bus.get_history(correlation_id=root.correlation_id)
    assert [event for event in history if event.event_type == STALE_DECISION_EVENT_TYPE] == []
    assert engine.state_manager.world.devices[LIGHT_ID].state.extra["brightness"] == 70
