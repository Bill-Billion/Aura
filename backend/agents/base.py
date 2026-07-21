"""Base agent abstract class for smart home automation agents."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

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
)
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES, ENVIRONMENT_ROOT_EVENT_TYPES
from backend.engine.state import DeviceState, WorldState


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

    def __init__(self, agent_id: str, name: str) -> None:
        self.agent_id = agent_id
        self.name = name

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

    async def handle_event(
        self,
        *,
        root_event: SimEvent,
        world_state: WorldState,
        memory_store: AgentMemoryStore,
        llm_provider: LLMProvider,
    ) -> AgentDecisionEnvelope | None:
        if not self.is_relevant(world_state, root_event):
            return None

        relevant_devices = self.get_relevant_devices(world_state, root_event)
        relevant_rooms = self.get_relevant_rooms(world_state, root_event)
        world_summary = self.build_world_summary(world_state, root_event)
        recent_events = memory_store.build_recent_event_lines(
            self.agent_id,
            root_event.correlation_id,
        )
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
            return AgentDecisionEnvelope(
                agent_id=self.agent_id,
                agent_name=self.name,
                mode="llm",
                trigger_event_type=root_event.event_type,
                intent=llm_decision.intent,
                confidence=llm_decision.confidence,
                explanation=llm_decision.explanation,
                needs_coordination=llm_decision.needs_coordination,
                priority=self.determine_priority(world_state, root_event),
                task_steps=llm_decision.task_steps,
                candidate_commands=self._filter_commands(
                    llm_decision,
                    allowed_commands,
                ),
                root_event_id=root_event.event_id,
                root_event_timestamp=root_event.timestamp,
                provider_name=getattr(llm_provider, "provider_name", "llm"),
                model=getattr(llm_provider, "model", ""),
                latency_ms=latency_ms,
                world_summary=world_summary,
                relevant_devices=[device.id for device in relevant_devices],
                relevant_rooms=relevant_rooms,
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
        fallback_actions = self.decide(world_state)
        return AgentDecisionEnvelope(
            agent_id=self.agent_id,
            agent_name=self.name,
            mode="fallback_rule_based",
            trigger_event_type=root_event.event_type,
            intent=f"{self.name} fallback decision",
            confidence=0.55,
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
