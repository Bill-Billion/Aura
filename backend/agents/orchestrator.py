"""S3-T3：HomeOrchestratorAgent —— spec §8.1/§8.3 的编排层。

一句话职责：**把一条根事件变成一份可断言的 :class:`~backend.agents.contracts.TaskPlan`**，
并且这件事在没有任何 LLM 的情况下也必须完成。

三条设计约束（每条都对应审计 §六 里的一个坑）：

1. **混合，不是"LLM 优先 + 兜底"**（决策 #3）。规则路径 :func:`classify_intent_rule_based`
   是一个**纯函数**：输入 :class:`RootEventContext`，输出 :class:`RuleIntent`，不读环境、
   不碰世界、不打网。LLM 只在 §11.1 的 live/recorded 模式下被叫来**精炼** intent 文本与
   置信度；它失败、超时、返回垃圾，编排结果都仍然是那份规则计划，只是多盖一个
   ``fallback_reason``。mocked 模式（含全部单测的默认路径）压根不调用 provider——
   "测试永远不打网"这件事不能靠自觉，要靠模式判定。

2. **task_decomposition 是契约，不是散文**。旧实现把模型写的自由文本塞进
   ``reasoning.task_decomposition``，于是"派了谁、动哪台设备、什么优先级"这三个问题
   在测试里根本问不出来。现在这条事件的 data 直接是
   :meth:`OrchestrationDecision.task_decomposition_event_data` 产出的结构化 domain_tasks。

3. **confidence 必须有真实消费者**。旧代码里 ``confidence`` 一路传到前端却不影响任何
   分支，回退路径还硬编码 ``0.55``。这里把阈值（``AGENT_MIN_CONFIDENCE``，默认
   :data:`~backend.agents.contracts.DEFAULT_MIN_CONFIDENCE`）接成真分支：低于阈值就给出
   ``low_confidence`` 结论并**降级为可见 no-op**（策略要求人工确认时改为标记
   ``requires_confirmation`` 并保留任务），理由写进 ``reasoning.intent_recognized``。

**确定性**（S2-T9 字节一致性门在 S3 之后要继续成立）：``domain_tasks`` 的顺序恒等于
调用方传进来的 :class:`DomainAgentBinding` 顺序，也就是 agent 注册顺序；本模块内部所有
集合投影（设备 id、房间 id、设备类型）一律排序后输出，绝不泄漏 dict 迭代序。

**不写世界**：spec §8.1 "The orchestrator should not directly mutate world state"。执行方式
不是靠约定——:meth:`RootEventContext.from_observable_world` 交给编排器的本来就是一份深拷贝，
本模块也没有任何 ``state_manager`` 句柄。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.contracts import (
    ConfidenceSource,
    DEFAULT_MIN_CONFIDENCE,
    DomainTask,
    PriorityLevel,
    ProposalOutcome,
    RootEventContext,
    TaskPlan,
    priority_rank,
)
from backend.agents.llm import (
    LLMProvider,
    LLMProviderError,
    ORCHESTRATOR_DECISION_SCHEMA,
)
from backend.agents.llm_modes import (
    BUDGET_EXCEEDED_REASON,
    EPISODE_BUDGET_ENV,
    resolve_mode_for_provider,
)
from backend.agents.types import LLMDecisionRequest
from backend.config.event_mapping import DeviceSearchSpace, get_device_search_space
from backend.core.logging import log
from backend.engine.event_types import (
    DEVICE_OFFLINE,
    DEVICE_RECOVERED,
    ENVIRONMENT_LIGHT_LEVEL_THRESHOLD,
    ENVIRONMENT_STATE_REFRESH,
    ENVIRONMENT_TEMPERATURE_THRESHOLD,
    ENVIRONMENT_WEATHER_CHANGE,
    SAFETY_SMOKE_DETECTED,
    SECURITY_DOOR_OPENED,
    SECURITY_PRESENCE_DETECTED,
    USER_ACTIVITY_CHANGE,
    USER_ARRIVES_HOME,
    USER_COMMAND,
    USER_ENDS_ACTIVITY,
    USER_ENTERS_ROOM,
    USER_EXITS_ROOM,
    USER_LEAVES_HOME,
    USER_STARTS_ACTIVITY,
)
from backend.engine.run_manager import LLMMode

__all__ = [
    "DEFAULT_ORCHESTRATOR_ID",
    "DOMAIN_PRIORITY",
    "INTENT_DOMAINS",
    "MIN_CONFIDENCE_ENV",
    "ORCHESTRATOR_INTENT_SCHEMA",
    "ORCHESTRATOR_SYSTEM_PROMPT",
    "RULE_CONFIDENCE_BY_RESOLUTION",
    "DomainAgentBinding",
    "HomeOrchestratorAgent",
    "OrchestrationDecision",
    "RuleIntent",
    "classify_intent_rule_based",
    "resolve_min_confidence",
    "rule_based_confidence",
]


DEFAULT_ORCHESTRATOR_ID = "home_orchestrator"
DEFAULT_ORCHESTRATOR_NAME = "Home Orchestrator"

MIN_CONFIDENCE_ENV = "AGENT_MIN_CONFIDENCE"
"""低置信阈值（每 run 可配）。``OrchestrationPolicy.min_confidence`` 优先于它。"""


# ============================================================ §8.1 领域分类词表


INTENT_DOMAINS: tuple[str, ...] = (
    "safety",
    "security",
    "comfort",
    "energy",
    "ambience",
    "maintenance",
)
"""spec §8.1「Classify event domain: comfort, safety, security, energy, ambience, maintenance」。

顺序按"越靠前越不可让步"排，:func:`_domain_from_text` 命中多个时取第一个——模型同时提到
safety 与 ambience 时，安全必须赢。
"""

DOMAIN_PRIORITY: dict[str, PriorityLevel] = {
    "safety": PriorityLevel.SAFETY,
    "security": PriorityLevel.SECURITY,
    "comfort": PriorityLevel.COMFORT,
    "energy": PriorityLevel.ENERGY,
    "ambience": PriorityLevel.AMBIENCE,
    "maintenance": PriorityLevel.MAINTENANCE,
}
"""领域 → §9.1 档位。``explicit_user`` 不在这张表里：它不是"事件属于哪个领域"，
而是"这条指令是人亲口说的"，由 :data:`_RULE_TABLE` 的 ``user.command`` 行直接给出。"""


ORCHESTRATOR_SYSTEM_PROMPT = (
    "You are the orchestration layer of a smart-home simulator. "
    "Classify the root event into exactly one domain "
    f"({', '.join(INTENT_DOMAINS)}) and give a short intent label. "
    "You do NOT control devices: never propose commands. "
    "Return strict JSON only."
)

ORCHESTRATOR_INTENT_SCHEMA = ORCHESTRATOR_DECISION_SCHEMA
"""§8.3 command-free intent contract used by the real provider boundary.

The alias is retained for callers of the orchestration module, while
``backend.agents.llm`` remains the single source used to construct paid API
requests.  The exact five fields deliberately exclude device commands.
"""


# ======================================================== 规则路径（纯函数，零 LLM）


class RuleIntent(BaseModel):
    """规则路径的分类结果。可比较、可哈希地确定 —— 同输入必然同输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: str
    domain: str
    priority: PriorityLevel
    confidence: float = Field(ge=0.0, le=1.0)
    resolved_from: str = "unmapped"
    default_policy: str = ""
    device_types: tuple[str, ...] = ()
    room_scope: tuple[str, ...] = ()


# 事件类型 → (intent 标签, 领域, 可选的固定优先级)。
# 领域取自 §8.1 六分类；优先级默认由 DOMAIN_PRIORITY 推出，只有 user.command 例外
# （"人亲口说的"是 §9.1 的 explicit_user 档，不是某个领域）。
_RULE_TABLE: dict[str, tuple[str, str, PriorityLevel | None]] = {
    USER_ARRIVES_HOME: ("arrival_comfort", "comfort", None),
    # §7「energy saving and security」：离家的首要目标是省电，安防是同时发生的次目标。
    USER_LEAVES_HOME: ("departure_energy_saving", "energy", None),
    USER_ENTERS_ROOM: ("room_entry_comfort", "comfort", None),
    USER_EXITS_ROOM: ("room_exit_energy_release", "energy", None),
    USER_STARTS_ACTIVITY: ("activity_comfort", "comfort", None),
    USER_ENDS_ACTIVITY: ("activity_end_restore", "ambience", None),
    ENVIRONMENT_WEATHER_CHANGE: ("weather_adaptation", "comfort", None),
    # temperature_threshold 的领域按 §7「comfort if occupied, energy if empty」现场判，
    # 见 classify_intent_rule_based；这里给的是"有人"的那一支。
    ENVIRONMENT_TEMPERATURE_THRESHOLD: ("temperature_comfort", "comfort", None),
    ENVIRONMENT_LIGHT_LEVEL_THRESHOLD: ("preserve_target_light_level", "ambience", None),
    SECURITY_PRESENCE_DETECTED: ("security_presence_response", "security", None),
    SECURITY_DOOR_OPENED: ("security_entry_check", "security", None),
    SAFETY_SMOKE_DETECTED: ("safety_smoke_response", "safety", None),
    DEVICE_OFFLINE: ("device_offline_failover", "maintenance", None),
    DEVICE_RECOVERED: ("device_recovered_restore", "maintenance", None),
    # —— 迁移期兼容命名空间 ——
    USER_ACTIVITY_CHANGE: ("activity_change_comfort", "comfort", None),
    USER_COMMAND: ("explicit_user_command", "comfort", PriorityLevel.EXPLICIT_USER),
    ENVIRONMENT_STATE_REFRESH: ("environment_refresh_review", "maintenance", None),
}

# 活动限定行（§7 的 "user.starts_activity:sleeping" / ":cooking" 两行）。
_ACTIVITY_RULE_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    (USER_STARTS_ACTIVITY, "sleeping"): ("sleep_comfort", "comfort"),
    (USER_STARTS_ACTIVITY, "cooking"): ("cooking_task_lighting", "comfort"),
}


RULE_CONFIDENCE_BY_RESOLUTION: dict[str, float] = {
    # 置信度**推导自 §7 映射表对这条事件的覆盖强度**，不是拍脑袋的装饰数字：
    # 表里有专门的活动行 → 我们确切知道该看哪些设备；只有基础行 → 只能给通用面；
    # 表里根本没有 → 不该假装有把握。
    "activity": 0.85,
    "exact": 0.80,
    "dynamic": 0.75,
    "base": 0.70,
    "dynamic_unresolved": 0.40,
    "unmapped": 0.25,
}

COMPAT_CONFIDENCE_PENALTY = 0.05
"""迁移期兼容事件（``user.activity_change`` 等）语义比 §4.1 富事件糊，扣一点。"""

EMPTY_RESULT_CONFIDENCE_FACTOR = 0.5
"""规则跑完什么也没产出时的折减：没有产出就没有把握，这是回退置信度的**推导**来源。"""


def _activity_of(context_or_data: Mapping[str, Any]) -> str | None:
    value = context_or_data.get("activity")
    return str(value) if isinstance(value, str) and value else None


def _affected_device_type(data: Mapping[str, Any]) -> str | None:
    value = data.get("device_type")
    return str(value) if isinstance(value, str) and value else None


def _search_space_for(context: RootEventContext) -> DeviceSearchSpace:
    data = context.root_event.data
    return get_device_search_space(
        context.root_event.event_type,
        activity=_activity_of(data),
        affected_device_type=_affected_device_type(data),
    )


def rule_based_confidence(
    event_type: str,
    *,
    activity: str | None = None,
    affected_device_type: str | None = None,
    has_actions: bool = True,
) -> float:
    """规则路径的置信度：**§7 覆盖强度 × 是否真的产出了什么**。

    这个函数存在的唯一理由是替掉审计里那个硬编码的 ``0.55``——那个数字既不是阈值也不是
    测量值，只是一个装饰。这里给的数会随事件类型与产出变化，因此它**可以被证伪**：
    同一条链上两次不同的回退必然给出不同的数。
    """

    space = get_device_search_space(
        event_type, activity=activity, affected_device_type=affected_device_type
    )
    confidence = RULE_CONFIDENCE_BY_RESOLUTION.get(space.resolved_from, 0.25)
    if space.source == "compat":
        confidence -= COMPAT_CONFIDENCE_PENALTY
    if not has_actions:
        confidence *= EMPTY_RESULT_CONFIDENCE_FACTOR
    return round(max(0.0, min(confidence, 1.0)), 3)


def classify_intent_rule_based(context: RootEventContext) -> RuleIntent:
    """§8.3 的**确定性**意图分类：纯函数，无 LLM、无环境变量、无世界写入。

    覆盖 §4.1 全部十七个根事件类型（十四个富事件 + 三个兼容事件）；未登记的类型不抛异常，
    而是给出一条低置信的 ``unclassified_event:*``——未知事件应该"被看见且不被信任"，
    而不是让整条 episode 崩掉。
    """

    event_type = context.root_event.event_type
    data = context.root_event.data
    activity = _activity_of(data)
    space = _search_space_for(context)

    entry = None
    if activity is not None:
        entry = _ACTIVITY_RULE_TABLE.get((event_type, activity))
        if entry is not None:
            entry = (entry[0], entry[1], None)
    if entry is None:
        entry = _RULE_TABLE.get(event_type)

    if entry is None:
        return RuleIntent(
            intent=f"unclassified_event:{event_type}",
            domain="maintenance",
            priority=PriorityLevel.MAINTENANCE,
            confidence=rule_based_confidence(event_type),
            resolved_from=space.resolved_from,
            default_policy=space.default_policy,
            device_types=space.device_types,
            room_scope=space.room_scope,
        )

    intent, domain, forced_priority = entry

    # §7「environment.temperature_threshold → comfort if occupied, energy if empty」：
    # 同一条事件在有人/没人的房子里属于两个不同领域，这一条必须现场判，不能写死在表里。
    if event_type == ENVIRONMENT_TEMPERATURE_THRESHOLD and not context.observable_state.occupied_room_ids:
        intent, domain = "temperature_energy_saving", "energy"

    # 活动名带在事件里但 §7 没有专门行：意图标签仍带上活动名，方便研究者对照。
    if event_type in {USER_STARTS_ACTIVITY, USER_ENDS_ACTIVITY} and activity:
        if (event_type, activity) not in _ACTIVITY_RULE_TABLE:
            intent = f"{intent}:{activity}"

    priority = forced_priority or DOMAIN_PRIORITY[domain]
    return RuleIntent(
        intent=intent,
        domain=domain,
        priority=priority,
        confidence=rule_based_confidence(
            event_type,
            activity=activity,
            affected_device_type=_affected_device_type(data),
            has_actions=not space.is_empty,
        ),
        resolved_from=space.resolved_from,
        default_policy=space.default_policy,
        device_types=space.device_types,
        room_scope=space.room_scope,
    )


# ================================================================ 分派对象与结论


class DomainAgentBinding(BaseModel):
    """编排器眼里的一个域 agent：**角色 + 它控什么设备**，不含运行期句柄。

    刻意不持有 agent 实例：编排是一次纯计算，拿着实例就迟早有人在这里调
    ``agent.decide(world)`` 甚至写世界。runtime 拿着 :attr:`agent_id` 去自己的注册表里找。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(min_length=1)
    agent_role: str = Field(min_length=1)
    device_types: tuple[str, ...] = ()

    @classmethod
    def from_agent(cls, agent: Any) -> "DomainAgentBinding":
        return cls(
            agent_id=agent.agent_id,
            agent_role=getattr(agent, "agent_role", "") or agent.agent_id.removesuffix("_agent"),
            device_types=tuple(sorted(agent.get_controlled_device_types())),
        )


class OrchestrationDecision(BaseModel):
    """一次编排的完整结论：计划 + 为什么是这个计划。

    ``plan`` 是给下游（分派、仲裁）的契约，其余字段是给**事件流**的解释。两者分开是因为
    §8.3 的 TaskPlan 形状要能与 spec 示例逐字对上，而可观测性需要的东西比它多。
    """

    model_config = ConfigDict(extra="forbid")

    plan: TaskPlan
    outcome: ProposalOutcome = ProposalOutcome.ACTED
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    rule_intent: RuleIntent
    # 缺省是 rule_based 而不是 mocked：这份决策的默认来路是纯规则编排（没有任何 LLM），
    # 与 provider="rule_based" / model="rule_based" 自洽。真有罐头 provider 时
    # plan() 会显式把 mocked 盖上去。
    llm_mode: str = LLMMode.RULE_BASED.value
    provider: str = "rule_based"
    model: str = "rule_based"
    latency_ms: int = 0
    explanation: str = ""
    provider_failure_reason: str | None = None
    tasks_by_agent_id: dict[str, DomainTask] = Field(default_factory=dict)

    @property
    def selected_agent_ids(self) -> tuple[str, ...]:
        """被派到任务的 agent id，**按分派顺序**（= 注册顺序）。"""

        return tuple(self.tasks_by_agent_id)

    @property
    def is_low_confidence(self) -> bool:
        return self.outcome in {ProposalOutcome.LOW_CONFIDENCE, ProposalOutcome.NEEDS_HUMAN_CONFIRMATION}

    def task_for_agent(self, agent_id: str) -> DomainTask | None:
        return self.tasks_by_agent_id.get(agent_id)

    # --- 事件 payload（S3-T6 的六环事件直接消费这两个方法）------------------

    def perception_event_data(self, context: RootEventContext) -> dict[str, Any]:
        state = context.observable_state
        return {
            "orchestrator_id": self.plan.orchestrator_id,
            "trigger_event_type": context.root_event.event_type,
            "time_of_day": state.time_of_day,
            "weather": state.weather,
            "occupied_room_ids": list(state.occupied_room_ids),
            "unavailable_device_ids": list(state.unavailable_device_ids),
            "search_space_device_types": list(self.rule_intent.device_types),
            "search_space_resolved_from": self.rule_intent.resolved_from,
            "default_policy": self.rule_intent.default_policy,
        }

    def intent_event_data(self) -> dict[str, Any]:
        """``reasoning.intent_recognized`` 的 data。

        ``low_confidence`` / ``min_confidence`` / ``confidence_source`` 三个键是审计坑
        "confidence 无消费者"的落点：研究者从事件流里就能看出这一轮**因为不够确定而没动手**。
        """

        return {
            "orchestrator_id": self.plan.orchestrator_id,
            # Stable rule-classifier label for canonical evaluation. ``intent`` may be
            # refined into free text by live/recorded providers and must not bias A/B.
            "normalized_intent": self.rule_intent.intent,
            "intent": self.plan.intent,
            "domain": self.rule_intent.domain,
            "confidence": self.plan.confidence,
            "confidence_source": self.plan.confidence_source.value,
            "rule_confidence": self.rule_intent.confidence,
            "min_confidence": self.min_confidence,
            "low_confidence": self.plan.is_low_confidence(self.min_confidence),
            "outcome": self.outcome.value,
            "requires_confirmation": self.plan.requires_confirmation,
            "fallback_reason": self.plan.fallback_reason,
            "provider_failure_reason": self.provider_failure_reason,
            "llm_mode": self.llm_mode,
            "provider": self.provider,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "explanation": self.explanation,
        }

    def task_decomposition_event_data(self) -> dict[str, Any]:
        """``reasoning.task_decomposition`` 的 data —— **结构化契约，不是 LLM 散文**。"""

        return {
            "orchestrator_id": self.plan.orchestrator_id,
            "intent": self.plan.intent,
            "domain_tasks": [task.model_dump(mode="json") for task in self.plan.domain_tasks],
            "agent_roles": list(self.plan.agent_roles),
            "agent_ids": list(self.selected_agent_ids),
            "noop_reason": self.plan.noop_reason,
            "outcome": self.outcome.value,
        }


def resolve_min_confidence(
    policy: Any = None, env: Mapping[str, str] | None = None
) -> float:
    """低置信阈值：``policy.min_confidence`` > ``AGENT_MIN_CONFIDENCE`` > 默认 0.5。

    环境变量给不出合法数字时**不静默降级**成默认值就完事——记一条 warning，
    因为"阈值配错了"与"阈值没配"在实验记录里是两件事。
    """

    if policy is not None and getattr(policy, "min_confidence", None) is not None:
        return float(policy.min_confidence)

    source = os.environ if env is None else env
    raw = str(source.get(MIN_CONFIDENCE_ENV, "")).strip()
    if not raw:
        return DEFAULT_MIN_CONFIDENCE
    try:
        value = float(raw)
    except ValueError:
        log.warning("orchestrator_min_confidence_invalid", value=raw)
        return DEFAULT_MIN_CONFIDENCE
    return min(max(value, 0.0), 1.0)


def _fallback_explanation(reason: str) -> str:
    """intent 步骤降级时写进 ``reasoning.intent_recognized`` 的那句话。

    ``budget_exceeded``（S3-T8 的成本护栏）单列一句：它与"LLM 挂了"是两件不同的事——
    模型好好的，是**我们**按预算主动不发这一次调用。两者在推理流里长得一样的话，
    研究者会把一次可复现的成本决策误读成一次 provider 故障。
    """

    if reason == BUDGET_EXCEEDED_REASON:
        return (
            f"episode LLM 预算已用尽（{BUDGET_EXCEEDED_REASON}，见 {EPISODE_BUDGET_ENV}），"
            "本次未调用模型，回退规则分类"
        )
    return f"LLM 不可用（{reason}），回退规则分类"


def _domain_from_text(*texts: str) -> str | None:
    """从模型自由文本里认领域词。命中多个取 :data:`INTENT_DOMAINS` 里靠前的那个。"""

    blob = " ".join(text.lower() for text in texts if text)
    for domain in INTENT_DOMAINS:
        if domain in blob:
            return domain
    return None


class HomeOrchestratorAgent:
    """spec §8.1 的编排层。

    它**不是** :class:`~backend.agents.base.BaseAgent`：BaseAgent 的契约是"控某些设备类型、
    产出设备命令"，而编排器一条命令都不发。让它继承 BaseAgent 只会得到一堆必须 raise 的
    抽象方法，以及一条"编排器也能 decide(world)"的错误暗示。
    """

    def __init__(
        self,
        *,
        orchestrator_id: str = DEFAULT_ORCHESTRATOR_ID,
        name: str = DEFAULT_ORCHESTRATOR_NAME,
        llm_intent_enabled: bool | None = None,
    ) -> None:
        self.orchestrator_id = orchestrator_id
        self.name = name
        # None = 按 §11.1 模式自动决定（mocked 不打网）；True/False 供测试与实验显式钉死。
        self.llm_intent_enabled = llm_intent_enabled

    # --- 规则路径（可独立测试，零 provider）--------------------------------

    def plan_rule_based(
        self,
        context: RootEventContext,
        bindings: Sequence[DomainAgentBinding],
    ) -> OrchestrationDecision:
        """纯规则编排。**这条路径必须在没有任何 LLM 的情况下产出合法 TaskPlan。**"""

        rule = classify_intent_rule_based(context)
        return self._assemble(
            context=context,
            bindings=bindings,
            rule=rule,
            intent=rule.intent,
            confidence=rule.confidence,
            priority=rule.priority,
            confidence_source=ConfidenceSource.RULE_BASED,
            explanation=f"rule-based classification from §7 mapping ({rule.resolved_from})",
        )

    # --- 混合路径（decision #3）-------------------------------------------

    async def plan(
        self,
        context: RootEventContext,
        bindings: Sequence[DomainAgentBinding],
        *,
        llm_provider: LLMProvider | None = None,
    ) -> OrchestrationDecision:
        """混合编排：LLM 精炼意图，失败即回落规则路径并盖 ``fallback_reason``。

        注意 LLM 只影响 **intent 文本 / 置信度 / 领域**，不影响"派给谁"——派给谁由 §7
        搜索空间 ∩ agent 控的设备类型决定，那是数据，不该随模型措辞漂移。而且模型
        **不能降档**：领域档位取规则档与模型档中更高的那个（§19 fail-closed）。
        """

        rule = classify_intent_rule_based(context)
        if not self._should_call_llm(llm_provider):
            # 没打 provider 的两种情形必须分开标注（§11.1 / S3 review minor-3）：
            # 罐头 provider 在场 = mocked；压根没有 LLM、或意图 LLM 被显式关掉 = rule_based。
            mode = resolve_mode_for_provider(llm_provider)
            label = LLMMode.RULE_BASED if mode.calls_provider else mode
            fallback = self.plan_rule_based(context, bindings)
            return fallback.model_copy(update={"llm_mode": label.value})

        assert llm_provider is not None  # _should_call_llm 已经保证
        mode = resolve_mode_for_provider(llm_provider)
        started_at = time.monotonic()
        try:
            decision = await llm_provider.generate_decision(self._build_request(context, rule))
        except LLMProviderError as exc:
            log.warning(
                "orchestrator_intent_fallback",
                orchestrator_id=self.orchestrator_id,
                reason=exc.reason,
                root_event_type=context.root_event.event_type,
            )
            fallback = self.plan_rule_based(context, bindings)
            return fallback.model_copy(
                update={
                    "plan": fallback.plan.model_copy(update={"fallback_reason": exc.reason}),
                    "llm_mode": mode.value,
                    "provider": str(getattr(llm_provider, "provider_name", "llm")),
                    "model": str(getattr(llm_provider, "model", "")),
                    "latency_ms": int((time.monotonic() - started_at) * 1000),
                    "explanation": _fallback_explanation(exc.reason),
                }
            )

        latency_ms = int((time.monotonic() - started_at) * 1000)
        if decision.provider_failure_reason:
            return self._assemble(
                context=context,
                bindings=bindings,
                rule=rule,
                intent=decision.intent,
                confidence=0.0,
                priority=rule.priority,
                confidence_source=ConfidenceSource.LLM,
                explanation=decision.explanation,
                llm_mode=mode.value,
                provider=str(getattr(llm_provider, "provider_name", "llm")),
                model=str(getattr(llm_provider, "model", "")),
                latency_ms=latency_ms,
                provider_failure_reason=decision.provider_failure_reason,
            )
        llm_domain = _domain_from_text(decision.intent, decision.explanation)
        domain_priority = DOMAIN_PRIORITY.get(llm_domain or "", PriorityLevel.MAINTENANCE)
        # fail closed：模型只能升档，不能把 safety/security/explicit_user 降下来。
        priority = (
            domain_priority
            if priority_rank(domain_priority) > priority_rank(rule.priority)
            else rule.priority
        )
        if llm_domain is not None and llm_domain == rule.domain:
            confidence = round((decision.confidence + rule.confidence) / 2, 3)
            source = ConfidenceSource.BLENDED
        else:
            confidence = round(decision.confidence, 3)
            source = ConfidenceSource.LLM

        return self._assemble(
            context=context,
            bindings=bindings,
            rule=rule,
            intent=decision.intent.strip() or rule.intent,
            confidence=confidence,
            priority=priority,
            confidence_source=source,
            explanation=decision.explanation,
            llm_mode=mode.value,
            provider=str(getattr(llm_provider, "provider_name", "llm")),
            model=str(getattr(llm_provider, "model", "")),
            latency_ms=latency_ms,
        )

    # --- 内部 ---------------------------------------------------------------

    def _should_call_llm(self, llm_provider: LLMProvider | None) -> bool:
        if llm_provider is None:
            return False
        if self.llm_intent_enabled is not None:
            return self.llm_intent_enabled
        # §11.1：mocked（罐头）与 rule_based（未配 key / 被禁用）都不打 provider，
        # 编排器走纯规则。判据是 calls_provider，不是 "is not MOCKED"。
        return resolve_mode_for_provider(llm_provider).calls_provider

    def _build_request(self, context: RootEventContext, rule: RuleIntent) -> LLMDecisionRequest:
        state = context.observable_state
        return LLMDecisionRequest(
            decision_role="home_orchestrator",
            agent_id=self.orchestrator_id,
            agent_name=self.name,
            root_event_type=context.root_event.event_type,
            world_summary=(
                f"event={context.root_event.event_type}; "
                f"time={state.time_of_day}; weather={state.weather}; "
                f"occupied_rooms={','.join(state.occupied_room_ids)}; "
                f"policy={rule.default_policy}; "
                f"candidate_domains={','.join(INTENT_DOMAINS)}"
            ),
            recent_events=[],
            available_devices=[],
            # 空 allowed_commands 是硬约束的一部分：编排器不发命令（spec §8.1）。
            allowed_commands=[],
        )

    def _assemble(
        self,
        *,
        context: RootEventContext,
        bindings: Sequence[DomainAgentBinding],
        rule: RuleIntent,
        intent: str,
        confidence: float,
        priority: PriorityLevel,
        confidence_source: ConfidenceSource,
        explanation: str,
        llm_mode: str = LLMMode.RULE_BASED.value,
        provider: str = "rule_based",
        model: str = "rule_based",
        latency_ms: int = 0,
        provider_failure_reason: str | None = None,
    ) -> OrchestrationDecision:
        min_confidence = resolve_min_confidence(context.policy)
        space = _search_space_for(context)

        common = {
            "outcome": ProposalOutcome.ACTED,
            "min_confidence": min_confidence,
            "rule_intent": rule,
            "llm_mode": llm_mode,
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "explanation": explanation,
            "provider_failure_reason": provider_failure_reason,
        }

        def _noop(outcome: ProposalOutcome, reason: str, **plan_kwargs: Any) -> OrchestrationDecision:
            return OrchestrationDecision(
                plan=TaskPlan.noop(
                    orchestrator_id=self.orchestrator_id,
                    intent=intent,
                    confidence=confidence,
                    noop_reason=reason,
                    confidence_source=confidence_source,
                    **plan_kwargs,
                ),
                **{**common, "outcome": outcome},
            )

        # §8.4「Missing observations」：没有可观测世界就无法点名设备，说清楚而不是瞎派。
        if context.observable_world is None:
            return _noop(
                ProposalOutcome.MISSING_OBSERVATIONS,
                "没有可观测世界快照，无法确定相关设备（missing observations）",
            )

        tasks: list[DomainTask] = []
        tasks_by_agent_id: dict[str, DomainTask] = {}
        for binding in bindings:  # 顺序 = 注册顺序 = 分派顺序（确定性契约）
            matched = self._matched_types(space, binding)
            if not matched:
                continue
            device_ids, room_ids = self._devices_for(context, matched, space.room_scope)
            if not device_ids:
                # §7 搜索面在这个世界里落不到该 agent 的任何设备 → 派了也没事可做。
                continue
            task = DomainTask(
                agent_role=binding.agent_role,
                task=f"{intent}: {rule.default_policy or 'apply domain policy'}",
                relevant_device_ids=device_ids,
                priority=priority,
                relevant_room_ids=room_ids,
            )
            tasks.append(task)
            tasks_by_agent_id[binding.agent_id] = task

        if not tasks:
            return _noop(
                ProposalOutcome.NO_ACTION_NEEDED,
                (
                    f"没有域 agent 落在 {context.root_event.event_type} 的 §7 搜索面上"
                    f"（{'/'.join(space.device_types) or '空'}）"
                ),
            )

        # —— confidence 的真实消费者（审计坑：全链路无消费者、回退硬编码 0.55）——
        if confidence < min_confidence:
            if context.policy.require_human_confirmation:
                plan = TaskPlan(
                    orchestrator_id=self.orchestrator_id,
                    intent=intent,
                    confidence=confidence,
                    domain_tasks=tasks,
                    requires_confirmation=True,
                    confidence_source=confidence_source,
                )
                return OrchestrationDecision(
                    plan=plan,
                    tasks_by_agent_id=tasks_by_agent_id,
                    **{**common, "outcome": ProposalOutcome.NEEDS_HUMAN_CONFIRMATION},
                )
            return _noop(
                ProposalOutcome.LOW_CONFIDENCE,
                (
                    f"confidence {confidence} < min_confidence {min_confidence}："
                    f"不足以自动执行，本轮降级为可见 no-op"
                ),
            )

        plan = TaskPlan(
            orchestrator_id=self.orchestrator_id,
            intent=intent,
            confidence=confidence,
            domain_tasks=tasks,
            requires_confirmation=context.policy.require_human_confirmation,
            confidence_source=confidence_source,
        )
        return OrchestrationDecision(plan=plan, tasks_by_agent_id=tasks_by_agent_id, **common)

    @staticmethod
    def _matched_types(space: DeviceSearchSpace, binding: DomainAgentBinding) -> tuple[str, ...]:
        """该 agent 落在这条事件搜索面上的设备类型（排序）。

        搜索面为空（未登记事件类型）时 fail-open 回该 agent 的全部设备类型：新事件类型上线
        时宁可多开一轮推理，也不要让所有 agent 对它静默失明——与 ``relevance.py`` 同口径。
        """

        if space.is_empty:
            return tuple(sorted(binding.device_types))
        return space.intersect_controlled(binding.device_types)

    @staticmethod
    def _devices_for(
        context: RootEventContext,
        device_types: Iterable[str],
        room_scope: Sequence[str],
    ) -> tuple[list[str], list[str]]:
        """世界里符合"类型 ∈ 搜索面 且 房间 ∈ room_scope"的设备与它们所在房间（均排序）。"""

        world = context.observable_world
        assert world is not None  # 调用点已经查过
        wanted = set(device_types)
        scope = set(room_scope)
        device_ids: list[str] = []
        rooms: set[str] = set()
        for device_id, device in world.devices.items():
            if device.type not in wanted:
                continue
            room_id = device.location.room
            if scope and room_id not in scope:
                continue
            device_ids.append(device_id)
            rooms.add(room_id)
        return sorted(device_ids), sorted(rooms)
