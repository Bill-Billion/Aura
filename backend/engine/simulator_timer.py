from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from backend.engine.event_bus import SimEvent


PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]


@dataclass(frozen=True)
class SimulationModeSpec:
    mode: str
    speed: float
    simulated_dt: float


class SimulatorTimer:
    """Publish wall-clock driven timer events without mutating world state."""

    def __init__(
        self,
        publish_event: PublishEvent,
        tick_interval: float = 2.0,
        simulated_dt: float = 10.0,
        *,
        default_mode: str = "observe",
        mode_specs: dict[str, SimulationModeSpec] | None = None,
    ) -> None:
        self._publish_event = publish_event
        self.tick_interval = tick_interval
        self._mode_specs = mode_specs or {
            "observe": SimulationModeSpec(mode="observe", speed=1.0, simulated_dt=simulated_dt),
            "demo": SimulationModeSpec(mode="demo", speed=4.0, simulated_dt=simulated_dt * 6),
        }
        self.mode = default_mode if default_mode in self._mode_specs else "observe"
        current_spec = self._mode_specs[self.mode]
        self.simulated_dt = current_spec.simulated_dt
        self.speed = current_spec.speed
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

    def set_mode(self, mode: str) -> None:
        spec = self._mode_specs.get(mode, self._mode_specs["observe"])
        self.mode = spec.mode
        self.speed = spec.speed
        self.simulated_dt = spec.simulated_dt

    def apply_legacy_speed(self, speed: float) -> None:
        # 设计意图：旧协议继续可用，但新的墙钟节拍只保留 observe/demo 两档。
        self.set_mode("demo" if speed >= 2.0 else "observe")
        self.speed = float(speed)

    async def _run_loop(self) -> None:
        try:
            while self.is_running:
                # 设计意图：tick 永远按真实墙钟节拍走，mode 只控制每拍推进多少模拟时间。
                await asyncio.sleep(self.tick_interval)
                if not self.is_running:
                    break

                self.current_tick += 1
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
                            "mode": self.mode,
                            "wall_tick_ms": int(self.tick_interval * 1000),
                        },
                    )
                )
        except asyncio.CancelledError:
            pass
