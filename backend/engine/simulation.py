"""SimulationEngine — event-driven orchestrator built on top of SimulatorTimer."""

from __future__ import annotations

import time
import uuid
from typing import Any

from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.runtime import AgentRuntime
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent, WorldEvent
from backend.engine.simulator_timer import SimulatorTimer
from backend.engine.state import AgentRuntimeState, Location3D
from backend.engine.state_manager import DeltaChange, StateManager
from backend.models.schemas import WSMessage
from backend.simulators.environment import EnvironmentSimulator
from backend.simulators.user_behavior import UserBehaviorSimulator


class SimulationEngine:
    """Event-driven simulation orchestrator."""

    TICK_INTERVAL = 0.1
    SIMULATED_DT = 60.0

    def __init__(
        self,
        event_bus: EventBus,
        state_manager: StateManager,
        connection_manager: ConnectionManager,
    ) -> None:
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.conn = connection_manager
        self.is_running = False

        self.agent_runtime = AgentRuntime()
        self.agent_runtime.register(LightingAgent())
        self.agent_runtime.register(HVACAgent())
        self.env_sim = EnvironmentSimulator()
        self.user_sim = UserBehaviorSimulator()

        self.timer = SimulatorTimer(
            publish_event=self._publish_sim_event,
            tick_interval=self.TICK_INTERVAL,
            simulated_dt=self.SIMULATED_DT,
        )
        self._pending_deltas: list[DeltaChange] = []
        self._subscriptions_registered = False
        self._subscribe_handlers()

    @property
    def speed(self) -> float:
        return self.timer.speed

    @speed.setter
    def speed(self, value: float) -> None:
        self.timer.speed = float(value)
        self.state_manager.world.simulation_speed = float(value)

    async def start(self) -> None:
        """Start the timer-driven simulation loop."""
        if self.is_running:
            return

        self.is_running = True
        self.state_manager.world.is_running = True
        self.state_manager.world.simulation_speed = self.speed
        await self._publish_sim_event(
            SimEvent(
                event_type="system.simulation_started",
                source="simulation_engine",
                timestamp=float(self.state_manager.world.simulation_tick),
                priority=2,
                data={"simulation_speed": self.speed},
            )
        )
        await self.timer.start()
        log.info("sim_started")

    async def pause(self) -> None:
        """Pause the timer-driven simulation loop."""
        if not self.is_running:
            return

        await self.timer.pause()
        self.is_running = False
        self.state_manager.world.is_running = False
        await self._publish_sim_event(
            SimEvent(
                event_type="system.simulation_paused",
                source="simulation_engine",
                timestamp=float(self.state_manager.world.simulation_tick),
                priority=2,
                data={"simulation_speed": self.speed},
            )
        )
        log.info("sim_paused")

    async def stop(self) -> None:
        await self.pause()

    async def reset(self, new_state_manager: StateManager | None = None) -> None:
        """Pause and reset simulation state, optionally replacing the world."""
        await self.pause()
        if new_state_manager is not None:
            self.state_manager = new_state_manager
        else:
            self.state_manager.world.simulation_tick = 0
            self.state_manager.world.environment.time_of_day = "12:00"
            self.state_manager.world.is_running = False

        self.timer.reset()
        self.user_sim = UserBehaviorSimulator()
        self._pending_deltas = []
        await self._publish_sim_event(
            SimEvent(
                event_type="system.simulation_reset",
                source="simulation_engine",
                timestamp=float(self.state_manager.world.simulation_tick),
                priority=2,
                data={"scene_id": self.state_manager.world.scene_id},
            )
        )
        log.info("sim_reset")

    async def close(self) -> None:
        await self.stop()
        self._unsubscribe_handlers()

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

        timer_tick = int(event.data["tick"])
        simulated_dt = float(event.data["simulated_dt"])
        time_of_day = self._next_time_of_day(world.environment.time_of_day, simulated_dt)

        self._pending_deltas.extend(
            self.state_manager.apply_updates(
                caused_by="simulator_timer",
                updates=[
                    ("simulation_tick", timer_tick),
                    ("simulation_speed", float(event.data["simulation_speed"])),
                    ("is_running", True),
                    ("environment.time_of_day", time_of_day),
                ],
                reason="timer tick",
            )
        )

        root_event = event
        user_events = self.user_sim.step(world)
        published_user_events: list[SimEvent] = []
        for user_event in user_events:
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
                    "outdoor_temp": world.environment.outdoor_temp,
                },
            )
        )

        actions = await self.agent_runtime.step(world)
        for action in actions:
            action_event = self._build_action_event(
                world_tick=world.simulation_tick,
                action=action,
                root_event=root_event,
            )
            published_action = await self._publish_sim_event(action_event)
            try:
                deltas = self.state_manager.apply_action(
                    agent_id=action["agent_id"],
                    device_id=action["device_id"],
                    property_path=action["property"],
                    new_value=action["value"],
                    reason=action.get("reason", ""),
                )
            except KeyError:
                log.warning(
                    "agent_action_failed",
                    device_id=action.get("device_id"),
                    agent_id=action.get("agent_id"),
                )
                continue

            self._pending_deltas.extend(deltas)
            for delta in deltas:
                await self._publish_sim_event(
                    SimEvent(
                        event_type="feedback.state_delta",
                        source="state_manager",
                        timestamp=float(world.simulation_tick),
                        wall_time=time.time(),
                        correlation_id=published_action.correlation_id,
                        causal_parent=published_action.event_id,
                        priority=1,
                        data=delta.model_dump(),
                    )
                )

        self._sync_agent_states(world, actions)
        await self._flush_pending_deltas()
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
            updates.extend([
                (f"rooms[{old_room}].persons", remaining),
                (f"rooms[{old_room}].occupancy", bool(remaining)),
            ])

        if target_room in world.rooms:
            next_persons = [*world.rooms[target_room].persons]
            if user_id not in next_persons:
                next_persons.append(user_id)
            updates.extend([
                (f"rooms[{target_room}].persons", next_persons),
                (f"rooms[{target_room}].occupancy", True),
            ])

        self._pending_deltas.extend(
            self.state_manager.apply_updates(
                caused_by=event.source,
                updates=updates,
                reason="apply user activity change",
            )
        )

    async def _handle_environment_refresh(self, event: SimEvent) -> None:
        updates = self.env_sim.step(
            self.state_manager.world,
            dt=float(event.data["simulated_dt"]),
        )
        self._pending_deltas.extend(
            self.state_manager.apply_updates(
                caused_by=event.source,
                updates=list(updates.items()),
                reason="apply environment refresh",
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

    async def _broadcast_agent_status(self, world: "WorldState") -> None:  # noqa: F821
        await self.conn.broadcast(
            WSMessage(
                type="AGENT_STATUS",
                payload={"agents": {agent_id: agent.model_dump() for agent_id, agent in world.agents.items()}},
            )
        )

    def _next_time_of_day(self, time_of_day: str, simulated_dt: float | None = None) -> str:
        simulated_dt = self.SIMULATED_DT if simulated_dt is None else simulated_dt
        hours, minutes = time_of_day.split(":")
        total_minutes = int(hours) * 60 + int(minutes) + simulated_dt / 60.0
        total_minutes %= 24 * 60
        return f"{int(total_minutes // 60):02d}:{int(total_minutes % 60):02d}"

    def _build_action_event(
        self,
        world_tick: int,
        action: dict,
        root_event: SimEvent | None,
    ) -> SimEvent:
        correlation_id = root_event.correlation_id if root_event else None
        causal_parent = root_event.event_id if root_event else None
        return SimEvent(
            event_type="action.device_control",
            source=action["agent_id"],
            timestamp=float(world_tick),
            wall_time=time.time(),
            correlation_id=correlation_id or uuid.uuid4().hex,
            causal_parent=causal_parent,
            priority=2,
            data={
                "agent_name": action.get("agent_name", ""),
                "device_id": action["device_id"],
                "property": action["property"],
                "value": action["value"],
                "reason": action.get("reason", ""),
            },
        )

    async def _publish_sim_event(self, event: WorldEvent | SimEvent) -> SimEvent:
        sim_event = self.event_bus.coerce_event(event)
        # 先对外广播根事件，再让订阅器派生子事件，这样前端看到的因果顺序才稳定。
        await self.conn.broadcast(WSMessage(type="SIM_EVENT", payload=sim_event.model_dump()))
        await self.event_bus.publish(sim_event)
        return sim_event

    def _sync_agent_states(self, world: "WorldState", actions: list[dict]) -> None:  # noqa: F821
        """Update AgentRuntimeState entries in the world based on latest actions."""
        agent_action_map: dict[str, str] = {}
        for action in actions:
            aid = action.get("agent_id", "")
            agent_action_map[aid] = action.get("reason", "")

        for agent in self.agent_runtime.agents:
            entry = world.agents.get(agent.agent_id)
            if entry is None:
                entry = AgentRuntimeState(id=agent.agent_id, name=agent.name)
                world.agents[agent.agent_id] = entry

            if agent.agent_id in agent_action_map:
                entry.status = "active"
                entry.last_action = agent_action_map[agent.agent_id]
                entry.current_strategy = "auto"
                entry.confidence = 0.8
            else:
                entry.status = "idle"
