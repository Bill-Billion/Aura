"""S3-T5 集成门：仲裁门坐在 proposed 与 approved 之间，用户命令真的能压过 agent。

这份测试钉的是审计 §六里最难用单测证伪的那条坑：

    *USER COMMANDS NEVER ENTER THE ARBITER —— main.py 直接落地，runtime 又显式跳过
    CMD_DEVICE_CONTROL，所以"用户覆盖 agent"只是名义上的。*

要证明它被真正修好，需要三件事同时成立，而且每件都必须是**端到端可观测**的：

1. agent 命令在等仲裁（``proposed``）时是一条**真实在飞**的命令——它进了 executor 的
   在飞注册表，所以能被取代。S1 把取代范围写成"同批/待发队列内"，这里扩宽到仲裁窗口。
2. 用户命令经 S1 预留的 ``pre_submit`` 接缝进来，把它取代掉（``superseded`` 生命周期
   事件 + §10.2 的 ``superseded_by_newer_command`` 失败码），世界留用户的值。
3. 一条 episode 只发**一条** ``reasoning.coordination_decision``——旧实现每个 agent 各发
   一条，于是"全序仲裁"在事件流里根本没有落点。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from backend.agents.arbiter import (
    ARBITER_ID,
    COORDINATION_DECISION_EVENT_TYPE,
    UI_ACTOR_ID,
    Arbiter,
    ArbitrationGate,
    ConflictClass,
)
from backend.agents.contracts import AgentProposal, PriorityLevel
from backend.agents.llm import LLMProvider, LLMProviderError
from backend.agents.types import AgentCommandProposal
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.simulation import SimulationEngine
from backend.engine.state_manager import StateManager
from backend.execution.command import (
    CommandSource,
    CommandStatus,
    DeviceCommand,
)
from backend.execution.executor import CommandExecutor
from backend.main import _init_default_state


class _DisabledProvider(LLMProvider):
    """一律失败 → 全体 agent 走规则回退（确定、无网络）。"""

    provider_name = "disabled"
    model = "rule_based"

    async def generate_decision(self, request):  # type: ignore[override]
        raise LLMProviderError("provider_error", "LLM provider is disabled")


def _root_event(event_type: str, **data) -> SimEvent:
    return SimEvent(
        event_id=f"root-{event_type}",
        event_type=event_type,
        source="test",
        timestamp=3.0,
        wall_time=3.0,
        correlation_id=f"corr-{event_type}",
        priority=2,
        data=data,
    )


# ------------------------------------------- 用户命令取代仲裁窗口里的 agent 命令


@pytest.mark.anyio
async def test_user_ws_command_supersedes_inflight_agent_command_on_same_device():
    """agent 命令还在等仲裁批准时，用户直控把它取代掉——世界留用户的值。

    这条链路在 S1 里是断的：``proposed`` 记录不进在飞注册表，取代只在
    ``submit_batch`` 同批内成立，所以"仲裁窗口里的命令"根本没有被取代的可能。
    """

    state_manager = _init_default_state()
    executor = CommandExecutor(state_manager)
    gate = ArbitrationGate(executor=executor, arbiter=Arbiter())
    events: list[SimEvent] = []

    async def publish(event: SimEvent) -> SimEvent:
        events.append(event)
        return event

    light = state_manager.world.devices["light_living_01"]
    assert light.state.power is True

    # 1) agent 命令开仲裁窗口：出生成 proposed，并**进在飞注册表**。
    agent_record = await gate.open(
        DeviceCommand(
            source=CommandSource.AGENT,
            actor="lighting_agent",
            actor_name="Lighting Agent",
            device_id="light_living_01",
            capability="power",
            value=True,
            reason="agent 想保持开灯",
            correlation_id="corr-agent",
            issued_tick=0,
        ),
        publish=publish,
        tick=0,
    )
    assert agent_record.status is CommandStatus.PROPOSED
    assert ("light_living_01", "power") in executor.pending

    # 2) 用户直控经 S1 的 pre_submit 接缝进来。
    user_record = await executor.submit(
        DeviceCommand(
            source=CommandSource.UI,
            device_id="light_living_01",
            capability="power",
            value=False,
            reason="ui turn_off",
            correlation_id="corr-user",
            issued_tick=0,
        ),
        tick=0,
        publish=publish,
        pre_submit=gate.pre_submit,
    )

    assert user_record.status is CommandStatus.SUCCEEDED
    assert agent_record.status is CommandStatus.SUPERSEDED
    assert state_manager.world.devices["light_living_01"].state.power is False

    superseded = [
        event
        for event in events
        if event.event_type == "command.lifecycle" and event.data["to_status"] == "superseded"
    ]
    assert len(superseded) == 1
    assert superseded[0].data["from_status"] == "proposed"
    assert superseded[0].data["failure_code"] == "superseded_by_newer_command"
    assert superseded[0].data["agent_id"] == "lighting_agent"

    # 3) 仲裁窗口关闭后再执行那条 agent 命令：安静收工，绝不复活、绝不改世界。
    executed = await gate.execute(agent_record, publish=publish, tick=0)
    assert executed.status is CommandStatus.SUPERSEDED
    assert state_manager.world.devices["light_living_01"].state.power is False
    assert [event.event_type for event in events].count("action.device_control") == 1


@pytest.mark.anyio
async def test_user_claim_makes_a_concurrently_arbitrating_episode_lose():
    """用户占用被登记后，同控制点上并发 episode 的 agent 提案在仲裁里输。

    这就是"用户覆盖 agent"从名义变机制的读取端：占用写在 pre_submit 钩子里，
    读在 :meth:`Arbiter.resolve` 的 ``user_claims``。
    """

    state_manager = _init_default_state()
    executor = CommandExecutor(state_manager)
    gate = ArbitrationGate(executor=executor, arbiter=Arbiter())

    async def publish(event: SimEvent) -> SimEvent:
        return event

    episode_root = _root_event("user.activity_change")  # wall_time=3.0
    await executor.submit(
        DeviceCommand(
            source=CommandSource.UI,
            device_id="light_living_01",
            capability="brightness",
            value=10,
            reason="ui dim",
            correlation_id="corr-user",
            issued_tick=0,
        ),
        tick=0,
        publish=publish,
        pre_submit=gate.pre_submit,
    )

    claims = gate.claims_since(float(episode_root.wall_time))
    assert ("light_living_01", "brightness") in claims
    assert claims[("light_living_01", "brightness")].agent_id == UI_ACTOR_ID

    result = gate.arbiter.resolve(
        [
            AgentProposal(
                agent_id="lighting_agent",
                intent="raise brightness",
                priority=PriorityLevel.COMFORT,
                confidence=0.9,
                commands=[
                    AgentCommandProposal(
                        device_id="light_living_01", property="extra.brightness", value=90
                    )
                ],
            )
        ],
        episode_root,
        state_manager.world,
        user_claims=claims,
    )

    assert result.approved_commands == []
    assert result.rejected_commands[0].winner_priority is PriorityLevel.EXPLICIT_USER
    assert result.rejected_commands[0].conflict_class is ConflictClass.SAME_DEVICE_PROPERTY


# ------------------------------ explicit_user 属于**真人**，不属于响应真人的 agent


def test_an_agent_reacting_to_a_user_command_does_not_claim_the_user_tier():
    """S3 复审 blocker：``explicit_user`` 说的是"命令的行为体是用户"。

    一个因 ``user.command`` 被唤醒的域 agent 是在**替**用户做事，它仍然是 agent；
    自称 explicit_user 会同时买到两样它不该有的东西：对真人占用的免疫，以及对
    §9.2 单边拒绝（设备不可用 / 隐私 / 能耗否决）的豁免。
    """

    from backend.agents.contracts import outranks
    from backend.agents.hvac import HVACAgent
    from backend.agents.lighting import LightingAgent

    world = _init_default_state().world
    root = _root_event("user.command", device_id="light_living_01", action="turn_on")

    for agent in (LightingAgent(), HVACAgent()):
        tier = agent.proposal_priority(world, root)
        assert tier is not PriorityLevel.EXPLICIT_USER, f"{agent.agent_id} 自称了用户档"
        assert outranks(PriorityLevel.EXPLICIT_USER, tier), (
            f"{agent.agent_id} 的档位 {tier.value} 压过了真人直控"
        )


@pytest.mark.anyio
async def test_a_real_user_command_beats_the_agent_woken_by_that_same_user_event():
    """真人直控 vs 因同一条 user.command 被唤醒的 agent，落在同一控制点上——用户赢。

    这是复审给出的复现路径：占用 ``brightness=5``（真人，wall_time 9.0）对
    ``brightness=95``（lighting_agent，根事件 wall_time 1.0）。旧实现里 agent 自称
    explicit_user，于是 ``_check_user_claim`` 直接放行，用户的值输掉且**连一条冲突都
    没有记下**。
    """

    from backend.agents.lighting import LightingAgent

    state_manager = _init_default_state()
    executor = CommandExecutor(state_manager)
    gate = ArbitrationGate(executor=executor, arbiter=Arbiter(), clock=lambda: 9.0)

    async def publish(event: SimEvent) -> SimEvent:
        return event

    # 根事件比占用**早**：用户是在这一轮推理期间说的话（wall_time 1.0 < 9.0）。
    root = SimEvent(
        event_id="root-user-command",
        event_type="user.command",
        source="user_ui",
        timestamp=1.0,
        wall_time=1.0,
        correlation_id="corr-user-command",
        data={"device_id": "light_living_01", "action": "set_state"},
    )

    await executor.submit(
        DeviceCommand(
            source=CommandSource.UI,
            device_id="light_living_01",
            capability="brightness",
            value=5,
            reason="ui dim to 5",
            correlation_id="corr-user",
            issued_tick=0,
        ),
        tick=0,
        publish=publish,
        pre_submit=gate.pre_submit,
    )

    agent = LightingAgent()
    agent_proposal = AgentProposal(
        agent_id=agent.agent_id,
        agent_role=agent.role,
        intent="lighting reacts to the user command",
        # 关键：agent **自己**声明的档位，不是测试硬塞的
        priority=agent.proposal_priority(state_manager.world, root),
        confidence=0.9,
        commands=[
            AgentCommandProposal(
                device_id="light_living_01", property="extra.brightness", value=95
            )
        ],
    )
    result = gate.arbiter.resolve(
        [agent_proposal],
        root,
        state_manager.world,
        user_claims=gate.claims_since(float(root.wall_time or 0.0)),
    )

    assert result.approved_commands == [], "agent 的 95 压过了用户的 5"
    rejected = result.rejected_commands[0]
    assert rejected.agent_id == agent.agent_id
    assert rejected.winner_agent_id == UI_ACTOR_ID
    assert rejected.winner_priority is PriorityLevel.EXPLICIT_USER
    assert rejected.conflict_class is ConflictClass.SAME_DEVICE_PROPERTY
    assert state_manager.world.devices["light_living_01"].state.extra["brightness"] == 5


@pytest.mark.anyio
async def test_an_agent_command_on_an_offline_device_still_fires_device_unavailable():
    """§9.2 第五类必须按**行为体**判豁免：agent 不能靠自称档位绕过它。

    根事件是 ``user.command``——旧实现下 lighting/hvac 正是在这里自称 explicit_user，
    于是离线设备的提案被直接批准，仲裁层的 device_unavailable 类从未出现（世界没坏，
    因为 S1 校验层仍会给 device_offline，但那是另一层的护栏）。
    """

    state_manager = _init_default_state()
    state_manager.world.devices["light_loft_01"].state.extra["online"] = False

    from backend.agents.lighting import LightingAgent

    agent = LightingAgent()
    root = _root_event("user.command", device_id="light_loft_01", action="turn_on")
    proposal = AgentProposal(
        agent_id=agent.agent_id,
        agent_role=agent.role,
        intent="lighting reacts to the user command",
        priority=agent.proposal_priority(state_manager.world, root),
        confidence=0.9,
        commands=[AgentCommandProposal(device_id="light_loft_01", property="power", value=True)],
    )
    result = Arbiter().resolve([proposal], root, state_manager.world)

    assert result.approved_commands == []
    assert result.rejected_commands[0].conflict_class is ConflictClass.DEVICE_UNAVAILABLE


# --------------------------------------------- 一条 episode 只发一条协调决策


def _make_engine() -> SimulationEngine:
    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=_DisabledProvider(),
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


def _sim_events(engine: SimulationEngine) -> list[dict]:
    return [
        call.args[0].payload
        for call in engine.conn.broadcast.call_args_list
        if call.args[0].type == "SIM_EVENT"
    ]


@pytest.mark.anyio
async def test_episode_emits_exactly_one_coordination_decision_event():
    """多个域 agent 参与的一条 episode，只发一条 ``reasoning.coordination_decision``。

    旧实现按 agent 各发一条（审计：仲裁全序名存实亡）——那时这条断言的计数会等于
    参与 agent 的个数。per-agent 结论现在降级为本事件 data 里的 ``per_agent`` 分解。
    """

    engine = _make_engine()
    root = _root_event("user.leaves_home", user_id="user_01", from_room="living_room")

    await engine._publish_sim_event(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    events = _sim_events(engine)
    coordination = [
        event
        for event in events
        if event["event_type"] == COORDINATION_DECISION_EVENT_TYPE
        and event["correlation_id"] == root.correlation_id
    ]
    assert len(coordination) == 1, "一条 episode 只能有一条协调决策"

    data = coordination[0]["data"]
    assert coordination[0]["source"] == ARBITER_ID
    # §9.3 三项输出 + winning_priority + 解释，全部在这一条事件里
    for key in ("approved_commands", "rejected_commands", "conflicts", "winning_priority", "explanation"):
        assert key in data
    assert data["explanation"]

    execution_plans = [
        event
        for event in events
        if event["event_type"] == "reasoning.execution_plan"
        and event["correlation_id"] == root.correlation_id
    ]
    assert len(execution_plans) >= 2, "这条根事件本该唤醒不止一个域 agent"
    # 每个域 agent 的执行计划都挂在**同一条**协调决策之下（§4.3 环序）
    assert {event["causal_parent"] for event in execution_plans} == {coordination[0]["event_id"]}
    # per_agent 分解覆盖了所有参与者
    assert {item["agent_id"] for item in data["per_agent"]} == {
        event["data"]["agent_id"] for event in execution_plans
    }


# ------------------------------------------------ 用户命令确实进了仲裁（WS 腿）


def test_user_ws_command_enters_arbitration_at_explicit_user_tier():
    """WS 直控现在会产出一条 explicit_user 档的协调决策。

    审计原文：main.py 直接落地 UI 命令、runtime 又显式跳过 CMD_DEVICE_CONTROL，
    于是 §9.1 的 explicit_user 档从来没被走过。
    """

    from backend.main import app

    with TestClient(app) as client:
        with client.websocket_connect("/ws/simulation") as ws:
            ws.receive_json()  # initial STATE_FULL
            ws.send_json(
                {
                    "type": "CMD_DEVICE_CONTROL",
                    "payload": {"device_id": "light_living_01", "action": "turn_off"},
                }
            )

            coordination = None
            for _ in range(40):
                message = ws.receive_json()
                if (
                    message["type"] == "SIM_EVENT"
                    and message["payload"]["event_type"] == COORDINATION_DECISION_EVENT_TYPE
                ):
                    coordination = message["payload"]
                    break
            assert coordination is not None, "UI 命令没有产生协调决策——用户仍然没进仲裁"

    data = coordination["data"]
    assert data["winning_priority"] == PriorityLevel.EXPLICIT_USER.value
    assert [item["agent_id"] for item in data["approved_commands"]] == [UI_ACTOR_ID]
    assert data["approved_commands"][0]["device_id"] == "light_living_01"
    assert data["rejected_commands"] == []


def test_multi_property_user_command_is_not_self_conflicted():
    """同一条 UI ``set_state`` 里的多个属性属于同一份计划，不得互相否决。

    ``power=False`` 压低房间光照、``brightness=100`` 抬高它——若把"同房间互斥环境目标"
    也套在同一个提案者身上，用户的多属性直控会被自己吞掉一半。
    """

    state_manager = _init_default_state()
    proposal = AgentProposal(
        agent_id=UI_ACTOR_ID,
        intent="ui set_state",
        priority=PriorityLevel.EXPLICIT_USER,
        confidence=1.0,
        commands=[
            AgentCommandProposal(device_id="light_living_01", property="power", value=False),
            AgentCommandProposal(
                device_id="light_living_01", property="extra.brightness", value=100
            ),
        ],
    )
    result = Arbiter().resolve([proposal], _root_event("user.command"), state_manager.world)

    assert len(result.approved_commands) == 2
    assert result.conflicts == []


@pytest.mark.anyio
async def test_arbitration_loser_gets_a_rejected_lifecycle_with_the_conflict_class():
    """仲裁落败的命令走 proposed→rejected，detail 里带 §9.2 分类（不静默消失）。"""

    engine = _make_engine()
    # 让一台离线设备被 agent 选中：§9.2 第五类，仲裁在 proposed 阶段就否掉它。
    engine.state_manager.world.devices["light_loft_01"].state.extra["online"] = False

    from backend.agents.base import BaseAgent

    class _LoftLightingAgent(BaseAgent):
        agent_role = "lighting"

        def __init__(self) -> None:
            super().__init__(agent_id="loft_agent", name="Loft Agent")

        def get_controlled_device_types(self):
            return ["light"]

        def determine_priority(self, world_state, root_event):
            return "user_comfort"

        def is_relevant(self, world_state, root_event) -> bool:
            return root_event.event_type == "user.activity_change"

        def get_allowed_command_specs(self, world_state, root_event):
            return [{"device_id": "light_loft_01", "property": "power"}]

        def decide(self, world_state):
            return [
                {
                    "device_id": "light_loft_01",
                    "property": "power",
                    "value": True,
                    "reason": "点亮阁楼",
                }
            ]

    engine.agent_runtime.agents.clear()
    engine.agent_runtime.register(_LoftLightingAgent())

    root = _root_event("user.activity_change", user_id="user_01", to_room="loft")
    await engine._publish_sim_event(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    events = _sim_events(engine)
    rejected = [
        event
        for event in events
        if event["event_type"] == "command.lifecycle" and event["data"]["to_status"] == "rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0]["data"]["from_status"] == "proposed"
    assert ConflictClass.DEVICE_UNAVAILABLE.value in rejected[0]["data"]["detail"]
    # §10.2 词表照常给出（agent 腿与 UI 腿的错误码奇偶护栏不能因为仲裁抢先而失效）
    assert rejected[0]["data"]["failure_code"] == "device_offline"
    assert "action.device_control" not in [event["event_type"] for event in events]
