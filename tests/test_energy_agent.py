"""S3-T4：EnergyAgent —— §8.2「may veto or downgrade comfort actions when home is empty」。

否决权是 S3 唯一一处**解释**而非转写 spec 的地方，所以规则本身要被测死、也要被写进
解释文本里（plan risk 原文：*record the chosen rule in the coordination_decision
explanation so researchers see it*）：

    仅当全屋无人时，energy 档可以否决 comfort/ambience 档提案；
    有人在家时不产生任何否决——§9.1 的 comfort > energy 全序保持不变。

另外两件事同样被钉死：

* energy 档此前是**死档**（审计 §六：safety / energy_efficiency / background_optimization
  三档没有任何 agent 产出过），EnergyAgent 是它的第一个真实生产者；
* 否决**永远不越级**：safety / explicit_user / security 三档不可否决，否则"省电"能
  盖过"烟雾报警"。
"""

from __future__ import annotations

import pytest

from backend.agents.contracts import AgentProposal, PriorityLevel, ProposalOutcome
from backend.agents.energy import ENERGY_AGENT_ID, EnergyAgent, EnergyVeto
from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    WorldState,
)


# --------------------------------------------------------------------- 夹具


def _world(*, occupied: bool = False) -> WorldState:
    world = WorldState(environment=EnvironmentState(time_of_day="14:00"))
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            temperature=28.0,
            occupancy=occupied,
            persons=["user_01"] if occupied else [],
        ),
    }
    world.devices = {
        "hvac_living_01": DeviceState(
            id="hvac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True, extra={"target_temp": 22.0, "mode": "cool", "speed": "high"}
            ),
        ),
        "fan_living_01": DeviceState(
            id="fan_living_01",
            type="fan",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(power=True, extra={"speed": "high", "shake": False}),
        ),
    }
    return world


def _event(event_type: str, **data) -> SimEvent:
    return SimEvent(event_type=event_type, source="test", timestamp=1.0, data=data)


def _comfort_proposal(agent_id: str = "hvac_agent") -> AgentProposal:
    return AgentProposal(
        agent_id=agent_id,
        agent_role="hvac",
        intent="维持舒适区温度",
        priority=PriorityLevel.COMFORT,
        confidence=0.8,
        commands=[
            AgentCommandProposal(
                device_id="hvac_living_01",
                property="extra.target_temp",
                value=22.0,
                reason="降到舒适区",
            )
        ],
    )


# --------------------------------------------------------------- 自身提案


def test_empty_home_proposes_shutdown_at_energy_tier():
    agent = EnergyAgent()
    proposal = agent.propose(world_state=_world(occupied=False), root_event=_event("user.leaves_home"))

    assert proposal.agent_id == ENERGY_AGENT_ID
    assert proposal.priority is PriorityLevel.ENERGY  # 审计坑：此前是死档
    assert proposal.outcome is ProposalOutcome.ACTED
    assert {(command.device_id, command.property, command.value) for command in proposal.commands} == {
        ("hvac_living_01", "power", False),
        ("fan_living_01", "power", False),
    }


def test_occupied_home_yields_explicit_no_action_needed():
    agent = EnergyAgent()
    proposal = agent.propose(world_state=_world(occupied=True), root_event=_event("user.arrives_home"))

    assert proposal.outcome is ProposalOutcome.NO_ACTION_NEEDED
    assert proposal.commands == []
    assert proposal.noop_reason


# ------------------------------------------------------------------- 否决权


def test_empty_home_vetoes_comfort_hvac_proposal():
    """§8.2 否决权：全屋无人时，energy 档否决 comfort 档对空调的动作。"""

    agent = EnergyAgent()
    review = agent.review_peer_proposals([_comfort_proposal()], _world(occupied=False))

    assert review.home_occupied is False
    assert len(review.vetoes) == 1
    veto = review.vetoes[0]
    assert isinstance(veto, EnergyVeto)
    assert veto.device_id == "hvac_living_01"
    assert veto.property == "extra.target_temp"
    assert veto.vetoed_agent_id == "hvac_agent"
    assert veto.vetoed_priority is PriorityLevel.COMFORT
    assert veto.rule == EnergyAgent.VETO_RULE_ID
    assert veto.reason
    # 研究者必须能在解释里读到"这条规则是 S3 选的"，而不是从代码里反推。
    assert EnergyAgent.VETO_RULE_ID in review.explanation


def test_occupied_home_does_not_veto_comfort():
    """§9.1：有人在家时 comfort > energy 的全序不被否决权破坏。"""

    agent = EnergyAgent()
    review = agent.review_peer_proposals([_comfort_proposal()], _world(occupied=True))

    assert review.home_occupied is True
    assert review.vetoes == ()
    assert review.explanation


@pytest.mark.parametrize(
    "priority",
    [PriorityLevel.SAFETY, PriorityLevel.EXPLICIT_USER, PriorityLevel.SECURITY],
)
def test_veto_never_crosses_above_energy(priority: PriorityLevel):
    """省电不得盖过安全 / 用户明示 / 安防——否则否决权就是一个越级 bug。"""

    agent = EnergyAgent()
    peer = _comfort_proposal().model_copy(update={"priority": priority})
    review = agent.review_peer_proposals([peer], _world(occupied=False))
    assert review.vetoes == ()


def test_veto_ignores_non_action_proposals():
    agent = EnergyAgent()
    peer = AgentProposal(
        agent_id="lighting_agent",
        intent="无事可做",
        priority=PriorityLevel.COMFORT,
        confidence=0.7,
        outcome=ProposalOutcome.NO_ACTION_NEEDED,
        noop_reason="本轮没有需要调整的灯",
    )
    review = agent.review_peer_proposals([peer], _world(occupied=False))
    assert review.vetoes == ()


def test_veto_never_targets_itself():
    agent = EnergyAgent()
    own = AgentProposal(
        agent_id=ENERGY_AGENT_ID,
        intent="停机省电",
        priority=PriorityLevel.ENERGY,
        confidence=0.7,
        commands=[
            AgentCommandProposal(device_id="hvac_living_01", property="power", value=False)
        ],
    )
    review = agent.review_peer_proposals([own], _world(occupied=False))
    assert review.vetoes == ()


def test_veto_order_is_deterministic():
    """确定性门（S2-T9）：否决列表不能有集合迭代序泄漏。"""

    agent = EnergyAgent()
    peers = [
        _comfort_proposal(agent_id="zeta_agent"),
        _comfort_proposal(agent_id="alpha_agent"),
    ]
    forward = agent.review_peer_proposals(peers, _world(occupied=False))
    backward = agent.review_peer_proposals(list(reversed(peers)), _world(occupied=False))
    assert [veto.model_dump() for veto in forward.vetoes] == [
        veto.model_dump() for veto in backward.vetoes
    ]
    assert [veto.vetoed_agent_id for veto in forward.vetoes] == ["alpha_agent", "zeta_agent"]


def test_energy_agent_relevance_is_narrow():
    agent = EnergyAgent()
    world = _world()
    assert agent.is_relevant(world, _event("user.leaves_home")) is True
    assert agent.is_relevant(world, _event("user.arrives_home")) is True
    assert agent.is_relevant(world, _event("security.presence_detected")) is False
    assert agent.is_relevant(world, _event("environment.state_refresh")) is False
