from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from backend.engine.event_bus import SimEvent


PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]


class SimulatorTimer:
    """Publish wall-clock driven timer events without mutating world state."""

    def __init__(
        self,
        publish_event: PublishEvent,
        tick_interval: float = 0.1,
        simulated_dt: float = 60.0,
    ) -> None:
        self._publish_event = publish_event
        self.tick_interval = tick_interval
        self.simulated_dt = simulated_dt
        self.speed = 1.0
        self.is_running = False
        self.current_tick = 0
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.is_running:
          return

        self.is_running = True
        self._task = asyncio.create_task(self._run_loop())

    async def pause(self) -> None:
        if not self.is_running:
            return

        self.is_running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def reset(self) -> None:
        self.current_tick = 0

    async def _run_loop(self) -> None:
        next_delay = self.tick_interval / max(self.speed, 0.001)
        try:
            while self.is_running:
                # 第一拍先等待一个节拍，再发 tick，避免 start 后立刻堆积一串旧事件。
                await asyncio.sleep(next_delay)
                if not self.is_running:
                    break

                self.current_tick += 1
                started_at = time.monotonic()
                await self._publish_event(
                    SimEvent(
                        event_type="system.timer_tick",
                        source="simulator_timer",
                        timestamp=float(self.current_tick),
                        priority=1,
                        data={
                            "tick": self.current_tick,
                            "simulated_dt": self.simulated_dt,
                            "simulation_speed": self.speed,
                        },
                    )
                )
                elapsed = time.monotonic() - started_at
                next_delay = max(0.0, self.tick_interval / max(self.speed, 0.001) - elapsed)
        except asyncio.CancelledError:
            pass
