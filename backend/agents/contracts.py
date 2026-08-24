"""S3 编排契约：spec §8.3 / §8.4 / §9.1 的**单一来源**。

这个模块存在的理由只有一条：**让"agent 想了什么、派了谁、按什么优先级"变成可断言的
结构化数据**。2026-07-16 的复审把三个坑记在同一处：

1. ``reasoning.task_decomposition`` 事件里装的是 LLM 自由文本。谁也没法对散文提问
   "你派了哪个 agent、动了哪台设备"，于是"任务拆分"这一环在测试里根本不存在。
   :class:`TaskPlan` / :class:`DomainTask` 就是把那段散文换成契约。
2. 旧 ``PriorityLabel`` 只有六档，且 ``safety`` 与 ``direct_user_command`` 在
   ``arbiter.PRIORITY_RANK`` 里**并列第 5**，``security`` 档压根不存在。并列意味着
   "谁先进循环谁赢"——仲裁结果因此不可解释。:class:`PriorityLevel` 按 §9.1 恢复七档
   **严格全序**。
3. ``confidence`` 全链路没有消费者（回退路径还硬编码 0.55）。契约层先把
   :meth:`TaskPlan.is_low_confidence` 和 :class:`ConfidenceSource` 做出来，S3-T3 才
   有地方接阈值。

三条刻意的形状决定：

- **零 LLM 依赖**。本模块只 import pydantic 与 engine 的只读类型，因此
  ``tests/test_orchestrator_contract.py`` 能在没有任何 provider 的情况下验证契约。
- **``extra="forbid"``**（与 ``backend/scenarios/spec.py::_StrictModel`` 同口径）。
  拼错字段名必须当场炸；契约模型静默吞字段等于没有契约。
- **不改 ``types.py``**。旧 :class:`~backend.agents.types.AgentDecisionEnvelope` 与
  六标签 ``PriorityLabel`` 仍作为 runtime 的决策过程证据，结论再转换为本模块的
  :class:`AgentProposal`。命令形状则**直接复用**
  ``types.AgentCommandProposal``——同一个概念两份模型是下一个 bug 的温床。

词表纪律：新产出的 ``reasoning.coordination_decision`` / ``TaskPlan`` /
:class:`AgentProposal` 一律使用 §9.1 词表（``explicit_user`` 等）；
:data:`LEGACY_PRIORITY_MIGRATION` **只用于解析历史 payload**（旧事件日志、旧测试夹具、
仍在发六标签的前端），不是新代码的合法输入通道。
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent
from backend.engine.observation import device_reports_online
from backend.engine.state import WorldState

__all__ = [
    "AgentProposal",
    "ConfidenceSource",
    "DEFAULT_MIN_CONFIDENCE",
    "DomainTask",
    "LEGACY_PRIORITY_MIGRATION",
    "NON_ACTION_OUTCOMES",
    "ObservableStateView",
    "OrchestrationPolicy",
    "PRIORITY_LEVELS",
    "PriorityLevel",
    "ProposalAssumption",
    "ProposalOutcome",
    "RootEventContext",
    "RootEventRef",
    "TaskPlan",
    "highest_priority",
    "migrate_legacy_priority",
    "outranks",
    "priority_rank",
]


class _StrictModel(BaseModel):
    """契约基类：多一个字段就报错（与 scenarios/spec.py 同口径）。"""

    model_config = ConfigDict(extra="forbid")


# ==================================================================== §9.1 优先级


class PriorityLevel(str, Enum):
    """spec §9.1 的七档默认优先级。

    成员声明顺序 = 优先级从高到低，:data:`PRIORITY_LEVELS` 与 :func:`priority_rank`
    都从这里派生，**不要**在别处再写一份排名表（旧 ``arbiter.PRIORITY_RANK`` 就是
    第二份真相，并列档位正是从那里溜进来的）。

    与旧六标签的差异（见 :data:`LEGACY_PRIORITY_MIGRATION`）：
    ``direct_user_command`` → ``explicit_user``、``user_comfort`` → ``comfort``、
    ``convenience`` → ``ambience``、``energy_efficiency`` → ``energy``、
    ``background_optimization`` → ``maintenance``，并新增 ``security`` 档。
    """

    SAFETY = "safety"
    EXPLICIT_USER = "explicit_user"
    SECURITY = "security"
    COMFORT = "comfort"
    ENERGY = "energy"
    AMBIENCE = "ambience"
    MAINTENANCE = "maintenance"


# 从高到低。任何需要"按优先级遍历"的地方都用它，别再手写列表。
PRIORITY_LEVELS: tuple[PriorityLevel, ...] = tuple(PriorityLevel)

# rank 越大越优先。刻意不用 0 起算：0 在旧 arbiter 里是 "未知标签" 的默认值，
# 让未知标签和最低档撞在一起是上一版并列问题的另一个来源。
_PRIORITY_RANK: dict[PriorityLevel, int] = {
    level: len(PRIORITY_LEVELS) - index for index, level in enumerate(PRIORITY_LEVELS)
}

# 旧六标签 → §9.1 七档。**仅用于历史 payload 解析**，新事件必须直接使用 §9.1 词表。
LEGACY_PRIORITY_MIGRATION: dict[str, PriorityLevel] = {
    "direct_user_command": PriorityLevel.EXPLICIT_USER,
    "safety": PriorityLevel.SAFETY,
    "user_comfort": PriorityLevel.COMFORT,
    "convenience": PriorityLevel.AMBIENCE,
    "energy_efficiency": PriorityLevel.ENERGY,
    "background_optimization": PriorityLevel.MAINTENANCE,
}


def priority_rank(level: PriorityLevel | str) -> int:
    """档位的数值秩（越大越优先）。七个秩两两不等，因此比较是全序。"""

    return _PRIORITY_RANK[PriorityLevel(level)]


def outranks(left: PriorityLevel | str, right: PriorityLevel | str) -> bool:
    """``left`` 是否严格压过 ``right``（同档返回 False：全序里没有"并列获胜"）。"""

    return priority_rank(left) > priority_rank(right)


def highest_priority(levels: Iterable[PriorityLevel | str]) -> PriorityLevel | None:
    """一组档位里最高的那个；空集合返回 None（调用方必须显式处理"没人提案"）。"""

    resolved = [PriorityLevel(level) for level in levels]
    if not resolved:
        return None
    return max(resolved, key=priority_rank)


def _reject_unmapped_legacy_label(value: Any) -> Any:
    """把"直接塞旧词表"拦成一条**可行动**的错误信息。

    不拦的话，``convenience`` 只会得到 pydantic 的"不是合法枚举成员"，调用方看不出
    "这是历史词表、请走 :func:`migrate_legacy_priority`"。``safety`` 新旧同名，放行。
    """

    if isinstance(value, str) and value in LEGACY_PRIORITY_MIGRATION and value != "safety":
        raise ValueError(
            f"{value!r} 是旧优先级词表；请先调用 migrate_legacy_priority() 迁移到 "
            f"spec §9.1 词表（新事件不得再产出旧标签）"
        )
    return value


def migrate_legacy_priority(label: str) -> PriorityLevel:
    """把任意历史标签映射到 §9.1 档位；已是新词表则原样返回（幂等）。

    未知标签抛 :class:`ValueError` 而不是回落到最低档——静默降档会让一条本该赢的
    安全提案输掉，且不留痕迹。
    """

    mapped = LEGACY_PRIORITY_MIGRATION.get(label)
    if mapped is not None:
        return mapped
    try:
        return PriorityLevel(label)
    except ValueError as exc:  # noqa: TRY003 - 信息量比异常类型更重要
        raise ValueError(
            f"未知优先级标签 {label!r}；取值须是 spec §9.1 七档之一"
            f"（{', '.join(level.value for level in PRIORITY_LEVELS)}），"
            f"或旧词表之一（{', '.join(sorted(LEGACY_PRIORITY_MIGRATION))}）"
        ) from exc


# ================================================================ §8.3 输入契约


class RootEventRef(_StrictModel):
    """根事件在 §8.3 上下文里的投影。

    spec 的示例只写了 ``event_type`` / ``source`` 两个字段，这里其余字段**全部可选**：
    示例能原样 round-trip，真实运行期又能带上因果链所需的 id（没有 ``event_id`` /
    ``correlation_id``，T6 的 ``reasoning.*`` 事件就挂不回根事件下面）。
    """

    event_type: str
    source: str
    event_id: str | None = None
    correlation_id: str | None = None
    causal_parent: str | None = None
    timestamp: float | None = None
    sim_time_s: float | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_sim_event(cls, event: SimEvent) -> "RootEventRef":
        return cls(
            event_type=event.event_type,
            source=event.source,
            event_id=event.event_id,
            correlation_id=event.correlation_id,
            causal_parent=event.causal_parent,
            timestamp=event.timestamp,
            sim_time_s=event.sim_time_s,
            data=dict(event.data),
        )


class ObservableStateView(_StrictModel):
    """§8.3 ``observable_state``：**摘要**，不是世界。

    这是进事件 payload 的那一份，所以刻意只留可读的少量字段；agent 真正要遍历设备时
    用 :attr:`RootEventContext.observable_world`（S2 ``build_observable_view`` 的产物）。

    所有列表字段一律按 id 升序：确定性回放门（tests/test_replay_determinism.py）要求
    任何投影都不泄漏 dict 迭代序。
    """

    time_of_day: str = ""
    weather: str = ""
    rooms: list[str] = Field(default_factory=list)
    devices: list[str] = Field(default_factory=list)
    # 以下两项超出 spec 示例但默认空，示例仍能 round-trip。
    # 它们是 §7 映射表两行的直接输入：`comfort if occupied, energy if empty` 需要占用，
    # `device.offline → fail closed` 需要不可用设备名单。
    occupied_room_ids: list[str] = Field(default_factory=list)
    unavailable_device_ids: list[str] = Field(default_factory=list)

    @classmethod
    def from_world(cls, world: WorldState) -> "ObservableStateView":
        return cls(
            time_of_day=world.environment.time_of_day,
            weather=world.environment.weather,
            rooms=sorted(world.rooms),
            devices=sorted(world.devices),
            occupied_room_ids=sorted(
                room_id for room_id, room in world.rooms.items() if room.occupancy
            ),
            unavailable_device_ids=sorted(
                device_id
                for device_id, device in world.devices.items()
                if not device_reports_online(device)
            ),
        )


class OrchestrationPolicy(_StrictModel):
    """§8.3 ``policy``：这一轮编排允许做什么。"""

    allow_fallback: bool = True
    require_human_confirmation: bool = False
    # 超出 spec 示例的可选项（默认 None = 用全局默认），S3-T3 的 AGENT_MIN_CONFIDENCE
    # 落在这里：审计坑"confidence 无消费者"要求阈值是**每 run 可配**的真实参数。
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    def effective_min_confidence(self) -> float:
        return DEFAULT_MIN_CONFIDENCE if self.min_confidence is None else self.min_confidence


class RootEventContext(_StrictModel):
    """§8.3 编排器输入：一条根事件 + 这一刻 agent **看得见**的世界。

    ``observable_world`` 刻意不进序列化（``exclude=True``）：它是给编排器遍历用的运行期
    句柄，不是事件 payload 的一部分——把整个世界塞进每条 ``reasoning.*`` 事件会把
    events.jsonl 撑爆，也会让 canonical trace 对无关字段敏感。
    """

    run_id: str | None = None
    scenario_id: str | None = None
    root_event: RootEventRef
    observable_state: ObservableStateView = Field(default_factory=ObservableStateView)
    # §2.3/§5.3：ground truth **不对 agent 可见**，只给评估侧（S4）读。留成松散 dict 是
    # 为了不让 agents 包反向依赖 scenarios 包；键与 ScenarioSpec.ground_truth 一致。
    ground_truth_labels: dict[str, Any] = Field(default_factory=dict)
    policy: OrchestrationPolicy = Field(default_factory=OrchestrationPolicy)
    observable_world: WorldState | None = Field(default=None, exclude=True, repr=False)

    @property
    def expected_intent(self) -> str | None:
        """§5.3 ``expected_intent``；仅评估侧使用，编排器不得据此决策。"""

        value = self.ground_truth_labels.get("expected_intent")
        return value if isinstance(value, str) else None

    @classmethod
    def from_observable_world(
        cls,
        *,
        root_event: SimEvent,
        observable_world: WorldState,
        run_id: str | None = None,
        scenario_id: str | None = None,
        ground_truth_labels: dict[str, Any] | None = None,
        policy: OrchestrationPolicy | None = None,
    ) -> "RootEventContext":
        """从运行期对象装配上下文。

        ``observable_world`` 一律深拷贝：spec §8.1 "The orchestrator should not directly
        mutate world state"——最可靠的执行方式不是靠约定，是让它手上根本没有真世界的引用。
        """

        return cls(
            run_id=run_id if run_id is not None else root_event.run_id,
            scenario_id=scenario_id if scenario_id is not None else root_event.scenario_id,
            root_event=RootEventRef.from_sim_event(root_event),
            observable_state=ObservableStateView.from_world(observable_world),
            ground_truth_labels=dict(ground_truth_labels or {}),
            policy=policy or OrchestrationPolicy(),
            observable_world=observable_world.model_copy(deep=True),
        )


# ================================================================ §8.3 输出契约


DEFAULT_MIN_CONFIDENCE: float = 0.5
"""低置信阈值默认值。S3-T3 用 ``AGENT_MIN_CONFIDENCE`` 环境变量覆写它。

它取代的是回退路径里那个硬编码的 ``0.55``——那个数字既不是阈值也不是测量值，
只是一个没人消费的装饰。
"""


class ConfidenceSource(str, Enum):
    """置信度是**怎么来的**。

    没有这一位，``confidence=0.55`` 与 ``confidence=0.91`` 在下游看起来同样权威。
    评估侧（S4）需要区分"模型自评"与"规则推导"才能谈校准。
    """

    LLM = "llm"
    RULE_BASED = "rule_based"
    BLENDED = "blended"


class DomainTask(_StrictModel):
    """§8.3 ``domain_tasks[]`` 单项：编排器派给某个域 agent 的一件事。

    ``agent_role`` 是角色名（lighting / hvac / security / energy / scene），不是
    ``agent_id``：同一角色在不同 run 里可以换实现，契约不该被实例 id 绑死。
    """

    agent_role: str = Field(min_length=1)
    task: str = Field(min_length=1)
    relevant_device_ids: list[str] = Field(default_factory=list)
    priority: PriorityLevel
    # 超出 spec 示例但默认空：§8.1 要求编排器"determine relevant rooms and device groups"，
    # 只给设备名单会把房间级目标（"卧室安静"）压成设备级噪声。
    relevant_room_ids: list[str] = Field(default_factory=list)

    _no_legacy_priority = field_validator("priority", mode="before")(
        _reject_unmapped_legacy_label
    )


class TaskPlan(_StrictModel):
    """§8.3 编排器输出：**结构化**的任务拆分。

    ``domain_tasks`` 的**列表顺序即分派顺序**，且必须是确定性的（编排器按 agent 注册序
    生成，同序并列时用注册序 tie-break）。本模型刻意**不**在校验阶段重排：重排会把
    "注册序"这条 S2 字节一致性门赖以成立的约定悄悄改掉。
    """

    orchestrator_id: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    domain_tasks: list[DomainTask] = Field(default_factory=list)
    noop_reason: str | None = None
    requires_confirmation: bool = False
    # 超出 spec 示例但默认值安全：审计坑"confidence 无消费者"的落点。
    confidence_source: ConfidenceSource = ConfidenceSource.RULE_BASED
    fallback_reason: str | None = None

    @model_validator(mode="after")
    def _check_noop_contract(self) -> "TaskPlan":
        # §8.3："The orchestrator may return noop_reason when no action is needed."
        # 反过来说：不派任务又不说为什么，就是审计里那种"事件发了、episode 是空的"。
        if not self.domain_tasks and not self.noop_reason:
            raise ValueError(
                "空 TaskPlan 必须给出 noop_reason（spec §8.3 no-op 契约）："
                "不派任务又不解释，等于把 episode 静默吞掉"
            )
        if self.domain_tasks and self.noop_reason:
            raise ValueError(
                "noop_reason 与 domain_tasks 互斥：既然派了任务就不是 no-op"
            )
        return self

    @property
    def is_noop(self) -> bool:
        return not self.domain_tasks

    @property
    def agent_roles(self) -> tuple[str, ...]:
        """按分派顺序列出角色（去重前的原序，重复角色是编排器的 bug 而非本层的）。"""

        return tuple(task.agent_role for task in self.domain_tasks)

    def is_low_confidence(self, threshold: float | None = None) -> bool:
        """置信度是否低于阈值。**这是 confidence 的第一个真实消费者。**"""

        limit = DEFAULT_MIN_CONFIDENCE if threshold is None else threshold
        return self.confidence < limit

    @classmethod
    def noop(
        cls,
        *,
        orchestrator_id: str,
        intent: str,
        confidence: float,
        noop_reason: str,
        confidence_source: ConfidenceSource = ConfidenceSource.RULE_BASED,
        requires_confirmation: bool = False,
        fallback_reason: str | None = None,
    ) -> "TaskPlan":
        """构造一个**可见的** no-op：§15 要求"每条根事件都产生可见 episode"。"""

        return cls(
            orchestrator_id=orchestrator_id,
            intent=intent,
            confidence=confidence,
            domain_tasks=[],
            noop_reason=noop_reason,
            requires_confirmation=requires_confirmation,
            confidence_source=confidence_source,
            fallback_reason=fallback_reason,
        )


# ================================================================ §8.4 提案契约


class ProposalOutcome(str, Enum):
    """§8.4 结论枚举：一次动手 + 规格点名要求的五种"没动手"。

    没有这一位时，"没有命令"是个歧义状态——可能是无需动作，可能是观测缺失，也可能是
    LLM 失败。可观测性平台把这三种情况混在一起，就等于什么也没观测到。
    """

    ACTED = "acted"
    NO_ACTION_NEEDED = "no_action_needed"
    LOW_CONFIDENCE = "low_confidence"
    MISSING_OBSERVATIONS = "missing_observations"
    UNSAFE_REJECTED = "unsafe_rejected"
    NEEDS_HUMAN_CONFIRMATION = "needs_human_confirmation"


NON_ACTION_OUTCOMES: frozenset[ProposalOutcome] = frozenset(
    {
        ProposalOutcome.NO_ACTION_NEEDED,
        ProposalOutcome.LOW_CONFIDENCE,
        ProposalOutcome.MISSING_OBSERVATIONS,
        ProposalOutcome.UNSAFE_REJECTED,
        ProposalOutcome.NEEDS_HUMAN_CONFIRMATION,
    }
)


_ASSUMPTION_PATH_RE = re.compile(
    r"^(?:"
    r"users\[[^\]]+\]\.(?:activity|location\.room)|"
    r"rooms\[[^\]]+\]\.occupancy|"
    r"devices\[[^\]]+\]\.state\.(?:power|extra\.[A-Za-z0-9_-]+)|"
    r"environment\.(?:time_of_day|weather|outdoor_temp|outdoor_humidity)"
    r")$"
)


class ProposalAssumption(_StrictModel):
    """One observable guard that must still hold when a proposal executes."""

    path: str = Field(min_length=1, max_length=512)
    equals: str | int | float | bool | None

    @field_validator("path")
    @classmethod
    def _allow_observable_path(cls, value: str) -> str:
        if not _ASSUMPTION_PATH_RE.fullmatch(value):
            raise ValueError(f"unsupported proposal assumption path: {value!r}")
        return value


class AgentProposal(_StrictModel):
    """§8.4 统一提案契约：agent **只提案，不写世界**。

    与 :class:`~backend.agents.types.AgentDecisionEnvelope` 的分工：envelope 是既有的
    "一次 LLM 决策的全过程记录"（provider / latency / world_summary…），本模型是仲裁器
    真正消费的那一层**结论**。命令形状直接复用 ``AgentCommandProposal``，不另造一份。
    """

    agent_id: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    priority: PriorityLevel
    confidence: float = Field(ge=0.0, le=1.0)
    commands: list[AgentCommandProposal] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    requires_coordination: bool = False
    # —— 以下字段承载 §8.4 "Agents must also be able to express" 的五种表达 ——
    outcome: ProposalOutcome = ProposalOutcome.ACTED
    noop_reason: str | None = None
    missing_observations: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    # 被扣下没发的命令。unsafe_rejected / needs_human_confirmation 必须留住"本来想做什么"，
    # 否则 §9.3 的 "rejected commands" 输出只剩一句话，研究者无从复盘。
    withheld_commands: list[AgentCommandProposal] = Field(default_factory=list)
    confidence_source: ConfidenceSource = ConfidenceSource.RULE_BASED
    agent_role: str = ""
    assumptions: list[ProposalAssumption] = Field(default_factory=list)
    observed_world_version: int | None = Field(default=None, ge=0)

    _no_legacy_priority = field_validator("priority", mode="before")(
        _reject_unmapped_legacy_label
    )

    @model_validator(mode="after")
    def _check_outcome_contract(self) -> "AgentProposal":
        if self.outcome in NON_ACTION_OUTCOMES:
            if self.commands:
                raise ValueError(
                    f"outcome={self.outcome.value} 表示本轮没有动手，commands 必须为空；"
                    f"要保留原本打算做什么，请放进 withheld_commands"
                )
            if not self.noop_reason:
                raise ValueError(
                    f"outcome={self.outcome.value} 必须给出 noop_reason："
                    f"五种非动作表达的价值全在解释上"
                )
        if self.outcome is ProposalOutcome.MISSING_OBSERVATIONS and not self.missing_observations:
            raise ValueError("outcome=missing_observations 必须点名缺了哪些观测")
        if self.outcome is ProposalOutcome.UNSAFE_REJECTED and not self.risks:
            raise ValueError("outcome=unsafe_rejected 必须在 risks 里写明被拒的风险")
        return self

    @property
    def has_commands(self) -> bool:
        return bool(self.commands)

    @property
    def is_non_action(self) -> bool:
        return self.outcome in NON_ACTION_OUTCOMES
