from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from backend.core.logging import log
from backend.engine.event_bus import SimEvent
from backend.engine.event_types import TIMER_TICK_EVENT_TYPE


PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]
# tick 体抛异常时的上报回调（SimulationEngine 注入）。返回后循环即停止：
# "循环死了但 is_running 还是 True" 是 S2 之前的假活形态，绝不保留。
TickErrorHandler = Callable[[BaseException], Awaitable[None]]


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
        on_error: TickErrorHandler | None = None,
    ) -> None:
        self._publish_event = publish_event
        self._on_error = on_error
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
        self._stop_requested = asyncio.Event()

    async def start(self) -> None:
        if self.is_running:
            return

        self.is_running = True
        self._stop_requested.clear()
        self._task = asyncio.create_task(self._run_loop())
        # 主循环非取消式退出必须留痕：不加这条回调，一个逃逸的异常只会变成解释器那句
        # "Task exception was never retrieved"，而仿真已经停了（critic 修正③）。
        self._task.add_done_callback(self._log_termination)

    async def pause(self) -> None:
        self.request_stop_after_current_tick()
        task = self._task
        if task is None:
            return
        if task is asyncio.current_task():
            # The loop will observe is_running=False as soon as this callback
            # unwinds. Awaiting itself would deadlock.
            return
        # Graceful drain: if a tick is inside EventBus fan-out, let every exact
        # and wildcard subscriber (especially the artifact writer) finish. The
        # stop event also wakes an idle wall-clock sleep immediately.
        try:
            await task
        except asyncio.CancelledError:
            pass
        if self._task is task:
            self._task = None

    def request_stop_after_current_tick(self) -> None:
        """Prevent another tick without interrupting the current event fan-out.

        This method is safe to call from inside a timer-tick subscriber.  The
        in-progress ``_emit_tick`` continues through every exact and wildcard
        subscriber; the run loop observes the flag only after it returns.
        """

        self.is_running = False
        self._stop_requested.set()

    def reset(self) -> None:
        self.current_tick = 0
        self._stop_requested.clear()

    @property
    def sim_time_s(self) -> float:
        """Run-relative simulated seconds; tick 1 is t=0 in both drive modes."""

        return max(0, self.current_tick - 1) * float(self.simulated_dt)

    def set_mode(self, mode: str) -> None:
        spec = self._mode_specs.get(mode, self._mode_specs["observe"])
        self.mode = spec.mode
        self.speed = spec.speed
        self.simulated_dt = spec.simulated_dt

    def apply_legacy_speed(self, speed: float) -> None:
        # 设计意图：旧协议继续可用，但新的墙钟节拍只保留 observe/demo 两档。
        self.set_mode("demo" if speed >= 2.0 else "observe")
        self.speed = float(speed)

    async def tick_once(self) -> SimEvent:
        """手动步进一拍（headless 驱动），绕开墙钟 sleep。

        存在的理由：S2-T9 的确定性门要 8 个场景各跑两遍，live 模式下那是 10 分钟的墙钟；
        S4 的 suite runner 同理。tick 体与 :meth:`_run_loop` **逐字共用** :meth:`_emit_tick`，
        因此两种驱动方式产出同一条事件序列——否则 headless 跑绿、live 跑挂就没人能发现。

        异常照样经 ``on_error`` 上报（与 live 模式同一条归因路径），随后**原样抛出**：
        headless 的调用方是 ScenarioRunner，它必须能 fail fast，而不是拿到一份被悄悄
        截断的 trace。
        """

        try:
            return await self._emit_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.is_running = False
            await self._report_tick_error(exc)
            raise

    async def _emit_tick(self) -> SimEvent:
        self.current_tick += 1
        return await self._publish_event(
            SimEvent(
                event_type=TIMER_TICK_EVENT_TYPE,
                source="simulator_timer",
                timestamp=float(self.current_tick),
                priority=1,
                # timer/reset 这类事件不是"被生成的世界事件"，标 system 以便与
                # scripted/rule_based/stochastic 三种 §4.5 生成模式区分。
                event_generation_mode="system",
                data={
                    "tick": self.current_tick,
                    "simulated_dt": self.simulated_dt,
                    "simulation_speed": self.speed,
                    "mode": self.mode,
                    "wall_tick_ms": int(self.tick_interval * 1000),
                },
            )
        )

    async def _report_tick_error(self, error: BaseException) -> None:
        log.error("simulator_tick_failed", tick=self.current_tick, error=repr(error))
        if self._on_error is None:
            return
        try:
            await self._on_error(error)
        except Exception as handler_error:  # noqa: BLE001 - 上报失败也不能再吞
            log.error("simulator_tick_error_handler_failed", error=repr(handler_error))

    def _log_termination(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error("simulator_loop_terminated", tick=self.current_tick, error=repr(error))

    async def _run_loop(self) -> None:
        try:
            while self.is_running:
                # 设计意图：tick 永远按真实墙钟节拍走，mode 只控制每拍推进多少模拟时间。
                try:
                    await asyncio.wait_for(
                        self._stop_requested.wait(), timeout=self.tick_interval
                    )
                except TimeoutError:
                    pass
                if not self.is_running or self._stop_requested.is_set():
                    break

                try:
                    await self._emit_tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # critic 修正③：旧实现只捕 CancelledError，任何 tick 异常都会静默
                    # 杀死循环而 is_running 仍是 True——引擎"假活"，start() 还拒绝重启。
                    # 现在：停机 + 上报 + 退出循环，让调用方看得见故障。
                    self.is_running = False
                    await self._report_tick_error(exc)
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.is_running = False
            self._stop_requested.set()
