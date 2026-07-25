"""S3-T4：§8.4 统一提案契约在 BaseAgent 上的落点。

§8.4 的要求不是"多一个字段"，而是**五种非动作表达都必须有一条真实的产生路径**——
否则 "episode 完整性" 这条验收（§15）就只能靠"事件流里有没有东西"来猜。
这份测试逐条钉住五种表达的产生者，并钉住"判定顺序只有一处"这件事：

    领域裁决 > 需人确认 > 置信度不足 > 无事可做 > 动手

以及一条结构钉：既有的 lighting/hvac 不改一行就能给出 §9.1 档位（旧六值标签迁移），
迁移失败必须**抛错**而不是悄悄降档。
"""

from __future__ import annotations

import pytest

from backend.agents.base import ProposalReview
from backend.agents.contracts import (
    NON_ACTION_OUTCOMES,
    ConfidenceSource,
    OrchestrationPolicy,
    PriorityLevel,
    ProposalOutcome,
)
from backend.agents.energy import EnergyAgent
from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.runtime import DEFAULT_AGENT_FACTORIES, build_default_agents
from backend.agents.scene import SceneAgent
from backend.agents.security import SecurityAgent
from backend.engine.event_bus import SimEvent
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    WorldState,
)


def _world(*, occupied: bool = True) -> WorldState:
    world = WorldState(environment=EnvironmentState(time_of_day="20:00"))
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=30.0,
            occupancy=occupied,
            persons=["user_01"] if occupied else [],
        )
    }
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(power=True, extra={"brightness": 5, "color_temp": 6000}),
        ),
        "hvac_living_01": DeviceState(
            id="hvac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True, extra={"target_temp": 26.0, "mode": "cool", "speed": "low"}
            ),
        ),
    }
    return world


def _event(event_type: str = "user.enters_room", **data) -> SimEvent:
    return SimEvent(event_type=event_type, source="test", timestamp=1.0, data=data)


# --------------------------------------------------- 既有 agent 免改迁移


@pytest.mark.parametrize("agent_cls", [LightingAgent, HVACAgent])
def test_legacy_agents_get_section_9_1_tiers_for_free(agent_cls):
    agent = agent_cls()
    world = _world()
    tier = agent.proposal_priority(world, _event())
    assert isinstance(tier, PriorityLevel)
    assert tier is PriorityLevel.COMFORT


@pytest.mark.parametrize("agent_cls", [LightingAgent, HVACAgent])
def test_a_domain_agent_woken_by_a_user_command_does_not_land_on_the_user_tier(agent_cls):
    """S3 复审 blocker：``explicit_user`` 属于**真人**，不属于替真人做事的 agent。

    这条测试原本断言的正是那个缺陷（``user.command`` → ``explicit_user``）。它读起来像
    在测迁移表，实际上钉住了两件坏事：域 agent 对真人占用免疫、且白拿 §9.2 三类单边拒绝
    的豁免。迁移表本身（``direct_user_command`` → ``EXPLICIT_USER``）没有问题，仍由
    tests/test_orchestrator_contract.py 覆盖——它只该用来解析**历史 payload**。
    """

    from backend.agents.contracts import outranks

    agent = agent_cls()
    tier = agent.proposal_priority(_world(), _event("user.command", device_id="light_living_01"))
    assert tier is not PriorityLevel.EXPLICIT_USER
    assert outranks(PriorityLevel.EXPLICIT_USER, tier)
    # 替用户做事仍落在域内最高的那一档，不是被降到氛围档
    assert tier is PriorityLevel.COMFORT


def test_unmapped_legacy_label_raises_instead_of_downgrading():
    class BrokenAgent(LightingAgent):
        def determine_priority(self, world_state, root_event):  # type: ignore[override]
            return "whatever_tier"

    with pytest.raises(ValueError):
        BrokenAgent().proposal_priority(_world(), _event())


# ------------------------------------------------------- 五种表达的产生者


def test_acted_is_the_default_when_rules_produce_commands():
    proposal = LightingAgent().propose(world_state=_world(), root_event=_event())
    assert proposal.outcome is ProposalOutcome.ACTED
    assert proposal.has_commands
    assert proposal.confidence_source is ConfidenceSource.RULE_BASED


def test_no_action_needed_says_why_instead_of_returning_nothing():
    """审计 §六「silently emitting nothing」的正面回归钉。"""

    proposal = EnergyAgent().propose(
        world_state=_world(occupied=True), root_event=_event("user.arrives_home")
    )
    assert proposal.outcome is ProposalOutcome.NO_ACTION_NEEDED
    assert proposal.noop_reason
    assert proposal.commands == []


def test_low_confidence_withholds_commands_rather_than_executing_silently():
    """confidence 终于有了消费者：低于阈值 → 命令被扣下并留痕。"""

    agent = LightingAgent()
    policy = OrchestrationPolicy(min_confidence=0.99)
    proposal = agent.propose(world_state=_world(), root_event=_event(), policy=policy)
    assert proposal.outcome is ProposalOutcome.LOW_CONFIDENCE
    assert proposal.commands == []
    assert proposal.withheld_commands, "被扣下的命令必须留痕"
    assert "0.99" in (proposal.noop_reason or "")


def test_needs_human_confirmation_is_policy_driven():
    agent = LightingAgent()
    policy = OrchestrationPolicy(require_human_confirmation=True, min_confidence=0.0)
    proposal = agent.propose(world_state=_world(), root_event=_event(), policy=policy)
    assert proposal.outcome is ProposalOutcome.NEEDS_HUMAN_CONFIRMATION
    assert proposal.requires_confirmation is True
    assert proposal.withheld_commands


def test_missing_observations_names_what_is_missing():
    proposal = SceneAgent().propose(
        world_state=_world(),
        root_event=_event("user.command", message_type="CMD_SCENE_APPLY"),
    )
    assert proposal.outcome is ProposalOutcome.MISSING_OBSERVATIONS
    assert proposal.missing_observations == ["scene_id"]


def test_unsafe_rejected_carries_risks_and_withheld_commands():
    world = _world(occupied=False)
    world.devices["camera_living_01"] = DeviceState(
        id="camera_living_01",
        type="camera",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"online": False}),
    )
    proposal = SecurityAgent().propose(
        world_state=world,
        root_event=_event("device.offline", device_id="camera_living_01", device_type="camera"),
    )
    assert proposal.outcome is ProposalOutcome.UNSAFE_REJECTED
    assert proposal.risks
    assert proposal.withheld_commands


def test_every_non_action_outcome_has_a_real_producer():
    """五种非动作表达一个都不能是"只有合成夹具走过"。"""

    produced = set()
    for outcome in (
        SceneAgent().propose(
            world_state=_world(),
            root_event=_event("user.command", message_type="CMD_SCENE_APPLY"),
        ),
        SecurityAgent().propose(
            world_state=_world(occupied=True), root_event=_event("user.arrives_home")
        ),
        LightingAgent().propose(
            world_state=_world(),
            root_event=_event(),
            policy=OrchestrationPolicy(min_confidence=0.99),
        ),
        LightingAgent().propose(
            world_state=_world(),
            root_event=_event(),
            policy=OrchestrationPolicy(require_human_confirmation=True, min_confidence=0.0),
        ),
    ):
        produced.add(outcome.outcome)

    world = _world(occupied=False)
    world.devices["camera_living_01"] = DeviceState(
        id="camera_living_01",
        type="camera",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"online": False}),
    )
    produced.add(
        SecurityAgent()
        .propose(
            world_state=world,
            root_event=_event("device.offline", device_id="camera_living_01", device_type="camera"),
        )
        .outcome
    )
    assert produced == set(NON_ACTION_OUTCOMES)


# ------------------------------------------------------------- 判定顺序


def test_domain_review_outranks_policy_and_confidence():
    """判定顺序只有一处：领域裁决在最前，不被策略/置信度盖掉。"""

    class RejectingAgent(LightingAgent):
        def review_proposal(self, **kwargs):  # type: ignore[override]
            return ProposalReview(
                outcome=ProposalOutcome.UNSAFE_REJECTED,
                noop_reason="领域判定拒绝",
                risks=["测试风险"],
            )

    proposal = RejectingAgent().propose(
        world_state=_world(),
        root_event=_event(),
        policy=OrchestrationPolicy(require_human_confirmation=True, min_confidence=0.99),
    )
    assert proposal.outcome is ProposalOutcome.UNSAFE_REJECTED


# --------------------------------------------------------- 默认注册顺序


def test_default_agent_registration_order_is_the_contract():
    """注册顺序 = TaskPlan.domain_tasks 序 = canonical trace 行序（S2-T9 门的前提）。"""

    assert [agent.agent_id for agent in build_default_agents()] == [
        "lighting_agent",
        "hvac_agent",
        "security_agent",
        "energy_agent",
        "scene_agent",
    ]
    assert len(DEFAULT_AGENT_FACTORIES) == 5


def test_default_agents_cover_the_five_section_8_2_roles():
    roles = [agent.role for agent in build_default_agents()]
    assert roles == ["lighting", "hvac", "security", "energy", "scene"]


# ----------------------------------------------- 迁移期旧标签的取舍（可证伪）


def test_no_domain_agent_can_outrank_an_explicit_user_command():
    """迁移期护栏（S3-T5 后改钉 §9.1 全序表）：域 agent 不得压过用户直控。

    这条护栏原本钉的是旧 ``arbiter.PRIORITY_RANK``——那张表里 safety 与
    direct_user_command **同分**，于是任何借 safety 表达自己的新 agent 都会把
    "用户覆盖 agent" 变成掷硬币。S3-T5 换上 §9.1 七档严格全序之后，同分这件事在结构上
    已经不可能，护栏因此改成断言**真正的语义**：唯一能压过用户的只有 safety，而且只有
    真安全事件（烟雾）能拿到 safety 档。
    """

    from backend.agents.contracts import outranks

    world = _world()
    cases = [
        (SecurityAgent(), _event("user.leaves_home")),
        (SecurityAgent(), _event("security.presence_detected")),
        (EnergyAgent(), _event("user.leaves_home")),
        (SceneAgent(), _event("user.command", message_type="CMD_SCENE_APPLY", scene_id="away")),
    ]
    for agent, event in cases:
        tier = agent.proposal_priority(world, event)
        assert outranks(PriorityLevel.EXPLICIT_USER, tier), (
            f"{agent.agent_id} 的档位 {tier.value} 压过了用户指令"
        )

    # 烟雾是唯一的例外：safety 是 §9.1 里唯一严格高于 explicit_user 的档。
    smoke_tier = SecurityAgent().proposal_priority(world, _event("safety.smoke_detected"))
    assert smoke_tier is PriorityLevel.SAFETY
    assert outranks(smoke_tier, PriorityLevel.EXPLICIT_USER)


@pytest.mark.parametrize(
    ("agent", "event", "expected"),
    [
        (SceneAgent(), "user.command", PriorityLevel.AMBIENCE),
        (EnergyAgent(), "user.leaves_home", PriorityLevel.ENERGY),
    ],
)
def test_legacy_label_and_section_9_1_tier_agree_where_the_old_table_allows(
    agent, event, expected
):
    """旧表存在对应档时，新旧两张表必须一致（不一致 = 迁移期埋了一个隐性降级）。"""

    from backend.agents.contracts import migrate_legacy_priority

    world = _world()
    root = _event(event, message_type="CMD_SCENE_APPLY", scene_id="away")
    assert agent.proposal_priority(world, root) is expected
    assert migrate_legacy_priority(agent.determine_priority(world, root)) is expected
