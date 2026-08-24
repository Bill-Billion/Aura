"""§9 协调与仲裁：七档严格全序 + 五类冲突 + 三项输出 + 一条 coordination_decision。

2026-07-16 复审对旧仲裁器（69 行）记了四条坑，这个模块逐条收：

1. **档位不是全序**。旧 ``PRIORITY_RANK`` 只有六档，``safety`` 与
   ``direct_user_command`` 并列第 5，``security`` 档不存在。并列意味着"谁先进循环谁赢"，
   仲裁结果无法解释。现在排名唯一来源是
   :func:`~backend.agents.contracts.priority_rank`（七档、秩两两不等），本模块**不再**
   自带第二张排名表。
2. **只实现了五类冲突里的一类**。§9.2 五类现在都有产生路径，且每条被拒命令都带
   :class:`ConflictClass` 标签——"为什么没执行"从散文变成可断言的枚举。
3. **从不读世界**。"设备不可用却被选中"这一类在结构上要求读世界，因此
   :meth:`Arbiter.resolve` 收一个 ``world_snapshot``；不传就发现不了这一类
   （见 tests/test_arbiter.py::test_device_unavailable_conflict_requires_world_snapshot）。
4. **用户命令从不进仲裁**。main.py 过去直接落地 UI 命令，runtime 又显式跳过
   ``CMD_DEVICE_CONTROL``，于是"用户覆盖 agent"只是名义上的。现在 UI 腿也走仲裁，
   并通过 :class:`ArbitrationGate` 在 §9.1 的 ``explicit_user`` 档登记**占用**
   （:class:`ExplicitUserClaim`）：并发 episode 读到这条占用就会输。

—— 两条口径（S3 复审加的，别再退回去）———————————————————————

**(a) ``explicit_user`` 按行为体判，不按自称的档位判。** 该档的含义是"这条命令的行为体
是用户"，不是"某个 agent 在响应用户事件"。因此凡是"用户特权"（对占用免疫、豁免 §9.2
三类单边拒绝）一律用 :func:`is_user_actor` 按 ``agent_id`` 判定——否则任何 agent 只要在
``determine_priority`` 里返回 ``direct_user_command``，就能同时买到免疫与豁免，
"用户覆盖 agent" 在用户最活跃的那些 episode 上原地失效（复审复现过两次）。

**(b) 严格全序有且只有一个例外，它必须自报家门。** §9.1 是七档严格全序，
:func:`priority_rank` 是唯一排名来源；但 §8.2 给了能耗域一条**全序之外**的约束否决
（全屋无人时 ``energy`` 可以否掉 ``comfort``——低档否掉高档）。行为保留，但这类拒绝在
输出里打 :attr:`ConflictResolution.CONSTRAINT_VETO` 标签并在理由里写明"位于全序之外"。
不变式因此可证伪：凡标 :attr:`ConflictResolution.TOTAL_ORDER` 的拒绝，赢家的秩不低于
输家（tests/test_arbiter.py::test_no_rejection_claims_a_lower_tier_won_the_total_order）。

—— 仲裁门坐在哪 ——————————————————————————————————————————————

S1 的迁移表把 ``proposed → approved | rejected`` 这条岔口留给了 S3，并在
``CommandExecutor.submit(..., pre_submit=hook)`` 预留了接缝（出厂 no-op）。
:class:`ArbitrationGate` 就装在那条接缝上，顺序是：

    §8.4 提案 → DeviceCommand(proposed，登记进 executor 在飞表)
      → 仲裁 → 落败者 proposed→rejected（带冲突详情，绝不静默消失）
      → 胜出者 executor.execute_approved(...)（proposed→approved→…→succeeded）

"登记进 executor 在飞表"是关键的一步：它把 S1 文档里"取代只在同批/待发队列内"的边界
**扩宽**到了仲裁窗口——一条用户命令因此能在 agent 命令还停在 ``proposed`` 等仲裁时把它
取代掉。S1/S2 修过的那条中途取代竞态（先落状态再发事件、每个 await 之后复查终态）在这里
必须继续成立：本模块每次动记录之前都问一次 ``record.is_terminal``。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.contracts import (
    AgentProposal,
    PriorityLevel,
    priority_rank,
)
from backend.agents.energy import ENERGY_AGENT_ID, EnergyVetoReview
from backend.agents.types import AgentCommandProposal
from backend.core.logging import log
from backend.engine.event_bus import SimEvent
from backend.engine.observation import device_reports_online
from backend.engine.state import DeviceState, WorldState
from backend.execution.command import (
    CommandRecord,
    CommandSource,
    CommandStatus,
    DeviceCommand,
    PublishEvent,
)
from backend.execution.executor import (
    CommandExecutor,
    CommandTarget,
    PreExecuteHook,
    PreSubmitDecision,
)

__all__ = [
    "ARBITER_ID",
    "COORDINATION_DECISION_EVENT_TYPE",
    "UI_ACTOR_ID",
    "UNILATERAL_EXEMPT_TIERS",
    "USER_ACTOR_IDS",
    "AgentArbitrationOutcome",
    "Arbiter",
    "ArbiterResult",
    "ArbitratedCommand",
    "ArbitrationConflict",
    "ArbitrationGate",
    "ConflictClass",
    "ConflictResolution",
    "ExplicitUserClaim",
    "RejectedCommand",
    "is_user_actor",
]


ARBITER_ID = "arbiter"
COORDINATION_DECISION_EVENT_TYPE = "reasoning.coordination_decision"
# UI 直控在 §9.1 里的提案身份。它不是 agent，但仲裁的输入契约是"提案"，
# 所以用户也要有一个稳定的提案者 id（占用记录、冲突记录都按它归因）。
UI_ACTOR_ID = "user_ui"

# 行为体确实是"真人"的提案者 id。UI 腿的提案与 :class:`ExplicitUserClaim` 都用它们归因。
#
# ``"user"`` 在列是因为 :attr:`ExplicitUserClaim.agent_id` 的默认值是它（手搓占用的夹具、
# 以及 ``DeviceCommand.actor`` 为空时 :meth:`ArbitrationGate.pre_submit` 的回退口径）。
USER_ACTOR_IDS: frozenset[str] = frozenset({UI_ACTOR_ID, "user"})


def is_user_actor(agent_id: str) -> bool:
    """这条提案的**行为体**是不是真人（UI 腿）。

    §9.1 的 ``explicit_user`` 档说的是"命令的行为体是用户"，不是"某个 agent 在响应用户
    事件"。所有"用户特权"都按这个函数判定，**不**按提案自称的档位判定——档位是提案者
    自己填的字段，用它发豁免等于让 agent 自己给自己签特权（S3 复审 blocker）。
    """

    return agent_id in USER_ACTOR_IDS


# 不受"单边拒绝"（设备不可用 / 隐私 / 能耗否决）约束的**档位**。
#
# 为什么安全档要豁免：这三类拒绝都是仲裁器**代替**用户/安全策略做的判断，而烟雾疏散
# 不该被"省电"或"隐私"拦下。
#
# 用户直控同样豁免，但那一条**不在这张表里**：它按 :func:`is_user_actor` 走。理由是
# 同一件事的两种问法——"这条命令是谁发的"（身份，不可伪造）与"这条命令自称几档"
# （字段，谁都能填）。用户点了一台离线设备，正确的回答是 §10.2 词表里的
# ``device_offline``（由校验层给出，前端据此提示），不是一条"仲裁没通过"——把两套词表
# 混在一起，UI 腿与 agent 腿的错误码奇偶校验（S0-4 那条护栏）当场失效。
UNILATERAL_EXEMPT_TIERS: frozenset[PriorityLevel] = frozenset({PriorityLevel.SAFETY})

# §9.2 第二类冲突的**最小**效果轴。刻意不做通用效果引擎（plan risk 原文：
# *scope to the minimal detectors listed, do not build a general effect engine*）：
# 只认"这条命令把某个房间的光照/温度往哪个方向推"，认不出来就不判这一类。
LIGHT_AXIS = "light_level"
TEMPERATURE_AXIS = "temperature"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConflictClass(str, Enum):
    """spec §9.2 的五类冲突。每条被拒命令**必须**带其中一个标签。"""

    SAME_DEVICE_PROPERTY = "same_device_property"
    ROOM_ENVIRONMENT_GOAL = "room_environment_goal"
    ENERGY_VS_COMFORT = "energy_vs_comfort"
    SECURITY_VS_PRIVACY = "security_vs_privacy"
    DEVICE_UNAVAILABLE = "device_unavailable"


class ConflictResolution(str, Enum):
    """这条拒绝是**按什么**做出的——全序、全序之外的约束、还是无对手的单边判定。

    没有这一位时，一条 ``winner_priority=energy`` / ``priority=comfort`` 的拒绝在事件流
    里读起来就是"低档赢了全序"，研究者据此会推翻整张 §9.1 排名表。有了它，"严格全序"
    这句话变成一条可证伪的不变式（见模块头 (b)）。
    """

    #: 按 §9.1 秩裁决（同档由 :meth:`_Entry.sort_key` 的确定性 tie-break 定胜负）。
    #: 不变式：``priority_rank(winner) >= priority_rank(loser)``。
    TOTAL_ORDER = "total_order"
    #: §8.2 的能耗否决：**位于全序之外**的约束，允许低档否掉高档（且仅此一处）。
    CONSTRAINT_VETO = "constraint_veto"
    #: 无对手命令：这条命令按自身条件就不该执行（设备不可用 / 在家模式下的隐私）。
    UNILATERAL = "unilateral"


class ArbitratedCommand(_StrictModel):
    """一条进过仲裁的命令（批准侧）。"""

    agent_id: str
    agent_role: str = ""
    priority: PriorityLevel
    device_id: str
    property: str
    value: Any = None
    reason: str = ""

    @property
    def capability(self) -> str:
        return self.property.removeprefix("extra.")

    @property
    def target(self) -> CommandTarget:
        return (self.device_id, self.capability)

    def as_proposal(self) -> AgentCommandProposal:
        return AgentCommandProposal(
            device_id=self.device_id,
            property=self.property,
            value=self.value,
            reason=self.reason,
        )


class RejectedCommand(ArbitratedCommand):
    """§9.3 "Rejected commands"：被拒的命令 + 为什么 + 归哪一类冲突。"""

    conflict_class: ConflictClass
    rejection_reason: str
    winner_agent_id: str | None = None
    winner_priority: PriorityLevel | None = None
    resolution: ConflictResolution = ConflictResolution.TOTAL_ORDER


class ArbitrationConflict(_StrictModel):
    """§9.3 "Conflict explanation"：一条可读的冲突记录。"""

    conflict_class: ConflictClass
    loser_agent_id: str
    loser_priority: PriorityLevel
    explanation: str
    device_id: str | None = None
    property: str | None = None
    room_id: str | None = None
    winner_agent_id: str | None = None
    winner_priority: PriorityLevel | None = None
    resolution: ConflictResolution = ConflictResolution.TOTAL_ORDER


class AgentArbitrationOutcome(_StrictModel):
    """每个 agent 在这一轮里的结论。

    审计原文说旧实现"仲裁全序名存实亡"，症状之一就是 runtime 每个 agent 各发一条
    ``reasoning.coordination_decision``——于是"仲裁"看起来像每人自说自话。现在一条
    episode 只发一条，per-agent 结论降级成本事件里的这一段分解。
    """

    agent_id: str
    agent_role: str = ""
    priority: PriorityLevel
    outcome: str
    approved: int = 0
    rejected: int = 0
    noop_reason: str | None = None


class ArbiterResult(_StrictModel):
    """§9.3 的三项输出（批准 / 拒绝 / 冲突解释）+ winning_priority + 解释。"""

    approved_commands: list[ArbitratedCommand] = Field(default_factory=list)
    rejected_commands: list[RejectedCommand] = Field(default_factory=list)
    conflicts: list[ArbitrationConflict] = Field(default_factory=list)
    winning_priority: PriorityLevel | None = None
    explanation: str = ""
    per_agent: list[AgentArbitrationOutcome] = Field(default_factory=list)

    def approved_for(self, agent_id: str) -> list[ArbitratedCommand]:
        return [item for item in self.approved_commands if item.agent_id == agent_id]

    def rejected_for(self, agent_id: str) -> list[RejectedCommand]:
        return [item for item in self.rejected_commands if item.agent_id == agent_id]

    def event_data(self) -> dict[str, Any]:
        """``reasoning.coordination_decision`` 的 §9.3 payload。"""

        winning = self.winning_priority.value if self.winning_priority is not None else None
        if self.approved_commands and self.rejected_commands:
            outcome = "partial"
        elif self.approved_commands:
            outcome = "approved"
        elif self.rejected_commands:
            outcome = "conflicted"
        else:
            outcome = "no_commands"
        return {
            # §9.3 三项输出
            "approved_commands": [item.model_dump(mode="json") for item in self.approved_commands],
            "rejected_commands": [item.model_dump(mode="json") for item in self.rejected_commands],
            "conflicts": [item.model_dump(mode="json") for item in self.conflicts],
            "winning_priority": winning,
            "explanation": self.explanation,
            "per_agent": [item.model_dump(mode="json") for item in self.per_agent],
            # event_schema_version=1.0 compatibility; remove only with a v2 migrator.
            "agent_id": ARBITER_ID,
            "outcome": outcome,
            "priority": winning or "",
            "winning_commands": [item.as_proposal().model_dump() for item in self.approved_commands],
        }


class ExplicitUserClaim(_StrictModel):
    """一条 ``explicit_user`` 档的控制点占用。

    它是"用户覆盖 agent"从名义变成机制的那一位：用户命令经 :class:`ArbitrationGate`
    的 pre_submit 钩子登记占用，之后**在这条占用之后开始的**任何 episode，只要提案
    落在同一控制点，就在仲裁里输给它。

    占用带 ``issued_wall_time`` 而不是永久生效：一条早于本 episode 根事件的用户命令
    不该永久否掉后续所有自动化——那不是"用户优先"，那是"自动化被关掉了"。
    """

    device_id: str
    capability: str
    value: Any = None
    agent_id: str = "user"
    command_id: str = ""
    issued_wall_time: float = 0.0
    correlation_id: str | None = None

    @property
    def target(self) -> CommandTarget:
        return (self.device_id, self.capability)


class _Entry:
    """仲裁循环里的一条候选命令（提案 × 命令的笛卡尔积）。"""

    __slots__ = ("proposal", "command", "index", "recency")

    def __init__(
        self,
        proposal: AgentProposal,
        command: AgentCommandProposal,
        index: int,
        recency: float,
    ) -> None:
        self.proposal = proposal
        self.command = command
        self.index = index
        self.recency = recency

    @property
    def capability(self) -> str:
        return self.command.property.removeprefix("extra.")

    @property
    def target(self) -> CommandTarget:
        return (self.command.device_id, self.capability)

    @property
    def priority(self) -> PriorityLevel:
        return self.proposal.priority

    def sort_key(self) -> tuple:
        """裁决顺序：档位 → 根事件新近度 → agent_id → 命令序 → 目标。

        末两项让顺序**完全由内容决定**：把同一批提案打乱顺序再喂一遍，裁决与输出行序
        必须逐字节相同（tests/test_arbiter.py::test_resolve_is_deterministic_regardless_of_input_order），
        否则 S2 的字节一致性门会在 S3 落地后悄悄失效。
        """

        return (
            -priority_rank(self.proposal.priority),
            -self.recency,
            self.proposal.agent_id,
            self.index,
            self.command.device_id,
            self.command.property,
        )

    def to_approved(self) -> ArbitratedCommand:
        return ArbitratedCommand(
            agent_id=self.proposal.agent_id,
            agent_role=self.proposal.agent_role,
            priority=self.proposal.priority,
            device_id=self.command.device_id,
            property=self.command.property,
            value=self.command.value,
            reason=self.command.reason,
        )

    def to_rejected(
        self,
        *,
        conflict_class: ConflictClass,
        rejection_reason: str,
        resolution: ConflictResolution,
        winner_agent_id: str | None = None,
        winner_priority: PriorityLevel | None = None,
    ) -> RejectedCommand:
        return RejectedCommand(
            agent_id=self.proposal.agent_id,
            agent_role=self.proposal.agent_role,
            priority=self.proposal.priority,
            device_id=self.command.device_id,
            property=self.command.property,
            value=self.command.value,
            reason=self.command.reason,
            conflict_class=conflict_class,
            rejection_reason=rejection_reason,
            resolution=resolution,
            winner_agent_id=winner_agent_id,
            winner_priority=winner_priority,
        )


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _environment_goal(
    device: DeviceState, capability: str, value: Any, room_temperature: float | None
) -> tuple[str, int] | None:
    """这条命令把房间的哪个环境量往哪个方向推？认不出来返回 None。

    只覆盖 §9.2 第二类点名的最小面：灯/窗帘 → 房间光照；空调 mode/target_temp → 温度。
    风扇、湿度、空气质量一律不认——宁可漏判一类冲突，也不要一个半正确的通用效果模型
    在仲裁里凭空否掉合法命令。
    """

    if device.type == "light":
        if capability == "power":
            return (LIGHT_AXIS, 1 if value else -1)
        if capability == "brightness":
            number = _as_number(value)
            if number is None:
                return None
            return (LIGHT_AXIS, 1 if number > 0 else -1)
        return None

    if device.type == "curtain" and capability == "open_percent":
        number = _as_number(value)
        if number is None:
            return None
        return (LIGHT_AXIS, 1 if number > 0 else -1)

    if device.type == "hvac":
        if capability == "mode":
            if value == "cool":
                return (TEMPERATURE_AXIS, -1)
            if value == "heat":
                return (TEMPERATURE_AXIS, 1)
            return None
        if capability == "target_temp":
            target = _as_number(value)
            if target is None or room_temperature is None or target == room_temperature:
                return None
            return (TEMPERATURE_AXIS, 1 if target > room_temperature else -1)
    return None


class Arbiter:
    """spec §9 的仲裁器：**纯函数**，不写世界、不发事件、不碰 executor。

    发事件与推进命令生命周期是 :class:`ArbitrationGate` 的事。两者分开的理由很实际：
    仲裁必须能在单测里被逐类断言（五类冲突 × 七档全序 = 大量组合），而任何一处 I/O
    都会把那些组合变成 async 夹具。
    """

    def resolve(
        self,
        proposals: Sequence[AgentProposal],
        root_event: SimEvent,
        world_snapshot: WorldState | None = None,
        *,
        energy_review: EnergyVetoReview | None = None,
        user_claims: Mapping[CommandTarget, ExplicitUserClaim] | None = None,
    ) -> ArbiterResult:
        """裁决一轮提案。

        ``world_snapshot`` 是 §9.2 第五类（设备不可用却被选中）的**唯一**信息来源，
        也是第二类（同房间互斥环境目标）与第四类（安防 vs 隐私）判定的前提；不传就
        只做纯提案层的裁决。传进来的必须是 §2.3 的 observable 投影，不是 ground truth。
        """

        entries: list[_Entry] = []
        recency = float(root_event.timestamp or 0.0)
        for proposal in proposals:
            for index, command in enumerate(proposal.commands):
                entries.append(_Entry(proposal, command, index, recency))
        ordered = sorted(entries, key=lambda entry: entry.sort_key())

        veto_index = self._veto_index(energy_review)
        claims = dict(user_claims or {})
        root_wall_time = float(root_event.wall_time or 0.0)

        approved: list[ArbitratedCommand] = []
        rejected: list[RejectedCommand] = []
        conflicts: list[ArbitrationConflict] = []
        owners: dict[CommandTarget, _Entry] = {}
        axis_owners: dict[tuple[str, str], tuple[_Entry, int]] = {}

        for entry in ordered:
            device = (
                world_snapshot.devices.get(entry.command.device_id)
                if world_snapshot is not None
                else None
            )
            room = (
                world_snapshot.rooms.get(device.location.room)
                if world_snapshot is not None and device is not None
                else None
            )
            # 身份在前、档位在后：agent 不得靠自称档位买到用户的豁免（模块头 (a)）。
            exempt = (
                is_user_actor(entry.proposal.agent_id)
                or entry.priority in UNILATERAL_EXEMPT_TIERS
            )

            # —— 单边拒绝（不需要对手命令）——————————————————————————
            outcome = None
            if not exempt and world_snapshot is not None:
                outcome = self._check_device_available(entry, device)
            if outcome is None and not exempt and device is not None:
                outcome = self._check_privacy(entry, device, room)
            if outcome is None and not exempt:
                outcome = self._check_energy_veto(entry, veto_index)

            # —— 对手冲突（同控制点 / 同房间互斥目标）——————————————
            if outcome is None:
                outcome = self._check_user_claim(entry, claims, root_wall_time)
            if outcome is None:
                outcome = self._check_same_target(entry, owners)
            if outcome is None and device is not None:
                outcome = self._check_environment_goal(entry, device, room, axis_owners)

            if outcome is not None:
                rejected_command, conflict = outcome
                rejected.append(rejected_command)
                conflicts.append(conflict)
                continue

            owners.setdefault(entry.target, entry)
            if device is not None:
                goal = _environment_goal(
                    device,
                    entry.capability,
                    entry.command.value,
                    room.temperature if room is not None else None,
                )
                if goal is not None:
                    axis_owners.setdefault((device.location.room, goal[0]), (entry, goal[1]))
            approved.append(entry.to_approved())

        winning_priority = (
            max((item.priority for item in approved), key=priority_rank) if approved else None
        )
        per_agent = self._per_agent(proposals, approved, rejected)
        result = ArbiterResult(
            approved_commands=approved,
            rejected_commands=rejected,
            conflicts=conflicts,
            winning_priority=winning_priority,
            per_agent=per_agent,
            explanation=self._explain(approved, rejected, conflicts, winning_priority, energy_review),
        )
        if rejected:
            log.info(
                "arbitration_rejected_commands",
                rejected=len(rejected),
                classes=sorted({item.conflict_class.value for item in rejected}),
                winning_priority=winning_priority.value if winning_priority else None,
            )
        return result

    @staticmethod
    def _veto_index(
        energy_review: EnergyVetoReview | None,
    ) -> dict[tuple[str, str, str], Any]:
        """能耗否决索引：(被否 agent, 设备, 能力) → 否决记录。

        占用门（"仅当全屋无人"）已经在 :class:`~backend.agents.energy.EnergyAgent` 内部
        判过，仲裁器**不复判**——复判等于把同一条规则写两遍，两处迟早分叉。仲裁器只
        再守一条结构底线：否决不得越级（§9.1 的上三档不可被能耗否决）。
        """

        if energy_review is None:
            return {}
        return {
            (veto.vetoed_agent_id, veto.device_id, veto.property.removeprefix("extra.")): veto
            for veto in energy_review.vetoes
        }

    # -------------------------------------------------------------- 五类冲突

    def _check_device_available(
        self, entry: _Entry, device: DeviceState | None
    ) -> tuple[RejectedCommand, ArbitrationConflict] | None:
        """§9.2 (5) 设备不可用却被选中——**唯一**需要读世界的一类。"""

        if device is None:
            reason = f"{entry.command.device_id} 不在这个世界里（设备不存在）"
        elif not device_reports_online(device):
            reason = f"{entry.command.device_id} 处于离线状态，命令不可能被执行"
        else:
            return None

        return self._reject(
            entry,
            conflict_class=ConflictClass.DEVICE_UNAVAILABLE,
            reason=reason,
            resolution=ConflictResolution.UNILATERAL,
            room_id=device.location.room if device is not None else None,
        )

    def _check_privacy(
        self, entry: _Entry, device: DeviceState, room: Any
    ) -> tuple[RejectedCommand, ArbitrationConflict] | None:
        """§9.2 (4) 安防策略 vs 隐私/在家模式：有人在的房间不得开启摄像头。

        最小检测器：只认"摄像头被打开"这一个动作。SecurityAgent 按契约根本不写摄像头
        （camera 是只读观测面），所以这一类真正防的是**别的**提案源（LLM 幻觉出的命令、
        场景脚本、将来新增的 agent）绕过那条自律。
        """

        if device.type != "camera":
            return None
        if entry.capability not in {"power", "recording", "enabled", "streaming"}:
            return None
        if not entry.command.value:
            return None
        if room is None or not room.occupancy:
            return None

        return self._reject(
            entry,
            conflict_class=ConflictClass.SECURITY_VS_PRIVACY,
            reason=(
                f"{room.id} 有人在场，安防提案要打开 {device.id}；"
                f"在家模式下隐私优先于安防覆盖"
            ),
            resolution=ConflictResolution.UNILATERAL,
            room_id=room.id,
        )

    def _check_energy_veto(
        self, entry: _Entry, veto_index: Mapping[tuple[str, str, str], Any]
    ) -> tuple[RejectedCommand, ArbitrationConflict] | None:
        """§9.2 (3) 能耗策略 vs 舒适策略：EnergyAgent 的否决在这里变成一类冲突。

        这是**唯一**一处允许低档否掉高档的地方（``energy`` 否 ``comfort``），因此它必须
        自报家门：:attr:`ConflictResolution.CONSTRAINT_VETO` + 理由里写明"位于 §9.1 全序
        之外"。否则事件流会声称一条严格全序，同时展示一个低档赢家，两者只能有一个是真的。
        """

        veto = veto_index.get((entry.proposal.agent_id, entry.command.device_id, entry.capability))
        if veto is None:
            return None
        # 结构底线：否决不越级。EnergyAgent 已经自己守了一遍（VETOABLE_TIERS），
        # 这里再守一遍是因为否决来自外部输入——外部输入不该有能力废掉 §9.1 的上三档。
        if priority_rank(entry.priority) >= priority_rank(PriorityLevel.SECURITY):
            log.warning(
                "energy_veto_ignored_out_of_rank",
                agent_id=entry.proposal.agent_id,
                priority=entry.priority.value,
            )
            return None

        return self._reject(
            entry,
            conflict_class=ConflictClass.ENERGY_VS_COMFORT,
            reason=(
                f"{veto.reason}（规则 {veto.rule}；这是 §8.2 的约束否决，位于 §9.1 全序"
                f"**之外**：energy 档并未在秩上压过 {entry.priority.value} 档）"
            ),
            resolution=ConflictResolution.CONSTRAINT_VETO,
            winner_agent_id=ENERGY_AGENT_ID,
            winner_priority=PriorityLevel.ENERGY,
        )

    def _check_user_claim(
        self,
        entry: _Entry,
        claims: Mapping[CommandTarget, ExplicitUserClaim],
        root_wall_time: float,
    ) -> tuple[RejectedCommand, ArbitrationConflict] | None:
        """并发的用户直控占用同一控制点 → agent 提案输（§9.1 explicit_user > 除 safety 外）。

        比较的是**占用的权威**（真人 = ``explicit_user``）与提案的档位，而不是"提案自称
        是不是 explicit_user"。旧实现写成 ``>= rank(EXPLICIT_USER) → return None``，于是
        一个在 ``determine_priority`` 里返回 ``direct_user_command`` 的域 agent 对真人占用
        完全免疫——审计坑 (b) 在用户最活跃的那些 episode 上原地复活（S3 复审 blocker）。
        """

        claim = claims.get(entry.target)
        if claim is None:
            return None
        if claim.issued_wall_time <= root_wall_time:
            return None  # 用户是在本 episode 之前说的话，不构成对本轮的抢占
        if is_user_actor(entry.proposal.agent_id) or entry.proposal.agent_id == claim.agent_id:
            return None  # 用户不与自己冲突（UI 腿同一轮里既提案又登记占用）
        if priority_rank(entry.priority) > priority_rank(PriorityLevel.EXPLICIT_USER):
            return None  # §9.1 里严格高于用户的只有 safety

        return self._reject(
            entry,
            conflict_class=ConflictClass.SAME_DEVICE_PROPERTY,
            reason=(
                f"用户在本轮推理期间直接控制了 {entry.command.device_id}."
                f"{entry.capability}（explicit_user 档，行为体 {claim.agent_id}），"
                f"{entry.proposal.agent_id}（{entry.priority.value} 档）的提案让位"
            ),
            resolution=ConflictResolution.TOTAL_ORDER,
            winner_agent_id=claim.agent_id,
            winner_priority=PriorityLevel.EXPLICIT_USER,
        )

    def _check_same_target(
        self, entry: _Entry, owners: Mapping[CommandTarget, _Entry]
    ) -> tuple[RejectedCommand, ArbitrationConflict] | None:
        """§9.2 (1) 同设备同属性不同值。同值不算冲突——凭空造一条只会污染解释。"""

        owner = owners.get(entry.target)
        if owner is None or owner.command.value == entry.command.value:
            return None

        return self._reject(
            entry,
            conflict_class=ConflictClass.SAME_DEVICE_PROPERTY,
            reason=(
                f"{owner.proposal.agent_id}（{owner.priority.value}）已占用 "
                f"{entry.command.device_id}.{entry.command.property} = "
                f"{owner.command.value!r}，本条要写 {entry.command.value!r}"
            ),
            resolution=ConflictResolution.TOTAL_ORDER,
            winner_agent_id=owner.proposal.agent_id,
            winner_priority=owner.priority,
        )

    def _check_environment_goal(
        self,
        entry: _Entry,
        device: DeviceState,
        room: Any,
        axis_owners: Mapping[tuple[str, str], tuple[_Entry, int]],
    ) -> tuple[RejectedCommand, ArbitrationConflict] | None:
        """§9.2 (2) 同房间互斥的环境目标（关灯 vs 开窗帘同争 light_level）。

        **只在不同提案者之间成立**。同一个提案者在一轮里同时写 ``power=False`` 与
        ``brightness=100``（UI 的 set_state 就长这样）是它自己那份计划的内部结构，
        不是两条互斥的策略；把它判成冲突会让用户的多属性直控被自己否掉一半。
        """

        if room is None:
            return None
        goal = _environment_goal(device, entry.capability, entry.command.value, room.temperature)
        if goal is None:
            return None
        axis, direction = goal
        owned = axis_owners.get((room.id, axis))
        if owned is None:
            return None
        owner, owner_direction = owned
        if owner_direction == direction or owner.target == entry.target:
            return None
        if owner.proposal.agent_id == entry.proposal.agent_id:
            return None

        return self._reject(
            entry,
            conflict_class=ConflictClass.ROOM_ENVIRONMENT_GOAL,
            reason=(
                f"{room.id} 的 {axis} 已由 {owner.proposal.agent_id}"
                f"（{owner.priority.value}，{owner.command.device_id}."
                f"{owner.command.property}）朝 {'升高' if owner_direction > 0 else '降低'} 推；"
                f"本条要反向推，同一房间的环境目标互斥"
            ),
            resolution=ConflictResolution.TOTAL_ORDER,
            room_id=room.id,
            winner_agent_id=owner.proposal.agent_id,
            winner_priority=owner.priority,
        )

    # ---------------------------------------------------------------- 输出

    @staticmethod
    def _reject(
        entry: _Entry,
        *,
        conflict_class: ConflictClass,
        reason: str,
        resolution: ConflictResolution,
        room_id: str | None = None,
        winner_agent_id: str | None = None,
        winner_priority: PriorityLevel | None = None,
    ) -> tuple[RejectedCommand, ArbitrationConflict]:
        rejected = entry.to_rejected(
            conflict_class=conflict_class,
            rejection_reason=reason,
            resolution=resolution,
            winner_agent_id=winner_agent_id,
            winner_priority=winner_priority,
        )
        conflict = ArbitrationConflict(
            conflict_class=conflict_class,
            loser_agent_id=entry.proposal.agent_id,
            loser_priority=entry.priority,
            explanation=reason,
            device_id=entry.command.device_id,
            property=entry.command.property,
            room_id=room_id,
            winner_agent_id=winner_agent_id,
            winner_priority=winner_priority,
            resolution=resolution,
        )
        return rejected, conflict

    @staticmethod
    def _per_agent(
        proposals: Sequence[AgentProposal],
        approved: Sequence[ArbitratedCommand],
        rejected: Sequence[RejectedCommand],
    ) -> list[AgentArbitrationOutcome]:
        outcomes: list[AgentArbitrationOutcome] = []
        for proposal in proposals:
            wins = sum(1 for item in approved if item.agent_id == proposal.agent_id)
            losses = sum(1 for item in rejected if item.agent_id == proposal.agent_id)
            if proposal.is_non_action:
                label = proposal.outcome.value
            elif wins and losses:
                label = "partial"
            elif wins:
                label = "approved"
            elif losses:
                label = "rejected"
            else:
                label = "no_commands"
            outcomes.append(
                AgentArbitrationOutcome(
                    agent_id=proposal.agent_id,
                    agent_role=proposal.agent_role,
                    priority=proposal.priority,
                    outcome=label,
                    approved=wins,
                    rejected=losses,
                    noop_reason=proposal.noop_reason,
                )
            )
        # 按 agent_id 排序：输入顺序不得泄漏进事件 payload（确定性门）。
        return sorted(outcomes, key=lambda item: item.agent_id)

    @staticmethod
    def _explain(
        approved: Sequence[ArbitratedCommand],
        rejected: Sequence[RejectedCommand],
        conflicts: Sequence[ArbitrationConflict],
        winning_priority: PriorityLevel | None,
        energy_review: EnergyVetoReview | None,
    ) -> str:
        classes = sorted({item.conflict_class.value for item in conflicts})
        parts = [
            f"仲裁完成：批准 {len(approved)} 条、拒绝 {len(rejected)} 条；"
            f"最高生效优先级 {winning_priority.value if winning_priority else '无'}"
        ]
        if classes:
            parts.append(f"冲突 {len(conflicts)} 条（{'、'.join(classes)}）")
        # 能耗否决用的是哪条规则必须写进解释里：否则研究者只看到"被否了"，看不到"按什么否的"。
        if energy_review is not None and energy_review.vetoes and energy_review.explanation:
            parts.append(energy_review.explanation)
        return "；".join(parts)


class ArbitrationGate:
    """仲裁门：把 §8.4 提案变成命令生命周期上的 ``proposed → approved | rejected``。

    三件事，缺一件审计那条坑就补不上：

    1. **建命令**：每条被批准的提案在这里出生成 ``proposed`` 的 :class:`CommandRecord`，
       并**登记进 executor 的在飞注册表**——这一步把 S1 文档里"取代只在同批/待发队列内"
       的边界扩宽到仲裁窗口。
    2. **落败者不消失**：仲裁落败的命令走 ``proposed → rejected``，detail 带冲突详情。
    3. **装在 S1 的接缝上**：:meth:`pre_submit` 就是 ``CommandExecutor.submit(...,
       pre_submit=hook)`` 那个出厂 no-op 钩子的真实实现——用户命令在这里登记
       ``explicit_user`` 占用，并点名要取代的在飞目标。

    竞态纪律（S1/S2 修过一次，别再退回去）：动任何记录之前先问 ``record.is_terminal``；
    记录的状态由 :meth:`CommandRecord.transition` 在发事件**之前**落定。
    """

    def __init__(
        self,
        *,
        executor: CommandExecutor | None = None,
        arbiter: Arbiter | None = None,
        clock=time.time,
    ) -> None:
        self.executor = executor
        self.arbiter = arbiter or Arbiter()
        self.clock = clock
        # 已 proposed、还没交给 executor 跑流水线的记录（= 仲裁窗口里的在飞命令）。
        self._pre_arbitration: dict[CommandTarget, CommandRecord] = {}
        # 控制点 → 最近一次 explicit_user 占用。键有界（设备 × 能力），不会无限增长。
        self._claims: dict[CommandTarget, ExplicitUserClaim] = {}

    def bind(self, executor: CommandExecutor) -> None:
        """换绑 executor（reset 换世界后引擎会重新解析那台唯一 executor）。"""

        if self.executor is not executor:
            self.executor = executor
            self._pre_arbitration.clear()

    # ------------------------------------------------------- S1 预留的接缝

    def pre_submit(self, command: DeviceCommand) -> PreSubmitDecision | None:
        """``CommandExecutor.submit(..., pre_submit=…)`` 的真实实现（S1 出厂是 no-op）。

        钩子是同步的，因此这里**只登记与点名**，真正的取代由 executor 执行——它持有
        在飞注册表，也只有它能在正确的时点 await 那次迁移。
        """

        target: CommandTarget = (command.device_id, command.capability)
        if command.source is CommandSource.UI:
            self._claims[target] = ExplicitUserClaim(
                device_id=command.device_id,
                capability=command.capability,
                value=command.value,
                agent_id=command.actor or "user_ui",
                command_id=command.command_id,
                issued_wall_time=float(self.clock()),
                correlation_id=command.correlation_id,
            )

        victim = self._pre_arbitration.get(target)
        if victim is None or victim.is_terminal:
            return None
        if victim.command.command_id == command.command_id:
            return None
        return PreSubmitDecision(superseded_targets=(target,))

    def claims_since(self, wall_time: float) -> dict[CommandTarget, ExplicitUserClaim]:
        """在 *wall_time* 之后登记的用户占用（喂给 :meth:`Arbiter.resolve`）。"""

        return {
            target: claim
            for target, claim in self._claims.items()
            if claim.issued_wall_time > wall_time
        }

    def forget_claims(self) -> None:
        """reset / 换世界：旧世界的占用不属于新世界。"""

        self._claims.clear()
        self._pre_arbitration.clear()

    # --------------------------------------------------- proposed 生命周期

    @property
    def pre_arbitration(self) -> dict[CommandTarget, CommandRecord]:
        return dict(self._pre_arbitration)

    async def open(
        self,
        command: DeviceCommand,
        *,
        publish: PublishEvent,
        tick: int | None = None,
    ) -> CommandRecord:
        """让一条被批准的命令**出生**（``proposed``）并进在飞注册表，但先不执行。

        出生与执行之间隔着若干次事件外发（await）——那正是用户命令能插进来取代它的窗口，
        也是 §9 "仲裁坐在 proposed 与 approved 之间" 这句话的物理落点。
        """

        executor = self._require_executor()
        record = await executor.propose(command, tick=tick, publish=publish)
        self._pre_arbitration[(command.device_id, command.capability)] = record
        return record

    async def reject(
        self,
        command: DeviceCommand,
        *,
        publish: PublishEvent,
        detail: str,
        failure_code: str | None = None,
        tick: int | None = None,
    ) -> CommandRecord:
        """仲裁落败：``proposed → rejected``，``detail`` 带 §9.2 冲突分类。

        ``failure_code`` 只在这条命令**本来也过不了校验**时由调用方给出，取值必须来自
        ``validation.py`` 的 §10.2 十码词表——本模块绝不自造失败码（"仲裁落败"是协作结论，
        不是执行失败，它的位置是 ``detail``）。

        落败记录**不进**在飞注册表：它出生即终态，登记只会让同控制点的胜出命令误以为
        撞上了一条在飞命令。
        """

        record = await CommandRecord.propose(command, publish, tick=tick)
        if not record.is_terminal:
            await record.transition(
                CommandStatus.REJECTED, failure=failure_code, detail=detail, tick=tick
            )
        return record

    def discard(self, record: CommandRecord) -> None:
        """把一条记录移出仲裁窗口（它已终态：被取代 / 被取消 / 属于旧 run）。"""

        target = (record.command.device_id, record.command.capability)
        if self._pre_arbitration.get(target) is record:
            del self._pre_arbitration[target]
        if self.executor is not None:
            self.executor.forget(record)

    async def execute(
        self,
        record: CommandRecord,
        *,
        publish: PublishEvent,
        pre_execute: PreExecuteHook | None = None,
        tick: int | None = None,
    ) -> CommandRecord:
        """把一条已批准的 ``proposed`` 记录交给 executor 跑完剩余流水线。

        期间它可能已经被用户命令取代（终态）——那是普通结局，安静收工，绝不重新下发。
        """

        executor = self._require_executor()
        self._pre_arbitration.pop((record.command.device_id, record.command.capability), None)
        if record.is_terminal:
            return record
        return await executor.execute_approved(
            record,
            pre_submit=self.pre_submit,
            pre_execute=pre_execute,
            tick=tick,
            publish=publish,
        )

    def _require_executor(self) -> CommandExecutor:
        if self.executor is None:
            raise RuntimeError("ArbitrationGate 未绑定 CommandExecutor")
        return self.executor
