"""Agent runtime for legacy step mode and Phase 2 event-driven episodes."""

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
from backend.config.device_registry import validate_device_command
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.state import AgentRuntimeState, WorldState
from backend.engine.state_manager import DeltaChange, StateManager
from backend.models.schemas import WSMessage
from backend.simulators.environment import calculate_room_light_level

PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]


class DisabledLLMProvider(LLMProvider):
    provider_name = "disabled"
    model = "rule_based"

    async def generate_decision(self, request):  # type: ignore[override]
        raise LLMProviderError("provider_error", "LLM provider is disabled")


class TriggerClassifier:
    """Decide whether a root event should start a new agent episode."""

    def should_start_episode(self, event: SimEvent) -> bool:
        if event.event_type == "user.command":
            return True
        if event.event_type == "user.activity_change":
            return True
        if event.event_type == "environment.state_refresh":
            reasons = event.data.get("significant_change_reasons")
            return isinstance(reasons, list) and len(reasons) > 0
        return False


def _device_command_affects_room_light(device_type: str, property_path: str) -> bool:
    normalized_property = property_path.removeprefix("extra.")
    if device_type == "light":
        return normalized_property in {"power", "brightness"}
    if device_type == "curtain":
        return normalized_property == "open_percent"
    return False


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
    ) -> None:
        self.agents: list[BaseAgent] = []
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.conn = connection_manager
        self.publish_event = publish_event
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
    ) -> None:
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.conn = connection_manager
        self.publish_event = publish_event
        self._refresh_subscriptions()

    def update_state_manager(self, state_manager: StateManager) -> None:
        self.state_manager = state_manager

    def reset(self) -> None:
        self.memory_store.clear()

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
                        },
                    )
                )
            for agent_id in episode["agent_ids"]:
                self._set_agent_idle(agent_id)
        await self._broadcast_agent_status()

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

        relevant_agents = [
            agent for agent in self.agents if agent.is_relevant(self.state_manager.world, event)
        ]
        if not relevant_agents:
            return

        now = time.monotonic()
        agents_to_run: list[BaseAgent] = []
        for agent in relevant_agents:
            if event.event_type == "environment.state_refresh":
                existing = self._active_tasks.get(agent.agent_id)
                if existing is not None and not existing.done():
                    continue
                last_started_at = self._last_environment_episode_started_at.get(agent.agent_id, 0.0)
                if (now - last_started_at) * 1000 < self.environment_debounce_ms:
                    continue
            agents_to_run.append(agent)

        if not agents_to_run:
            return

        for agent in agents_to_run:
            existing = self._active_tasks.get(agent.agent_id)
            if existing is not None and not existing.done():
                existing.cancel()

        task = asyncio.create_task(self._run_episode(event, agents_to_run))
        self._background_tasks.add(task)
        self._episode_meta[task] = {
            "correlation_id": event.correlation_id,
            "root_event_id": event.event_id,
            "agent_ids": [agent.agent_id for agent in agents_to_run],
        }
        for agent in agents_to_run:
            self._active_tasks[agent.agent_id] = task
            if event.event_type == "environment.state_refresh":
                self._last_environment_episode_started_at[agent.agent_id] = now

        def _cleanup(done_task: asyncio.Task[None], agent_ids: list[str]) -> None:
            self._background_tasks.discard(done_task)
            self._episode_meta.pop(done_task, None)
            for agent_id in agent_ids:
                if self._active_tasks.get(agent_id) is done_task:
                    self._active_tasks.pop(agent_id, None)

        task.add_done_callback(lambda done_task, agent_ids=[agent.agent_id for agent in agents_to_run]: _cleanup(done_task, agent_ids))

    async def _run_episode(self, root_event: SimEvent, agents: list[BaseAgent]) -> None:
        if self.state_manager is None or self.publish_event is None or self.conn is None:
            return

        world = self.state_manager.world
        snapshot = world.snapshot()
        envelopes: list[AgentDecisionEnvelope] = []
        causal_heads: dict[str, str] = {}

        for agent in agents:
            self._ensure_agent_state(agent)
            self._set_agent_thinking(agent, root_event.correlation_id, root_event.event_type)
        await self._broadcast_agent_status()

        # 同一根事件下的 agent episode 并发执行，避免慢 provider 把整条因果链串行拖死。
        evaluation_tasks = [
            asyncio.create_task(self._evaluate_agent(root_event=root_event, snapshot=snapshot, agent=agent))
            for agent in agents
        ]
        evaluated = await asyncio.gather(*evaluation_tasks)

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

            last_action = envelope.explanation
            for command in winning_commands:
                # 审计必修①：agent 路径和 UI 路径共用 validate_device_command，
                # 校验必须发生在 action.device_control 之前（spec §10 步骤 5 先于步骤 6）。
                device = self.state_manager.world.devices.get(command.device_id)
                if device is None:
                    error_code: str | None = "UNKNOWN_DEVICE"
                    error_message = f"设备 {command.device_id} 不存在"
                else:
                    error_code, error_message = validate_device_command(
                        device,
                        action="",
                        property_path=command.property,
                    )
                if error_code:
                    await self._emit_command_failed(
                        root_event=root_event,
                        agent_id=agent_id,
                        causal_parent=execution_event.event_id,
                        command=command,
                        error_code=error_code,
                        reason=error_message,
                    )
                    continue

                action_event = await self.publish_event(
                    SimEvent(
                        event_type="action.device_control",
                        source=agent_id,
                        timestamp=float(self.state_manager.world.simulation_tick),
                        wall_time=time.time(),
                        correlation_id=root_event.correlation_id,
                        causal_parent=execution_event.event_id,
                        priority=2,
                        data={
                            "agent_name": envelope.agent_name,
                            "device_id": command.device_id,
                            "property": command.property,
                            "value": command.value,
                            "reason": command.reason,
                        },
                    )
                )
                self.memory_store.remember(action_event, agent_id=agent_id)
                try:
                    deltas = self.state_manager.apply_action(
                        agent_id=agent_id,
                        device_id=command.device_id,
                        property_path=command.property,
                        new_value=command.value,
                        reason=command.reason,
                    )
                except KeyError as exc:
                    # 校验通过后设备仍可能在 apply 前消失（如并发 reset）；
                    # 不再静默吞掉，转成同一结构化失败事件。
                    await self._emit_command_failed(
                        root_event=root_event,
                        agent_id=agent_id,
                        causal_parent=action_event.event_id,
                        command=command,
                        error_code="UNKNOWN_DEVICE",
                        reason=str(exc),
                    )
                    continue
                device = self.state_manager.world.devices.get(command.device_id)
                if device is not None and _device_command_affects_room_light(device.type, command.property):
                    room_id = device.location.room
                    if room_id in self.state_manager.world.rooms:
                        deltas.extend(
                            self.state_manager.apply_path_update(
                                caused_by=agent_id,
                                path=f"rooms[{room_id}].light_level",
                                new_value=calculate_room_light_level(self.state_manager.world, room_id),
                                reason="apply device light feedback",
                            )
                        )

                pending_deltas.extend(deltas)
                last_action = command.reason or last_action
                for delta in deltas:
                    feedback_event = await self.publish_event(
                        SimEvent(
                            event_type="feedback.state_delta",
                            source="state_manager",
                            timestamp=float(self.state_manager.world.simulation_tick),
                            wall_time=time.time(),
                            correlation_id=root_event.correlation_id,
                            causal_parent=action_event.event_id,
                            priority=1,
                            data=delta.model_dump(),
                        )
                    )
                    self.memory_store.remember(feedback_event, agent_id=agent_id)

            self._set_agent_complete(
                agent_id=agent_id,
                envelope=envelope,
                last_action=last_action,
            )

        if pending_deltas:
            await self.conn.broadcast(
                WSMessage(
                    type="STATE_DELTA",
                    payload={"deltas": [delta.model_dump() for delta in pending_deltas]},
                )
            )

        for agent in agents:
            if all(envelope.agent_id != agent.agent_id for envelope in envelopes):
                self._set_agent_idle(agent.agent_id)

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

    async def _emit_command_failed(
        self,
        *,
        root_event: SimEvent,
        agent_id: str,
        causal_parent: str,
        command: AgentCommandProposal,
        error_code: str,
        reason: str,
    ) -> SimEvent:
        log.warning(
            "agent_command_rejected",
            agent_id=agent_id,
            device_id=command.device_id,
            property=command.property,
            error_code=error_code,
            reason=reason,
        )
        return await self._emit_agent_event(
            root_event=root_event,
            agent_id=agent_id,
            event_type="device.command_failed",
            causal_parent=causal_parent,
            data={
                "agent_id": agent_id,
                "device_id": command.device_id,
                "property": command.property,
                "value": command.value,
                "error_code": error_code,
                "reason": reason,
            },
        )

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
