"""S3-T1：编排契约的红线测试（spec §8.3 / §8.4 / §9.1）。

这个文件存在的唯一理由，是把"任务拆分"从 LLM 自由文本变回**可断言的结构化数据**。
审计 §六 记下的坑是：``reasoning.task_decomposition`` 事件里装的是模型写的散文，
没有任何测试能对它提问"你派了哪个 agent、动了哪台设备、按什么优先级"。因此本文件
全程**不碰 LLM**：契约必须能在零 provider 的情况下被验证，否则它就不是契约。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.agents.contracts import (
    DEFAULT_MIN_CONFIDENCE,
    LEGACY_PRIORITY_MIGRATION,
    NON_ACTION_OUTCOMES,
    PRIORITY_LEVELS,
    AgentProposal,
    ConfidenceSource,
    DomainTask,
    ObservableStateView,
    OrchestrationPolicy,
    PriorityLevel,
    ProposalOutcome,
    RootEventContext,
    RootEventRef,
    migrate_legacy_priority,
    outranks,
    priority_rank,
)
from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent
from backend.engine.state import DeviceState, DeviceStateValues, Location3D, RoomState, WorldState


# spec §8.3 里逐字给出的输入示例（docs/architecture/simulation-requirements-spec.md:806-828）。
SPEC_8_3_ROOT_EVENT_CONTEXT = {
    "run_id": "run_001",
    "scenario_id": "user_arrives_home_evening",
    "root_event": {
        "event_type": "user.arrives_home",
        "source": "scenario_runner",
    },
    "observable_state": {
        "time_of_day": "18:30",
        "weather": "cloudy",
        "rooms": ["living_room"],
        "devices": ["light_living_01", "ac_living_01"],
    },
    "ground_truth_labels": {
        "expected_intent": "arrival_comfort",
    },
    "policy": {
        "allow_fallback": True,
        "require_human_confirmation": False,
    },
}

# spec §8.3 输出示例（同文件 832-852 行）。
SPEC_8_3_TASK_PLAN = {
    "orchestrator_id": "home_orchestrator",
    "intent": "arrival_comfort",
    "confidence": 0.88,
    "domain_tasks": [
        {
            "agent_role": "lighting",
            "task": "prepare occupied living-room lighting",
            "relevant_device_ids": ["light_living_01"],
            "priority": "comfort",
        }
    ],
    "noop_reason": None,
    "requires_confirmation": False,
}

# spec §8.4 的 agent 输出示例（同文件 862-880 行）。
SPEC_8_4_PROPOSAL = {
    "agent_id": "lighting_agent",
    "intent": "prepare living room lighting for arrival",
    "priority": "comfort",
    "confidence": 0.91,
    "commands": [
        {
            "device_id": "light_living_01",
            "property": "extra.brightness",
            "value": 70,
            "reason": "living room is occupied after sunset",
        }
    ],
    "risks": [],
    "requires_coordination": False,
}


def _import_task_plan():
    from backend.agents.contracts import TaskPlan

    return TaskPlan


# ------------------------------------------------------------------ §8.3 契约


def test_task_plan_matches_spec_8_3_field_names():
    """spec §8.3 的输出示例必须原样进、原样出——字段名是对外契约的一部分。"""

    TaskPlan = _import_task_plan()
    plan = TaskPlan.model_validate(SPEC_8_3_TASK_PLAN)
    dumped = plan.model_dump(mode="json")

    for key, value in SPEC_8_3_TASK_PLAN.items():
        assert key in dumped, f"spec §8.3 字段 {key} 在 TaskPlan 里消失了"
        if key == "domain_tasks":
            continue
        assert dumped[key] == value

    # domain_tasks 是结构化数据而不是自由文本：逐字段断言得起来才算修好审计那个坑。
    assert len(plan.domain_tasks) == 1
    task = plan.domain_tasks[0]
    assert task.agent_role == "lighting"
    assert task.task == "prepare occupied living-room lighting"
    assert task.relevant_device_ids == ["light_living_01"]
    assert task.priority is PriorityLevel.COMFORT
    for key, value in SPEC_8_3_TASK_PLAN["domain_tasks"][0].items():
        assert dumped["domain_tasks"][0][key] == value


def test_root_event_context_matches_spec_8_3_field_names():
    context = RootEventContext.model_validate(SPEC_8_3_ROOT_EVENT_CONTEXT)

    assert context.run_id == "run_001"
    assert context.scenario_id == "user_arrives_home_evening"
    assert context.root_event.event_type == "user.arrives_home"
    assert context.root_event.source == "scenario_runner"
    assert context.observable_state.time_of_day == "18:30"
    assert context.observable_state.weather == "cloudy"
    assert context.observable_state.rooms == ["living_room"]
    assert context.observable_state.devices == ["light_living_01", "ac_living_01"]
    assert context.ground_truth_labels == {"expected_intent": "arrival_comfort"}
    assert context.policy.allow_fallback is True
    assert context.policy.require_human_confirmation is False

    dumped = context.model_dump(mode="json")
    for key in SPEC_8_3_ROOT_EVENT_CONTEXT:
        assert key in dumped


def test_contracts_reject_unknown_fields():
    """契约模型一律 extra=forbid：拼错字段名必须当场炸，不能静默丢数据。"""

    TaskPlan = _import_task_plan()
    with pytest.raises(ValidationError):
        TaskPlan.model_validate({**SPEC_8_3_TASK_PLAN, "domain_task": []})
    with pytest.raises(ValidationError):
        RootEventContext.model_validate({**SPEC_8_3_ROOT_EVENT_CONTEXT, "observable": {}})


def test_task_plan_noop_requires_reason_and_reason_forbids_tasks():
    """§8.3 no-op 契约：空计划必须自带理由，有理由的计划不能同时派任务。"""

    TaskPlan = _import_task_plan()

    with pytest.raises(ValidationError):
        TaskPlan(orchestrator_id="home_orchestrator", intent="unknown", confidence=0.2)

    plan = TaskPlan.noop(
        orchestrator_id="home_orchestrator",
        intent="unrelated_event",
        confidence=0.9,
        noop_reason="no controlled device is relevant to this root event",
    )
    assert plan.domain_tasks == []
    assert plan.noop_reason
    assert plan.is_noop is True

    with pytest.raises(ValidationError):
        TaskPlan(
            orchestrator_id="home_orchestrator",
            intent="arrival_comfort",
            confidence=0.9,
            noop_reason="nothing to do",
            domain_tasks=[
                DomainTask(
                    agent_role="lighting",
                    task="x",
                    relevant_device_ids=["light_living_01"],
                    priority=PriorityLevel.COMFORT,
                )
            ],
        )


def test_task_plan_preserves_domain_task_order():
    """domain_tasks 的顺序即分派顺序：S2 的字节一致性门靠"注册序"稳定，禁止重排。"""

    TaskPlan = _import_task_plan()
    roles = ["lighting", "hvac", "security", "energy", "scene"]
    plan = TaskPlan(
        orchestrator_id="home_orchestrator",
        intent="arrival_comfort",
        confidence=0.8,
        domain_tasks=[
            DomainTask(agent_role=role, task=f"task for {role}", priority=PriorityLevel.COMFORT)
            for role in roles
        ],
    )
    assert plan.agent_roles == tuple(roles)
    assert [t.agent_role for t in plan.model_copy(deep=True).domain_tasks] == roles


# ------------------------------------------------------------------ §9.1 全序


def test_priority_level_covers_all_seven_spec_9_1_tiers_and_is_totally_ordered():
    """§9.1 七档，且是**严格全序**。

    审计原文：旧 arbiter 只有六档且 safety 与 direct_user_command 并列第 5，security
    档根本不存在——并列意味着"谁先来谁赢"，仲裁结果不可解释。
    """

    expected = [
        "safety",
        "explicit_user",
        "security",
        "comfort",
        "energy",
        "ambience",
        "maintenance",
    ]
    assert [level.value for level in PRIORITY_LEVELS] == expected
    assert len(PriorityLevel) == 7

    ranks = [priority_rank(level) for level in PRIORITY_LEVELS]
    assert len(set(ranks)) == 7, "存在并列档位——§9.1 要求全序"

    # 两两成对：反自反 + 反对称 + 传递（全序的定义）。
    for a in PriorityLevel:
        assert not outranks(a, a)
        for b in PriorityLevel:
            if a is b:
                continue
            assert outranks(a, b) != outranks(b, a)
            assert (priority_rank(a) > priority_rank(b)) == outranks(a, b)
    for a in PriorityLevel:
        for b in PriorityLevel:
            for c in PriorityLevel:
                if outranks(a, b) and outranks(b, c):
                    assert outranks(a, c)

    # 规格逐条：safety > explicit_user > security > comfort > energy > ambience > maintenance
    for higher, lower in zip(PRIORITY_LEVELS, PRIORITY_LEVELS[1:]):
        assert outranks(higher, lower)
    assert outranks(PriorityLevel.SAFETY, PriorityLevel.EXPLICIT_USER)
    assert outranks(PriorityLevel.EXPLICIT_USER, PriorityLevel.SECURITY)


def test_legacy_priority_labels_migrate():
    """旧六标签只在**历史 payload** 上通过迁移映射存活，新事件一律用 §9.1 词表。"""

    assert LEGACY_PRIORITY_MIGRATION == {
        "direct_user_command": PriorityLevel.EXPLICIT_USER,
        "safety": PriorityLevel.SAFETY,
        "user_comfort": PriorityLevel.COMFORT,
        "convenience": PriorityLevel.AMBIENCE,
        "energy_efficiency": PriorityLevel.ENERGY,
        "background_optimization": PriorityLevel.MAINTENANCE,
    }
    for legacy, expected in LEGACY_PRIORITY_MIGRATION.items():
        assert migrate_legacy_priority(legacy) is expected
    # §9.1 词表自身也能过（幂等），方便调用方无脑迁移。
    for level in PriorityLevel:
        assert migrate_legacy_priority(level.value) is level

    with pytest.raises(ValueError):
        migrate_legacy_priority("totally_unknown_tier")


def test_no_legacy_label_validates_unmapped():
    """未经迁移的旧标签**不得**直接通过校验，否则并列档位会从后门溜回来。"""

    for legacy in ("direct_user_command", "user_comfort", "convenience", "energy_efficiency", "background_optimization"):
        with pytest.raises(ValueError):
            PriorityLevel(legacy)
        with pytest.raises(ValidationError):
            DomainTask(agent_role="lighting", task="x", priority=legacy)

    # 唯一同名幸存者：safety。
    assert PriorityLevel("safety") is PriorityLevel.SAFETY


def test_legacy_label_error_message_points_at_the_migration_helper():
    """旧词表的报错必须可行动：说清"这是历史标签、请走迁移函数"。"""

    for model_kwargs in (
        {"model": DomainTask, "agent_role": "lighting", "task": "x"},
        {"model": AgentProposal, "agent_id": "lighting_agent", "intent": "x", "confidence": 0.9},
    ):
        model = model_kwargs.pop("model")
        with pytest.raises(ValidationError, match="migrate_legacy_priority"):
            model(priority="user_comfort", **model_kwargs)


# ------------------------------------------------------------------ §8.4 五种非动作表达


def test_agent_proposal_matches_spec_8_4_field_names():
    proposal = AgentProposal.model_validate(SPEC_8_4_PROPOSAL)
    dumped = proposal.model_dump(mode="json")
    for key, value in SPEC_8_4_PROPOSAL.items():
        assert key in dumped, f"spec §8.4 字段 {key} 缺失"
        if key == "commands":
            continue
        assert dumped[key] == value
    assert proposal.commands[0].device_id == "light_living_01"
    assert proposal.outcome is ProposalOutcome.ACTED
    assert proposal.has_commands is True


def test_proposal_expresses_all_five_non_action_outcomes():
    """spec §8.4 要求 agent 能表达的五种"我没动手"，逐一构造得出来。"""

    assert NON_ACTION_OUTCOMES == frozenset(
        {
            ProposalOutcome.NO_ACTION_NEEDED,
            ProposalOutcome.LOW_CONFIDENCE,
            ProposalOutcome.MISSING_OBSERVATIONS,
            ProposalOutcome.UNSAFE_REJECTED,
            ProposalOutcome.NEEDS_HUMAN_CONFIRMATION,
        }
    )

    no_action = AgentProposal(
        agent_id="lighting_agent",
        intent="none",
        priority=PriorityLevel.COMFORT,
        confidence=0.9,
        outcome=ProposalOutcome.NO_ACTION_NEEDED,
        noop_reason="living room lights already match the target level",
    )
    assert no_action.has_commands is False
    assert no_action.is_non_action is True

    low_conf = AgentProposal(
        agent_id="lighting_agent",
        intent="unclear",
        priority=PriorityLevel.COMFORT,
        confidence=0.2,
        outcome=ProposalOutcome.LOW_CONFIDENCE,
        noop_reason="intent confidence 0.20 below threshold",
    )
    assert low_conf.is_non_action

    missing = AgentProposal(
        agent_id="hvac_agent",
        intent="cool living room",
        priority=PriorityLevel.COMFORT,
        confidence=0.7,
        outcome=ProposalOutcome.MISSING_OBSERVATIONS,
        noop_reason="temperature reading is stale",
        missing_observations=["room.living_room.temperature"],
    )
    assert missing.missing_observations == ["room.living_room.temperature"]

    unsafe = AgentProposal(
        agent_id="security_agent",
        intent="disable camera",
        priority=PriorityLevel.SECURITY,
        confidence=0.9,
        outcome=ProposalOutcome.UNSAFE_REJECTED,
        noop_reason="would disable security while user is away",
        risks=["do_not_disable_security_when_user_is_away"],
        withheld_commands=[
            AgentCommandProposal(device_id="camera_entry_01", property="power", value=False, reason="x")
        ],
    )
    assert unsafe.withheld_commands[0].device_id == "camera_entry_01"

    confirm = AgentProposal(
        agent_id="scene_agent",
        intent="apply away scene",
        priority=PriorityLevel.AMBIENCE,
        confidence=0.8,
        outcome=ProposalOutcome.NEEDS_HUMAN_CONFIRMATION,
        noop_reason="away scene turns off every light while a user is still home",
        requires_confirmation=True,
        withheld_commands=[
            AgentCommandProposal(device_id="light_living_01", property="power", value=False, reason="x")
        ],
    )
    assert confirm.requires_confirmation is True

    # 非动作结论不得同时夹带命令——否则"没动手"就成了空话。
    with pytest.raises(ValidationError):
        AgentProposal(
            agent_id="lighting_agent",
            intent="none",
            priority=PriorityLevel.COMFORT,
            confidence=0.9,
            outcome=ProposalOutcome.NO_ACTION_NEEDED,
            noop_reason="nothing to do",
            commands=[
                AgentCommandProposal(device_id="light_living_01", property="power", value=True, reason="x")
            ],
        )
    # 非动作结论必须给理由。
    with pytest.raises(ValidationError):
        AgentProposal(
            agent_id="lighting_agent",
            intent="none",
            priority=PriorityLevel.COMFORT,
            confidence=0.9,
            outcome=ProposalOutcome.LOW_CONFIDENCE,
        )
    # missing_observations 结论必须点名缺了哪条观测。
    with pytest.raises(ValidationError):
        AgentProposal(
            agent_id="hvac_agent",
            intent="cool",
            priority=PriorityLevel.COMFORT,
            confidence=0.7,
            outcome=ProposalOutcome.MISSING_OBSERVATIONS,
            noop_reason="stale",
        )
    # unsafe_rejected 结论必须写明风险。
    with pytest.raises(ValidationError):
        AgentProposal(
            agent_id="security_agent",
            intent="x",
            priority=PriorityLevel.SECURITY,
            confidence=0.9,
            outcome=ProposalOutcome.UNSAFE_REJECTED,
            noop_reason="unsafe",
        )


# --------------------------------------------------- confidence 必须有消费者的形状


def test_task_plan_confidence_has_a_threshold_consumer_hook():
    """审计坑：confidence 全链路无消费者。契约层先把阈值判定做成可调用的方法。"""

    TaskPlan = _import_task_plan()
    low = TaskPlan.noop(
        orchestrator_id="home_orchestrator",
        intent="unclear",
        confidence=0.2,
        noop_reason="low confidence",
        confidence_source=ConfidenceSource.LLM,
    )
    assert 0.0 < DEFAULT_MIN_CONFIDENCE < 1.0
    assert low.is_low_confidence() is True
    assert low.is_low_confidence(threshold=0.1) is False
    assert low.confidence_source is ConfidenceSource.LLM

    high = TaskPlan.model_validate(SPEC_8_3_TASK_PLAN)
    assert high.is_low_confidence() is False
    # 默认来源必须是诚实的规则值，而不是"不知道哪来的 0.55"。
    assert high.confidence_source is ConfidenceSource.RULE_BASED

    with pytest.raises(ValidationError):
        TaskPlan.model_validate({**SPEC_8_3_TASK_PLAN, "confidence": 1.4})

    # policy 可以按 run 覆盖阈值（S3-T3 的 AGENT_MIN_CONFIDENCE 落点）。
    policy = OrchestrationPolicy(min_confidence=0.1)
    assert low.is_low_confidence(threshold=policy.effective_min_confidence()) is False
    assert OrchestrationPolicy().effective_min_confidence() == DEFAULT_MIN_CONFIDENCE


# ------------------------------------------------- 从真实 observable 世界构造上下文


def _tiny_world() -> WorldState:
    world = WorldState()
    world.environment.time_of_day = "18:30"
    world.environment.weather = "cloudy"
    world.rooms["living_room"] = RoomState(id="living_room", occupancy=True)
    world.rooms["bedroom"] = RoomState(id="bedroom", occupancy=False)
    world.devices["light_living_01"] = DeviceState(
        id="light_living_01",
        type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=False),
    )
    world.devices["ac_living_01"] = DeviceState(
        id="ac_living_01",
        type="hvac",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=False, extra={"online": False}),
    )
    return world


def test_root_event_context_from_observable_world_projects_and_never_shares_state():
    """编排器只拿只读投影：spec §8.1 "orchestrator should not directly mutate world state"。"""

    world = _tiny_world()
    event = SimEvent(
        event_type="user.arrives_home",
        source="scenario_runner",
        timestamp=12.0,
        run_id="run_001",
        scenario_id="user_arrives_home_evening",
        data={"user_id": "user_1"},
    )
    context = RootEventContext.from_observable_world(
        root_event=event,
        observable_world=world,
        ground_truth_labels={"expected_intent": "arrival_comfort"},
    )

    assert context.run_id == "run_001"
    assert context.scenario_id == "user_arrives_home_evening"
    assert context.root_event.event_id == event.event_id
    assert context.root_event.correlation_id == event.correlation_id
    assert context.root_event.data == {"user_id": "user_1"}
    assert context.observable_state.time_of_day == "18:30"
    assert context.observable_state.weather == "cloudy"
    # 排序固定：确定性回放门要求任何投影都不泄漏 dict 迭代序。
    assert context.observable_state.rooms == ["bedroom", "living_room"]
    assert context.observable_state.devices == ["ac_living_01", "light_living_01"]
    assert context.observable_state.occupied_room_ids == ["living_room"]
    assert context.observable_state.unavailable_device_ids == ["ac_living_01"]

    # observable_world 是深拷贝：改了它，真实世界不动。
    assert context.observable_world is not world
    context.observable_world.devices["light_living_01"].state.power = True
    assert world.devices["light_living_01"].state.power is False

    # 且这份句柄不进序列化 payload（事件里只带 §8.3 摘要）。
    assert "observable_world" not in context.model_dump(mode="json")


def test_root_event_ref_from_sim_event_keeps_causal_fields():
    event = SimEvent(
        event_type="security.presence_detected",
        source="sensor",
        timestamp=3.0,
        causal_parent="parent-id",
        sim_time_s=30.0,
    )
    ref = RootEventRef.from_sim_event(event)
    assert ref.event_type == "security.presence_detected"
    assert ref.source == "sensor"
    assert ref.causal_parent == "parent-id"
    assert ref.sim_time_s == 30.0
    assert ref.timestamp == 3.0


def test_observable_state_view_is_a_summary_not_the_world():
    view = ObservableStateView.from_world(_tiny_world())
    assert isinstance(view, ObservableStateView)
    assert view.devices == ["ac_living_01", "light_living_01"]
