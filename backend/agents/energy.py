"""EnergyAgent —— §8.2「may veto or downgrade comfort actions when home is empty」。

这是 S3 唯一一处**解释**而非转写 spec 的地方，所以规则被写成常量 + 解释文本，而不是
埋在分支里（plan risk 原文：*record the chosen rule in the coordination_decision
explanation so researchers see it*）：

    :data:`EnergyAgent.VETO_RULE_ID` = ``unoccupied_home_only``
    仅当全屋无人时，energy 档可以否决 comfort/ambience 档提案；
    有人在家时**不产生任何否决**——§9.1 的 comfort > energy 全序保持不变。

为什么否决权必须存在：审计 §六 记着两条坑，这个文件同时收两条——

* ``energy_efficiency`` 是**死档**（没有任何 agent 产出过），§9.1 下半张表从未被走过；
* 仲裁器只处理"同设备同属性"一类冲突（§9.2 五类里的一类）。否决是第二类冲突
  （"资源竞争 / 策略互斥"）的第一个真实生产者：它不是"同一属性被写了两个值"，
  而是"这条动作根本不该发生"。

否决**永不越级**：safety / explicit_user / security 三档不可否决（:data:`VETOABLE_TIERS`），
否则"省电"能盖过"烟雾报警"。

**这条否决位于 §9.1 全序之外，不是全序的一部分**（S3 复审 minor）。``energy`` 在秩上低于
``comfort``，可它在全屋无人时否得掉 comfort——那是一次刻意的倒置，来自 §8.2 而不是 §9.1。
两者只能有一个口径写进事件流，所以仲裁器把这一类拒绝标成
:attr:`~backend.agents.arbiter.ConflictResolution.CONSTRAINT_VETO`，并在理由里写明它不是
按秩赢的。S4 的评估器据此挑出"可以假设秩单调"的那些拒绝，不会把每条无人在家的 episode
误判成排名表被违反。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from backend.agents.base import BaseAgent, ProposalReview
from backend.agents.contracts import (
    AgentProposal,
    DomainTask,
    PriorityLevel,
    ProposalOutcome,
)
from backend.agents.types import AgentCommandProposal, PriorityLabel
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import (
    ENVIRONMENT_TEMPERATURE_THRESHOLD,
    USER_ARRIVES_HOME,
    USER_LEAVES_HOME,
)
from backend.engine.state import DeviceState, WorldState

__all__ = [
    "ENERGY_AGENT_ID",
    "ENERGY_ROOT_EVENT_TYPES",
    "VETOABLE_TIERS",
    "EnergyAgent",
    "EnergyVeto",
    "EnergyVetoReview",
]


ENERGY_AGENT_ID = "energy_agent"

# 能耗 agent 只对这三类根事件开 episode：人不在了、人回来了、温度越界。
# 别的事件（进出房间、光照阈值、安防）与"该不该停机"无关，多开一轮只是噪声。
ENERGY_ROOT_EVENT_TYPES: frozenset[str] = frozenset(
    {USER_LEAVES_HOME, USER_ARRIVES_HOME, ENVIRONMENT_TEMPERATURE_THRESHOLD}
)

# 可被否决的档位：**严格低于** security。这张表是"否决不越级"的机制表达，
# 不要改成 `priority_rank(x) <= priority_rank(ENERGY)`——那样 energy 会否决自己。
#
# 注意 COMFORT 在表里是一次**有意的全序倒置**（energy 秩低于 comfort 却否得掉它）：
# 见模块头。倒置本身合法（§8.2），但它必须在输出里被标成 constraint_veto，
# 不能冒充"低档赢了 §9.1 全序"。
VETOABLE_TIERS: frozenset[PriorityLevel] = frozenset(
    {PriorityLevel.COMFORT, PriorityLevel.AMBIENCE, PriorityLevel.MAINTENANCE}
)


class EnergyVeto(BaseModel):
    """一条否决：谁的哪条命令被否了、为什么、按哪条规则（§9.2 的冲突分类输入）。

    S3-T5 的仲裁器消费它，把它归成 ``energy_vs_comfort`` 类冲突并写进 §9.3 的三项输出。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    device_id: str
    property: str
    vetoed_agent_id: str
    vetoed_priority: PriorityLevel
    reason: str
    rule: str


class EnergyVetoReview(BaseModel):
    """一次否决评审的完整结果（含"这次为什么没否"）。

    ``explanation`` 必须自带规则 id：研究者在事件流里看到一条否决时，要能不看代码就知道
    它是按哪条规则做的——这正是"S3 解释了 spec"这件事的可审计化。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    home_occupied: bool
    vetoes: tuple[EnergyVeto, ...] = ()
    explanation: str = ""
    reviewed_agent_ids: tuple[str, ...] = ()


class EnergyAgent(BaseAgent):
    """能耗域：无人时停机；并对舒适档提案行使否决权。"""

    agent_role = "energy"
    subscribed_event_types = tuple(sorted(ENERGY_ROOT_EVENT_TYPES))

    #: 否决规则 id。改名等于改口径——它会出现在事件流与仲裁输出里。
    VETO_RULE_ID = "unoccupied_home_only"
    VETO_RULE_TEXT = (
        "仅当全屋无人时，energy 档可否决 comfort/ambience/maintenance 档提案；"
        "有人在家时 §9.1 的 comfort > energy 全序不变（S3 对 §8.2 的显式解释）。"
        "这条否决是 §9.1 全序之外的约束，不代表 energy 档在秩上压过了 comfort 档"
    )

    def __init__(self) -> None:
        super().__init__(agent_id=ENERGY_AGENT_ID, name="Energy Agent")

    # ------------------------------------------------------------- 基本声明

    def get_controlled_device_types(self) -> list[str]:
        return ["hvac", "fan"]

    def determine_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> PriorityLabel:
        return "energy_efficiency"

    def proposal_priority(self, world_state: WorldState, root_event: SimEvent) -> PriorityLevel:
        return PriorityLevel.ENERGY

    def proposal_intent(self, world_state: WorldState, root_event: SimEvent) -> str:
        if self.home_occupied(world_state):
            return "有人在家：能耗让位于舒适"
        return "全屋无人：停掉制冷/送风负载"

    # ------------------------------------------------------------- 相关面

    def is_relevant(self, world_state: WorldState, root_event: SimEvent) -> bool:
        if root_event.event_type not in ENERGY_ROOT_EVENT_TYPES:
            return False
        # 环境类根事件沿用基类那条去抖前提：没有"显著变化理由"就不值得开一轮。
        if root_event.event_type == ENVIRONMENT_TEMPERATURE_THRESHOLD:
            return bool(root_event.data.get("significant_change_reasons"))
        return True

    def get_relevant_devices(
        self, world_state: WorldState, root_event: SimEvent
    ) -> list[DeviceState]:
        return sorted(self._get_my_devices(world_state), key=lambda device: device.id)

    def get_allowed_command_specs(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for device in self.get_relevant_devices(world_state, root_event):
            specs.append({"device_id": device.id, "property": "power"})
            if device.type == "hvac":
                specs.append({"device_id": device.id, "property": "extra.target_temp"})
                specs.append({"device_id": device.id, "property": "extra.mode"})
            if device.type in ("hvac", "fan"):
                specs.append({"device_id": device.id, "property": "extra.speed"})
        return specs

    # ------------------------------------------------------------- 规则决策

    def decide(self, world_state: WorldState) -> list[dict]:
        if self.home_occupied(world_state):
            # 有人在家：舒适域说了算，能耗域一条命令都不发（§9.1 comfort > energy）。
            return []

        actions: list[dict] = []
        for device in sorted(self._get_my_devices(world_state), key=lambda item: item.id):
            if not device.state.power:
                continue
            actions.append(
                {
                    "device_id": device.id,
                    "property": "power",
                    "value": False,
                    "reason": f"全屋无人：停掉 {device.id} 的运行负载",
                }
            )
        return actions

    def review_proposal(
        self,
        *,
        world_state: WorldState,
        root_event: SimEvent,
        commands: list[AgentCommandProposal],
        domain_task: DomainTask | None = None,
    ) -> ProposalReview | None:
        if commands:
            return None
        if self.home_occupied(world_state):
            return ProposalReview(
                outcome=ProposalOutcome.NO_ACTION_NEEDED,
                noop_reason=(
                    "有人在家：§9.1 舒适档高于能耗档，本轮不提案，也不否决舒适域"
                ),
            )
        return ProposalReview(
            outcome=ProposalOutcome.NO_ACTION_NEEDED,
            noop_reason="全屋无人，但受控设备已全部停机，无需再动",
        )

    # --------------------------------------------------------------- 否决权

    def review_peer_proposals(
        self,
        proposals: Sequence[AgentProposal],
        world_state: WorldState,
    ) -> EnergyVetoReview:
        """对同一轮里其他 agent 的提案行使 §8.2 否决权。

        纯函数（不写世界、不改自身状态），因此仲裁器可以在任意时刻重放它。
        输出按 ``(agent_id, device_id, property)`` 升序——确定性门不接受集合迭代序泄漏。
        """

        occupied = self.home_occupied(world_state)
        reviewed = tuple(sorted({proposal.agent_id for proposal in proposals}))

        if occupied:
            return EnergyVetoReview(
                home_occupied=True,
                vetoes=(),
                reviewed_agent_ids=reviewed,
                explanation=(
                    f"规则 {self.VETO_RULE_ID}：有人在家，能耗域不行使否决权。"
                    f"{self.VETO_RULE_TEXT}"
                ),
            )

        vetoes: list[EnergyVeto] = []
        for proposal in proposals:
            if proposal.agent_id == self.agent_id:
                continue  # 不否决自己
            if proposal.is_non_action or not proposal.commands:
                continue  # 没动作就没有可否决的对象
            if proposal.priority not in VETOABLE_TIERS:
                continue  # 不越级（safety / explicit_user / security / energy 本档）
            for command in proposal.commands:
                vetoes.append(
                    EnergyVeto(
                        device_id=command.device_id,
                        property=command.property,
                        vetoed_agent_id=proposal.agent_id,
                        vetoed_priority=proposal.priority,
                        rule=self.VETO_RULE_ID,
                        reason=(
                            f"全屋无人时 {proposal.agent_id} 仍要把 {command.device_id}."
                            f"{command.property} 设为 {command.value!r}；"
                            f"无人受益的舒适动作按能耗策略否决"
                        ),
                    )
                )

        ordered = tuple(
            sorted(vetoes, key=lambda veto: (veto.vetoed_agent_id, veto.device_id, veto.property))
        )
        explanation = (
            f"规则 {self.VETO_RULE_ID}：全屋无人，否决 {len(ordered)} 条舒适/氛围档命令。"
            f"{self.VETO_RULE_TEXT}"
        )
        return EnergyVetoReview(
            home_occupied=False,
            vetoes=ordered,
            reviewed_agent_ids=reviewed,
            explanation=explanation,
        )

    # ------------------------------------------------------------- 观测助手

    @staticmethod
    def home_occupied(world_state: WorldState) -> bool:
        """全屋是否有人。房间占用是 §2.3 可观测投影里保留的字段，安全可读。"""

        return any(room.occupancy for room in world_state.rooms.values())
