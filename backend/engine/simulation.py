"""SimulationEngine — event-driven orchestrator built on top of SimulatorTimer."""

from __future__ import annotations

import time
from typing import Any

from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.llm import LLMProvider
from backend.agents.runtime import AgentRuntime
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent, WorldEvent
from backend.engine.simulator_timer import SimulatorTimer
from backend.engine.state import Location3D, WorldState
from backend.engine.state_manager import (
    INVARIANT_VIOLATION_EVENT_TYPE,
    DeltaChange,
    StateManager,
    find_world_invariant_violation,
)
from backend.execution.executor import CommandExecutor, InvariantReportDebounce
from backend.models.schemas import WSMessage
from backend.simulators.environment import EnvironmentSimulator
from backend.simulators.user_behavior import UserBehaviorSimulator


class SimulationEngine:
    """Event-driven simulation orchestrator."""

    TICK_INTERVAL = 2.0
    DEFAULT_MODE = "observe"

    def __init__(
        self,
        event_bus: EventBus,
        state_manager: StateManager,
        connection_manager: ConnectionManager,
        llm_provider: LLMProvider | None = None,
        agent_episode_timeout_ms: int | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.conn = connection_manager
        self.is_running = False

        self.env_sim = EnvironmentSimulator()
        self.user_sim = UserBehaviorSimulator()
        self.timer = SimulatorTimer(
            publish_event=self._publish_sim_event,
            tick_interval=self.TICK_INTERVAL,
            default_mode=self.DEFAULT_MODE,
        )

        self._pending_deltas: list[DeltaChange] = []
        # 已上报且尚未修复的不变式违规去抖：同一条违规每 tick 重报会淹没事件流，
        # 世界恢复一致（或换成另一条违规）后自动复位重报。
        # 这一份实例同时注入 CommandExecutor：仿真侧与命令侧共用一份签名，一次损坏只报一次
        # （否则命令侧还会每条命令补一条 pre_existing 事件，review2 finding-3）。
        self._invariant_report = InvariantReportDebounce()
        self._subscriptions_registered = False
        self._is_processing_timer_tick = False
        self._time_of_day_seconds = self._parse_time_of_day_to_seconds(
            self.state_manager.world.environment.time_of_day
        )

        # 全系统唯一一台 CommandExecutor（S1 review finding-8）：UI 腿（main.py）与
        # agent 腿（runtime.py）共用它，各自把 publish 包装按次传入。用完即弃的 executor
        # 会让 _pending 注册表没有生产寿命——cancel_pending 无事可取消、跨调用取代不成立。
        # 不在此处绑 publish：引擎自身不发命令，包装归各条腿所有。
        self.command_executor = CommandExecutor(
            self.state_manager, invariant_debounce=self._invariant_report
        )

        self.agent_runtime = AgentRuntime(
            llm_provider=llm_provider,
            episode_timeout_ms=agent_episode_timeout_ms,
            command_executor=self.command_executor,
        )
        self.agent_runtime.register(LightingAgent())
        self.agent_runtime.register(HVACAgent())

        self._subscribe_handlers()
        self.agent_runtime.bind(
            event_bus=self.event_bus,
            state_manager=self.state_manager,
            connection_manager=self.conn,
            publish_event=self._publish_sim_event,
            command_executor=self.command_executor,
        )
        self._sync_world_timing_state(reset_mode=True)
        self._sync_agent_diagnostics()

    @property
    def speed(self) -> float:
        return self.timer.speed

    @speed.setter
    def speed(self, value: float) -> None:
        self.apply_legacy_speed(float(value))

    @property
    def mode(self) -> str:
        return self.timer.mode

    @mode.setter
    def mode(self, value: str) -> None:
        self.timer.set_mode(value)
        self._sync_world_timing_state()

    @property
    def wall_tick_ms(self) -> int:
        return int(self.timer.tick_interval * 1000)

    @property
    def simulated_dt_seconds(self) -> float:
        return self.timer.simulated_dt

    def apply_legacy_speed(self, value: float) -> None:
        self.timer.apply_legacy_speed(value)
        self._sync_world_timing_state()

    async def start(self) -> None:
        if self.is_running:
            return

        self.is_running = True
        self.state_manager.world.is_running = True
        self._sync_world_timing_state()
        self._sync_agent_diagnostics()
        await self._publish_sim_event(
            SimEvent(
                event_type="system.simulation_started",
                source="simulation_engine",
                timestamp=float(self.state_manager.world.simulation_tick),
                priority=2,
                data=self.build_simulation_status_payload(),
            )
        )
        await self.timer.start()
        log.info("sim_started")

    async def pause(self) -> None:
        if not self.is_running:
            return

        await self.timer.pause()
        self.is_running = False
        self.state_manager.world.is_running = False
        self._sync_world_timing_state()
        await self._publish_sim_event(
            SimEvent(
                event_type="system.simulation_paused",
                source="simulation_engine",
                timestamp=float(self.state_manager.world.simulation_tick),
                priority=2,
                data=self.build_simulation_status_payload(),
            )
        )
        log.info("sim_paused")

    async def stop(self) -> None:
        await self.pause()

    async def reset(self, new_state_manager: StateManager | None = None) -> None:
        await self.pause()
        # 审计必修②：cancel-before-swap——先取消并落账在飞 episode，
        # 再替换世界，否则旧任务可能在 swap 之后、cancel 之前写入新世界。
        await self.agent_runtime.cancel_active_episodes()
        # 同一条 cancel-before-swap 纪律延伸到命令层：episode 被砍后仍挂在注册表里的
        # 在飞命令必须先落账成 cancelled（§15「reset 取消在飞命令」），再换世界，
        # 否则它们的终态迁移会记在重置后的新世界头上。
        await self.command_executor.cancel_pending("simulation_reset")
        if new_state_manager is not None:
            self.state_manager = new_state_manager
        else:
            self.state_manager.world.simulation_tick = 0
            self.state_manager.world.environment.time_of_day = "12:00"
            self.state_manager.world.is_running = False

        self.timer.reset()
        self.timer.set_mode(self.DEFAULT_MODE)
        self.user_sim = UserBehaviorSimulator()
        self._pending_deltas = []
        # 换了世界（或清了世界）之后旧违规不再成立，去抖状态必须跟着复位。
        self._invariant_report.reset()
        self._time_of_day_seconds = self._parse_time_of_day_to_seconds(
            self.state_manager.world.environment.time_of_day
        )
        # executor 换绑新世界并清空注册表（cancel 已落账，这里是防残留的兜底）。
        self.command_executor.bind_state_manager(self.state_manager)
        self.agent_runtime.update_state_manager(self.state_manager)
        self.agent_runtime.reset()
        self._sync_world_timing_state(reset_mode=True)
        self._sync_agent_diagnostics()
        await self._publish_sim_event(
            SimEvent(
                event_type="system.simulation_reset",
                source="simulation_engine",
                timestamp=float(self.state_manager.world.simulation_tick),
                priority=2,
                data={
                    "scene_id": self.state_manager.world.scene_id,
                    **self.build_simulation_status_payload(),
                },
            )
        )
        log.info("sim_reset")

    async def close(self) -> None:
        await self.stop()
        self._unsubscribe_handlers()
        await self.agent_runtime.close()

    def _subscribe_handlers(self) -> None:
        if self._subscriptions_registered:
            return

        self.event_bus.subscribe("system.timer_tick", self._handle_timer_tick)
        self.event_bus.subscribe("user.activity_change", self._handle_user_activity_change)
        self.event_bus.subscribe("environment.state_refresh", self._handle_environment_refresh)
        self._subscriptions_registered = True

    def _unsubscribe_handlers(self) -> None:
        if not self._subscriptions_registered:
            return

        self.event_bus.unsubscribe("system.timer_tick", self._handle_timer_tick)
        self.event_bus.unsubscribe("user.activity_change", self._handle_user_activity_change)
        self.event_bus.unsubscribe("environment.state_refresh", self._handle_environment_refresh)
        self._subscriptions_registered = False

    async def _handle_timer_tick(self, event: SimEvent) -> None:
        world = self.state_manager.world
        self._pending_deltas = []
        self._is_processing_timer_tick = True

        timer_tick = int(event.data["tick"])
        simulated_dt = float(event.data["simulated_dt"])
        previous_time_of_day = world.environment.time_of_day
        previous_weather = world.environment.weather
        self._time_of_day_seconds = (
            self._time_of_day_seconds + simulated_dt
        ) % (24 * 60 * 60)
        time_of_day = self._format_time_of_day(self._time_of_day_seconds)

        self._pending_deltas.extend(
            self.state_manager.apply_updates(
                caused_by="simulator_timer",
                updates=[
                    ("simulation_tick", timer_tick),
                    ("simulation_speed", float(event.data["simulation_speed"])),
                    ("simulation_mode", str(event.data["mode"])),
                    ("wall_tick_ms", int(event.data["wall_tick_ms"])),
                    ("simulated_dt_seconds", simulated_dt),
                    ("is_running", True),
                    ("environment.time_of_day", time_of_day),
                ],
                reason="timer tick",
            )
        )

        root_event = event
        published_user_events: list[SimEvent] = []
        for user_event in self.user_sim.step(world):
            published = await self._publish_sim_event(
                SimEvent.from_world_event(
                    user_event,
                    timestamp=float(world.simulation_tick),
                    wall_time=time.time(),
                    priority=2,
                )
            )
            published_user_events.append(published)

        if published_user_events:
            root_event = published_user_events[-1]

        env_updates = self.env_sim.step(world, dt=simulated_dt)
        significant_change_reasons = self._collect_environment_change_reasons(
            world=world,
            updates=env_updates,
            previous_time_of_day=previous_time_of_day,
            previous_weather=previous_weather,
            next_time_of_day=time_of_day,
        )

        await self._publish_sim_event(
            SimEvent(
                event_type="environment.state_refresh",
                source="environment_sim",
                timestamp=float(world.simulation_tick),
                wall_time=time.time(),
                correlation_id=root_event.correlation_id,
                causal_parent=root_event.event_id,
                priority=1,
                data={
                    "simulated_dt": simulated_dt,
                    "time_of_day": world.environment.time_of_day,
                    "outdoor_temp": env_updates.get("environment.outdoor_temp", world.environment.outdoor_temp),
                    "outdoor_humidity": env_updates.get("environment.outdoor_humidity", world.environment.outdoor_humidity),
                    "weather": env_updates.get("environment.weather", world.environment.weather),
                    "significant_change_reasons": significant_change_reasons,
                    "updates": env_updates,
                },
            )
        )

        self._is_processing_timer_tick = False
        await self._flush_pending_deltas()
        # tick 收尾探测：本 tick 内的仿真写入（timer/user/env）是否把世界写成不一致。
        await self._report_world_invariants(
            phase="tick_end", attributed_to="simulator_timer", root_event=event
        )
        await self._broadcast_agent_status(world)

    async def _handle_user_activity_change(self, event: SimEvent) -> None:
        world = self.state_manager.world
        user_id = str(event.data["user_id"])
        target_room = str(event.data["to_room"])
        activity = str(event.data["activity"])
        old_room = str(event.data.get("from_room") or "")

        user = world.users.get(user_id)
        if user is None:
            return

        updates: list[tuple[str, Any]] = [
            (f"users[{user_id}].location", Location3D(room=target_room)),
            (f"users[{user_id}].activity", activity),
        ]

        if old_room and old_room in world.rooms:
            remaining = [person for person in world.rooms[old_room].persons if person != user_id]
            updates.extend(
                [
                    (f"rooms[{old_room}].persons", remaining),
                    (f"rooms[{old_room}].occupancy", bool(remaining)),
                ]
            )

        if target_room in world.rooms:
            next_persons = [*world.rooms[target_room].persons]
            if user_id not in next_persons:
                next_persons.append(user_id)
            updates.extend(
                [
                    (f"rooms[{target_room}].persons", next_persons),
                    (f"rooms[{target_room}].occupancy", True),
                ]
            )

        self._pending_deltas.extend(
            self.state_manager.apply_updates(
                caused_by=event.source,
                updates=updates,
                reason="apply user activity change",
            )
        )

        if not self._is_processing_timer_tick:
            await self._flush_pending_deltas()
            # tick 内的写入由 tick 收尾统一探测（中途状态可以是半成品）；tick 外的写入自查。
            await self._report_world_invariants(
                phase="user_activity_change",
                attributed_to=event.source,
                root_event=event,
            )

    async def _handle_environment_refresh(self, event: SimEvent) -> None:
        updates = event.data.get("updates") or self.env_sim.step(
            self.state_manager.world,
            dt=float(event.data["simulated_dt"]),
        )
        self._pending_deltas.extend(
            self.state_manager.apply_updates(
                caused_by=event.source,
                updates=list(dict(updates).items()),
                reason="apply environment refresh",
            )
        )

        if not self._is_processing_timer_tick:
            await self._flush_pending_deltas()
            await self._report_world_invariants(
                phase="environment_refresh",
                attributed_to=event.source,
                root_event=event,
            )

    async def _report_world_invariants(
        self,
        *,
        phase: str,
        attributed_to: str,
        root_event: SimEvent | None = None,
    ) -> None:
        """§2.2 仿真写入后的不变式探测：检测 + 归因，绝不回滚、绝不静默纠正。

        为什么不像命令路径那样回滚：仿真产出的是世界 ground truth，回滚它会撕裂物理演化；
        为什么必须在这里探：不然仿真写坏的世界要等到下一条无辜命令执行时才被发现，责任被
        错记在那条命令上，且世界永不修复 → 此后每条命令连锁失败（审计 finding 3）。
        """

        violation = find_world_invariant_violation(self.state_manager.world)
        # 去抖对象与 CommandExecutor 共用：仿真侧报过的那条违规，命令侧不会再补一遍。
        # 同 executor：should_report 需在 violation 为 None 时也被调用以复位签名，
        # 且该情形自身返回 False，故无需再补 or-is-None。
        if not self._invariant_report.should_report(violation):
            return

        world = self.state_manager.world
        await self._publish_sim_event(
            SimEvent(
                event_type=INVARIANT_VIOLATION_EVENT_TYPE,
                # source=检测方（状态层），data.attributed_to=写坏世界的仿真源。
                source="state_manager",
                timestamp=float(world.simulation_tick),
                wall_time=time.time(),
                correlation_id=root_event.correlation_id if root_event else None,
                causal_parent=root_event.event_id if root_event else None,
                priority=3,
                data={
                    "invariant": violation.invariant,
                    "message": violation.message,
                    "details": violation.details,
                    "phase": phase,
                    "attributed_to": attributed_to,
                    "source": attributed_to,
                    # 仿真侧违规按定义不是「某条命令造成的」，字段保持与命令路径同形。
                    "pre_existing": False,
                    "command_id": None,
                    "device_id": None,
                    "capability": None,
                    "command_failed": False,
                    "reverted_paths": [],
                },
            )
        )

    async def _flush_pending_deltas(self) -> None:
        if not self._pending_deltas:
            return

        await self.conn.broadcast(
            WSMessage(
                type="STATE_DELTA",
                payload={"deltas": [delta.model_dump() for delta in self._pending_deltas]},
            )
        )
        self._pending_deltas = []

    async def _broadcast_agent_status(self, world: WorldState) -> None:
        self._sync_agent_diagnostics()
        await self.conn.broadcast(
            WSMessage(
                type="AGENT_STATUS",
                payload={"agents": {agent_id: agent.model_dump() for agent_id, agent in world.agents.items()}},
            )
        )

    def _next_time_of_day(self, time_of_day: str, simulated_dt: float | None = None) -> str:
        simulated_dt = self.simulated_dt_seconds if simulated_dt is None else simulated_dt
        total_seconds = (
            self._parse_time_of_day_to_seconds(time_of_day) + simulated_dt
        ) % (24 * 60 * 60)
        return self._format_time_of_day(total_seconds)

    async def _publish_sim_event(self, event: WorldEvent | SimEvent) -> SimEvent:
        sim_event = self.event_bus.coerce_event(event)
        await self.conn.broadcast(WSMessage(type="SIM_EVENT", payload=sim_event.model_dump()))
        await self.event_bus.publish(sim_event)
        return sim_event

    def _collect_environment_change_reasons(
        self,
        *,
        world: WorldState,
        updates: dict[str, float],
        previous_time_of_day: str,
        previous_weather: str,
        next_time_of_day: str,
    ) -> list[str]:
        reasons: list[str] = []
        if self._time_bucket(previous_time_of_day) != self._time_bucket(next_time_of_day):
            reasons.append("time_bucket")
        next_weather = str(updates.get("environment.weather", previous_weather))
        if previous_weather != next_weather:
            reasons.append("weather")

        for path, next_value in updates.items():
            if not path.endswith(".temperature"):
                continue
            try:
                current_value = float(StateManager._get_nested(world, path))
            except Exception:
                continue
            if abs(float(next_value) - current_value) >= 1.0:
                reasons.append("room_temperature_delta")
                break

        return reasons

    @staticmethod
    def _time_bucket(time_of_day: str) -> str:
        hour = int(time_of_day.split(":")[0])
        if 6 <= hour < 12:
            return "morning"
        if 12 <= hour < 18:
            return "day"
        if 18 <= hour < 23:
            return "evening"
        return "night"

    def build_simulation_status_payload(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "speed": self.speed,
            "mode": self.mode,
            "wall_tick_ms": self.wall_tick_ms,
            "simulated_dt_seconds": self.simulated_dt_seconds,
        }

    def _sync_world_timing_state(self, *, reset_mode: bool = False) -> None:
        world = self.state_manager.world
        if reset_mode:
            world.simulation_mode = self.mode  # type: ignore[assignment]
        world.simulation_speed = float(self.speed)
        world.simulation_mode = self.mode  # type: ignore[assignment]
        world.wall_tick_ms = self.wall_tick_ms
        world.simulated_dt_seconds = self.simulated_dt_seconds

    def _sync_agent_diagnostics(self) -> None:
        provider_name = getattr(self.agent_runtime.llm_provider, "provider_name", "disabled")
        configured = self.agent_runtime.is_provider_configured
        for agent in self.state_manager.world.agents.values():
            agent.provider = provider_name
            agent.provider_configured = configured

    @staticmethod
    def _parse_time_of_day_to_seconds(time_of_day: str) -> float:
        hours, minutes = time_of_day.split(":")
        return int(hours) * 3600 + int(minutes) * 60

    @staticmethod
    def _format_time_of_day(total_seconds: float) -> str:
        total_minutes = int(total_seconds // 60) % (24 * 60)
        return f"{int(total_minutes // 60):02d}:{int(total_minutes % 60):02d}"
