"""Agent runtime for legacy step mode and Phase 2 event-driven episodes.

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
from typing import Any

from backend.agents.arbiter import Arbiter
from backend.agents.base import BaseAgent
from backend.agents.llm import (
    AnthropicCompatibleProvider,
    LLMProvider,
    LLMProviderError,
    OpenAIResponsesProvider,
)
from backend.agents.memory import AgentMemoryStore
from backend.agents.types import AgentCommandProposal, AgentDecisionEnvelope
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.event_types import (
    ALL_ROOT_EVENT_TYPES,
    ENVIRONMENT_ROOT_EVENT_TYPES,
)
from backend.engine.observation import ObservableProjector
from backend.engine.run_manager import (
    STALE_RUN_DISCARD_EVENT_TYPE,
    STALE_RUN_DISCARD_REASON,
)
from backend.engine.state import AgentRuntimeState, WorldState
from backend.engine.state_manager import DeltaChange, StateManager
from backend.execution.command import (
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
from backend.models.schemas import WSMessage

PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]
# 当前活跃 run 的 run_id 读数源（由 SimulationEngine 注入 RunManager.run_id）。
# 注入的是"读数函数"而不是 run_id 值：run 会在 episode 在飞期间被换掉，
# 拿到的必须是**此刻**的活跃 run，不是绑定那一刻的快照。
RunIdSource = Callable[[], str | None]

__all__ = [
    "AgentRuntime",
    "DisabledLLMProvider",
    "SerialEmissionGate",
    "TriggerClassifier",
    "STALE_RUN_DISCARD_EVENT_TYPE",
    "STALE_RUN_DISCARD_REASON",
]


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


class TriggerClassifier:
    """Decide whether a root event should start a new agent episode.

    §15 验收 3「每条根事件都要产生一条可见的 episode」：分类学扩到 §4.1 十四个富根事件
    之后，这里必须同步放宽——否则场景 timeline 声明的 ``user.arrives_home`` 会被静默
    丢弃，事件流里有根事件却没有任何推理链，看起来像 agent 死了。

    唯一保留的"看情况"分支是环境类事件：它们每 tick 都可能来一条，只有带
    ``significant_change_reasons`` 的才值得开一轮推理（阈值事件由生成器盖这个键）。
    """

    def should_start_episode(self, event: SimEvent) -> bool:
        event_type = event.event_type
        if event_type in ENVIRONMENT_ROOT_EVENT_TYPES:
            reasons = event.data.get("significant_change_reasons")
            return isinstance(reasons, list) and len(reasons) > 0
        return event_type in ALL_ROOT_EVENT_TYPES


class AgentRuntime:
    """Manage agent registration, legacy step mode, and event-driven episodes."""

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
    ) -> None:
        self.agents: list[BaseAgent] = []
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
        self.llm_provider = llm_provider or self._build_default_provider()
        self.memory_store = memory_store or AgentMemoryStore()
        self.arbiter = arbiter or Arbiter()
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

    @staticmethod
    def _build_default_provider() -> LLMProvider:
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
    ) -> None:
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.conn = connection_manager
        self.publish_event = publish_event
        if command_executor is not None:
            self.command_executor = command_executor
        if run_id_source is not None:
            self.run_id_source = run_id_source
        # 绑定 = 换了世界（引擎构造 / 重新接线）：上一份观测历史不属于这个世界。
        self.observation_projector.reset()
        self._refresh_subscriptions()

    def update_state_manager(self, state_manager: StateManager) -> None:
        self.state_manager = state_manager
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
        return self.command_executor

    def reset(self) -> None:
        self.memory_store.clear()
        self.observation_projector.reset()

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

    async def step(self, world_state: WorldState) -> list[dict]:
        all_actions: list[dict] = []
        for agent in self.agents:
            actions = agent.decide(world_state)
            if actions:
                for action in actions:
                    action["agent_id"] = agent.agent_id
                    action["agent_name"] = agent.name
                all_actions.extend(actions)
        return all_actions

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
        relevant_agents = [agent for agent in self.agents if agent.is_relevant(observable, event)]
        if not relevant_agents:
            return

        now = time.monotonic()
        agents_to_run: list[BaseAgent] = []
        for agent in relevant_agents:
            # 去抖对**所有**环境类根事件生效（不止旧的 state_refresh）：富分类学把
            # 温度/光照阈值也变成了根事件，不一起去抖就会把 episode 触发频率放大一倍。
            if event.event_type in ENVIRONMENT_ROOT_EVENT_TYPES:
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
            if event.event_type in ENVIRONMENT_ROOT_EVENT_TYPES:
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
                root_event, agents, run_id=run_id, observable=observable, ticket=ticket
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
    ) -> None:
        if self.state_manager is None or self.publish_event is None or self.conn is None:
            return

        # §2.3：agent 拿到的是 observable 投影，不是 ground truth 快照。
        # 调用方（_handle_root_event）已经投影过一次就复用那一份——同一条 episode 里
        # 相关性判定与推理必须基于同一次观测。
        snapshot = observable if observable is not None else self.observable_world()
        agents = self._ordered_agents(agents)

        for agent in agents:
            self._ensure_agent_state(agent)
            self._set_agent_thinking(agent, root_event.correlation_id, root_event.event_type)
        await self._broadcast_agent_status()

        # 同一根事件下的 agent episode 并发执行，避免慢 provider 把整条因果链串行拖死。
        # 这里是契约允许的"并发算"：gather 的**结果顺序**恒等于入参顺序，谁先算完不影响。
        evaluation_tasks = [
            asyncio.create_task(self._evaluate_agent(root_event=root_event, snapshot=snapshot, agent=agent))
            for agent in agents
        ]
        evaluated = await asyncio.gather(*evaluation_tasks)

        # 从这里往下是"发"：整段持号，按取号序（=根事件发布序）串行。
        async with self.emission_gate.turn(ticket):
            await self._emit_episode(
                root_event=root_event, agents=agents, evaluated=list(evaluated), run_id=run_id
            )

    async def _emit_episode(
        self,
        *,
        root_event: SimEvent,
        agents: list[BaseAgent],
        evaluated: list[AgentDecisionEnvelope | None],
        run_id: str | None,
    ) -> None:
        """episode 的**发射**阶段：本方法内发出的每一条事件都在持号期间。

        S3 提醒：新增的推理事件、编排事件一律加在这里；加在 ``_run_episode_body`` 的
        并发段里（gather 之前/之中）就等于绕开闸门，S2 的确定性门会在某次 CI 上莫名变红。
        """

        if self.state_manager is None or self.publish_event is None or self.conn is None:
            return

        envelopes: list[AgentDecisionEnvelope] = []
        causal_heads: dict[str, str] = {}

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

        for agent, envelope in zip(agents, evaluated, strict=False):
            if envelope is None:
                self._set_agent_idle(agent.agent_id)
                continue

            envelopes.append(envelope)
            causal_heads[agent.agent_id] = root_event.event_id
            causal_heads[agent.agent_id] = await self._emit_reasoning_prefix(root_event, envelope, causal_heads[agent.agent_id])

        result = self.arbiter.resolve(envelopes, root_event)
        pending_deltas: list[DeltaChange] = []

        for envelope in envelopes:
            # 推理事件外发同样是 await 点；每个信封落地前复查一次 run 身份，
            # 保证"最后一个 await 之后才写世界"这条边界不被拉长的 episode 撑破。
            if self._is_stale_run(run_id):
                # 本轮已落地的 delta 照常广播：它们是在 run 还有效时写进世界的既成事实，
                # 丢掉它们只会让前端状态与世界永久错位。要丢的是**尚未落地**的产物。
                await self._broadcast_pending_deltas(pending_deltas)
                await self._discard_stale_episode(
                    root_event=root_event,
                    agents=agents,
                    episode_run_id=run_id,
                    stage="before_command_execution",
                    envelopes=envelopes,
                )
                return

            agent_id = envelope.agent_id
            winning_commands = result.winning_commands_by_agent.get(agent_id, [])
            relevant_conflicts = [
                conflict
                for conflict in result.conflicts
                if conflict.get("winner_agent_id") == agent_id or conflict.get("loser_agent_id") == agent_id
            ]
            outcome = "approved" if winning_commands else ("conflicted" if relevant_conflicts else "no_commands")

            coordination_event = await self._emit_agent_event(
                root_event=root_event,
                agent_id=agent_id,
                event_type="reasoning.coordination_decision",
                causal_parent=causal_heads.get(agent_id, root_event.event_id),
                data={
                    "agent_id": agent_id,
                    "outcome": outcome,
                    "priority": envelope.priority,
                    "conflicts": relevant_conflicts,
                    "winning_commands": [command.model_dump() for command in winning_commands],
                },
            )
            causal_heads[agent_id] = coordination_event.event_id
            self._update_reasoning_step(agent_id, coordination_event.event_type)

            execution_event = await self._emit_agent_event(
                root_event=root_event,
                agent_id=agent_id,
                event_type="reasoning.execution_plan",
                causal_parent=coordination_event.event_id,
                data={
                    "agent_id": agent_id,
                    "execution_mode": envelope.mode,
                    "commands": [command.model_dump() for command in winning_commands],
                },
            )
            causal_heads[agent_id] = execution_event.event_id
            self._update_reasoning_step(agent_id, execution_event.event_type)

            # 审计必修①：命令落地全部交给 CommandExecutor——六级校验、十态生命周期、
            # 失败事件、房间光照 effect 都在那一条流水线里，runtime 不再自己 apply_action，
            # 也不再有 `except KeyError: continue` 的静默吞失败。
            command_source = (
                CommandSource.RULE_FALLBACK
                if envelope.mode == "fallback_rule_based"
                else CommandSource.AGENT
            )
            # 唯一 executor 由引擎注入；per-agent 的 publish 包装按次传给 submit，
            # 这样 _pending 注册表跨 agent、跨 episode 只有一份（S1 review finding-8）。
            executor = await self._resolve_executor()
            agent_publish = self._agent_publish_event(agent_id, pending_deltas)

            # 仲裁 loser 不再凭空消失：走 proposed→rejected，带上冲突详情。
            for proposal in envelope.candidate_commands:
                if any(proposal is winner for winner in winning_commands):
                    continue
                await self._reject_losing_command(
                    root_event=root_event,
                    agent_id=agent_id,
                    agent_name=envelope.agent_name,
                    source=command_source,
                    proposal=proposal,
                    causal_parent=coordination_event.event_id,
                    conflicts=relevant_conflicts,
                )

            last_action = envelope.explanation
            for command in winning_commands:
                record = await executor.submit(
                    self._build_device_command(
                        proposal=command,
                        source=command_source,
                        root_event=root_event,
                        causal_parent=execution_event.event_id,
                        actor=agent_id,
                        actor_name=envelope.agent_name,
                    ),
                    tick=int(self.state_manager.world.simulation_tick),
                    publish=agent_publish,
                )
                if record.status is CommandStatus.SUCCEEDED:
                    last_action = command.reason or last_action

            self._set_agent_complete(
                agent_id=agent_id,
                envelope=envelope,
                last_action=last_action,
            )

        await self._broadcast_pending_deltas(pending_deltas)

        for agent in agents:
            if all(envelope.agent_id != agent.agent_id for envelope in envelopes):
                self._set_agent_idle(agent.agent_id)

        await self._broadcast_agent_status()

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
    ) -> AgentDecisionEnvelope | None:
        try:
            if self.episode_timeout_ms is None:
                return await agent.handle_event(
                    root_event=root_event,
                    world_state=snapshot,
                    memory_store=self.memory_store,
                    llm_provider=self.llm_provider,
                )

            return await asyncio.wait_for(
                agent.handle_event(
                    root_event=root_event,
                    world_state=snapshot,
                    memory_store=self.memory_store,
                    llm_provider=self.llm_provider,
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

    async def _emit_reasoning_prefix(
        self,
        root_event: SimEvent,
        envelope: AgentDecisionEnvelope,
        causal_parent: str,
    ) -> str:
        perception_event = await self._emit_agent_event(
            root_event=root_event,
            agent_id=envelope.agent_id,
            event_type="reasoning.perception_snapshot",
            causal_parent=causal_parent,
            data={
                "agent_id": envelope.agent_id,
                "trigger_event_type": envelope.trigger_event_type,
                "world_summary": envelope.world_summary,
                "relevant_devices": envelope.relevant_devices,
                "relevant_rooms": envelope.relevant_rooms,
            },
        )
        self._update_reasoning_step(envelope.agent_id, perception_event.event_type)

        intent_event = await self._emit_agent_event(
            root_event=root_event,
            agent_id=envelope.agent_id,
            event_type="reasoning.intent_recognized",
            causal_parent=perception_event.event_id,
            data={
                "agent_id": envelope.agent_id,
                "intent": envelope.intent,
                "confidence": envelope.confidence,
                "explanation": envelope.explanation,
                "provider": envelope.provider_name,
                "model": envelope.model,
                "latency_ms": envelope.latency_ms,
            },
        )
        self._update_reasoning_step(envelope.agent_id, intent_event.event_type)

        task_event = await self._emit_agent_event(
            root_event=root_event,
            agent_id=envelope.agent_id,
            event_type="reasoning.task_decomposition",
            causal_parent=intent_event.event_id,
            data={
                "agent_id": envelope.agent_id,
                "intent": envelope.intent,
                "task_steps": envelope.task_steps,
            },
        )
        self._update_reasoning_step(envelope.agent_id, task_event.event_type)

        parent_id = task_event.event_id
        if envelope.mode == "fallback_rule_based":
            fallback_event = await self._emit_agent_event(
                root_event=root_event,
                agent_id=envelope.agent_id,
                event_type="reasoning.fallback_rule_based",
                causal_parent=task_event.event_id,
                data={
                    "agent_id": envelope.agent_id,
                    "reason": envelope.fallback_reason,
                    "failed_step": envelope.failed_step or "intent_generation",
                    "fallback_strategy": "rule_based",
                },
            )
            self._update_reasoning_step(envelope.agent_id, fallback_event.event_type)
            parent_id = fallback_event.event_id

        return parent_id

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

    async def _reject_losing_command(
        self,
        *,
        root_event: SimEvent,
        agent_id: str,
        agent_name: str | None = None,
        source: CommandSource,
        proposal: AgentCommandProposal,
        causal_parent: str,
        conflicts: list[dict[str, Any]],
    ) -> CommandRecord:
        """仲裁落败的提案：proposed→rejected，detail 记冲突详情。

        §10.2 十码词表里没有"仲裁落败"这一类（那是执行前的协作结果而非执行失败），
        因此这里只写 detail 不写 failure_code，避免在校验层词表外私自造码。
        """

        conflict = next(
            (
                item
                for item in conflicts
                if item.get("loser_agent_id") == agent_id
                and item.get("device_id") == proposal.device_id
                and item.get("property") == proposal.property
            ),
            None,
        )
        detail = (
            f"仲裁落败：{conflict['winner_agent_id']}（{conflict['winner_priority']}）"
            f"占用 {proposal.device_id}.{proposal.property}"
            if conflict is not None
            else f"仲裁未采纳 {proposal.device_id}.{proposal.property}"
        )
        log.warning(
            "agent_command_rejected",
            agent_id=agent_id,
            device_id=proposal.device_id,
            property=proposal.property,
            reason=detail,
        )

        tick = int(self.state_manager.world.simulation_tick) if self.state_manager else 0
        record = await CommandRecord.propose(
            self._build_device_command(
                proposal=proposal,
                source=source,
                root_event=root_event,
                causal_parent=causal_parent,
                actor=agent_id,
                actor_name=agent_name,
            ),
            self._agent_publish_event(agent_id, []),
            tick=tick,
        )
        await record.transition(CommandStatus.REJECTED, detail=detail, tick=tick)
        return record

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
