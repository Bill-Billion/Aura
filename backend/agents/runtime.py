"""Agent runtime for event-driven episodes.

================================================================================
事件发射顺序契约（EMISSION ORDERING CONTRACT）—— S2-T9 确定性门的地基
================================================================================

**同一场景 + 同一 seed 的两次运行，必须产出逐字节相同的 canonical trace**
（backend/scenarios/trace.py）。事件流里只要有一处顺序随事件循环调度而变，这条门就
永久失效——而且它失效的方式是"偶尔红一次"，最容易被当成 flaky 测试关掉。

因此本模块把顺序拆成两件互不相干的事：

  **算，可以并发。发，必须串行。**

1. *算*（compute）：同一条 episode 里，多个 agent 的 ``handle_event`` 在
   ``asyncio.gather`` 下并发跑。慢 provider 不该把整条因果链串行拖死，而且"谁先算完"
   本来就不该被任何人观测到。

2. *发*（emit）：一条 episode 的**全部**事件（reasoning 六环 / 仲裁 / 命令生命周期 /
   action / feedback）在它**持号**期间一次性发完。号（ticket）由
   :class:`SerialEmissionGate` 严格按取号顺序服务：

   - 取号发生在 :meth:`AgentRuntime._handle_root_event` 里，即**根事件派发的那一刻**，
     而 EventBus.publish 是顺序的 → 取号顺序 == 根事件发布顺序 == 确定的。
   - 服务严格 FIFO：2 号即使先算完也必须等 0 号、1 号发完。于是"谁算得快"这个墙钟
     变量被彻底挡在事件流之外。

3. *tie-break*：一条 episode 内部，多个 agent 的事件按 **agent 注册顺序**发射
   （``self.agents`` 的下标），不是按算完先后。见 ``_ordered_agents``。

4. *不抢占*：后到的根事件**不再** ``cancel()`` 前一条在飞 episode，它只是排到下一号。
   旧实现在这里 ``existing.cancel()``，后果有两条：(a) 前一条推理链在任意 await 点被
   腰斩，斩在哪里取决于墙钟——不确定性；(b) 被斩掉的 episode 连一条
   ``system.episode_cancelled`` 都不发，整条链**静默消失**，而 §15 验收 3 要求"每条
   根事件都要产生一条可见的 episode"。这正是 S1 在命令层根治的那一类静默失败。

**给 S3 的话**（S3-T3 会重写 episode 派发循环、S3-T4 会再注册三个并发 agent、
S3-T6 会合并 per-agent 推理树）：编排器的分发循环必须继续满足上面四条。
``TaskPlan.domain_tasks`` 的顺序要是确定的（同样用 agent 注册序兜底），而"并发算 /
串行发"的闸门**不能**被绕过——绕过它的代价不是某个测试变红，是整个 S2 阶段门在
S3 落地后悄悄失去意义。tests/test_replay_determinism.py 里的
``test_agent_runtime_pins_the_serial_emission_gate`` 与
``test_two_root_events_both_emit_grouped_in_root_event_order`` 是这条契约的绊线。

**已知未覆盖的接缝**（S2-T9 file scope 之外，见交接说明）：引擎 tick 自己发布的事件
（timer_tick / scripted / environment.*）与 episode 的发射之间没有共用闸门。headless
runner 每拍之后 ``wait_for_idle``，因此跨拍不会交错；但同一拍内，若将来有人在
``SimulationEngine._tick`` 的两次 publish 之间加入新的 await，episode 的发射就可能挤进
那个缝里。真要封死，需要引擎在一拍之内也持同一把闸门。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.agents.arbiter import (
    ARBITER_ID,
    COORDINATION_DECISION_EVENT_TYPE,
    Arbiter,
    ArbiterResult,
    ArbitrationGate,
    RejectedCommand,
)
from backend.agents.base import BaseAgent
from backend.agents.contracts import AgentProposal, DomainTask, RootEventContext
from backend.agents.energy import EnergyAgent
from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.scene import SceneAgent
from backend.agents.security import SecurityAgent
from backend.agents.orchestrator import (
    DomainAgentBinding,
    HomeOrchestratorAgent,
    OrchestrationDecision,
)
from backend.agents.llm import (
    AnthropicCompatibleProvider,
    LLMProvider,
    LLMProviderError,
    OpenAIResponsesProvider,
)
from backend.agents.llm_modes import (
    BudgetGuardedLLMProvider,
    EpisodeCostGuard,
    LLMMode,
    RECORDING_CORRUPT_REASON,
    RECORDING_MISS_REASON,
    RECORDING_UNAVAILABLE_REASON,
    RECORDING_WRITE_REASON,
    ReplayLLMProvider,
    RunScopedRecordedProvider,
    VersionedDecision,
    WorldVersionTracker,
    build_provider_for_mode,
    build_stale_decision_event,
    check_stale_decision,
    live_llm_allowed,
    llm_mode_health,
    resolve_llm_mode_from_env,
    resolve_mode_for_provider,
    validate_recording_artifact,
)
from backend.agents.memory import AgentMemoryStore
from backend.agents.types import AgentCommandProposal, AgentDecisionEnvelope
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.event_log import (
    LLM_RECORDINGS_FILENAME,
    RunArtifactError,
    artifacts_enabled,
    read_run_metadata,
    run_dir,
)
from backend.engine.event_types import (
    ENVIRONMENT_STATE_REFRESH,
    starts_agent_episode,
)
from backend.engine.observation import ObservableProjector
from backend.engine.run_manager import (
    STALE_RUN_DISCARD_EVENT_TYPE,
    STALE_RUN_DISCARD_REASON,
    baseline_policy_for_llm_mode,
    effective_llm_mode_for_policy,
)
from backend.engine.state import AgentRuntimeState, WorldState
from backend.engine.state_manager import DeltaChange, StateManager
from backend.execution.command import (
    LEGAL_TRANSITIONS,
    CommandRecord,
    CommandSource,
    CommandStatus,
    DeviceCommand,
)
from backend.execution.executor import (
    ACTION_EVENT_TYPE,
    FEEDBACK_EVENT_TYPE,
    CommandExecutor,
)
from backend.execution.validation import validate_command
from backend.models.schemas import BaselinePolicy, WSMessage

PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]
# 当前活跃 run 的 run_id 读数源（由 SimulationEngine 注入 RunManager.run_id）。
# 注入的是"读数函数"而不是 run_id 值：run 会在 episode 在飞期间被换掉，
# 拿到的必须是**此刻**的活跃 run，不是绑定那一刻的快照。
RunIdSource = Callable[[], str | None]

__all__ = [
    "DEFAULT_AGENT_FACTORIES",
    "AgentRuntime",
    "ArbitrationGate",
    "BaselinePolicyUnavailableError",
    "DisabledLLMProvider",
    "HomeOrchestratorAgent",
    "SerialEmissionGate",
    "RuntimePolicySelection",
    "TriggerClassifier",
    "STALE_RUN_DISCARD_EVENT_TYPE",
    "STALE_RUN_DISCARD_REASON",
    "build_default_agents",
    "register_default_agents",
]


# —— 默认域 agent 注册表（§8.2 五个域）——————————————————————————————
#
# **顺序即契约**：它同时决定三件事——
#   1. ``TaskPlan.domain_tasks`` 的顺序（S3-T3 编排器按绑定顺序生成）；
#   2. episode 内事件的发射顺序（``_ordered_agents`` 的 tie-break）；
#   3. S2-T9 字节一致性门看到的 canonical trace 行序。
# 改这张表的顺序 = 改 canonical trace。lighting/hvac 排在前面是为了让 S2 之前
# 落地的 trace 前缀保持稳定。
DEFAULT_AGENT_FACTORIES: tuple[Callable[[], BaseAgent], ...] = (
    LightingAgent,
    HVACAgent,
    SecurityAgent,
    EnergyAgent,
    SceneAgent,
)


def build_default_agents() -> list[BaseAgent]:
    """按 :data:`DEFAULT_AGENT_FACTORIES` 的顺序造一组默认域 agent。"""

    return [factory() for factory in DEFAULT_AGENT_FACTORIES]


def register_default_agents(runtime: "AgentRuntime") -> list[BaseAgent]:
    """把默认域 agent 注册进 runtime（引擎装配的唯一入口）。

    引擎侧只留这一行调用，"注册了谁、什么顺序"这件事就只有一处真相；否则
    S4 的 headless runner、S5 的对比实验各抄一份注册代码，顺序迟早分叉。
    """

    agents = build_default_agents()
    for agent in agents:
        runtime.register(agent)
    return agents


class SerialEmissionGate:
    """按取号顺序服务的发射闸门（见模块开头的「事件发射顺序契约」）。

    与 :class:`asyncio.Lock` 的关键差别：Lock 的唤醒顺序取决于**谁先来排队**，而排队
    先后正是墙钟变量；本闸门的顺序取决于**谁先取号**，取号发生在根事件派发那一刻，
    是确定的。两者在无竞争时行为相同，有竞争时只有本闸门可复现。

    用法::

        ticket = gate.take_ticket()      # 同步取号（在确定的时点）
        ...                              # 并发计算，随便算多久
        async with gate.turn(ticket):    # 轮到自己才发射
            await publish(...)

    放号必须万无一失：任何一号漏放，后面所有 episode 会永久排队。因此
    :meth:`release` 幂等，且调用方（AgentRuntime）在 ``finally`` 与任务完成回调里
    各放一次——协程体压根没开始就被取消时，只有完成回调会执行。
    """

    def __init__(self) -> None:
        self._next_ticket = 0
        self._now_serving = 0
        # 已放但还没轮到的号（提前退出/被取消的 episode）：轮到时直接跳过。
        self._released_ahead: set[int] = set()
        self._waiters: dict[int, asyncio.Event] = {}

    @property
    def now_serving(self) -> int:
        return self._now_serving

    @property
    def outstanding(self) -> int:
        """已取号但还没放的号数（正常收尾时应为 0）。"""

        return self._next_ticket - self._now_serving - len(self._released_ahead)

    def take_ticket(self) -> int:
        ticket = self._next_ticket
        self._next_ticket += 1
        return ticket

    async def wait_turn(self, ticket: int) -> None:
        """等到轮到 *ticket*。刻意不设超时：超时放行 = 乱序发射，比慢更糟。

        （挂死的兜底在外层：headless runner 的 wait_for_idle 与
        cancel_active_episodes 都带超时，闸门卡住会以"等不到空闲"的形式暴露出来。）
        """

        while self._now_serving != ticket:
            waiter = self._waiters.get(ticket)
            if waiter is None:
                waiter = asyncio.Event()
                self._waiters[ticket] = waiter
            await waiter.wait()
        self._waiters.pop(ticket, None)

    def release(self, ticket: int) -> None:
        """放号（幂等）。已放过或早已过号的号再放一次是空操作。"""

        if ticket < self._now_serving or ticket in self._released_ahead:
            return
        self._released_ahead.add(ticket)
        while self._now_serving in self._released_ahead:
            self._released_ahead.discard(self._now_serving)
            self._now_serving += 1
        waiter = self._waiters.get(self._now_serving)
        if waiter is not None:
            waiter.set()

    def turn(self, ticket: int) -> "_EmissionTurn":
        return _EmissionTurn(self, ticket)

    def reset(self) -> None:
        """归零（换世界 / 取消全部 episode 之后）。调用方必须已经确保没有在飞持号者。"""

        self._next_ticket = 0
        self._now_serving = 0
        self._released_ahead.clear()
        for waiter in self._waiters.values():
            # 唤醒所有等待者：它们随后会看到号已归零并退出（其任务此刻已被取消）。
            waiter.set()
        self._waiters.clear()


class _EmissionTurn:
    """``async with gate.turn(ticket)``：进 = 等到自己的号，出 = 放号。"""

    def __init__(self, gate: SerialEmissionGate, ticket: int) -> None:
        self._gate = gate
        self._ticket = ticket

    async def __aenter__(self) -> "SerialEmissionGate":
        await self._gate.wait_turn(self._ticket)
        return self._gate

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._gate.release(self._ticket)
        return False


class DisabledLLMProvider(LLMProvider):
    provider_name = "disabled"
    model = "rule_based"

    async def generate_decision(self, request):  # type: ignore[override]
        raise LLMProviderError("provider_error", "LLM provider is disabled")


class BaselinePolicyUnavailableError(RuntimeError):
    """请求的 baseline 无法在当前服务端安全执行。"""

    def __init__(self, policy: BaselinePolicy, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.policy = policy
        self.reason = reason
        self.details = {"baseline_policy": policy.value, **details}


@dataclass(frozen=True)
class RuntimePolicySelection:
    """已校验但尚未激活的逐-run provider 选择。"""

    baseline_policy: BaselinePolicy
    llm_mode: LLMMode
    provider: LLMProvider
    recording_source_run_id: str | None = None


class TriggerClassifier:
    """Decide whether a root event should start a new agent episode.

    §15 验收 3「每条根事件都要产生一条可见的 episode」：分类学扩到 §4.1 十四个富根事件
    之后，这里必须同步放宽——否则场景 timeline 声明的 ``user.arrives_home`` 会被静默
    丢弃，事件流里有根事件却没有任何推理链，看起来像 agent 死了。

    唯一保留的"看情况"分支是环境类事件：它们每 tick 都可能来一条，只有带
    ``significant_change_reasons`` 的才值得开一轮推理（阈值事件由生成器盖这个键）。
    """

    def should_start_episode(self, event: SimEvent) -> bool:
        return starts_agent_episode(event.event_type, event.data)


class AgentRuntime:
    """Manage agent registration and event-driven episodes."""

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        state_manager: StateManager | None = None,
        connection_manager: ConnectionManager | None = None,
        publish_event: PublishEvent | None = None,
        llm_provider: LLMProvider | None = None,
        memory_store: AgentMemoryStore | None = None,
        arbiter: Arbiter | None = None,
        trigger_classifier: TriggerClassifier | None = None,
        episode_timeout_ms: int | None = None,
        command_executor: CommandExecutor | None = None,
        run_id_source: RunIdSource | None = None,
        observation_projector: ObservableProjector | None = None,
        orchestrator: HomeOrchestratorAgent | None = None,
        arbitration_gate: ArbitrationGate | None = None,
    ) -> None:
        self.agents: list[BaseAgent] = []
        # 编排层不在 self.agents 里：BaseAgent 控设备、发命令，编排器只负责派发任务。
        self.orchestrator = orchestrator or HomeOrchestratorAgent()
        self.event_bus = event_bus
        self.run_id_source = run_id_source
        self.state_manager = state_manager
        # §2.3：agent 只消费 observable 投影，不消费 ground truth。这台观测者是本 runtime
        # 里"世界 → agent"的**唯一**通道（见 observable_world），它持有的历史（每台设备
        # 最后一次在线读数）必须与世界同寿命——换世界时 update_state_manager 会清掉它。
        self.observation_projector = observation_projector or ObservableProjector()
        self.conn = connection_manager
        self.publish_event = publish_event
        # 引擎注入的那台唯一 executor；未注入时首次用到才自建一台并长期持有
        # （S1 review finding-8：绝不再每个 agent 每条 episode 造一台）。
        self.command_executor = command_executor
        self._recording_integrity_error_handler: (
            Callable[[str, str], None] | None
        ) = None
        # §11.1 模式路由的**唯一**生产入口（S3 review major-2）。注入的 provider 在这里的
        # 角色是"live 那一档"：逐 run 切策略时始终从这台服务端工厂重建，客户端既不能
        # 传 key，也不能传 provider URL/path。
        self._live_provider_factory: Callable[[], LLMProvider] = (
            (lambda: llm_provider) if llm_provider is not None else self._build_default_provider
        )
        self.llm_provider = self._resolve_llm_provider()
        # S3-T8 成本护栏（S3 review major-4）：台账挂在 runtime 上（跨 episode 累计，
        # run 工件要的是整份 run 的汇总），收费口按 episode 现套——见 _episode_llm_provider。
        self.cost_guard = EpisodeCostGuard()
        log.info("llm_mode_resolved", **llm_mode_health(self.llm_provider))
        self.memory_store = memory_store or AgentMemoryStore()
        self.arbiter = arbiter or Arbiter()
        # 仲裁门（S3-T5）：命令的 proposed→approved|rejected 都经它，且它就是装在
        # S1 预留的 ``submit(..., pre_submit=…)`` 接缝上的那个实现。UI 腿（main.py）与
        # agent 腿共用**同一台**，否则用户占用登记在一台、agent 仲裁读另一台，
        # "用户覆盖 agent" 又会退回名义状态。
        self.arbitration_gate = arbitration_gate or ArbitrationGate(arbiter=self.arbiter)
        self.trigger_classifier = trigger_classifier or TriggerClassifier()
        if episode_timeout_ms is None:
            # 设计意图：默认把单个 agent episode 的最长耗时压到和 LLM 超时同量级，
            # 避免兼容 provider 没有及时中断时，把整条事件链长时间卡住。
            timeout_value = os.getenv("AGENT_EPISODE_TIMEOUT_MS", "15000").strip()
            episode_timeout_ms = int(timeout_value) if timeout_value else None
        provider_timeout_ms = getattr(self.llm_provider, "timeout_ms", None)
        if episode_timeout_ms is not None and isinstance(provider_timeout_ms, int):
            # 设计意图：episode 超时至少比 provider 超时多留一点缓冲，
            # 避免 provider 已经拿到结果，但协作层还没来得及落地就被外层取消。
            episode_timeout_ms = max(episode_timeout_ms, provider_timeout_ms + 3000)
        self.episode_timeout_ms = episode_timeout_ms
        self._subscriptions_registered = False
        self._subscribed_event_types: set[str] = set()
        self._active_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        # 「算可以并发，发必须串行」的那把闸门（见模块开头的顺序契约）。
        # 它是 S2-T9 确定性门的机制本体，S3 的编排器分发循环必须继续走它。
        self.emission_gate = SerialEmissionGate()
        # 每条 episode task 记 correlation/root/agents，取消时才能落账
        # system.episode_cancelled（取消的协程自身只会收到 CancelledError）。
        self._episode_meta: dict[asyncio.Task[None], dict[str, Any]] = {}
        self.environment_debounce_ms = int(os.getenv("AGENT_ENV_DEBOUNCE_MS", "5000"))
        self._last_environment_episode_started_at: dict[str, float] = {}

    @property
    def is_provider_configured(self) -> bool:
        if isinstance(self.llm_provider, DisabledLLMProvider):
            return False
        return bool(getattr(self.llm_provider, "api_key", None))

    def _resolve_llm_provider(self) -> LLMProvider:
        """§11.1：``LLM_MODE`` 决定这台 runtime 跑的是罐头 / 录制回放 / 真 provider。

        **``under_test=False`` 是刻意的。** ``resolve_llm_mode_from_env`` 那条"pytest 下
        缺省 mocked"的分岔是给库调用方的便利默认；用在这条生产接线上，会让整套后端测试
        在没设 ``LLM_MODE`` 时静默切成罐头决策（其中大量断言断的正是规则回退链的形状）。
        缺省 = ``live`` = 既有行为：没有 key 时 :meth:`_build_default_provider` 返回
        :class:`DisabledLLMProvider`，与 S2 逐字一致。想要罐头就显式说 ``LLM_MODE=mocked``。

        注入的 provider 在这里的角色是 **live 那一档**，而不是"绕开模式系统"：
        ``LLM_MODE=recorded`` 时它被 :class:`RecordingLLMProvider` 包住（第一次跑落录制），
        ``LLM_MODE=mocked`` 时它整个被罐头顶掉。ScenarioRunner 注入的那台 disabled provider
        因此也吃 ``LLM_MODE``——headless 跑法不需要再开第二个开关。
        """

        mode = resolve_llm_mode_from_env(under_test=False)
        if mode is LLMMode.RECORDED:
            # 录制文件的位置要 run_id，而 run_id 在本方法执行时还不存在（RunManager 后建，
            # run_id_source 要到 bind() 才注入）。所以交给按 run 惰性解析的那层。
            return RunScopedRecordedProvider(
                live_provider_factory=self._live_provider_factory,
                run_id_source=self.current_run_id,
                integrity_error_handler=self._mark_recording_integrity_error,
            )
        return build_provider_for_mode(
            mode, live_provider_factory=self._live_provider_factory
        )

    def prepare_baseline_policy(
        self,
        policy: BaselinePolicy | None,
        *,
        recording_source_run_id: str | None = None,
    ) -> RuntimePolicySelection:
        """校验并构造逐-run provider，但不改动当前 runtime。

        这是两阶段切换的 prepare：live 凭证、recorded 来源等错误必须在引擎 pause/
        cancel/swap 之前暴露；真正激活由 reset 在旧 episode 全部取消后执行。
        """

        if policy is None:
            provider = self._resolve_llm_provider()
            mode = resolve_mode_for_provider(provider)
            return RuntimePolicySelection(
                baseline_policy=baseline_policy_for_llm_mode(mode),
                llm_mode=mode,
                provider=provider,
            )

        mode = effective_llm_mode_for_policy(policy)
        if policy is BaselinePolicy.RULE_BASED:
            provider = build_provider_for_mode(
                mode, live_provider_factory=self._live_provider_factory
            )
        elif policy is BaselinePolicy.LLM_MOCKED:
            provider = build_provider_for_mode(
                mode,
                live_provider_factory=self._live_provider_factory,
                strict_mock=False,
            )
        elif policy is BaselinePolicy.LLM_LIVE:
            provider = self._live_provider_factory()
            if not bool(getattr(provider, "api_key", None)):
                raise BaselinePolicyUnavailableError(
                    policy,
                    "服务端未配置可用的 live LLM provider",
                    reason_code="live_provider_not_configured",
                )
        else:
            if recording_source_run_id is not None:
                try:
                    source = read_run_metadata(recording_source_run_id)
                    source_path = run_dir(recording_source_run_id) / LLM_RECORDINGS_FILENAME
                except RunArtifactError as exc:
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源 run 不存在或不可读",
                        reason_code="recording_source_not_found",
                        recording_source_run_id=recording_source_run_id,
                    ) from exc
                if source.get("ended_at") is None:
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源 run 尚未 finalized",
                        reason_code="recording_source_not_finalized",
                        recording_source_run_id=recording_source_run_id,
                    )
                if source.get("end_reason") != "completed":
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源 run 未成功完成",
                        reason_code="recording_artifact_invalid",
                        recording_source_run_id=recording_source_run_id,
                        source_end_reason=source.get("end_reason"),
                    )
                if source.get("artifact_error"):
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源 run 的工件已标记为无效",
                        reason_code="recording_artifact_invalid",
                        recording_source_run_id=recording_source_run_id,
                        artifact_error=source.get("artifact_error"),
                    )
                if source.get("llm_mode") != LLMMode.RECORDED.value:
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源 run 的实际 LLM 模式不是 recorded",
                        reason_code="recording_source_mode_mismatch",
                        recording_source_run_id=recording_source_run_id,
                        source_llm_mode=source.get("llm_mode"),
                    )
                if source.get("recording_source_run_id") is not None:
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源必须是原始录制 run，不能继续链式引用回放 run",
                        reason_code="recording_artifact_invalid",
                        recording_source_run_id=recording_source_run_id,
                        nested_source_run_id=source.get("recording_source_run_id"),
                    )
                if not source_path.is_file():
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源 run 没有 LLM 录制工件",
                        reason_code="recording_artifact_missing",
                        recording_source_run_id=recording_source_run_id,
                    )
                try:
                    validate_recording_artifact(source_path)
                    provider = ReplayLLMProvider.from_file(source_path)
                except (OSError, LLMProviderError, ValueError) as exc:
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源工件损坏或不可回放",
                        reason_code="recording_artifact_invalid",
                        recording_source_run_id=recording_source_run_id,
                    ) from exc
                if not provider.recordings:
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "recorded 来源工件为空",
                        reason_code="recording_artifact_empty",
                        recording_source_run_id=recording_source_run_id,
                    )
            else:
                if not artifacts_enabled():
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "run 工件已禁用，无法执行 recorded 录制",
                        reason_code="run_artifacts_disabled",
                    )
                live = self._live_provider_factory()
                if not bool(getattr(live, "api_key", None)):
                    raise BaselinePolicyUnavailableError(
                        policy,
                        "服务端未配置可供 recorded 模式录制的 live provider",
                        reason_code="recording_provider_not_configured",
                    )
                # 不读取 LLM_RECORDINGS_PATH：这是一次显式逐-run 策略选择，目标只能是
                # 新 run 自己的受控工件目录。回放来源只能由 recording_source_run_id 指定。
                provider = RunScopedRecordedProvider(
                    live_provider_factory=lambda: live,
                    run_id_source=self.current_run_id,
                    allow_env_override=False,
                    integrity_error_handler=self._mark_recording_integrity_error,
                )

        effective = resolve_mode_for_provider(provider)
        if effective is not mode:
            raise BaselinePolicyUnavailableError(
                policy,
                "baseline 策略没有解析为预期的实际 LLM 模式",
                reason_code="effective_mode_mismatch",
                effective_llm_mode=effective.value,
            )
        return RuntimePolicySelection(
            baseline_policy=policy,
            llm_mode=effective,
            provider=provider,
            recording_source_run_id=recording_source_run_id,
        )

    def activate_baseline_policy(self, selection: RuntimePolicySelection) -> None:
        """在旧 run 任务清空后原子替换 provider，并清空逐-run 成本账。"""

        self.llm_provider = selection.provider
        self.cost_guard.reset()
        log.info(
            "run_baseline_policy_activated",
            baseline_policy=selection.baseline_policy.value,
            llm_mode=selection.llm_mode.value,
            recording_source_run_id=selection.recording_source_run_id,
        )

    def _episode_llm_provider(self, correlation_id: str) -> LLMProvider:
        """本条 episode 的收费口（S3-T8）。

        预算是 **per-episode** 的，而 provider 是全局单例，所以按 correlation_id 现套一层：
        编排器的 intent 调用与每个域 agent 的调用因此记在同一条 episode 台账上，
        "预算是在哪一步咬下去的"才有定义。mocked/replay/disabled 由
        ``BudgetGuardedLLMProvider.is_billable`` 自动免检——预算绝不能改写确定性跑法的行为。
        """

        return BudgetGuardedLLMProvider(
            self.llm_provider, self.cost_guard, correlation_id=correlation_id
        )

    @staticmethod
    def _build_default_provider() -> LLMProvider:
        """``live`` 那一档：按 ``LLM_PROVIDER`` + key 选真 provider，都没有就 disabled。

        测试进程里必须显式设 ``AURA_ALLOW_LIVE_LLM=1`` 才会搭真 provider——缺省闸门把
        本机拉回 CI 那条路径（无 key → DisabledLLMProvider → 规则回退），根治
        S3 review-2 blocker：仓库根的 ``.env.local`` 带着真 key，不再被意外消费。
        """

        if not live_llm_allowed():
            return DisabledLLMProvider()

        provider_name = os.getenv("LLM_PROVIDER", "").strip()

        if provider_name == "anthropic_compatible":
            provider = AnthropicCompatibleProvider.from_env()
            if provider.api_key:
                return provider
            return DisabledLLMProvider()

        if provider_name == "openai_responses":
            provider = OpenAIResponsesProvider.from_env()
            if provider.api_key:
                return provider
            return DisabledLLMProvider()

        openai_provider = OpenAIResponsesProvider.from_env()
        if openai_provider.api_key:
            return openai_provider

        anthropic_provider = AnthropicCompatibleProvider.from_env()
        if anthropic_provider.api_key:
            return anthropic_provider

        return DisabledLLMProvider()

    def register(self, agent: BaseAgent) -> None:
        self.agents.append(agent)
        if self.event_bus is not None and not self._subscriptions_registered:
            self._subscribe_handlers()
        elif self.event_bus is not None:
            self._refresh_subscriptions()

    def bind(
        self,
        *,
        event_bus: EventBus,
        state_manager: StateManager,
        connection_manager: ConnectionManager,
        publish_event: PublishEvent,
        command_executor: CommandExecutor | None = None,
        run_id_source: RunIdSource | None = None,
        recording_integrity_error_handler: Callable[[str, str], None] | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.conn = connection_manager
        self.publish_event = publish_event
        if command_executor is not None:
            self.command_executor = command_executor
        if run_id_source is not None:
            self.run_id_source = run_id_source
        if recording_integrity_error_handler is not None:
            self._recording_integrity_error_handler = recording_integrity_error_handler
        # 绑定 = 换了世界（引擎构造 / 重新接线）：上一份观测历史不属于这个世界。
        self.observation_projector.reset()
        # S3-T7：世界版本簿记装在 StateManager 的唯一写入口上（幂等）。装不上就等于
        # 陈旧决策丢弃整条不存在——所以它跟 observation_projector 一样属于"绑定即装"。
        WorldVersionTracker.attach(state_manager)
        self._refresh_subscriptions()

    def update_state_manager(self, state_manager: StateManager) -> None:
        self.state_manager = state_manager
        WorldVersionTracker.attach(state_manager)
        # reset 换世界后必须丢掉观测历史，否则上一个 run 的读数会被当成本 run 的
        # "最后一次汇报"回放给 agent——与 S1 根治的 reset 污染同类。
        self.observation_projector.reset()

    # --- §2.3 observable 投影（agent 侧唯一的世界入口）----------------------

    def observable_world(self) -> WorldState:
        """agent 能看见的世界（§2.3）。

        S2-T6 的场景 runner 与 S3 的编排器要构造"agent 视角"时一律走这里，**不要**
        自己调 ``state_manager.world.snapshot()``——那条路径拿到的是 ground truth，
        会让感知受限的评估在悄无声息中失效。

        本期投影是恒等的（只有离线设备报陈旧读数），S4-T3 的漂移注入器接上噪声之后，
        所有已经走这条入口的调用方自动获得噪声，不需要再改一遍。
        """

        if self.state_manager is None:
            raise RuntimeError("AgentRuntime is not bound")
        return self.observation_projector.observe(self.state_manager.world)

    # --- run 身份（§2.2 "旧 run 的变更不得应用到活跃 run"）------------------

    def current_run_id(self) -> str | None:
        """此刻的活跃 run_id；没接 RunManager 时恒为 None（等于关掉 run 门）。"""

        return self.run_id_source() if self.run_id_source is not None else None

    def _mark_recording_integrity_error(self, message: str) -> None:
        run_id = self.current_run_id()
        if run_id is not None and self._recording_integrity_error_handler is not None:
            self._recording_integrity_error_handler(run_id, message)

    @staticmethod
    def _is_recording_integrity_reason(reason: str | None) -> bool:
        return reason in {
            RECORDING_CORRUPT_REASON,
            RECORDING_MISS_REASON,
            RECORDING_UNAVAILABLE_REASON,
            RECORDING_WRITE_REASON,
        }

    def _is_stale_run(self, episode_run_id: str | None) -> bool:
        """episode 起点抓的 run 是否已经不是活跃 run。

        ``episode_run_id is None`` 恒判不 stale：那是没接 run 模型的调用方
        （独立构造的 runtime / 老单测），对它们判 stale 等于凭空开始丢命令。
        """

        if episode_run_id is None:
            return False
        return self.current_run_id() != episode_run_id

    async def _resolve_executor(self) -> CommandExecutor:
        """本 runtime 使用的唯一 executor（引擎注入优先）。

        没有注入方（独立构造的 runtime / 单测）时自建一台并缓存：注册表照样要有寿命，
        否则 cancel_pending 与跨调用取代在这条腿上依旧是空转。

        换绑是 async 的：上一个 run 残留的在飞命令要**带生命周期事件**地取消，
        不能像 S1 那样 ``_pending.clear()`` 静默抹掉（见 executor.bind_state_manager）。
        """

        if self.state_manager is None:
            raise RuntimeError("AgentRuntime is not bound")
        if self.command_executor is None:
            self.command_executor = CommandExecutor(self.state_manager)
        elif self.command_executor.state_manager is not self.state_manager:
            # 世界被换过（reset）而 executor 没跟上：换绑并落账上一个 run 的残留。
            await self.command_executor.bind_state_manager(
                self.state_manager, reason="state_manager_rebound"
            )
        # 仲裁门必须跟着同一台 executor 走（换世界时它的仲裁窗口记录也一并作废）。
        self.arbitration_gate.bind(self.command_executor)
        return self.command_executor

    def reset(self) -> None:
        self.memory_store.clear()
        self.observation_projector.reset()
        # 成本预算是 per run；上一 run 的 episode 台账不能挤占下一 run 的预算。
        self.cost_guard.reset()
        # 旧世界的 explicit_user 占用不属于新世界：不清掉的话，reset 之后第一条
        # agent 提案会输给一条根本不存在的用户命令。
        self.arbitration_gate.forget_claims()

    async def cancel_active_episodes(self, reason: str = "simulation_reset") -> None:
        """Cancel in-flight episode tasks and surface each as system.episode_cancelled.

        审计必修②：调用方必须在世界替换之前 await 本方法（cancel-before-swap），
        否则旧 episode 恢复后会把命令写进重置后的新世界。
        """
        tasks = {task for task in self._active_tasks.values() if not task.done()} | {
            task for task in self._background_tasks if not task.done()
        }
        cancelled_episodes = [
            self._episode_meta[task] for task in tasks if task in self._episode_meta
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            # 取消可能卡在慢 provider/坏连接的收尾上，用 episode 超时同量级兜底。
            timeout_s = self.episode_timeout_ms / 1000 if self.episode_timeout_ms else None
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "episode_cancel_timeout",
                    pending=len([task for task in tasks if not task.done()]),
                )
                results = []
            for result in results:
                # CancelledError 是 BaseException，这里只暴露真正的收尾异常。
                if isinstance(result, Exception):
                    log.warning("episode_cancel_teardown_error", error=str(result))
        self._active_tasks.clear()
        self._background_tasks.clear()
        self._episode_meta.clear()
        self._last_environment_episode_started_at.clear()
        # 全部在飞 episode 已被取消并等回，此刻没有持号者：闸门归零，新 run 从 0 号重新开始。
        # （不归零的话，新 run 的第一条 episode 会去等一个永远不会被服务的旧号。）
        self.emission_gate.reset()

        if not cancelled_episodes:
            return

        for episode in cancelled_episodes:
            if self.publish_event is not None and self.state_manager is not None:
                await self.publish_event(
                    SimEvent(
                        event_type="system.episode_cancelled",
                        source="agent_runtime",
                        timestamp=float(self.state_manager.world.simulation_tick),
                        wall_time=time.time(),
                        correlation_id=episode["correlation_id"],
                        causal_parent=episode["root_event_id"],
                        priority=2,
                        data={
                            "correlation_id": episode["correlation_id"],
                            "agent_ids": list(episode["agent_ids"]),
                            "reason": reason,
                            # 被取消的 episode 属于哪个 run：reset 之后事件历史会清空，
                            # 这条收尾事件是工件里唯一还能指认旧 run 的线索。
                            "run_id": episode.get("run_id"),
                        },
                    )
                )
            for agent_id in episode["agent_ids"]:
                self._set_agent_idle(agent_id)
        await self._broadcast_agent_status()

    async def wait_for_idle(self, timeout: float | None = None) -> bool:
        """等所有在飞 episode 自然收工（不取消它们）。返回是否等到了空闲。

        S2-T6 的 headless runner 每拍调一次：episode 是后台任务，不等它跑完就进下一拍的话，
        同一场景两次运行的事件交错顺序会随事件循环调度而变——S2-T9 的字节一致性门当场失效。
        与 :meth:`cancel_active_episodes` 的区别是本方法**不取消**任何东西：它等的是
        正常结束，超时只如实返回 False（超时不等于"可以当作没发生"）。
        """

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while True:
            tasks = {task for task in self._active_tasks.values() if not task.done()} | {
                task for task in self._background_tasks if not task.done()
            }
            if not tasks:
                return True
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                log.warning("agent_runtime_wait_for_idle_timeout", pending=len(tasks))
                return False
            await asyncio.wait(tasks, timeout=remaining)

    async def close(self) -> None:
        self._unsubscribe_handlers()
        tasks = {task for task in self._active_tasks.values() if not task.done()} | {
            task for task in self._background_tasks if not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active_tasks.clear()
        self._background_tasks.clear()
        self._episode_meta.clear()
        self._last_environment_episode_started_at.clear()
        self.emission_gate.reset()

    def _refresh_subscriptions(self) -> None:
        self._unsubscribe_handlers()
        self._subscribe_handlers()

    def _subscribe_handlers(self) -> None:
        if self.event_bus is None or self._subscriptions_registered:
            return

        subscribed_types = {
            event_type
            for agent in self.agents
            for event_type in getattr(agent, "subscribed_event_types", ())
        }
        for event_type in subscribed_types:
            self.event_bus.subscribe(event_type, self._handle_root_event)

        self._subscribed_event_types = subscribed_types
        self._subscriptions_registered = True

    def _unsubscribe_handlers(self) -> None:
        if self.event_bus is None or not self._subscriptions_registered:
            return

        for event_type in self._subscribed_event_types:
            self.event_bus.unsubscribe(event_type, self._handle_root_event)

        self._subscribed_event_types.clear()
        self._subscriptions_registered = False

    async def _handle_root_event(self, event: SimEvent) -> None:
        self.memory_store.remember(event)
        if not self.trigger_classifier.should_start_episode(event):
            return
        # 当前前端发来的 CMD_DEVICE_CONTROL 已经是最终设备命令，
        # 这里不再重复触发一轮 agent 推理，避免淹没旧协议的即时反馈。
        if event.event_type == "user.command" and event.data.get("message_type") == "CMD_DEVICE_CONTROL":
            return
        if self.state_manager is None or self.publish_event is None or self.conn is None:
            return

        # §2.3：从这里往下，agent 侧看到的一律是 observable 投影。相关性判定也算 agent
        # 的感知（它读 devices/rooms），因此与 episode 共用**同一次**投影——分两次取会
        # 让"决定要不要跑"和"跑的时候看到的"来自两个不同时刻的观测。
        observable = self.observable_world()
        # S3-T7：与那次投影**同一刻**的世界版本。陈旧判定的语义就是"这条决策据以推理的
        # 世界读数是在这个版本上取的"，所以它必须与 observable 在同一行取，不能在
        # episode 协程里补取——那已经隔了若干次调度。
        observed_version = self._current_world_version()
        relevant_agents = [agent for agent in self.agents if agent.is_relevant(observable, event)]
        if not relevant_agents:
            return

        now = time.monotonic()
        agents_to_run: list[BaseAgent] = []
        for agent in relevant_agents:
            # Only the compatibility refresh is high-frequency. Rich threshold
            # roots are already edge-triggered by the generator's hysteresis and
            # §15 requires each of them to produce a visible queued episode.
            if event.event_type == ENVIRONMENT_STATE_REFRESH:
                existing = self._active_tasks.get(agent.agent_id)
                if existing is not None and not existing.done():
                    continue
                last_started_at = self._last_environment_episode_started_at.get(agent.agent_id, 0.0)
                if (now - last_started_at) * 1000 < self.environment_debounce_ms:
                    continue
            agents_to_run.append(agent)

        if not agents_to_run:
            return

        # 顺序契约第 4 条：后到的根事件**不抢占**前一条在飞 episode。
        # 旧实现在这里 existing.cancel()：前一条推理链会在任意 await 点被腰斩，斩在哪
        # 取决于墙钟（不确定性），而且连一条 system.episode_cancelled 都不发（静默消失，
        # 违反 §15 验收 3「每条根事件都要有一条可见 episode」）。现在它排到下一号，
        # 由 emission_gate 保证"先来的根事件先发完"。

        # 取号必须在这里：本方法由 EventBus 顺序派发，取号顺序 == 根事件发布顺序，
        # 与 agent 算得快慢无关——这是整条确定性链的起点。
        emission_ticket = self.emission_gate.take_ticket()
        # episode 在起点抓一次 run_id：它跑完时活跃 run 可能已经换了（reset / 场景连跑），
        # 落地前拿这份快照与当时的活跃 run 对账（§2.2）。
        episode_run_id = self.current_run_id()
        task = asyncio.create_task(
            self._run_episode(
                event,
                agents_to_run,
                run_id=episode_run_id,
                observable=observable,
                observed_version=observed_version,
                emission_ticket=emission_ticket,
            )
        )
        self._background_tasks.add(task)
        self._episode_meta[task] = {
            "correlation_id": event.correlation_id,
            "root_event_id": event.event_id,
            "agent_ids": [agent.agent_id for agent in agents_to_run],
            "run_id": episode_run_id,
        }
        for agent in agents_to_run:
            self._active_tasks[agent.agent_id] = task
            if event.event_type == ENVIRONMENT_STATE_REFRESH:
                self._last_environment_episode_started_at[agent.agent_id] = now

        def _cleanup(done_task: asyncio.Task[None], agent_ids: list[str]) -> None:
            # 放号（幂等）：协程体没来得及开始就被取消时，_run_episode 的 finally 不会执行，
            # 只有完成回调一定会跑。漏放一号 = 后续所有 episode 永久排队。
            self.emission_gate.release(emission_ticket)
            self._background_tasks.discard(done_task)
            self._episode_meta.pop(done_task, None)
            for agent_id in agent_ids:
                if self._active_tasks.get(agent_id) is done_task:
                    self._active_tasks.pop(agent_id, None)
            # episode 崩了必须当场取回异常并落成日志：不取回只会得到解释器那条
            # "Task exception was never retrieved"，agent 却已经停在半路——正是 S1 要
            # 根治的静默失败类（review2 finding-1 的 agent 腿症状）。
            if not done_task.cancelled():
                error = done_task.exception()
                if error is not None:
                    log.error(
                        "agent_episode_failed",
                        agent_ids=agent_ids,
                        error=repr(error),
                    )
                    for agent_id in agent_ids:
                        self._set_agent_idle(agent_id)

        task.add_done_callback(lambda done_task, agent_ids=[agent.agent_id for agent in agents_to_run]: _cleanup(done_task, agent_ids))

    def _ordered_agents(self, agents: list[BaseAgent]) -> list[BaseAgent]:
        """按 **agent 注册顺序** 排列（顺序契约第 3 条的 tie-break）。

        episode 内多个 agent 的事件必须按注册序发射，而不是按"谁先算完"。调用方今天
        传进来的本来就是注册序（``_handle_root_event`` 从 ``self.agents`` 过滤而来），
        这里再排一次是把**约定**变成**机制**：S3 的编排器会从 TaskPlan 生成 agent 列表，
        那条路径上的顺序不再天然是注册序。
        """

        registration = {agent.agent_id: index for index, agent in enumerate(self.agents)}
        return sorted(
            agents,
            key=lambda agent: (registration.get(agent.agent_id, len(registration)), agent.agent_id),
        )

    async def _run_episode(
        self,
        root_event: SimEvent,
        agents: list[BaseAgent],
        *,
        run_id: str | None = None,
        observable: WorldState | None = None,
        observed_version: int | None = None,
        emission_ticket: int | None = None,
    ) -> None:
        """一条 episode：并发**算**，持号**发**（见模块开头的顺序契约）。

        ``emission_ticket`` 由 ``_handle_root_event`` 在根事件派发那一刻取好传进来；
        直接调用本方法的入口（单测、未来的编排器）不传票号时就地取一张——闸门没有旁路，
        少一条旁路就少一处 S3 可能悄悄绕开确定性的地方。
        """

        ticket = self.emission_gate.take_ticket() if emission_ticket is None else emission_ticket
        try:
            await self._run_episode_body(
                root_event,
                agents,
                run_id=run_id,
                observable=observable,
                observed_version=observed_version,
                ticket=ticket,
            )
        finally:
            # 幂等放号。协程体压根没开始就被取消时，本 finally 不会执行——那条路径由
            # _handle_root_event 的任务完成回调兜底（两处都放，漏一处就永久堵死）。
            self.emission_gate.release(ticket)

    async def _run_episode_body(
        self,
        root_event: SimEvent,
        agents: list[BaseAgent],
        *,
        run_id: str | None,
        observable: WorldState | None,
        ticket: int,
        observed_version: int | None = None,
    ) -> None:
        if self.state_manager is None or self.publish_event is None or self.conn is None:
            return

        # §2.3：agent 拿到的是 observable 投影，不是 ground truth 快照。
        # 调用方（_handle_root_event）已经投影过一次就复用那一份——同一条 episode 里
        # 相关性判定与推理必须基于同一次观测。
        snapshot = observable if observable is not None else self.observable_world()
        if observed_version is None:
            # 直接调用本方法的入口（单测 / 未来的编排器）没给版本时就地取一次：
            # 与 observable 的兜底同源，陈旧判定因此没有"关掉"的旁路。
            observed_version = self._current_world_version()
        candidates = self._ordered_agents(agents)

        # —— 编排（§8.1）：也是"算"的一部分，因此在闸门之外做完。——
        # 它不写世界、不发事件，只决定"这一轮派给谁、每人干什么"。
        decision = await self._plan_episode(root_event, snapshot, candidates)
        selected = set(decision.selected_agent_ids)
        # 顺序仍取自 candidates（= 注册序）：TaskPlan.domain_tasks 本身就是按注册序
        # 生成的，这里再过一次是把"两处顺序必须一致"变成机制而不是巧合。
        agents = [agent for agent in candidates if agent.agent_id in selected]

        for agent in agents:
            self._ensure_agent_state(agent)
            self._set_agent_thinking(agent, root_event.correlation_id, root_event.event_type)
        await self._broadcast_agent_status()

        # 同一根事件下的 agent episode 并发执行，避免慢 provider 把整条因果链串行拖死。
        # 这里是契约允许的"并发算"：gather 的**结果顺序**恒等于入参顺序，谁先算完不影响。
        evaluation_tasks = [
            asyncio.create_task(
                self._evaluate_agent(
                    root_event=root_event,
                    snapshot=snapshot,
                    agent=agent,
                    domain_task=decision.task_for_agent(agent.agent_id),
                )
            )
            for agent in agents
        ]
        evaluated = await asyncio.gather(*evaluation_tasks)

        # 从这里往下是"发"：整段持号，按取号序（=根事件发布序）串行。
        async with self.emission_gate.turn(ticket):
            await self._emit_episode(
                root_event=root_event,
                agents=agents,
                evaluated=list(evaluated),
                run_id=run_id,
                decision=decision,
                candidates=candidates,
                snapshot=snapshot,
                observed_version=observed_version,
            )

    # --- §8.1 编排 ----------------------------------------------------------

    def _domain_bindings(self, agents: list[BaseAgent]) -> list[DomainAgentBinding]:
        """按**注册顺序**把候选 agent 投影成编排器认识的绑定（确定性契约的起点）。"""

        return [DomainAgentBinding.from_agent(agent) for agent in agents]

    async def _plan_episode(
        self,
        root_event: SimEvent,
        snapshot: WorldState,
        agents: list[BaseAgent],
    ) -> OrchestrationDecision:
        """根事件 → :class:`OrchestrationDecision`（§8.3）。

        编排器拿到的是 ``RootEventContext.from_observable_world`` 的**深拷贝**，因此
        "orchestrator 不写世界"不是靠约定：它手上根本没有真世界的引用。
        """

        context = RootEventContext.from_observable_world(
            root_event=root_event,
            observable_world=snapshot,
            run_id=root_event.run_id or self.current_run_id(),
        )
        decision = await self.orchestrator.plan(
            context,
            self._domain_bindings(agents),
            # 编排器的 intent 调用与域 agent 的调用记在**同一条** episode 台账上（S3-T8）。
            llm_provider=self._episode_llm_provider(root_event.correlation_id),
        )
        if self._is_recording_integrity_reason(decision.plan.fallback_reason):
            self._mark_recording_integrity_error(
                f"recorded orchestrator integrity failure: {decision.plan.fallback_reason}"
            )
        return decision

    async def _emit_episode(
        self,
        *,
        root_event: SimEvent,
        agents: list[BaseAgent],
        evaluated: list[AgentDecisionEnvelope | None],
        run_id: str | None,
        decision: OrchestrationDecision,
        candidates: list[BaseAgent],
        snapshot: WorldState,
        observed_version: int,
    ) -> None:
        """episode 的**发射**阶段：本方法内发出的每一条事件都在持号期间。

        S3 提醒：新增的推理事件、编排事件一律加在这里；加在 ``_run_episode_body`` 的
        并发段里（gather 之前/之中）就等于绕开闸门，S2 的确定性门会在某次 CI 上莫名变红。
        """

        if self.state_manager is None or self.publish_event is None or self.conn is None:
            return

        envelopes: list[AgentDecisionEnvelope] = []
        # run 门（§2.2）：LLM 那一段是本 episode 最长的 await，期间 run 可能已经换掉。
        # 取消在飞任务是第一道防线，但换 run 不一定伴随取消（场景连跑、headless runner），
        # 因此落地前必须再问一次"我还属于活跃 run 吗"。
        if self._is_stale_run(run_id):
            await self._discard_stale_episode(
                root_event=root_event,
                agents=agents,
                episode_run_id=run_id,
                stage="after_agent_evaluation",
                envelopes=[item for item in evaluated if item is not None],
            )
            return

        # 编排层的三环（感知 → 意图 → 任务拆分）先发，域 agent 的推理链挂在它下面：
        # §4.4「causal_parent 必须指向直接父事件」，而这一轮里域 agent 的直接父就是
        # 编排器的任务拆分，不是根事件本身。
        episode_parent = await self._emit_orchestrator_prefix(root_event, decision)

        # S3-T6：编排器在场时，**前三环归编排器**，域 agent 不再各起一棵平行小树。
        # 审计 §六「episode 被稀释」说的就是旧形状：一条 correlation 下挂着 N 套同名的
        # perception/intent/task_decomposition，六环名义上齐了，却没有一棵树能从感知一路
        # 读到反馈。域 agent 的感知/意图/置信度改由 reasoning.execution_plan 承载
        # （见 _agent_contribution_data）——信息一条不少，树只有一棵。
        for agent, envelope in zip(agents, evaluated, strict=False):
            if envelope is None:
                self._set_agent_idle(agent.agent_id)
                continue

            envelopes.append(envelope)
            # 降级发生在仲裁之前，必须当场留痕，不能挪到执行环补记。
            await self._emit_agent_fallback(root_event, envelope, episode_parent)

        pending_deltas: list[DeltaChange] = []
        agent_by_id = {agent.agent_id: agent for agent in agents}
        publishers = {
            envelope.agent_id: self._agent_publish_event(envelope.agent_id, pending_deltas)
            for envelope in envelopes
        }
        tick = int(self.state_manager.world.simulation_tick)

        # —— §8.4：信封 → 提案。仲裁器消费的是**结论**（提案），不是决策过程（信封）——
        # 也正是这一步让 §8.4 的五种非动作表达（低置信 / 缺观测 / 不安全…）真正生效：
        # 被扣下的命令进 withheld_commands，不会一路溜进 executor。
        proposals: list[AgentProposal] = []
        for envelope in envelopes:
            agent = agent_by_id.get(envelope.agent_id)
            if agent is None:
                continue
            proposals.append(
                agent.build_proposal(
                    envelope=envelope,
                    world_state=snapshot,
                    root_event=root_event,
                    domain_task=decision.task_for_agent(agent.agent_id),
                )
            )

        # §8.2 能耗否决权：占用门在 EnergyAgent 内部，这里只负责把评审结果喂给仲裁器。
        energy_review = None
        energy_agent = next(
            (agent for agent in self.agents if isinstance(agent, EnergyAgent)), None
        )
        if energy_agent is not None:
            energy_review = energy_agent.review_peer_proposals(proposals, snapshot)

        result = self.arbiter.resolve(
            proposals,
            root_event,
            snapshot,
            energy_review=energy_review,
            # 用户在本 episode 开始**之后**下的直控 → agent 提案在同一控制点上让位。
            # 这是"用户覆盖 agent"从名义变机制的读取端（写入端见 ArbitrationGate.pre_submit）。
            user_claims=self.arbitration_gate.claims_since(float(root_event.wall_time or 0.0)),
        )

        # run 门（§2.2）：推理事件外发也是 await 点，写世界之前再问一次身份。
        if self._is_stale_run(run_id):
            await self._broadcast_pending_deltas(pending_deltas)
            await self._discard_stale_episode(
                root_event=root_event,
                agents=agents,
                episode_run_id=run_id,
                stage="before_command_execution",
                envelopes=envelopes,
            )
            return

        # —— §9.3：一条 episode **只发一条** reasoning.coordination_decision ——
        # 审计原文「仲裁全序名存实亡」的症状之一就是这条事件按 agent 各发一条：
        # 于是"仲裁"看起来像每人自说自话，全序压根没有落点。per-agent 结论现在降级为
        # 本事件 data 里的 per_agent 分解。
        coordination_event = await self._emit_agent_event(
            root_event=root_event,
            agent_id=ARBITER_ID,
            event_type=COORDINATION_DECISION_EVENT_TYPE,
            causal_parent=episode_parent,
            data=result.event_data(),
        )
        for envelope in envelopes:
            self._update_reasoning_step(envelope.agent_id, coordination_event.event_type)

        # —— S3-T7 陈旧决策丢弃：仲裁之后、开命令之前的最后一问 ——
        # 位置是有讲究的：放在这里，本 episode 自己的命令一条都还没落地，因此"设备版本变了"
        # 只可能是**别人**改的（用户直控 / 另一条 episode / 仿真器），不会自己把自己判死。
        stale_by_agent = await self._discard_stale_decisions(
            root_event=root_event,
            envelopes=envelopes,
            result=result,
            causal_parent=coordination_event.event_id,
            observed_version=observed_version,
        )

        # 解析（并把仲裁门绑到）引擎持有的那台唯一 executor。
        executor = await self._resolve_executor()

        # —— 开仲裁窗口：每条胜出命令先出生成 proposed 并进在飞注册表 ——
        # 出生与执行之间隔着若干次事件外发（await），一条用户直控可以在这段窗口里
        # 把它取代掉（S1 把取代范围写成"同批内"，这里扩宽到仲裁窗口）。
        opened: list[tuple[str, CommandRecord, str]] = []
        for envelope in envelopes:
            agent_id = envelope.agent_id
            stale_devices = stale_by_agent.get(agent_id, frozenset())
            # 被判陈旧的命令在这里就出局：execution_plan 里因此只剩真会执行的那些，
            # "计划"与"落地"不再各说各话（丢了哪些、为什么，在上面那条
            # reasoning.decision_discarded 里）。
            approved = [
                command
                for command in result.approved_for(agent_id)
                if command.device_id not in stale_devices
            ]
            execution_event = await self._emit_agent_event(
                root_event=root_event,
                agent_id=agent_id,
                event_type="reasoning.execution_plan",
                causal_parent=coordination_event.event_id,
                data={
                    "agent_id": agent_id,
                    "agent_role": getattr(agent_by_id.get(agent_id), "role", ""),
                    "execution_mode": envelope.mode,
                    "commands": [command.as_proposal().model_dump() for command in approved],
                    # 域 agent 的推理贡献（编排器在场时这里是它唯一的落点）。
                    **self._agent_contribution_data(
                        envelope,
                        decision.task_for_agent(agent_id),
                    ),
                },
            )
            self._update_reasoning_step(agent_id, execution_event.event_type)

            # 仲裁落败的命令绝不凭空消失：proposed→rejected，detail 带 §9.2 冲突分类。
            for rejected in result.rejected_for(agent_id):
                await self._reject_arbitration_loser(
                    executor=executor,
                    rejected=rejected,
                    envelope=envelope,
                    root_event=root_event,
                    causal_parent=execution_event.event_id,
                    publish=publishers[agent_id],
                    tick=tick,
                )

            for command in approved:
                record = await self.arbitration_gate.open(
                    self._build_device_command(
                        proposal=command.as_proposal(),
                        source=self._command_source(envelope),
                        root_event=root_event,
                        causal_parent=execution_event.event_id,
                        actor=agent_id,
                        actor_name=envelope.agent_name,
                    ),
                    publish=publishers[agent_id],
                    tick=tick,
                )
                opened.append((agent_id, record, command.reason))

        # —— 执行：审计必修①，落地全部交给 CommandExecutor（六级校验 / 十态生命周期 /
        # 失败事件 / effect 钩子都在那一条流水线里）。
        last_actions = {envelope.agent_id: envelope.explanation for envelope in envelopes}
        for agent_id, record, reason in opened:
            if self._is_stale_run(run_id):
                await self._cancel_opened_commands(opened, tick=tick)
                await self._broadcast_pending_deltas(pending_deltas)
                await self._discard_stale_episode(
                    root_event=root_event,
                    agents=agents,
                    episode_run_id=run_id,
                    stage="before_command_execution",
                    envelopes=envelopes,
                )
                return
            executed = await self.arbitration_gate.execute(
                record, publish=publishers[agent_id], tick=tick
            )
            if executed.status is CommandStatus.SUCCEEDED and reason:
                last_actions[agent_id] = reason

        for envelope in envelopes:
            self._set_agent_complete(
                agent_id=envelope.agent_id,
                envelope=envelope,
                last_action=last_actions.get(envelope.agent_id, envelope.explanation),
            )

        await self._broadcast_pending_deltas(pending_deltas)

        # S3-T8：这条 episode 花了多少、有没有被预算挡下来，落进 run 成本工件。
        self._persist_llm_cost(run_id, root_event.correlation_id)

        # candidates ⊇ agents：被编排器判为"这一轮没你的事"的 agent 也要回到 idle，
        # 否则它们会永远停在 thinking——面板上看起来像卡死。
        for agent in candidates:
            if all(envelope.agent_id != agent.agent_id for envelope in envelopes):
                self._set_agent_idle(agent.agent_id)

        await self._broadcast_agent_status()

    # --- S3-T7 陈旧决策丢弃 / S3-T8 成本工件 --------------------------------

    def _current_world_version(self) -> int | None:
        """此刻的世界版本；没装 tracker（未绑定 runtime）时 None＝不做陈旧判定。"""

        if self.state_manager is None:
            return None
        tracker = WorldVersionTracker.of(self.state_manager)
        return tracker.version if tracker is not None else None

    async def _discard_stale_decisions(
        self,
        *,
        root_event: SimEvent,
        envelopes: list[AgentDecisionEnvelope],
        result: ArbiterResult,
        causal_parent: str,
        observed_version: int | None,
    ) -> dict[str, frozenset[str]]:
        """落地前比一次世界版本，命中就丢弃并留痕（evolution-review 风险 #2）。

        LLM 一轮要 1-5 秒，世界不会停下来等它：决策据以推理的那台设备可能已经被用户直控
        或另一条 episode 改过。此时照发下去，就是拿一份过期世界去改现在的世界，而事后没有
        任何记录说明这次改动的前提早已不成立。

        粒度是 **per-device**：环境每 tick 都在推进全局版本，用全局计数器判等于把所有决策
        都判死。返回 ``{agent_id: 陈旧设备集合}``，调用方据此把那些命令挡在 executor 之外
        （零状态改动）。
        """

        if observed_version is None or self.state_manager is None or self.publish_event is None:
            return {}
        tracker = WorldVersionTracker.of(self.state_manager)
        if tracker is None:
            return {}

        stale_by_agent: dict[str, frozenset[str]] = {}
        for envelope in envelopes:
            approved = result.approved_for(envelope.agent_id)
            if not approved:
                continue
            decision = VersionedDecision.at_version(
                observed_version,
                agent_id=envelope.agent_id,
                commands=[command.as_proposal() for command in approved],
                correlation_id=root_event.correlation_id,
                root_event_id=causal_parent,
            )
            check = check_stale_decision(decision, tracker)
            if not check.is_stale:
                continue

            event = build_stale_decision_event(
                decision,
                check,
                root_event=root_event,
                source=envelope.agent_id,
                sim_time_s=root_event.sim_time_s,
            )
            # 时间戳与 wall_time 对齐 _emit_agent_event：build_stale_decision_event 是纯函数，
            # 它拿不到"现在第几拍"。
            event.timestamp = float(self.state_manager.world.simulation_tick)
            event.wall_time = time.time()
            published = await self.publish_event(event)
            self.memory_store.remember(published, agent_id=envelope.agent_id)
            log.warning(
                "agent_decision_discarded_stale",
                agent_id=envelope.agent_id,
                correlation_id=root_event.correlation_id,
                decided_at_version=check.decided_at_version,
                current_version=check.current_version,
                stale_device_ids=check.stale_device_ids,
            )
            stale_by_agent[envelope.agent_id] = frozenset(check.stale_device_ids)
        return stale_by_agent

    def _persist_llm_cost(self, run_id: str | None, correlation_id: str) -> None:
        """把本 run 的 LLM 成本汇总落到 ``data/runs/{run_id}/llm_cost.json``。

        只在这条 episode 真的动过收费口时写（发生过调用，或被预算挡下过）：mocked/回放/
        disabled 跑法一次都不动，也就不该在 run 目录里凭空多出一份全零账单。
        """

        if not run_id:
            return
        episode = self.cost_guard.episode(correlation_id)
        if not episode.calls and not episode.blocked_calls:
            return
        self.cost_guard.write_run_artifact(run_id)

    async def _emit_orchestrator_prefix(
        self,
        root_event: SimEvent,
        decision: OrchestrationDecision,
    ) -> str:
        """编排层的三环事件，返回域 agent 推理链应当挂靠的父事件 id。

        ``reasoning.task_decomposition`` 的 data 是**结构化的 domain_tasks**，不是 LLM 散文
        （审计 §六 那条坑的落点）；LLM 失败时额外发一条 ``reasoning.fallback_rule_based``，
        让"这一轮是规则算出来的"在事件流里可见（§15 降级验收）。
        """

        context_view = decision.plan
        scope_devices = sorted(
            {device_id for task in context_view.domain_tasks for device_id in task.relevant_device_ids}
        )
        scope_rooms = sorted(
            {room_id for task in context_view.domain_tasks for room_id in task.relevant_room_ids}
        )
        perception = await self._emit_agent_event(
            root_event=root_event,
            agent_id=self.orchestrator.orchestrator_id,
            event_type="reasoning.perception_snapshot",
            causal_parent=root_event.event_id,
            data={
                "orchestrator_id": context_view.orchestrator_id,
                "trigger_event_type": root_event.event_type,
                "search_space_device_types": list(decision.rule_intent.device_types),
                "search_space_resolved_from": decision.rule_intent.resolved_from,
                "default_policy": decision.rule_intent.default_policy,
                # event_schema_version=1.0 compatibility; remove only with a v2 migrator.
                "world_summary": (
                    f"{root_event.event_type} → 搜索空间 {len(scope_devices)} 台设备 / "
                    f"{len(scope_rooms)} 个房间（{decision.rule_intent.resolved_from}）"
                ),
                "relevant_devices": scope_devices,
                "relevant_rooms": scope_rooms,
            },
        )
        intent_event = await self._emit_agent_event(
            root_event=root_event,
            agent_id=self.orchestrator.orchestrator_id,
            event_type="reasoning.intent_recognized",
            causal_parent=perception.event_id,
            data=decision.intent_event_data(),
        )
        decomposition_data = decision.task_decomposition_event_data()
        decomposition_data["task_steps"] = [
            f"{task.agent_role}: {task.task}" for task in context_view.domain_tasks
        ]
        decomposition = await self._emit_agent_event(
            root_event=root_event,
            agent_id=self.orchestrator.orchestrator_id,
            event_type="reasoning.task_decomposition",
            causal_parent=intent_event.event_id,
            data=decomposition_data,
        )
        parent_id = decomposition.event_id

        if decision.plan.fallback_reason:
            fallback = await self._emit_agent_event(
                root_event=root_event,
                agent_id=self.orchestrator.orchestrator_id,
                event_type="reasoning.fallback_rule_based",
                causal_parent=decomposition.event_id,
                data={
                    "orchestrator_id": context_view.orchestrator_id,
                    "reason": decision.plan.fallback_reason,
                    "failed_step": "intent_classification",
                    "fallback_strategy": "rule_based",
                },
            )
            parent_id = fallback.event_id

        return parent_id

    async def _broadcast_pending_deltas(self, pending_deltas: list[DeltaChange]) -> None:
        """把本 episode 已落地的 delta 一次性广播（保持"一 episode 一条 STATE_DELTA"口径）。"""

        if not pending_deltas or self.conn is None:
            return
        await self.conn.broadcast(
            WSMessage(
                type="STATE_DELTA",
                payload={"deltas": [delta.model_dump() for delta in pending_deltas]},
            )
        )

    async def _discard_stale_episode(
        self,
        *,
        root_event: SimEvent,
        agents: list[BaseAgent],
        episode_run_id: str | None,
        stage: str,
        envelopes: list[AgentDecisionEnvelope],
    ) -> None:
        """丢弃属于旧 run 的 episode 产物，并把这件事**发成事件**（discarded:stale_run）。

        为什么不能静默丢：§2.2 只禁止旧 run 的变更落地，没说可以让它凭空消失。
        一条 agent 推理链在事件流里走到一半忽然断掉，与 S1 根治的静默吞失败是同一种
        可观测性伤害——研究者会把它读成"agent 卡死了"。

        事件归属：本事件属于**活跃 run**（它发生在活跃 run 里，旧 run 的历史已被清空），
        故 run_id 留空由总线盖章；causal_parent 刻意不指向旧 run 的根事件——那条边跨 run，
        会在新 run 的因果图里留一个永远找不到父的悬挂指针。根事件 id 记在 data 里。
        """

        active_run_id = self.current_run_id()
        discarded_commands = [
            {
                "agent_id": envelope.agent_id,
                "device_id": proposal.device_id,
                "property": proposal.property,
                "value": proposal.value,
            }
            for envelope in envelopes
            for proposal in envelope.candidate_commands
        ]
        agent_ids = [agent.agent_id for agent in agents]
        log.warning(
            "stale_run_episode_discarded",
            stale_run_id=episode_run_id,
            active_run_id=active_run_id,
            correlation_id=root_event.correlation_id,
            agent_ids=agent_ids,
            stage=stage,
            discarded_commands=len(discarded_commands),
        )

        if self.publish_event is not None and self.state_manager is not None:
            await self.publish_event(
                SimEvent(
                    event_type=STALE_RUN_DISCARD_EVENT_TYPE,
                    source="agent_runtime",
                    timestamp=float(self.state_manager.world.simulation_tick),
                    wall_time=time.time(),
                    correlation_id=root_event.correlation_id,
                    priority=2,
                    event_generation_mode="system",
                    data={
                        "reason": STALE_RUN_DISCARD_REASON,
                        "discarded": "agent_episode",
                        "stage": stage,
                        "stale_run_id": episode_run_id,
                        "active_run_id": active_run_id,
                        "correlation_id": root_event.correlation_id,
                        "root_event_id": root_event.event_id,
                        "agent_ids": agent_ids,
                        "discarded_commands": discarded_commands,
                    },
                )
            )

        for agent_id in agent_ids:
            self._set_agent_idle(agent_id)
        await self._broadcast_agent_status()

    async def _evaluate_agent(
        self,
        *,
        root_event: SimEvent,
        snapshot: WorldState,
        agent: BaseAgent,
        domain_task: DomainTask | None = None,
    ) -> AgentDecisionEnvelope | None:
        # S3-T8：每条 episode 现套一层预算收费口。超预算时它抛
        # LLMProviderError(budget_exceeded)，被下面既有的 except 接住 → 规则回退，
        # fallback_reason 就是 budget_exceeded（"预算在哪一步咬下去"因此可数）。
        episode_provider = self._episode_llm_provider(root_event.correlation_id)
        try:
            if self.episode_timeout_ms is None:
                return await agent.handle_event(
                    root_event=root_event,
                    world_state=snapshot,
                    memory_store=self.memory_store,
                    llm_provider=episode_provider,
                    domain_task=domain_task,
                )

            return await asyncio.wait_for(
                agent.handle_event(
                    root_event=root_event,
                    world_state=snapshot,
                    memory_store=self.memory_store,
                    llm_provider=episode_provider,
                    domain_task=domain_task,
                ),
                timeout=self.episode_timeout_ms / 1000,
            )
        except asyncio.TimeoutError as exc:
            self._log_episode_failure(
                agent=agent,
                reason="timeout",
                error_message=(
                    f"agent episode timed out after {self.episode_timeout_ms}ms"
                ),
            )
            return self._build_agent_fallback(
                agent=agent,
                root_event=root_event,
                snapshot=snapshot,
                fallback_reason="timeout",
            )
        except LLMProviderError as exc:
            if self._is_recording_integrity_reason(exc.reason):
                self._mark_recording_integrity_error(
                    f"recorded agent integrity failure: {exc.reason}"
                )
            self._log_episode_failure(
                agent=agent,
                reason=exc.reason,
                error_message=str(exc),
                raw_output_preview=exc.raw_output_preview,
            )
            return self._build_agent_fallback(
                agent=agent,
                root_event=root_event,
                snapshot=snapshot,
                fallback_reason=exc.reason,
            )
        except Exception as exc:
            self._log_episode_failure(
                agent=agent,
                reason="provider_error",
                error_message=str(exc),
            )
            return self._build_agent_fallback(
                agent=agent,
                root_event=root_event,
                snapshot=snapshot,
                fallback_reason="provider_error",
            )

    def _build_agent_fallback(
        self,
        *,
        agent: BaseAgent,
        root_event: SimEvent,
        snapshot: WorldState,
        fallback_reason: str,
    ) -> AgentDecisionEnvelope:
        return agent._build_fallback_envelope(  # noqa: SLF001
            root_event=root_event,
            world_state=snapshot,
            world_summary=agent.build_world_summary(snapshot, root_event),
            relevant_devices=[device.id for device in agent.get_relevant_devices(snapshot, root_event)],
            relevant_rooms=agent.get_relevant_rooms(snapshot, root_event),
            fallback_reason=fallback_reason,
        )

    def _log_episode_failure(
        self,
        *,
        agent: BaseAgent,
        reason: str,
        error_message: str,
        raw_output_preview: str | None = None,
    ) -> None:
        log.warning(
            "agent_episode_fallback",
            agent_id=agent.agent_id,
            provider=getattr(self.llm_provider, "provider_name", "unknown"),
            model=getattr(self.llm_provider, "model", ""),
            reason=reason,
            error=error_message,
            raw_output_preview=raw_output_preview,
        )

    @staticmethod
    def _agent_contribution_data(
        envelope: AgentDecisionEnvelope,
        domain_task: DomainTask | None,
    ) -> dict[str, Any]:
        """域 agent 在 ``reasoning.execution_plan`` 上的推理贡献（§4.3 六环合并后的落点）。

        编排器接管前三环之后，域 agent 自己的感知与意图不再单独成环——但它们**不能消失**：
        "这个 agent 看到了什么、打算干什么、有多确信、是不是降级算出来的"正是可观测性
        面板要展示的东西。这些字段因此原样搬到执行环上，键名与旧的 per-agent 事件保持一致
        （``world_summary`` / ``relevant_devices`` / ``relevant_rooms`` 收进 ``perception``
        子对象，避免和执行环自己的 ``commands`` 混在一层）。
        """

        return {
            "intent": envelope.intent,
            "confidence": envelope.confidence,
            "explanation": envelope.explanation,
            "provider": envelope.provider_name,
            "model": envelope.model,
            "latency_ms": envelope.latency_ms,
            "task_steps": list(envelope.task_steps),
            "fallback_reason": envelope.fallback_reason,
            "perception": {
                "trigger_event_type": envelope.trigger_event_type,
                "world_summary": envelope.world_summary,
                "relevant_devices": list(envelope.relevant_devices),
                "relevant_rooms": list(envelope.relevant_rooms),
            },
            # 编排器派给它的那件事：没有这个字段就答不出"这条执行计划是谁点的名"。
            "domain_task": domain_task.model_dump(mode="json") if domain_task is not None else None,
        }

    async def _emit_agent_fallback(
        self,
        root_event: SimEvent,
        envelope: AgentDecisionEnvelope,
        causal_parent: str,
    ) -> None:
        """域 agent 的降级环（§4.3 ``reasoning.fallback_rule_based``）。

        编排器在场时这是域 agent 唯一自己发的推理事件：降级发生在仲裁**之前**，
        挪进执行环补记就读不出"哪一步失败了才转的规则"。非降级 episode 不发。
        """

        if envelope.mode != "fallback_rule_based":
            return
        event = await self._emit_agent_event(
            root_event=root_event,
            agent_id=envelope.agent_id,
            event_type="reasoning.fallback_rule_based",
            causal_parent=causal_parent,
            data={
                "agent_id": envelope.agent_id,
                "reason": envelope.fallback_reason,
                "failed_step": envelope.failed_step or "intent_generation",
                "fallback_strategy": "rule_based",
            },
        )
        self._update_reasoning_step(envelope.agent_id, event.event_type)

    def _build_device_command(
        self,
        *,
        proposal: AgentCommandProposal,
        source: CommandSource,
        root_event: SimEvent,
        causal_parent: str,
        actor: str | None = None,
        actor_name: str | None = None,
    ) -> DeviceCommand:
        """agent 提案 → executor 的 DeviceCommand（property 点路径归一为能力名）。

        ``actor``/``actor_name`` 承载执行者身份：source 只有四个值（agent/rule_fallback…），
        丢了 agent_id 可观测性面板就分不清是哪个 agent 干的（S1 review finding-1）。
        """

        if self.state_manager is None:
            raise RuntimeError("AgentRuntime is not bound")

        return DeviceCommand(
            source=source,
            actor=actor,
            actor_name=actor_name,
            device_id=proposal.device_id,
            capability=proposal.property.removeprefix("extra."),
            value=proposal.value,
            reason=proposal.reason,
            correlation_id=root_event.correlation_id,
            causal_parent=causal_parent,
            issued_tick=int(self.state_manager.world.simulation_tick),
            priority=2,
        )

    def _agent_publish_event(
        self, agent_id: str, collected_deltas: list[DeltaChange]
    ) -> PublishEvent:
        """给 executor 的 publish 包装：保留 agent 记忆归属，并采集 delta 供 STATE_DELTA 广播。

        executor 只认 publish 回调，不知道 agent 记忆与 WS 广播；这层包装把
        action/feedback 事件按原语义记进该 agent 的记忆，反馈里的 delta 攒起来在
        episode 收尾统一广播（保持"一次 episode 一条 STATE_DELTA"的既有口径）。
        """

        async def publish(event: SimEvent) -> SimEvent:
            if self.publish_event is None:
                raise RuntimeError("AgentRuntime is not bound")
            published = await self.publish_event(event)
            if published.event_type in {ACTION_EVENT_TYPE, FEEDBACK_EVENT_TYPE}:
                self.memory_store.remember(published, agent_id=agent_id)
            if published.event_type == FEEDBACK_EVENT_TYPE:
                collected_deltas.append(DeltaChange.model_validate(published.data))
            return published

        return publish

    async def _reject_arbitration_loser(
        self,
        *,
        executor: CommandExecutor,
        rejected: RejectedCommand,
        envelope: AgentDecisionEnvelope,
        root_event: SimEvent,
        causal_parent: str,
        publish: PublishEvent,
        tick: int,
    ) -> CommandRecord:
        """一条仲裁落败命令的收口：``proposed → rejected`` + （若适用）§10.2 失败上报。

        两层词表在这里刻意**同时**出现，而且各归各的所有者：

        * ``detail`` 写 §9.2 冲突分类——"为什么没被选中"是协作层的问题；
        * ``failure_code`` 与 ``device.command_failed`` 走 §10.2 词表，且失败码由
          ``validation.py`` 现算——"这条命令本来也执行不了"是执行层的判断。

        少了第二层，S0-4 那条奇偶护栏（同一条坏命令在 agent 腿与 UI 腿必须拿到相同错误码）
        就会在"仲裁抢先否掉"的路径上悄悄失效：UI 腿不受单边拒绝约束，照常拿到
        ``unknown_device``，agent 腿却只剩一句"仲裁落败"。
        """

        command = self._build_device_command(
            proposal=rejected.as_proposal(),
            source=self._command_source(envelope),
            root_event=root_event,
            causal_parent=causal_parent,
            actor=rejected.agent_id,
            actor_name=envelope.agent_name,
        )
        detail = f"仲裁落败（{rejected.conflict_class.value}）：{rejected.rejection_reason}"
        failure = (
            validate_command(
                self.state_manager.world,
                command.device_id,
                command.capability,
                command.value,
            )
            if self.state_manager is not None
            else None
        )
        log.warning(
            "agent_command_rejected",
            agent_id=rejected.agent_id,
            device_id=command.device_id,
            capability=command.capability,
            conflict_class=rejected.conflict_class.value,
            failure_code=failure.code.value if failure is not None else None,
        )
        record = await self.arbitration_gate.reject(
            command,
            publish=publish,
            detail=detail,
            failure_code=failure.code.value if failure is not None else None,
            tick=tick,
        )
        if failure is not None:
            await executor.report_command_failed(
                command,
                error_code=failure.code.value,
                reason=failure.message,
                causal_parent=causal_parent,
                tick=tick,
                publish=publish,
            )
        return record

    @staticmethod
    def _command_source(envelope: AgentDecisionEnvelope) -> CommandSource:
        """命令来源：规则回退与 LLM 决策在审计口径上必须分得开。"""

        return (
            CommandSource.RULE_FALLBACK
            if envelope.mode == "fallback_rule_based"
            else CommandSource.AGENT
        )

    async def _cancel_opened_commands(
        self, opened: list[tuple[str, CommandRecord, str]], *, tick: int | None = None
    ) -> None:
        """episode 中途判定 stale 时，收掉还停在仲裁窗口里的 proposed 记录。

        不收的话它们会以非终态永远留在 executor 的在飞注册表里：下一条同控制点的合法
        命令会发出一条它其实没经历过的 superseded，而这条命令自己的生命周期永远没有收尾
        ——正是 S1 全程根治的那类静默失败。
        """

        for _, record, _reason in opened:
            if not record.is_terminal and CommandStatus.CANCELLED in LEGAL_TRANSITIONS[record.status]:
                await record.transition(
                    CommandStatus.CANCELLED, detail=STALE_RUN_DISCARD_REASON, tick=tick
                )
            # 注册表与仲裁窗口都要清干净：终态记录留在里面会让下一条同控制点命令
            # 发出一条它其实没经历过的 superseded。
            self.arbitration_gate.discard(record)

    async def _emit_agent_event(
        self,
        *,
        root_event: SimEvent,
        agent_id: str,
        event_type: str,
        causal_parent: str,
        data: dict[str, Any],
    ) -> SimEvent:
        if self.publish_event is None or self.state_manager is None:
            raise RuntimeError("AgentRuntime is not bound")

        event = await self.publish_event(
            SimEvent(
                event_type=event_type,
                source=agent_id,
                timestamp=float(self.state_manager.world.simulation_tick),
                wall_time=time.time(),
                correlation_id=root_event.correlation_id,
                causal_parent=causal_parent,
                priority=1,
                data=data,
            )
        )
        self.memory_store.remember(event, agent_id=agent_id)
        return event

    def _ensure_agent_state(self, agent: BaseAgent) -> AgentRuntimeState:
        if self.state_manager is None:
            raise RuntimeError("AgentRuntime is not bound")

        entry = self.state_manager.world.agents.get(agent.agent_id)
        if entry is None:
            entry = AgentRuntimeState(id=agent.agent_id, name=agent.name)
            self.state_manager.world.agents[agent.agent_id] = entry
        return entry

    def _set_agent_thinking(self, agent: BaseAgent, correlation_id: str, trigger_event_type: str) -> None:
        entry = self._ensure_agent_state(agent)
        entry.status = "thinking"
        entry.mode = "llm"
        entry.current_strategy = "event_driven"
        entry.active_correlation_id = correlation_id
        entry.last_reasoning_step = "reasoning.perception_snapshot"
        entry.last_fallback_reason = None
        entry.last_latency_ms = None
        entry.last_trigger_event = trigger_event_type
        entry.provider = getattr(self.llm_provider, "provider_name", "disabled")
        entry.provider_configured = self.is_provider_configured

    def _update_reasoning_step(self, agent_id: str, event_type: str) -> None:
        if self.state_manager is None:
            return
        entry = self.state_manager.world.agents.get(agent_id)
        if entry is not None:
            entry.last_reasoning_step = event_type

    def _set_agent_complete(
        self,
        *,
        agent_id: str,
        envelope: AgentDecisionEnvelope,
        last_action: str,
    ) -> None:
        if self.state_manager is None:
            return
        entry = self.state_manager.world.agents.get(agent_id)
        if entry is None:
            return

        entry.status = "idle"
        entry.mode = envelope.mode
        entry.current_strategy = envelope.intent
        entry.confidence = envelope.confidence
        entry.last_action = last_action
        entry.active_correlation_id = None
        entry.last_fallback_reason = envelope.fallback_reason
        entry.provider = getattr(self.llm_provider, "provider_name", envelope.provider_name)
        entry.provider_configured = self.is_provider_configured
        entry.last_latency_ms = envelope.latency_ms
        entry.last_trigger_event = envelope.trigger_event_type

    def _set_agent_idle(self, agent_id: str) -> None:
        if self.state_manager is None:
            return
        entry = self.state_manager.world.agents.get(agent_id)
        if entry is not None:
            entry.status = "idle"
            entry.active_correlation_id = None
            entry.provider = getattr(self.llm_provider, "provider_name", entry.provider)
            entry.provider_configured = self.is_provider_configured

    async def _broadcast_agent_status(self) -> None:
        if self.conn is None or self.state_manager is None:
            return
        await self.conn.broadcast(
            WSMessage(
                type="AGENT_STATUS",
                payload={
                    "agents": {
                        agent_id: agent.model_dump()
                        for agent_id, agent in self.state_manager.world.agents.items()
                    }
                },
            )
        )
