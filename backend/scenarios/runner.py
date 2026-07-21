"""ScenarioRunner：把一份 ScenarioSpec 跑成一个 run（S2-T6）。

这是"跑仿真"变成"跑可复现实验"的那道门：加载场景 → 应用 initial_state → 开 run（带
scenario_id + seed）→ 按模拟秒驱动 tick → 到 duration 收尾 → 交出一份带元数据的结果。
S2-T9 的确定性门、S2-T8 的场景库自检、S4 的 suite runner / 评估器都从这里驱动。

**两种驱动方式，一条事件序列**：

  headless（默认）
      直接调 :meth:`SimulatorTimer.tick_once`，不睡任何墙钟。8 个场景各跑两遍的
      确定性门在 live 模式下是十分钟起步的墙钟等待，那种门没人愿意在 CI 里开着。

  live（``live=True``）
      起真实墙钟循环，用于演示与"headless 跑绿、live 跑挂"的双模式一致性回归。

两者的 ``sim_time`` 是同一个公式（``SimulationEngine.sim_time_s``），因此同一拍上
timeline 的触发时刻完全一致——一致性不是靠测试盯着，而是构造上只有一份定义。

**fail fast**（critic 修正③）：引擎主循环若因异常停摆，runner 抛
:class:`ScenarioRunError` 而不是交出一份被截断、看上去却正常的 trace。一份短了一半的
事件流会被 S4 评估成"指标莫名其妙变差"，而真相是基础设施死了。
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from backend.agents.llm import LLMProvider
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.rng import SimRandom
from backend.engine.run_manager import RunMetadata
from backend.engine.simulation import SimulationEngine
from backend.engine.state_manager import StateManager
from backend.scenarios.apply import (
    InitialStateApplication,
    InitialStateApplyError,
    apply_scenario_initial_state,
)
from backend.scenarios.generator import GenerationSources
from backend.scenarios.loader import ScenarioLoadError, get_scenario
from backend.scenarios.spec import ScenarioSpec

__all__ = [
    "DEFAULT_DURATION_SECONDS",
    "DEFAULT_MAX_TICKS",
    "ScenarioRunErrorCode",
    "ScenarioRunError",
    "ScenarioRunResult",
    "ScenarioRunner",
    "run_scenario",
]


# 场景没写 duration_seconds 时的兜底时长（§5.1 里它是可选字段）。
DEFAULT_DURATION_SECONDS = 60.0
# tick 预算上限：防止一个写错的 duration/dt 组合把 headless 跑成死循环。
DEFAULT_MAX_TICKS = 5000
# 每拍之后等 agent episode 收工的上限（秒，墙钟）。headless 要的是"这一拍的因果链跑完了"，
# 而不是"任务还挂着就进下一拍"——后者会让同一场景两次运行的事件交错顺序不同。
EPISODE_SETTLE_TIMEOUT_S = 30.0


class ScenarioRunErrorCode(str, Enum):
    """场景运行期失败词表（面向研究者：这次 run 为什么没有结果）。"""

    SCENARIO_NOT_FOUND = "scenario_not_found"
    INITIAL_STATE_INVALID = "initial_state_invalid"
    ENGINE_ERROR = "engine_error"
    TICK_BUDGET_EXCEEDED = "tick_budget_exceeded"


class ScenarioRunError(Exception):
    """结构化的场景运行失败。``code`` 机器可读，``details`` 带定位信息。"""

    def __init__(
        self,
        code: ScenarioRunErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": dict(self.details)}


@dataclass(frozen=True)
class ScenarioRunResult:
    """一次场景运行的完整结果（S2-T7 工件、S2-T9 trace、S4 评估器共同的输入）。"""

    run_id: str
    scenario_id: str
    seed: int
    ticks: int
    sim_time_s: float
    duration_seconds: float
    completed: bool
    events: tuple[SimEvent, ...]
    run_metadata: RunMetadata
    initial_state: InitialStateApplication
    rng_metadata: dict[str, Any] = field(default_factory=dict)
    fired_timeline_event_types: tuple[str, ...] = ()

    def events_of_type(self, event_type: str) -> tuple[SimEvent, ...]:
        return tuple(event for event in self.events if event.event_type == event_type)

    def generated_events(self) -> tuple[SimEvent, ...]:
        """三种 §4.5 生成模式产出的事件（system/agent 来源不算"被生成的世界事件"）。"""

        return tuple(
            event
            for event in self.events
            if event.event_generation_mode in {"scripted", "rule_based", "stochastic"}
        )


class ScenarioRunner:
    """驱动一份 ScenarioSpec 跑完一个 run。

    构造即建世界（``initial_state`` 在此落地），因此调用方可以在 ``run()`` 之前拿
    ``runner.engine`` 做注入/打桩——S2-T9 与 S4-T3 都需要这个接缝。
    """

    def __init__(
        self,
        spec: ScenarioSpec,
        *,
        llm_provider: LLMProvider | None = None,
        connection_manager: ConnectionManager | None = None,
        event_bus: EventBus | None = None,
        max_ticks: int = DEFAULT_MAX_TICKS,
        live: bool = False,
        tick_interval: float | None = None,
        episode_timeout_ms: int | None = None,
        stochastic_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.spec = spec
        self.live = live
        self.max_ticks = max_ticks
        self.stochastic_overrides = stochastic_overrides
        self._collected: list[SimEvent] = []

        self.state_manager, self.initial_state_application = self._build_world(spec)
        self.event_bus = event_bus or EventBus()
        self.conn = connection_manager or ConnectionManager()
        self.engine = SimulationEngine(
            event_bus=self.event_bus,
            state_manager=self.state_manager,
            connection_manager=self.conn,
            # 默认关掉 LLM：headless 运行绝不能因为本机 .env.local 配了 key 就
            # 悄悄发起真实网络调用（§11.1 的 mocked 模式才是本阶段门的对象）。
            llm_provider=llm_provider or _DisabledProvider(),
            agent_episode_timeout_ms=episode_timeout_ms,
        )
        if tick_interval is not None:
            self.engine.timer.tick_interval = float(tick_interval)

    # -- 构造期：世界 ---------------------------------------------------

    @staticmethod
    def _build_world(spec: ScenarioSpec) -> tuple[StateManager, InitialStateApplication]:
        """默认世界 + 场景 initial_state。

        延迟 import：backend.main 是 FastAPI 应用模块（含全局与 lifespan），让场景层在
        import 期就依赖它会把依赖图倒过来。世界仍由 ``_init_default_state()`` 建（S2-T5
        指定的唯一入口，不在这里重拼一遍），但 initial_state 显式走
        :func:`apply_scenario_initial_state`——因为 run 工件需要那份
        :class:`InitialStateApplication`（哪些字段是静态的、创建了哪些用户）。
        """

        from backend.main import _init_default_state

        state_manager = _init_default_state()
        try:
            application = apply_scenario_initial_state(state_manager, spec)
        except InitialStateApplyError as exc:
            raise ScenarioRunError(
                ScenarioRunErrorCode.INITIAL_STATE_INVALID,
                f"场景 {spec.id} 的 initial_state 无法一致地应用：{exc.message}",
                details={"scenario_id": spec.id, **exc.to_dict()},
            ) from exc
        return state_manager, application

    # -- 运行 -----------------------------------------------------------

    @property
    def duration_seconds(self) -> float:
        if self.spec.duration_seconds is not None:
            return float(self.spec.duration_seconds)
        last_at = max((entry.at for entry in self.spec.timeline), default=0.0)
        return max(DEFAULT_DURATION_SECONDS, float(last_at))

    async def run(self) -> ScenarioRunResult:
        engine = self.engine
        duration = self.duration_seconds

        # 换到"本场景的 run"：传入同一个 state_manager 是有意的——reset(None) 那条分支会把
        # time_of_day 抹回 12:00，正好毁掉场景刚摆好的起始世界。带 new_state_manager 的分支
        # 只做「取消在飞 → 换绑 executor → 清历史 → 开新 run」，世界原样保留。
        # 传 ScenarioSpec 而非裸 id：reset 会核对场景出身、（幂等地）复摆 initial_state，
        # 并装上 §4.5 三条产线——装配只有那一处，活着的服务端与本 runner 共用它
        # （S2 review major-1/major-2）。
        await engine.reset(
            new_state_manager=self.state_manager,
            scenario=self.spec,
            seed=self.spec.seed,
            stochastic_overrides=self.stochastic_overrides,
        )
        engine.mode = _world_mode(self.spec.mode)

        rng = engine.run_manager.rng or SimRandom(self.spec.seed)
        sources: GenerationSources = engine.generation_sources or GenerationSources()

        # 全量收事件：总线历史是 1000 条环形缓冲，长场景会把开头挤掉。
        self.event_bus.subscribe("*", self._collect)
        self._collected = []
        total_ticks = int(math.floor(duration / float(engine.simulated_dt_seconds))) + 1
        total_ticks = min(total_ticks, self.max_ticks)

        try:
            await engine.start(drive_timer=self.live)
            if self.live:
                await self._drive_live(total_ticks)
            else:
                await self._drive_headless(total_ticks)
        finally:
            self.event_bus.unsubscribe("*", self._collect)

        await engine.pause()
        await engine.agent_runtime.wait_for_idle(timeout=EPISODE_SETTLE_TIMEOUT_S)
        self._raise_if_engine_died()

        metadata = engine.run_manager.current
        assert metadata is not None  # reset 刚开过 run，除非被外部结束
        run_id = metadata.run_id
        engine.run_manager.end_run("completed")

        result = ScenarioRunResult(
            run_id=run_id,
            scenario_id=self.spec.id,
            seed=metadata.seed,
            ticks=engine.timer.current_tick,
            sim_time_s=engine.sim_time_s,
            duration_seconds=duration,
            completed=sources.scripted is not None and sources.scripted.exhausted,
            events=tuple(self._collected),
            run_metadata=metadata,
            initial_state=self.initial_state_application,
            rng_metadata=rng.metadata(),
            fired_timeline_event_types=tuple(
                entry.type
                for entry in self.spec.timeline[: (sources.scripted.fired_count if sources.scripted else 0)]
            ),
        )
        log.info(
            "scenario_run_completed",
            scenario_id=result.scenario_id,
            run_id=result.run_id,
            seed=result.seed,
            ticks=result.ticks,
            events=len(result.events),
        )
        return result

    async def _drive_headless(self, total_ticks: int) -> None:
        engine = self.engine
        while engine.timer.current_tick < total_ticks:
            try:
                await engine.timer.tick_once()
            except Exception as exc:  # noqa: BLE001 - 转成结构化失败，绝不吞
                raise self._engine_error(exc) from exc
            # 每拍等因果链跑完：headless 的确定性前提（同一场景两次运行事件顺序一致）。
            await engine.agent_runtime.wait_for_idle(timeout=EPISODE_SETTLE_TIMEOUT_S)
            self._raise_if_engine_died()

    async def _drive_live(self, total_ticks: int) -> None:
        engine = self.engine
        interval = max(float(engine.timer.tick_interval), 0.001)
        deadline = asyncio.get_running_loop().time() + interval * total_ticks * 20 + 5.0
        while engine.timer.current_tick < total_ticks:
            if not engine.is_running:
                self._raise_if_engine_died()
                raise ScenarioRunError(
                    ScenarioRunErrorCode.ENGINE_ERROR,
                    f"场景 {self.spec.id} 运行期间引擎停止推进",
                    details={"scenario_id": self.spec.id, "tick": engine.timer.current_tick},
                )
            if asyncio.get_running_loop().time() > deadline:
                raise ScenarioRunError(
                    ScenarioRunErrorCode.TICK_BUDGET_EXCEEDED,
                    f"场景 {self.spec.id} 在 live 模式下超出墙钟预算",
                    details={"scenario_id": self.spec.id, "tick": engine.timer.current_tick},
                )
            await asyncio.sleep(interval / 4)
        await engine.agent_runtime.wait_for_idle(timeout=EPISODE_SETTLE_TIMEOUT_S)

    # -- 内部工具 -------------------------------------------------------

    async def _collect(self, event: SimEvent) -> None:
        self._collected.append(event)

    def _raise_if_engine_died(self) -> None:
        detail = self.engine.last_engine_error
        if detail is None:
            return
        raise ScenarioRunError(
            ScenarioRunErrorCode.ENGINE_ERROR,
            f"场景 {self.spec.id} 运行期间仿真主循环停摆：{detail.get('error')}",
            details={"scenario_id": self.spec.id, **detail},
        )

    def _engine_error(self, exc: BaseException) -> ScenarioRunError:
        detail = self.engine.last_engine_error or {
            "error": repr(exc),
            "error_type": type(exc).__name__,
            "phase": "timer_tick",
            "tick": self.engine.timer.current_tick,
        }
        return ScenarioRunError(
            ScenarioRunErrorCode.ENGINE_ERROR,
            f"场景 {self.spec.id} 运行期间仿真主循环停摆：{detail.get('error')}",
            details={"scenario_id": self.spec.id, **detail},
        )


class _DisabledProvider(LLMProvider):
    """headless 默认 provider：一律失败 → agent 走规则回退（§11.1 mocked）。"""

    provider_name = "disabled"
    model = "rule_based"

    async def generate_decision(self, request):  # type: ignore[override]
        from backend.agents.llm import LLMProviderError

        raise LLMProviderError("provider_error", "LLM provider is disabled for headless runs")


def _world_mode(scenario_mode: str) -> str:
    """§5.1 场景模式 → 世界运行模式。

    世界只有 observe|demo 两档；``stress`` 是场景层语义（更密的事件/更狠的注入），
    落到运行模式上按 demo 处理——不把第三个值塞进 WorldState.simulation_mode。
    """

    return "demo" if scenario_mode in {"demo", "stress"} else "observe"



async def run_scenario(
    scenario: ScenarioSpec | str,
    *,
    dirs: Iterable[Path | str] | None = None,
    **kwargs: Any,
) -> ScenarioRunResult:
    """加载（若给的是 id）并跑完一个场景。S2-T9 / S4-T4 的统一入口。"""

    spec = scenario
    if isinstance(scenario, str):
        try:
            loaded = get_scenario(scenario, dirs)
        except ScenarioLoadError as exc:
            raise ScenarioRunError(
                ScenarioRunErrorCode.SCENARIO_NOT_FOUND,
                f"场景 {scenario!r} 加载失败：{exc}",
                details={"scenario_id": scenario, **exc.to_dict()},
            ) from exc
        if loaded is None:
            raise ScenarioRunError(
                ScenarioRunErrorCode.SCENARIO_NOT_FOUND,
                f"场景 {scenario!r} 不在场景库里",
                details={"scenario_id": scenario},
            )
        spec = loaded

    runner = ScenarioRunner(spec, **kwargs)
    return await runner.run()
