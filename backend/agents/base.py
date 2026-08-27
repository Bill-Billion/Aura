"""Base agent abstract class for smart home automation agents."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.agents.contracts import (  # noqa: TC001 - 运行期需要（默认参数注解求值）
    NON_ACTION_OUTCOMES,
    AgentProposal,
    ConfidenceSource,
    DomainTask,
    OrchestrationPolicy,
    PriorityLevel,
    ProposalAssumption,
    ProposalOutcome,
    migrate_legacy_priority,
)
from backend.agents.llm import LLMProvider, LLMProviderError
from backend.agents.memory import AgentMemoryStore
from backend.agents.relevance import (
    actionable_device_types,
    is_device_availability_event,
)
from backend.agents.types import (
    AgentCommandProposal,
    AgentDecisionEnvelope,
    AgentLLMDecision,
    LLMDecisionRequest,
    PriorityLabel,
    RawCommandAssessment,
)
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES, ENVIRONMENT_ROOT_EVENT_TYPES
from backend.engine.state import DeviceState, WorldState
from backend.execution.validation import validate_command


class ProposalReview(BaseModel):
    """域 agent 对"这一轮该不该动手"的裁决（§8.4 五种非动作表达的载体）。

    它刻意**不**是 :class:`~backend.agents.contracts.AgentProposal` 的子集模型：review 只
    回答"要不要改结论、为什么"，intent/confidence/priority 这些仍由 agent 的主路径给出，
    两边合成最终提案（见 :meth:`BaseAgent._finalize_proposal`）。
    """

    model_config = ConfigDict(extra="forbid")

    outcome: ProposalOutcome
    noop_reason: str = ""
    risks: list[str] = Field(default_factory=list)
    missing_observations: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False


class BaseAgent(ABC):
    """Abstract base class for all smart home agents."""

    # §4.1 全部根事件（十四个富事件 + 三个迁移期兼容事件）。
    # 只订旧三个的年代，场景 timeline 声明的 user.arrives_home 会一路走到 AgentRuntime
    # 却在 is_relevant 里被判不相关——根事件发出去了，episode 一条也没有。
    # 排序是为了让订阅注册顺序确定（S2-T9 的确定性门要求没有集合迭代序泄漏）。
    #
    # 订阅面宽 ≠ episode 面宽：真正决定"开不开一轮推理"的是 :meth:`is_relevant`，它按
    # spec §7 的事件→设备相关面（backend/agents/relevance.py）与本 agent 控的设备类型
    # 求交。两者分开，才能既不丢事件（订阅全收）又不炸 episode（相关性收紧）。
    subscribed_event_types = tuple(sorted(ALL_ROOT_EVENT_TYPES))

    # §8.3 ``DomainTask.agent_role``：编排器按**角色**派活，不按实例 id——同一角色在不同
    # run 里可以换实现，契约不该被实例名绑死。空串表示"从 agent_id 推导"（见 :attr:`role`）。
    agent_role: str = ""

    def __init__(self, agent_id: str, name: str) -> None:
        self.agent_id = agent_id
        self.name = name

    @property
    def role(self) -> str:
        """本 agent 在 §8.2 里的角色名（lighting / hvac / security / energy / scene）。"""

        return self.agent_role or self.agent_id.removesuffix("_agent")

    @abstractmethod
    def get_controlled_device_types(self) -> list[str]:
        """Return the list of device type strings this agent controls."""

    @abstractmethod
    def decide(self, world_state: WorldState) -> list[dict]:
        """Analyse world state and return a list of actions to take.

        Each action dict has keys:
        - ``device_id``: str
        - ``property``: str (dot-notation path)
        - ``value``: new value
        - ``reason``: human-readable reason
        """

    @abstractmethod
    def get_allowed_command_specs(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> list[dict[str, Any]]:
        """Return device/property pairs the LLM is allowed to emit."""

    @abstractmethod
    def determine_priority(
        self,
        world_state: WorldState,
        root_event: SimEvent,
    ) -> PriorityLabel:
        """Return the semantic priority label used by the arbiter."""

    def _get_my_devices(self, world_state: WorldState) -> list[DeviceState]:
        """Return all devices whose type matches this agent's controlled types."""
        controlled = set(self.get_controlled_device_types())
        return [d for d in world_state.devices.values() if d.type in controlled]

    def _affected_device_type(self, world_state: WorldState, root_event: SimEvent) -> str | None:
        """事件指向的那台设备是什么类型；说不清就返回 None。

        先查世界（权威），查不到再退回事件自带的 ``device_type``——随机故障注入与场景
        timeline 都会带这个键，而被注入的设备可能压根不在当前世界里。
        """

        device_id = str(root_event.data.get("device_id") or "")
        device = world_state.devices.get(device_id) if device_id else None
        if device is not None:
            return device.type

        declared = root_event.data.get("device_type")
        return str(declared) if declared else None

    def is_relevant(self, world_state: WorldState, root_event: SimEvent) -> bool:
        if root_event.event_type not in self.subscribed_event_types:
            return False

        # 环境类根事件（含 §4.1 的温度/光照阈值）只有带"显著变化理由"时才值得跑一轮，
        # 否则每 tick 的例行刷新都会开 episode。
        if root_event.event_type in ENVIRONMENT_ROOT_EVENT_TYPES:
            if not root_event.data.get("significant_change_reasons"):
                return False

        controlled = set(self.get_controlled_device_types())

        # §7「device.offline → affected device | alternatives」：可用性事件是**设备族**
        # 事件，只有控着这台设备（或它的同类替代品）的 agent 能做出动作。少了这一条，
        # 照明 agent 会为一台摄像头掉线跑一轮完整推理。
        if is_device_availability_event(root_event.event_type):
            affected = self._affected_device_type(world_state, root_event)
            # 连是哪台设备都说不出 → 没有任何 agent 能定位替代品，开 episode 只是噪声。
            return affected is not None and affected in controlled

        device_id = str(root_event.data.get("device_id") or "")
        if root_event.event_type == "user.command" and device_id:
            device = world_state.devices.get(device_id)
            return device is None or device.type in controlled

        # 兜底不再是 ``return True``：§7 给每个根事件类型声明了默认设备相关面，agent 只在
        # "自己控的设备类型"与之有交集时才开 episode。判定读的是 §7 那张表（数据），
        # 因此 S3 新增的 domain agent 只要声明它控什么设备，就自动落到正确的事件面上。
        return bool(controlled & actionable_device_types(root_event.event_type))

    def get_relevant_devices(self, world_state: WorldState, root_event: SimEvent) -> list[DeviceState]:
        return self._get_my_devices(world_state)

    def get_relevant_rooms(self, world_state: WorldState, root_event: SimEvent) -> list[str]:
        rooms = {device.location.room for device in self.get_relevant_devices(world_state, root_event)}
        return sorted(rooms)

    def serialize_device_for_llm(
        self,
        device: DeviceState,
        world_state: WorldState,
    ) -> dict[str, Any]:
        room = world_state.rooms.get(device.location.room)
        compact_state: dict[str, Any] = {"power": device.state.power}
        if device.state.extra:
            compact_state["extra"] = dict(device.state.extra)

        payload: dict[str, Any] = {
            "device_id": device.id,
            "type": device.type,
            "room": device.location.room,
            "state": compact_state,
        }
        if room is not None:
            payload["room_state"] = {
                "temperature": round(room.temperature, 1),
                "occupancy": room.occupancy,
                "light_level": round(room.light_level, 1),
            }
        return payload

    def build_world_summary(self, world_state: WorldState, root_event: SimEvent) -> str:
        rooms = self.get_relevant_rooms(world_state, root_event)
        room_summaries: list[str] = []
        for room_id in rooms:
            room = world_state.rooms.get(room_id)
            if room is None:
                continue
            room_summaries.append(
                (
                    f"{room_id}: temp={room.temperature:.1f}, "
                    f"occupancy={room.occupancy}, light_level={room.light_level:.1f}"
                )
            )

        return (
            f"event={root_event.event_type}; "
            f"time={world_state.environment.time_of_day}; "
            f"weather={world_state.environment.weather}; "
            f"rooms={' | '.join(room_summaries)}"
        )

    def narrow_to_domain_task(
        self,
        devices: list[DeviceState],
        domain_task: DomainTask | None,
    ) -> list[DeviceState]:
        """把本轮相关设备收窄到编排器点名的那几台（§8.3 ``relevant_device_ids``）。

        两条自律：
        1. **收窄后为空就不收窄**。编排器点名的设备与 agent 自己算出来的相关设备理应一致，
           但真不一致时，让 agent 看见零台设备等于把这一轮静默作废——比多看几台糟得多。
        2. 只收窄，不扩张：编排器不能把 agent 不控的设备塞给它（那是仲裁前的越权）。
        """

        if domain_task is None or not domain_task.relevant_device_ids:
            return devices
        allowed = set(domain_task.relevant_device_ids)
        narrowed = [device for device in devices if device.id in allowed]
        return narrowed or devices

    # ---------------------------------------------------------------- §8.4 提案契约

    def decide_for_event(self, world_state: WorldState, root_event: SimEvent) -> list[dict]:
        """事件感知的规则决策；默认退回与事件无关的 :meth:`decide`。

        S2 之前的规则路径只有 ``decide(world)``——照明按钟点、空调按温度，都不需要知道
        "是什么把我叫醒的"。S3 的三个域 agent 不成立：同一台设备在 ``user.leaves_home``
        与 ``safety.smoke_detected`` 下的正确动作是**相反**的。因此规则入口在这里分叉，
        既有 agent 一行不改（默认实现就是老行为）。
        """

        return self.decide(world_state)

    def proposal_priority(self, world_state: WorldState, root_event: SimEvent) -> PriorityLevel:
        """本轮提案落在 §9.1 的哪一档。

        默认由既有的 :meth:`determine_priority`（旧六值标签）迁移而来——这样 lighting/hvac
        不改一行就有了 §9.1 档位；映射失败**抛错**而不是悄悄降档（见 contracts 里
        ``migrate_legacy_priority`` 的注释）。
        """

        return migrate_legacy_priority(str(self.determine_priority(world_state, root_event)))

    def proposal_intent(self, world_state: WorldState, root_event: SimEvent) -> str:
        """规则路径提案的意图标签（域 agent 覆盖它给出人话）。"""

        return f"{self.role} rule response to {root_event.event_type}"

    def review_proposal(
        self,
        *,
        world_state: WorldState,
        root_event: SimEvent,
        commands: list[AgentCommandProposal],
        domain_task: DomainTask | None = None,
    ) -> ProposalReview | None:
        """域 agent 的"非动作表达"钩子；返回 None 表示按常规路径结论。

        unsafe_rejected / missing_observations / needs_human_confirmation 这三种表达只有
        领域知识说得清（"覆盖不可验证"、"缺 scene_id"），所以基类不猜，只留钩子。
        """

        return None

    def propose(
        self,
        *,
        world_state: WorldState,
        root_event: SimEvent,
        domain_task: DomainTask | None = None,
        policy: OrchestrationPolicy | None = None,
    ) -> AgentProposal:
        """规则路径的 §8.4 提案：**同步、零 LLM、可复现**。

        这是 S3-T5 仲裁器与全部单测的入口。它与 :meth:`handle_event` 是同一套结论逻辑的
        两条腿（LLM 腿见 :meth:`build_proposal`），共用 :meth:`_finalize_proposal`——
        两条腿各判一次 outcome 是 §8.4 最容易长出分叉的地方。
        """

        actions = self.decide_for_event(world_state, root_event)
        commands = [
            AgentCommandProposal(
                device_id=action["device_id"],
                property=action["property"],
                value=action["value"],
                reason=action.get("reason", ""),
            )
            for action in actions
        ]
        if domain_task is not None and domain_task.relevant_device_ids:
            allowed = set(domain_task.relevant_device_ids)
            narrowed = [command for command in commands if command.device_id in allowed]
            # 与 narrow_to_domain_task 同一条自律：收窄后为空就不收窄。
            commands = narrowed or commands

        return self._finalize_proposal(
            world_state=world_state,
            root_event=root_event,
            commands=commands,
            intent=self.proposal_intent(world_state, root_event),
            confidence=self._fallback_confidence(root_event, actions),
            confidence_source=ConfidenceSource.RULE_BASED,
            requires_coordination=bool(commands),
            domain_task=domain_task,
            policy=policy,
        )

    def build_proposal(
        self,
        *,
        envelope: AgentDecisionEnvelope,
        world_state: WorldState,
        root_event: SimEvent,
        domain_task: DomainTask | None = None,
        policy: OrchestrationPolicy | None = None,
        observed_world_version: int | None = None,
    ) -> AgentProposal:
        """把一次 LLM 决策（envelope）投影成 §8.4 提案。

        envelope 记录的是"这一轮怎么想的"（provider/latency/world_summary），提案是
        "仲裁器要消费的结论"。两者分开，可观测性面板才能既显示过程又显示结论。
        """

        return self._finalize_proposal(
            world_state=world_state,
            root_event=root_event,
            commands=list(envelope.candidate_commands),
            intent=envelope.intent,
            confidence=envelope.confidence,
            confidence_source=(
                ConfidenceSource.RULE_BASED
                if envelope.mode == "fallback_rule_based"
                else ConfidenceSource.LLM
            ),
            requires_coordination=envelope.needs_coordination,
            domain_task=domain_task,
            policy=policy,
            observed_world_version=observed_world_version,
        )

    def proposal_assumptions(
        self,
        world_state: WorldState,
        root_event: SimEvent,
        domain_task: DomainTask | None,
    ) -> list[ProposalAssumption]:
        """Return observable facts shared by every command in this proposal."""

        return []

    def _finalize_proposal(
        self,
        *,
        world_state: WorldState,
        root_event: SimEvent,
        commands: list[AgentCommandProposal],
        intent: str,
        confidence: float,
        confidence_source: ConfidenceSource,
        requires_coordination: bool,
        domain_task: DomainTask | None,
        policy: OrchestrationPolicy | None,
        observed_world_version: int | None = None,
    ) -> AgentProposal:
        """五种非动作表达的**唯一**判定点（§8.4）。

        判定顺序即优先级，写死在这里而不是散在各 agent 里：
        领域裁决（unsafe/missing/…）> 需人确认 > 置信度不足 > 无事可做 > 动手。
        "无事可做"排在最后是重点——审计 §六 那条"silently emitting nothing"就是它被
        排在最前、把真正的原因盖掉造成的。
        """

        from backend.agents.orchestrator import resolve_min_confidence

        review = self.review_proposal(
            world_state=world_state,
            root_event=root_event,
            commands=list(commands),
            domain_task=domain_task,
        )
        priority = self.proposal_priority(world_state, root_event)
        base: dict[str, Any] = {
            "agent_id": self.agent_id,
            "agent_role": self.role,
            "intent": intent or f"{self.role} response",
            "priority": priority,
            "confidence": confidence,
            "confidence_source": confidence_source,
            "requires_coordination": requires_coordination,
            "assumptions": self.proposal_assumptions(
                world_state, root_event, domain_task
            ),
            "observed_world_version": observed_world_version,
        }

        if review is not None and review.outcome in NON_ACTION_OUTCOMES:
            return AgentProposal(
                **base,
                outcome=review.outcome,
                noop_reason=review.noop_reason or f"{self.role}: 本轮不动手",
                risks=list(review.risks),
                missing_observations=list(review.missing_observations),
                requires_confirmation=review.requires_confirmation,
                withheld_commands=list(commands),
            )

        risks = list(review.risks) if review is not None else []

        if commands and (policy is not None and policy.require_human_confirmation):
            return AgentProposal(
                **base,
                outcome=ProposalOutcome.NEEDS_HUMAN_CONFIRMATION,
                noop_reason="策略要求人工确认后才能执行（OrchestrationPolicy.require_human_confirmation）",
                risks=risks,
                requires_confirmation=True,
                withheld_commands=list(commands),
            )

        threshold = resolve_min_confidence(policy)
        if commands and confidence < threshold:
            return AgentProposal(
                **base,
                outcome=ProposalOutcome.LOW_CONFIDENCE,
                noop_reason=(
                    f"置信度 {confidence:.2f} 低于阈值 {threshold:.2f}，不静默执行"
                ),
                risks=risks,
                withheld_commands=list(commands),
            )

        if not commands:
            return AgentProposal(
                **base,
                outcome=ProposalOutcome.NO_ACTION_NEEDED,
                noop_reason=(
                    review.noop_reason
                    if review is not None and review.noop_reason
                    else f"{self.role}: 当前世界状态下没有需要调整的设备"
                ),
                risks=risks,
            )

        return AgentProposal(**base, commands=list(commands), risks=risks)

    async def handle_event(
        self,
        *,
        root_event: SimEvent,
        world_state: WorldState,
        memory_store: AgentMemoryStore,
        llm_provider: LLMProvider,
        domain_task: DomainTask | None = None,
    ) -> AgentDecisionEnvelope | None:
        """处理一条根事件。

        ``domain_task`` 是编排器派下来的那件事（§8.3）；直接构造 agent 的单测可省略。
        """

        if not self.is_relevant(world_state, root_event):
            return None

        relevant_devices = self.narrow_to_domain_task(
            self.get_relevant_devices(world_state, root_event), domain_task
        )
        relevant_rooms = sorted({device.location.room for device in relevant_devices}) or (
            self.get_relevant_rooms(world_state, root_event)
        )
        world_summary = self.build_world_summary(world_state, root_event)
        recent_events = memory_store.build_recent_event_lines(
            self.agent_id,
            root_event.correlation_id,
        )
        # 刻意**不**按 domain_task 收窄 allowed_commands：§7 说得很清楚——
        # "This mapping is not the same as mandatory execution. It defines the search space."
        # 搜索面决定 agent 看什么，能发什么命令仍由 agent 自己的白名单决定，最终合法性由
        # CommandExecutor 的六级校验兜底（S1）。在这里替执行层"提前修好"坏命令，只会让
        # tests/test_agent_command_validation.py 那条"坏命令必须走到失败生命周期"的门失效。
        allowed_commands = self.get_allowed_command_specs(world_state, root_event)
        started_at = time.monotonic()

        try:
            llm_decision = await llm_provider.generate_decision(
                LLMDecisionRequest(
                    agent_id=self.agent_id,
                    agent_name=self.name,
                    root_event_type=root_event.event_type,
                    world_summary=world_summary,
                    recent_events=recent_events,
                    available_devices=[
                        self.serialize_device_for_llm(device, world_state)
                        for device in relevant_devices
                    ],
                    allowed_commands=allowed_commands,
                )
            )
            latency_ms = int((time.monotonic() - started_at) * 1000)
            provider_failure_reason = llm_decision.provider_failure_reason
            raw_commands = (
                []
                if provider_failure_reason
                else list(llm_decision.proposed_commands)
            )
            candidate_commands = (
                []
                if provider_failure_reason
                else self._filter_commands(llm_decision, allowed_commands)
            )
            return AgentDecisionEnvelope(
                agent_id=self.agent_id,
                agent_name=self.name,
                mode=(
                    "provider_failure_noop"
                    if provider_failure_reason
                    else "llm"
                ),
                trigger_event_type=root_event.event_type,
                intent=llm_decision.intent,
                confidence=llm_decision.confidence,
                explanation=llm_decision.explanation,
                needs_coordination=llm_decision.needs_coordination,
                priority=self.determine_priority(world_state, root_event),
                task_steps=llm_decision.task_steps,
                raw_candidate_commands=raw_commands,
                raw_command_assessments=(
                    []
                    if provider_failure_reason
                    else self._assess_raw_commands(
                        llm_decision,
                        allowed_commands,
                        world_state,
                    )
                ),
                candidate_commands=candidate_commands,
                root_event_id=root_event.event_id,
                root_event_timestamp=root_event.timestamp,
                provider_name=getattr(llm_provider, "provider_name", "llm"),
                model=getattr(llm_provider, "model", ""),
                latency_ms=latency_ms,
                world_summary=world_summary,
                relevant_devices=[device.id for device in relevant_devices],
                relevant_rooms=relevant_rooms,
                provider_failure_reason=provider_failure_reason,
                failed_step=("intent_generation" if provider_failure_reason else None),
            )
        except LLMProviderError as exc:
            return self._build_fallback_envelope(
                root_event=root_event,
                world_state=world_state,
                world_summary=world_summary,
                relevant_devices=[device.id for device in relevant_devices],
                relevant_rooms=relevant_rooms,
                fallback_reason=exc.reason,
            )

    def _build_fallback_envelope(
        self,
        *,
        root_event: SimEvent,
        world_state: WorldState,
        world_summary: str,
        relevant_devices: list[str],
        relevant_rooms: list[str],
        fallback_reason: str,
    ) -> AgentDecisionEnvelope:
        fallback_actions = self.decide_for_event(world_state, root_event)
        return AgentDecisionEnvelope(
            agent_id=self.agent_id,
            agent_name=self.name,
            mode="fallback_rule_based",
            trigger_event_type=root_event.event_type,
            intent=f"{self.name} fallback decision",
            # 审计坑：这里原本是硬编码的 0.55——既不是阈值也不是测量值，没有任何消费者。
            # 现在由 §7 映射表对这条事件的覆盖强度 × 规则是否真的产出动作推导而来，
            # 因此它**会随输入变化**，也就可以被证伪（见 orchestrator.rule_based_confidence）。
            confidence=self._fallback_confidence(root_event, fallback_actions),
            explanation="LLM 不可用，回退到规则引擎",
            needs_coordination=bool(fallback_actions),
            priority=self.determine_priority(world_state, root_event),
            task_steps=["apply rule-based policy"],
            candidate_commands=[
                AgentCommandProposal(
                    device_id=action["device_id"],
                    property=action["property"],
                    value=action["value"],
                    reason=action.get("reason", ""),
                )
                for action in fallback_actions
            ],
            root_event_id=root_event.event_id,
            root_event_timestamp=root_event.timestamp,
            provider_name="fallback",
            model="rule_based",
            latency_ms=0,
            world_summary=world_summary,
            relevant_devices=relevant_devices,
            relevant_rooms=relevant_rooms,
            fallback_reason=fallback_reason,
            failed_step="intent_generation",
        )

    def _fallback_confidence(self, root_event: SimEvent, actions: list[dict]) -> float:
        """规则回退的置信度：§7 覆盖强度 × 规则是否真的产出了动作。

        延迟导入是为了不让 ``base`` 在导入期依赖编排层（编排器只做纯计算，反向依赖会
        把"agent 也能编排"这条错误暗示写进导入图）。
        """

        from backend.agents.orchestrator import rule_based_confidence

        return rule_based_confidence(
            root_event.event_type,
            activity=str(root_event.data.get("activity") or "") or None,
            affected_device_type=str(root_event.data.get("device_type") or "") or None,
            has_actions=bool(actions),
        )

    @staticmethod
    def _filter_commands(
        decision: AgentLLMDecision,
        allowed_commands: list[dict[str, Any]],
    ) -> list[AgentCommandProposal]:
        allowed = {
            (spec["device_id"], spec["property"])
            for spec in allowed_commands
        }
        return [
            command
            for command in decision.proposed_commands
            if (command.device_id, command.property) in allowed
        ]

    @staticmethod
    def _assess_raw_commands(
        decision: AgentLLMDecision,
        allowed_commands: list[dict[str, Any]],
        world_state: WorldState,
    ) -> list[RawCommandAssessment]:
        """Seal objective command validity against the planning snapshot."""

        allowed = {
            (spec["device_id"], spec["property"])
            for spec in allowed_commands
        }
        assessments: list[RawCommandAssessment] = []
        for index, command in enumerate(decision.proposed_commands):
            admitted = (command.device_id, command.property) in allowed
            if not admitted:
                failure_code = "agent_whitelist_rejected"
            else:
                failure = validate_command(
                    world_state,
                    command.device_id,
                    command.property.removeprefix("extra."),
                    command.value,
                )
                failure_code = failure.code.value if failure is not None else None
            assessments.append(
                RawCommandAssessment(
                    command_index=index,
                    admitted_by_agent=admitted,
                    valid_at_plan_time=admitted and failure_code is None,
                    failure_code=failure_code,
                )
            )
        return assessments
