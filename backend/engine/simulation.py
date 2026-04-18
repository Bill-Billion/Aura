"""SimulationEngine — main simulation loop that ties all subsystems together."""

from __future__ import annotations

import asyncio
import time
import uuid

from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.runtime import AgentRuntime
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent, WorldEvent
from backend.engine.state import AgentRuntimeState
from backend.engine.state_manager import StateManager
from backend.models.schemas import WSMessage
from backend.simulators.environment import EnvironmentSimulator
from backend.simulators.user_behavior import UserBehaviorSimulator


class SimulationEngine:
    """Main simulation loop.

    Each tick advances simulated time by ``SIMULATED_DT`` seconds while
    maintaining a real-time cadence governed by ``TICK_INTERVAL``.
    """

    TICK_INTERVAL = 0.1  # 100 ms wall-clock per tick
    SIMULATED_DT = 60.0  # each tick = 60 simulated seconds

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
        self.speed = 1.0
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Subsystems
        self.agent_runtime = AgentRuntime()
        self.agent_runtime.register(LightingAgent())
        self.agent_runtime.register(HVACAgent())
        self.env_sim = EnvironmentSimulator()
        self.user_sim = UserBehaviorSimulator()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the simulation loop."""
        if self.is_running:
            return
        self.is_running = True
        self.state_manager.world.is_running = True
        self._task = asyncio.create_task(self._main_loop())
        log.info("sim_started")

    async def pause(self) -> None:
        """Pause the simulation loop."""
        if not self.is_running:
            return
        self.is_running = False
        self.state_manager.world.is_running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("sim_paused")

    async def stop(self) -> None:
        """Stop the simulation (same as pause)."""
        await self.pause()

    async def reset(self) -> None:
        """Pause and reset simulation tick to 0."""
        await self.pause()
        self.state_manager.world.simulation_tick = 0
        self.state_manager.world.environment.time_of_day = "12:00"
        log.info("sim_reset")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _main_loop(self) -> None:
        """Core simulation loop executed once per tick."""
        try:
            while self.is_running:
                t0 = time.monotonic()
                await self._tick()
                elapsed = time.monotonic() - t0
                delay = max(0.0, self.TICK_INTERVAL / self.speed - elapsed)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            pass

    async def _tick(self) -> None:
        """Execute one simulation tick."""
        world = self.state_manager.world

        # 1. Increment tick
        world.simulation_tick += 1

        # 2. Advance simulated time
        self._advance_time(world)

        # 3. User behaviour simulation → publish events
        user_events = self.user_sim.step(world)
        published_user_events: list[SimEvent] = []
        for event in user_events:
            sim_event = SimEvent.from_world_event(
                event,
                timestamp=float(world.simulation_tick),
                wall_time=time.time(),
                priority=2,
            )
            published_user_events.append(await self._publish_sim_event(sim_event))

        # 4. Environment physics step
        self.env_sim.step(world, dt=self.SIMULATED_DT)

        # 5. Agent decisions → apply actions
        actions = await self.agent_runtime.step(world)
        all_deltas: list[dict] = []
        root_event = published_user_events[-1] if published_user_events else None
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
                all_deltas.extend(d.model_dump() for d in deltas)
                for delta in deltas:
                    feedback_event = SimEvent(
                        event_type="feedback.state_delta",
                        source="state_manager",
                        timestamp=float(world.simulation_tick),
                        wall_time=time.time(),
                        correlation_id=published_action.correlation_id,
                        causal_parent=published_action.event_id,
                        priority=1,
                        data=delta.model_dump(),
                    )
                    await self._publish_sim_event(feedback_event)
            except KeyError:
                log.warning(
                    "agent_action_failed",
                    device_id=action.get("device_id"),
                    agent_id=action.get("agent_id"),
                )

        # 6. Update agent runtime states in world
        self._sync_agent_states(world, actions)

        # 7. Broadcast STATE_DELTA with all changes
        if all_deltas:
            await self.conn.broadcast(
                WSMessage(
                    type="STATE_DELTA",
                    payload={"deltas": all_deltas},
                )
            )

        # 8. Broadcast agent statuses
        agent_statuses = {
            agent_id: a.model_dump() for agent_id, a in world.agents.items()
        }
        await self.conn.broadcast(
            WSMessage(
                type="AGENT_STATUS",
                payload={"agents": agent_statuses},
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _advance_time(self, world: "WorldState") -> None:  # noqa: F821
        """Add SIMULATED_DT / 60 minutes to time_of_day, wrapping at 24h."""
        parts = world.environment.time_of_day.split(":")
        hours = int(parts[0])
        minutes = int(parts[1])
        total_minutes = hours * 60 + minutes + self.SIMULATED_DT / 60.0
        total_minutes %= 24 * 60  # wrap at 24h
        new_h = int(total_minutes // 60)
        new_m = int(total_minutes % 60)
        world.environment.time_of_day = f"{new_h:02d}:{new_m:02d}"

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
        await self.event_bus.publish(sim_event)
        await self.conn.broadcast(
            WSMessage(type="SIM_EVENT", payload=sim_event.model_dump())
        )
        return sim_event

    def _sync_agent_states(
        self,
        world: "WorldState",  # noqa: F821
        actions: list[dict],
    ) -> None:
        """Update AgentRuntimeState entries in the world based on latest actions."""
        # Build map: agent_id -> last action summary
        agent_action_map: dict[str, str] = {}
        for a in actions:
            aid = a.get("agent_id", "")
            agent_action_map[aid] = a.get("reason", "")

        for agent in self.agent_runtime.agents:
            entry = world.agents.get(agent.agent_id)
            if entry is None:
                entry = AgentRuntimeState(
                    id=agent.agent_id,
                    name=agent.name,
                )
                world.agents[agent.agent_id] = entry

            if agent.agent_id in agent_action_map:
                entry.status = "active"
                entry.last_action = agent_action_map[agent.agent_id]
                entry.current_strategy = "auto"
                entry.confidence = 0.8
            else:
                entry.status = "idle"
