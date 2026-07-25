"""S3-T3：HomeOrchestratorAgent —— 规则路径 + LLM 混合意图分类（spec §8.1/§8.3）。

这份测试盯死四件事，每一件都对应审计 §六 里的一条坑：

1. **规则路径必须能独立跑**（decision #3 的"混合"要求）。没有任何 provider、没有网络，
   ``classify_intent_rule_based`` / ``plan_rule_based`` 就要产出一份合法 TaskPlan。
   规则路径要是必须先有 LLM 才能测，"LLM 不可用时仍然可用"就永远只是句口号。
2. **task_decomposition 必须是契约不是散文**：派了哪个角色、动哪几台设备、什么优先级，
   都要能直接断言。
3. **confidence 必须有真实消费者**：低于阈值时编排器给出 ``low_confidence`` 结论并且
   **不静默执行**；回退路径不得再出现那个装饰性的硬编码 0.55。
4. **确定性**：``TaskPlan.domain_tasks`` 的顺序 = agent 注册顺序（S2 字节一致性门的
   前提），LLM 不得把安全事件降档。
"""

from __future__ import annotations

import inspect

import pytest
import structlog

from backend.agents import base as base_module
from backend.agents.contracts import (
    ConfidenceSource,
    OrchestrationPolicy,
    PriorityLevel,
    ProposalOutcome,
    RootEventContext,
    priority_rank,
)
from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.llm import LLMProvider, LLMProviderError
from backend.agents.orchestrator import (
    DEFAULT_ORCHESTRATOR_ID,
    MIN_CONFIDENCE_ENV,
    ORCHESTRATOR_ENABLED_ENV,
    ORCHESTRATOR_INTENT_SCHEMA,
    DomainAgentBinding,
    HomeOrchestratorAgent,
    classify_intent_rule_based,
    orchestrator_enabled,
    rule_based_confidence,
)
from backend.agents.types import AgentLLMDecision, LLMDecisionRequest
from backend.engine.event_bus import SimEvent
from backend.engine.run_manager import LLMMode
from backend.main import _init_default_state

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------- 夹具


def _world():
    return _init_default_state().world


def _bindings() -> list[DomainAgentBinding]:
    """注册顺序：lighting 在前、hvac 在后（与引擎默认注册序一致）。"""

    return [
        DomainAgentBinding.from_agent(LightingAgent()),
        DomainAgentBinding.from_agent(HVACAgent()),
    ]


def _context(event_type: str, *, data: dict | None = None, policy=None) -> RootEventContext:
    event = SimEvent(event_type=event_type, source="test", timestamp=1.0, data=data or {})
    return RootEventContext.from_observable_world(
        root_event=event,
        observable_world=_world(),
        run_id="run-test",
        scenario_id="scenario-test",
        policy=policy,
    )


class _StubProvider(LLMProvider):
    """声明 RECORDED 模式的桩 provider —— 让编排器自动选择"要调 LLM"。"""

    provider_name = "stub"
    model = "stub-1"
    llm_mode = LLMMode.RECORDED

    def __init__(self, decision: AgentLLMDecision | None = None, error: LLMProviderError | None = None):
        self.decision = decision
        self.error = error
        self.requests: list[LLMDecisionRequest] = []

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        assert self.decision is not None
        return self.decision


class _CountingProvider(_StubProvider):
    pass


# ------------------------------------------------------- 1. 规则路径（零 LLM）


async def test_arrives_home_rule_path_yields_comfort_taskplan_without_llm():
    """§6.1：到家 → 舒适 + 在场转移，照明与 HVAC 各拿到一条带设备名单的任务。"""

    provider = _CountingProvider(decision=AgentLLMDecision(intent="x", confidence=0.9, explanation="x"))
    orchestrator = HomeOrchestratorAgent()
    decision = orchestrator.plan_rule_based(_context("user.arrives_home"), _bindings())

    plan = decision.plan
    assert plan.orchestrator_id == DEFAULT_ORCHESTRATOR_ID
    assert "arrival" in plan.intent or "comfort" in plan.intent
    assert plan.is_noop is False
    assert plan.confidence_source is ConfidenceSource.RULE_BASED
    assert decision.outcome is ProposalOutcome.ACTED

    assert plan.agent_roles == ("lighting", "hvac")
    lighting_task = plan.domain_tasks[0]
    assert lighting_task.priority is PriorityLevel.COMFORT
    # §7「user.arrives_home → lights, HVAC | curtains, cameras」：照明拿到的是**灯**，
    # 不是全屋设备——搜索空间来自映射表，不是 agent 里的分支。
    assert lighting_task.relevant_device_ids
    assert all(device_id.startswith("light_") for device_id in lighting_task.relevant_device_ids)
    assert lighting_task.relevant_device_ids == sorted(lighting_task.relevant_device_ids)
    assert plan.domain_tasks[1].relevant_device_ids == sorted(plan.domain_tasks[1].relevant_device_ids)

    # 规则路径一次 provider 都不能碰
    assert provider.requests == []


def test_rule_classification_is_pure_and_deterministic():
    context = _context("security.presence_detected", data={"room_id": "living_room"})
    first = classify_intent_rule_based(context)
    second = classify_intent_rule_based(context)
    assert first == second
    assert first.priority is PriorityLevel.SECURITY
    assert first.default_policy == "security first"


def test_rule_path_covers_every_root_event_type():
    """§7 表里的每一个根事件类型都要有规则分类，不能落到"未知"。"""

    from backend.engine.event_types import ALL_ROOT_EVENT_TYPES, DEVICE_AVAILABILITY_EVENT_TYPES

    for event_type in sorted(ALL_ROOT_EVENT_TYPES):
        # 设备可用性事件的搜索面按"是哪台设备"现场解析（§7 dynamic 行），
        # 不带 device_type 的那种本来就该低置信（fail closed），单独在下一条断言。
        data = {"device_id": "light_living_01", "device_type": "light"} if event_type in DEVICE_AVAILABILITY_EVENT_TYPES else {}
        intent = classify_intent_rule_based(_context(event_type, data=data))
        assert intent.intent, event_type
        assert intent.confidence >= 0.5, f"{event_type} 的规则置信度低于默认阈值"


def test_unresolvable_device_event_is_low_confidence_not_confidently_wrong():
    """§7「device.offline → fail closed and explain」：说不清是哪台设备就不该有把握。"""

    intent = classify_intent_rule_based(_context("device.offline", data={"online": False}))
    assert intent.resolved_from == "dynamic_unresolved"
    assert intent.confidence < 0.5
    assert intent.default_policy == "fail closed and explain"


async def test_irrelevant_event_yields_noop_reason_not_empty_crash():
    """§8.3 no-op 契约：没人可派也要给出 noop_reason，而不是抛异常或静默空转。"""

    orchestrator = HomeOrchestratorAgent()
    # 灯掉线 → 搜索面是 light/curtain；只登记一个 HVAC agent 时无人可派。
    context = _context(
        "device.offline",
        data={"device_id": "light_living_01", "device_type": "light", "online": False},
    )
    decision = orchestrator.plan_rule_based(context, [DomainAgentBinding.from_agent(HVACAgent())])

    assert decision.plan.is_noop is True
    assert decision.plan.noop_reason
    assert decision.outcome is ProposalOutcome.NO_ACTION_NEEDED
    assert decision.selected_agent_ids == ()


async def test_missing_observations_when_no_observable_world():
    orchestrator = HomeOrchestratorAgent()
    event = SimEvent(event_type="user.arrives_home", source="test", timestamp=1.0)
    context = RootEventContext(root_event=__import__(
        "backend.agents.contracts", fromlist=["RootEventRef"]
    ).RootEventRef.from_sim_event(event))
    decision = orchestrator.plan_rule_based(context, _bindings())

    assert decision.outcome is ProposalOutcome.MISSING_OBSERVATIONS
    assert decision.plan.is_noop is True


# ------------------------------------------------------------- 2. 混合 LLM 路径


async def test_llm_intent_refines_rule_intent_when_mocked_provider_configured():
    provider = _StubProvider(
        decision=AgentLLMDecision(
            intent="welcome the resident home with warm evening lighting",
            confidence=0.9,
            explanation="comfort domain: the resident just arrived",
        )
    )
    orchestrator = HomeOrchestratorAgent()
    decision = await orchestrator.plan(_context("user.arrives_home"), _bindings(), llm_provider=provider)

    assert len(provider.requests) == 1
    assert provider.requests[0].agent_id == DEFAULT_ORCHESTRATOR_ID
    # LLM 只精炼 intent 与 confidence，不发命令
    assert decision.plan.intent == "welcome the resident home with warm evening lighting"
    assert decision.plan.confidence_source in {ConfidenceSource.LLM, ConfidenceSource.BLENDED}
    assert decision.plan.fallback_reason is None
    assert decision.plan.agent_roles == ("lighting", "hvac")


async def test_provider_error_falls_back_to_rule_intent_and_flags_fallback():
    provider = _StubProvider(error=LLMProviderError("timeout", "provider timed out"))
    orchestrator = HomeOrchestratorAgent()
    rule = classify_intent_rule_based(_context("user.arrives_home"))
    decision = await orchestrator.plan(_context("user.arrives_home"), _bindings(), llm_provider=provider)

    assert decision.plan.intent == rule.intent
    assert decision.plan.confidence == rule.confidence
    assert decision.plan.confidence_source is ConfidenceSource.RULE_BASED
    assert decision.plan.fallback_reason == "timeout"
    assert decision.outcome is ProposalOutcome.ACTED
    assert decision.plan.agent_roles == ("lighting", "hvac")


async def test_llm_cannot_downgrade_a_safety_event():
    """失败要 fail closed：模型说这是 ambience，安全档也不能被降下来（§19）。"""

    provider = _StubProvider(
        decision=AgentLLMDecision(
            intent="adjust ambience lighting",
            confidence=0.95,
            explanation="ambience only",
        )
    )
    orchestrator = HomeOrchestratorAgent()
    decision = await orchestrator.plan(
        _context("safety.smoke_detected", data={"room_id": "kitchen"}), _bindings(), llm_provider=provider
    )

    for task in decision.plan.domain_tasks:
        assert priority_rank(task.priority) >= priority_rank(PriorityLevel.SAFETY)


async def test_llm_is_not_called_in_mocked_mode():
    """§11.1：mocked 模式（含所有单测默认路径）不打网，编排器走纯规则。"""

    class _MockedProvider(_StubProvider):
        llm_mode = LLMMode.MOCKED

    provider = _MockedProvider(decision=AgentLLMDecision(intent="x", confidence=0.9, explanation="x"))
    decision = await HomeOrchestratorAgent().plan(
        _context("user.arrives_home"), _bindings(), llm_provider=provider
    )
    assert provider.requests == []
    assert decision.plan.confidence_source is ConfidenceSource.RULE_BASED


def test_intent_schema_is_small_and_domain_constrained():
    assert ORCHESTRATOR_INTENT_SCHEMA["required"] == ["intent", "domain", "confidence", "explanation"]
    assert "safety" in ORCHESTRATOR_INTENT_SCHEMA["properties"]["domain"]["enum"]


# ---------------------------------------------------- 3. confidence 的真实消费者


async def test_low_confidence_plan_yields_low_confidence_outcome_not_silent_execution(monkeypatch):
    """审计坑：confidence 全链路无消费者。阈值以下必须 no-op，不能照常执行。"""

    monkeypatch.setenv(MIN_CONFIDENCE_ENV, "0.99")
    provider = _StubProvider(
        decision=AgentLLMDecision(intent="not sure what happened", confidence=0.2, explanation="unclear")
    )
    decision = await HomeOrchestratorAgent().plan(
        _context("user.arrives_home"), _bindings(), llm_provider=provider
    )

    assert decision.outcome is ProposalOutcome.LOW_CONFIDENCE
    assert decision.plan.is_noop is True, "低置信必须落成可见 no-op，而不是照常派任务"
    assert decision.plan.noop_reason and "confidence" in decision.plan.noop_reason
    assert decision.selected_agent_ids == ()
    assert decision.min_confidence == pytest.approx(0.99)
    # 事件数据里要能读到"为什么没干"
    payload = decision.intent_event_data()
    assert payload["outcome"] == ProposalOutcome.LOW_CONFIDENCE.value
    assert payload["low_confidence"] is True
    assert payload["min_confidence"] == pytest.approx(0.99)


async def test_low_confidence_with_confirmation_policy_keeps_tasks_but_marks_confirmation(monkeypatch):
    monkeypatch.setenv(MIN_CONFIDENCE_ENV, "0.99")
    decision = HomeOrchestratorAgent().plan_rule_based(
        _context("user.arrives_home", policy=OrchestrationPolicy(require_human_confirmation=True)),
        _bindings(),
    )
    assert decision.outcome is ProposalOutcome.NEEDS_HUMAN_CONFIRMATION
    assert decision.plan.requires_confirmation is True
    assert decision.plan.is_noop is False


def test_policy_min_confidence_overrides_env(monkeypatch):
    monkeypatch.setenv(MIN_CONFIDENCE_ENV, "0.99")
    decision = HomeOrchestratorAgent().plan_rule_based(
        _context("user.arrives_home", policy=OrchestrationPolicy(min_confidence=0.1)), _bindings()
    )
    assert decision.min_confidence == pytest.approx(0.1)
    assert decision.outcome is ProposalOutcome.ACTED


def test_no_hardcoded_confidence_constant_in_fallback_path():
    """回退置信度必须是**推导**出来的，不是那个装饰性的 0.55。"""

    # 只看代码，不看注释——注释里可以（也应该）留下"这里原来是 0.55"的考古线索。
    code = "\n".join(
        line.split("#", 1)[0]
        for line in inspect.getsource(base_module.BaseAgent._build_fallback_envelope).splitlines()
    )
    assert "0.55" not in code
    assert "confidence=self._fallback_confidence" in code

    # 同一个规则回退，不同事件/不同产出 → 不同置信度（常量做不到这一点）
    covered = rule_based_confidence("user.arrives_home", has_actions=True)
    empty = rule_based_confidence("user.arrives_home", has_actions=False)
    unmapped = rule_based_confidence("totally.unknown_event", has_actions=True)
    assert covered > empty
    assert covered > unmapped
    assert 0.0 <= unmapped <= 1.0


# -------------------------------------------------------------- 4. 确定性契约


def test_domain_task_order_follows_agent_registration_order():
    """S2 字节一致性门的前提：分派顺序 = 注册顺序，不是集合迭代序。"""

    forward = HomeOrchestratorAgent().plan_rule_based(_context("user.arrives_home"), _bindings())
    reversed_bindings = list(reversed(_bindings()))
    backward = HomeOrchestratorAgent().plan_rule_based(_context("user.arrives_home"), reversed_bindings)

    assert forward.plan.agent_roles == ("lighting", "hvac")
    assert backward.plan.agent_roles == ("hvac", "lighting")
    # 同一份注册序反复计算恒等
    assert forward.plan.model_dump() == HomeOrchestratorAgent().plan_rule_based(
        _context("user.arrives_home"), _bindings()
    ).plan.model_dump()


async def test_orchestrator_never_mutates_world():
    """spec §8.1「The orchestrator should not directly mutate world state」。"""

    world = _world()
    event = SimEvent(event_type="user.arrives_home", source="test", timestamp=1.0)
    context = RootEventContext.from_observable_world(root_event=event, observable_world=world)
    before = world.model_dump_json()

    provider = _StubProvider(
        decision=AgentLLMDecision(intent="do a lot", confidence=0.9, explanation="comfort")
    )
    await HomeOrchestratorAgent().plan(context, _bindings(), llm_provider=provider)

    assert world.model_dump_json() == before
    assert not hasattr(HomeOrchestratorAgent(), "apply_action")


def test_orchestrator_enabled_flag_defaults_on_and_can_be_switched_off():
    assert orchestrator_enabled({}) is True
    assert orchestrator_enabled({ORCHESTRATOR_ENABLED_ENV: "0"}) is False
    assert orchestrator_enabled({ORCHESTRATOR_ENABLED_ENV: "false"}) is False
    assert orchestrator_enabled({ORCHESTRATOR_ENABLED_ENV: "1"}) is True


def test_disabling_the_orchestrator_warns_at_startup(monkeypatch):
    """逃生阀不许静默：关掉编排器 = 任务分解退回审计坑 (c) 的自由文本路径。

    默认开 + 静默关意味着一条流水线可以被无声降级，事后没人能从日志里看出
    这批数据是哪条路径产的。所以关闭必须在启动时留下一条 warning。
    """

    from backend.agents.runtime import AgentRuntime

    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "0")
    with structlog.testing.capture_logs() as logs:
        runtime = AgentRuntime()

    assert runtime.orchestrator_enabled is False
    warnings = [
        entry
        for entry in logs
        if entry["event"] == "orchestrator_disabled" and entry["log_level"] == "warning"
    ]
    assert len(warnings) == 1, "关闭编排器必须且只需喊一声"
    assert warnings[0]["env_var"] == ORCHESTRATOR_ENABLED_ENV
    assert "task_decomposition" in warnings[0]["impact"]

    # 负控：开着的时候不许喊狼来了，否则这条 warning 很快就没人看了。
    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "1")
    with structlog.testing.capture_logs() as logs:
        assert AgentRuntime().orchestrator_enabled is True
    assert [entry for entry in logs if entry["event"] == "orchestrator_disabled"] == []


async def test_rule_path_labels_itself_rule_based_not_mocked():
    """§11.1：没有任何 LLM 的编排结论必须自报 ``rule_based``。

    ``mocked`` 说的是"罐头 LLM 决策"。把"压根没有 LLM"也标成 mocked，等于让
    S5 对比视图把两种实验条件画进同一条曲线。
    """

    from backend.agents.runtime import DisabledLLMProvider

    context, bindings = _context("user.arrives_home"), _bindings()

    assert HomeOrchestratorAgent().plan_rule_based(context, bindings).llm_mode == (
        LLMMode.RULE_BASED.value
    )

    # 没配 key（= DisabledLLMProvider）也是"没有 LLM"，不是罐头。
    disabled = await HomeOrchestratorAgent().plan(
        context, bindings, llm_provider=DisabledLLMProvider()
    )
    assert disabled.llm_mode == LLMMode.RULE_BASED.value
    assert disabled.provider == "rule_based"

    # 反过来：真有罐头 provider 时标签必须是 mocked——两种条件双向可分。
    class _MockedProvider(_StubProvider):
        llm_mode = LLMMode.MOCKED

    mocked = await HomeOrchestratorAgent().plan(
        context,
        bindings,
        llm_provider=_MockedProvider(
            decision=AgentLLMDecision(intent="x", confidence=0.9, explanation="x")
        ),
    )
    assert mocked.llm_mode == LLMMode.MOCKED.value


def test_task_decomposition_event_data_is_a_contract_not_prose():
    """审计坑：task_decomposition 里装的是 LLM 散文。现在必须是结构化 domain_tasks。"""

    decision = HomeOrchestratorAgent().plan_rule_based(_context("user.arrives_home"), _bindings())
    payload = decision.task_decomposition_event_data()

    assert payload["orchestrator_id"] == DEFAULT_ORCHESTRATOR_ID
    assert payload["agent_roles"] == ["lighting", "hvac"]
    assert isinstance(payload["domain_tasks"], list) and payload["domain_tasks"]
    first = payload["domain_tasks"][0]
    assert set(first) >= {"agent_role", "task", "relevant_device_ids", "priority", "relevant_room_ids"}
    assert first["priority"] == PriorityLevel.COMFORT.value


# ------------------------------------------------------- 5. runtime 分发接线


def _engine(provider: LLMProvider):
    from unittest.mock import AsyncMock

    from backend.api.ws import ConnectionManager
    from backend.engine.event_bus import EventBus
    from backend.engine.simulation import SimulationEngine

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


def _sim_events(engine):
    return [
        call.args[0].payload
        for call in engine.conn.broadcast.call_args_list
        if call.args[0].type == "SIM_EVENT"
    ]


async def test_runtime_emits_orchestrator_reasoning_events_under_one_correlation(monkeypatch):
    # 显式钉住开关：本条测的是"编排器开着的时候长什么样"，不能被外部环境变量改掉语义。
    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "1")
    from backend.agents.runtime import DisabledLLMProvider

    engine = _engine(DisabledLLMProvider())
    root = SimEvent(
        event_type="user.arrives_home",
        source="test",
        timestamp=1.0,
        data={"user_id": "user_01", "room_id": "living_room", "to_room": "living_room"},
    )
    await engine.event_bus.publish(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    events = [e for e in _sim_events(engine) if e["correlation_id"] == root.correlation_id]
    orchestrator_events = [e for e in events if e["source"] == DEFAULT_ORCHESTRATOR_ID]
    types = [e["event_type"] for e in orchestrator_events]
    assert types[:3] == [
        "reasoning.perception_snapshot",
        "reasoning.intent_recognized",
        "reasoning.task_decomposition",
    ]

    decomposition = orchestrator_events[2]
    assert decomposition["data"]["domain_tasks"], "task_decomposition 必须带结构化 domain_tasks"
    assert decomposition["causal_parent"] == orchestrator_events[1]["event_id"]
    assert orchestrator_events[0]["causal_parent"] == root.event_id

    # S3-T6：前三环归编排器独有——域 agent 不再各起一棵平行小树（"episode 被稀释"）。
    # 它们的推理贡献改挂在 reasoning.execution_plan 上，而那一环挂在协调决策之下，
    # 协调决策又挂在编排器的任务拆分之下（§4.4 causal_parent 指向直接父）。
    assert not [
        e
        for e in events
        if e["event_type"] == "reasoning.perception_snapshot" and e["source"] != DEFAULT_ORCHESTRATOR_ID
    ]
    coordination = next(e for e in events if e["event_type"] == "reasoning.coordination_decision")
    assert coordination["causal_parent"] == decomposition["event_id"]
    agent_plans = [e for e in events if e["event_type"] == "reasoning.execution_plan"]
    assert agent_plans
    assert all(e["causal_parent"] == coordination["event_id"] for e in agent_plans)
    assert all(e["data"]["intent"] and e["data"]["perception"]["world_summary"] for e in agent_plans)


async def test_runtime_dispatches_only_agents_named_by_the_plan(monkeypatch):
    """§7「kitchen lights, sensors | fan」：做饭事件不该把 HVAC agent 也拖进来。"""

    from backend.agents.runtime import DisabledLLMProvider

    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "1")
    engine = _engine(DisabledLLMProvider())
    root = SimEvent(
        event_type="user.starts_activity",
        source="test",
        timestamp=1.0,
        data={"user_id": "user_01", "activity": "cooking", "room_id": "kitchen"},
    )
    await engine.event_bus.publish(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    events = [e for e in _sim_events(engine) if e["correlation_id"] == root.correlation_id]
    reasoning_agents = {
        e["data"].get("agent_id")
        for e in events
        if e["event_type"].startswith("reasoning.") and e["source"] != DEFAULT_ORCHESTRATOR_ID
    }
    assert "lighting_agent" in reasoning_agents
    assert "hvac_agent" not in reasoning_agents


async def test_orchestrator_can_be_disabled_restoring_fan_out(monkeypatch):
    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "0")
    from backend.agents.runtime import DisabledLLMProvider

    engine = _engine(DisabledLLMProvider())
    assert engine.agent_runtime.orchestrator_enabled is False
    root = SimEvent(
        event_type="user.starts_activity",
        source="test",
        timestamp=1.0,
        data={"user_id": "user_01", "activity": "cooking", "room_id": "kitchen"},
    )
    await engine.event_bus.publish(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    events = [e for e in _sim_events(engine) if e["correlation_id"] == root.correlation_id]
    sources = {e["source"] for e in events}
    assert DEFAULT_ORCHESTRATOR_ID not in sources
    reasoning_agents = {
        e["data"].get("agent_id") for e in events if e["event_type"].startswith("reasoning.")
    }
    # 旧扇出路径：所有 is_relevant 的 agent 都跑
    assert {"lighting_agent", "hvac_agent"} <= reasoning_agents


async def test_domain_agent_receives_its_domain_task():
    """域 agent 通过 handle_event(domain_task=...) 拿到编排器派的那件事。"""

    from backend.agents.contracts import DomainTask
    from backend.agents.memory import AgentMemoryStore
    from backend.agents.runtime import DisabledLLMProvider

    agent = LightingAgent()
    world = _world()
    event = SimEvent(
        event_type="user.arrives_home", source="test", timestamp=1.0, data={"room_id": "living_room"}
    )
    task = DomainTask(
        agent_role="lighting",
        task="prepare occupied living-room lighting",
        relevant_device_ids=["light_living_01"],
        priority=PriorityLevel.COMFORT,
        relevant_room_ids=["living_room"],
    )
    envelope = await agent.handle_event(
        root_event=event,
        world_state=world,
        memory_store=AgentMemoryStore(),
        llm_provider=DisabledLLMProvider(),
        domain_task=task,
    )
    assert envelope is not None
    assert envelope.relevant_devices == ["light_living_01"], "编排器点名的设备就是 agent 看到的设备"
    assert "handle_event" in dir(agent)
    assert "domain_task" in inspect.signature(agent.handle_event).parameters


async def test_low_confidence_episode_is_visible_but_executes_nothing(monkeypatch):
    """审计坑的端到端版：阈值拉满时，episode 仍可见，但一条设备命令都不许落地。"""

    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "1")
    monkeypatch.setenv(MIN_CONFIDENCE_ENV, "1.0")
    from backend.agents.runtime import DisabledLLMProvider

    engine = _engine(DisabledLLMProvider())
    root = SimEvent(
        event_type="user.arrives_home",
        source="test",
        timestamp=1.0,
        data={"user_id": "user_01", "room_id": "living_room", "to_room": "living_room"},
    )
    await engine.event_bus.publish(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
    await engine.close()

    events = [e for e in _sim_events(engine) if e["correlation_id"] == root.correlation_id]
    types = [e["event_type"] for e in events]
    # 可见：编排三环都在
    assert "reasoning.intent_recognized" in types
    assert "reasoning.task_decomposition" in types
    # 但没动手：既没有域 agent 的执行计划，也没有任何设备动作
    assert "action.device_control" not in types
    assert "reasoning.execution_plan" not in types

    intent_event = next(e for e in events if e["event_type"] == "reasoning.intent_recognized")
    assert intent_event["data"]["outcome"] == ProposalOutcome.LOW_CONFIDENCE.value
    assert intent_event["data"]["low_confidence"] is True
    decomposition = next(e for e in events if e["event_type"] == "reasoning.task_decomposition")
    assert decomposition["data"]["domain_tasks"] == []
    assert decomposition["data"]["noop_reason"]
