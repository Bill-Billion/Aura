"""SimulationEngine — event-driven orchestrator built on top of SimulatorTimer."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from backend.agents.llm import LLMProvider
from backend.agents.llm_modes import resolve_mode_for_provider
from backend.agents.runtime import (
    AgentRuntime,
    RuntimePolicySelection,
    register_default_agents,
)
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.event_bus import EventBus, SimEvent, WorldEvent
from backend.engine.event_types import (
    ENGINE_ERROR_EVENT_TYPE,
    ENVIRONMENT_STATE_REFRESH,
    TIMER_TICK_EVENT_TYPE,
    USER_ACTIVITY_CHANGE,
    USER_MOVEMENT_EVENT_TYPES,
)
from backend.engine.event_log import RunArtifactRecorder, attach_run_artifacts
from backend.engine.rng import SimRandom, validate_seed
from backend.engine.run_manager import (
    RunManager,
    RunMetadata,
    baseline_policy_for_llm_mode,
    new_run_id,
    resolve_run_scenario,
)
from backend.engine.simulator_timer import SimulatorTimer
from backend.engine.state import Location3D, WorldState
from backend.engine.state_manager import (
    INVARIANT_VIOLATION_EVENT_TYPE,
    DeltaChange,
    StateManager,
    find_world_invariant_violation,
)
from backend.execution.executor import (
    FEEDBACK_EVENT_TYPE,
    CommandExecutor,
    InvariantReportDebounce,
)
from backend.models.schemas import BaselinePolicy, WSMessage
from backend.scenarios.apply import apply_scenario_initial_state
from backend.scenarios.generator import (
    FAILURE_INJECTION_CAUSED_BY,
    DeviceAvailabilityWrite,
    GeneratedEvent,
    GenerationContext,
    GenerationSources,
    build_generation_sources,
)
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.spec import ScenarioSpec
from backend.simulators.environment import EnvironmentSimulator
from backend.simulators.user_behavior import UserBehaviorSimulator

# 引擎主循环停摆的 WS 广播类型（前端据此把"仿真已死"与普通错误区分开）。
# 纯增类型：schemas.MessageType 是 `str | Literal[...]`，旧前端不认识就直接忽略。
ENGINE_ERROR_WS_TYPE = "ENGINE_ERROR"


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
            # critic 修正③：tick 体的任何异常都必须把引擎停下并上报，而不是让循环
            # 静默死掉、is_running 继续说谎（"假活"）。
            on_error=self._handle_tick_error,
        )
        # §4.5 三条生成产线（scripted/rule_based/stochastic）。默认 None＝交互式运行沿用
        # 既有 user_sim/env_sim 行为；场景 runner 在 reset 之后绑一份。
        self.generation_sources: GenerationSources | None = None
        # 最近一次主循环故障（None＝健康）。ScenarioRunner 与 S4 suite runner 据此 fail fast。
        self.last_engine_error: dict[str, Any] | None = None

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
        # §8.2 五个域 agent；"注册了谁、什么顺序"的唯一真相在 runtime.DEFAULT_AGENT_FACTORIES
        # ——顺序同时决定 TaskPlan.domain_tasks 序与 canonical trace 行序（S2-T9 门）。
        register_default_agents(self.agent_runtime)

        # §11 run 模型：引擎一起来就处在一个 run 里——"没有 run 的事件"是 S2 之前那种
        # 无法归属的连续带，正是可复现性缺失的形态。scenario_id/seed 由场景运行方
        # （S2-T6 runner）在 reset 时给；交互式使用则每次 reset 换一个匿名 run。
        self.run_manager = RunManager(event_bus=self.event_bus)
        self.scenario_id: str | None = None
        # 模拟时钟读数源：总线据此给"不经引擎 _publish_sim_event"的事件也盖 sim_time_s。
        self.event_bus.set_sim_time_source(lambda: self.sim_time_s)
        # §11 工件：run 一开就有目录，事件随发随落（data/runs/{run_id}/）。
        # 挂在 RunManager 上而不是挂在引擎方法上——场景 runner 会直接调
        # run_manager.end_run("completed")，那条路径同样必须把 run.json 收尾。
        self.run_artifacts: RunArtifactRecorder = attach_run_artifacts(self.run_manager)
        self._start_run(scenario_id=None, seed=None, clear_event_history=False)

        self._subscribe_handlers()
        self.agent_runtime.bind(
            event_bus=self.event_bus,
            state_manager=self.state_manager,
            connection_manager=self.conn,
            publish_event=self._publish_sim_event,
            command_executor=self.command_executor,
            run_id_source=lambda: self.run_manager.run_id,
            recording_integrity_error_handler=self._mark_run_artifact_invalid,
        )
        self._sync_world_timing_state(reset_mode=True)
        self._sync_agent_diagnostics()

    @property
    def run_id(self) -> str | None:
        """当前活跃 run 的 id（§11）；事件、episode 与工件都以它为归属。"""

        return self.run_manager.run_id

    def _mark_run_artifact_invalid(self, run_id: str, message: str) -> None:
        metadata = self.run_manager.current
        if metadata is None or metadata.run_id != run_id:
            return
        if metadata.artifact_error is None:
            metadata.artifact_error = message
        writer = self.run_artifacts.writer
        if writer is not None and writer.metadata.run_id == run_id:
            writer.write_metadata()

    @property
    def sim_time_s(self) -> float:
        """自 run 起点起算的模拟秒（§4.5 timeline 的 ``at`` 与此同域）。

        定义为 ``(current_tick - 1) * simulated_dt``：第 1 拍代表 t=0，因此 ``at: 0``
        的 timeline 项在第一拍就触发。**live 与 headless 共用这一个公式**，两种驱动
        方式下同一拍的 sim_time 完全一致（这是双模式一致性的全部实现）。
        """

        return max(0, self.timer.current_tick - 1) * float(self.timer.simulated_dt)

    def bind_generation_sources(self, sources: GenerationSources | None) -> None:
        """挂上（或摘掉）本 run 的三条 §4.5 生成产线。

        绑定之后：timeline 根事件与其设备命令、规则事件、随机故障都由 tick 统一驱动。
        rule 源接管用户日程之后，引擎不再直接调 ``user_sim``——同一份日程发两遍会让
        因果图里出现两条彼此无关的用户事件。
        """

        self.generation_sources = sources

    def _install_generation_sources(
        self,
        spec: ScenarioSpec,
        *,
        stochastic_overrides: Mapping[str, Any] | None = None,
    ) -> GenerationSources:
        """按 *spec* 装配并挂上三条产线（**全系统唯一**的装配点）。

        必须在 ``_start_run`` 之后调用：``run_id`` 与本 run 的 :class:`SimRandom` 都从
        RunManager 取。ScenarioRunner 与活着的服务端共用这一条路径——S2 review major-1
        的根因正是"装配只写在 runner 里"，于是跑起来的服务端永远装不上。
        """

        rng = self.run_manager.rng or SimRandom(spec.seed)
        sources = build_generation_sources(
            spec,
            context=GenerationContext(run_id=self.run_id, scenario_id=spec.id),
            rng=rng,
            user_sim=self.user_sim,
            stochastic_overrides=stochastic_overrides,
        )
        self.bind_generation_sources(sources)
        return sources

    def _start_run(
        self,
        *,
        scenario_id: str | None,
        seed: int | None,
        baseline_policy: BaselinePolicy | None = None,
        recording_source_run_id: str | None = None,
        duration_seconds: float | None = None,
        scenario_schema_version: str | None = None,
        scenario_contract_hash: str | None = None,
        clear_event_history: bool = True,
    ) -> RunMetadata:
        """开一个新 run 并把 §11 元数据补齐（provider/model/agent 版本从运行期取）。

        agent 尚未各自声明版本，故统一继承 sim_version；某个 agent 一旦声明
        ``agent_version``，以它为准（S4 评估要能区分"换了 agent 实现"与"换了 seed"）。
        """

        self.scenario_id = scenario_id
        provider = self.agent_runtime.llm_provider
        effective_mode = resolve_mode_for_provider(provider)
        selected_policy = baseline_policy or baseline_policy_for_llm_mode(effective_mode)
        assigned_run_id = new_run_id()
        # recorded provider 的默认目录依赖新 run_id；此刻 RunManager.current 仍是旧 run，
        # 必须先显式绑定，不能让 provider 属性访问从 run_id_source 误取旧身份。
        bind_provider_run = getattr(provider, "bind_run", None)
        if callable(bind_provider_run):
            bind_provider_run(assigned_run_id)
        sim_version = self.run_manager.sim_version
        agent_versions = {
            agent.agent_id: str(getattr(agent, "agent_version", sim_version))
            for agent in self.agent_runtime.agents
        }
        return self.run_manager.start_run(
            world=self.state_manager.world,
            scenario_id=scenario_id,
            seed=seed,
            llm_provider=provider,
            # §11.1：run 工件必须记下用的是哪种模式。显式传是因为 run_manager 在 engine 层，
            # 它只会鸭子类型地看 provider_name/api_key，认不出 S3 的三层模式包装
            # （recorded 会被它当成 live 或 mocked）。裸 provider 走到这里结果与 S2 一致。
            llm_mode=effective_mode,
            baseline_policy=selected_policy,
            recording_source_run_id=recording_source_run_id,
            duration_seconds=duration_seconds,
            scenario_schema_version=scenario_schema_version,
            scenario_contract_hash=scenario_contract_hash,
            agent_versions=agent_versions,
            run_id=assigned_run_id,
            clear_event_history=clear_event_history,
        )

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

    async def start(self, *, drive_timer: bool = True) -> None:
        """启动仿真。

        ``drive_timer=False`` 是 headless 驱动（S2-T6 ScenarioRunner / S4 suite runner）：
        引擎照常进入运行态并发 ``system.simulation_started``，但不起墙钟循环——由调用方
        用 :meth:`SimulatorTimer.tick_once` 手动步进。
        """

        if self.is_running:
            return

        self.is_running = True
        # 上一次故障的痕迹在这里清掉：能重新起来才说明故障已经过去（假活时代
        # 连"重新起来"这个动作都不存在——start() 看见 is_running=True 直接 return）。
        self.last_engine_error = None
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
        if drive_timer:
            await self.timer.start()
        else:
            # headless：timer 不起循环，但要处于"可 tick_once"的运行态。
            self.timer.is_running = True
        log.info("sim_started", drive_timer=drive_timer)

    async def pause(self) -> None:
        was_running = self.is_running
        # Always drain the timer task. On a tick exception the error handler sets
        # engine.is_running=False before it finishes publishing system.engine_error;
        # an early return here would let finalization close the writer underneath it.
        await self.timer.pause()
        self.is_running = False
        self.state_manager.world.is_running = False
        self._sync_world_timing_state()
        if not was_running:
            return
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

    async def reset(
        self,
        new_state_manager: StateManager | None = None,
        *,
        scenario: ScenarioSpec | str | None = None,
        seed: int | None = None,
        scenario_dirs: Iterable[Path | str] | None = None,
        stochastic_overrides: Mapping[str, Any] | None = None,
        policy_selection: RuntimePolicySelection | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        """重置世界并开一个**新 run**（§11）。

        ``scenario`` 不传即匿名 run（交互式重置就是换了一次实验，沿用上一次的场景标签
        只会让工件说谎）；传 ScenarioSpec 或库里的 scenario_id 即"按这个场景开一个 run"。

        **provenance 门（S2 review major-2）**：给了场景就必须做到两件事，一件都不能少——
        ① id 在场景库里查得到（查不到抛 :class:`RunProvenanceError`，在动世界之前）；
        ② 它的 ``initial_state`` **真的**被摆到世界上。做不到就不该在 run.json 上盖它的章：
        一份声称自己是某场景、却从未按它摆过世界的工件，比没有场景标签更能误导人。

        **生成产线（S2 review major-1）**：给了场景就在这里装上 §4.5 三条产线，
        没给就摘掉——于是活着的服务端与 headless runner 走的是同一条装配路径，
        "跑起来的仿真永远走不到 scripted/rule_based/stochastic" 那个洞随之消失。
        """

        # 解析放在最前面：未知场景 id 必须在 pause/取消/换世界**之前**被拒，
        # 否则一次拼错的启动会把正在跑的 run 拆掉再报错。
        spec = resolve_run_scenario(scenario, dirs=scenario_dirs)
        resolved_seed = seed if seed is not None else (spec.seed if spec is not None else None)
        if resolved_seed is not None:
            # Validate before pause/cancel/world swap.  A late failure in
            # RunManager.start_run would otherwise leave main.state_manager and
            # engine.state_manager split across two worlds with no active run.
            resolved_seed = validate_seed(resolved_seed)
        if policy_selection is None:
            # ``None`` means "server default for this new run", never "inherit
            # whichever provider the previous experiment installed".  Without
            # this reset, leaving an explicit live/recorded run could leak paid
            # calls or replay state into later anonymous 3D interactions.
            policy_selection = self.agent_runtime.prepare_baseline_policy(None)

        if spec is not None:
            # Construct the complete generation graph before pausing the old run
            # or swapping worlds.  Nested stochastic config used to be an
            # untyped dict and could fail (for example float("nope")) only after
            # _start_run had committed a canonical writer, leaving the service
            # permanently locked behind a stopped half-run.
            build_generation_sources(
                spec,
                context=GenerationContext(run_id=None, scenario_id=spec.id),
                rng=SimRandom(resolved_seed),
                user_sim=UserBehaviorSimulator(),
                stochastic_overrides=stochastic_overrides,
            )

        await self.pause()
        # Defensive reset for legacy/manual headless failures which may have
        # interrupted a tick before its handler wrapper was installed.
        self._is_processing_timer_tick = False
        # 审计必修②：cancel-before-swap——先取消并落账在飞 episode，
        # 再替换世界，否则旧任务可能在 swap 之后、cancel 之前写入新世界。
        await self.agent_runtime.cancel_active_episodes()
        # 同一条 cancel-before-swap 纪律延伸到命令层：episode 被砍后仍挂在注册表里的
        # 在飞命令必须先落账成 cancelled（§15「reset 取消在飞命令」），再换世界，
        # 否则它们的终态迁移会记在重置后的新世界头上。
        await self.command_executor.cancel_pending("simulation_reset")
        if new_state_manager is not None:
            # 场景 initial_state 在**换世界之前**摆：摆不上（引用了不存在的房间/设备、
            # 与 §2.2 不变式冲突）时活跃世界一个字节都没被动过。
            if spec is not None:
                apply_scenario_initial_state(new_state_manager, spec)
            self.state_manager = new_state_manager
        else:
            self.state_manager.world.simulation_tick = 0
            self.state_manager.world.environment.time_of_day = "12:00"
            self.state_manager.world.is_running = False
            # 原地重置会把 time_of_day 抹回 12:00，因此场景覆盖必须**在那之后**再摆。
            if spec is not None:
                apply_scenario_initial_state(self.state_manager, spec)

        # provider 切换只能发生在旧 episode/命令都已取消、且 initial_state 已完整应用之后；
        # prepare 已在调用 reset 前完成全部可能失败的凭证/录制校验，所以这里是无失败的
        # commit，且仍早于 _start_run 写 provider/mode 元数据。
        self.agent_runtime.activate_baseline_policy(policy_selection)

        self.timer.reset()
        self.timer.set_mode(self.DEFAULT_MODE)
        self.user_sim = UserBehaviorSimulator()
        self.last_engine_error = None
        self._pending_deltas = []
        # 换了世界（或清了世界）之后旧违规不再成立，去抖状态必须跟着复位。
        self._invariant_report.reset()
        self._time_of_day_seconds = self._parse_time_of_day_to_seconds(
            self.state_manager.world.environment.time_of_day
        )
        # executor 换绑新世界并清空注册表（cancel 已落账，这里是防残留的兜底；
        # 万一还有残留，bind_state_manager 会带生命周期事件地取消，绝不静默丢）。
        await self.command_executor.bind_state_manager(
            self.state_manager, reason="simulation_reset"
        )
        self.agent_runtime.update_state_manager(self.state_manager)
        self.agent_runtime.reset()
        self._sync_world_timing_state(reset_mode=True)
        self._sync_agent_diagnostics()
        # 换 run 放在最后一步、system.simulation_reset 之前：上一 run 的收尾事件
        # （episode_cancelled / 命令取消）仍归旧 run，reset 事件已经属于新 run。
        # start_run 顺手清空事件历史——审计发现③：1000 条环形历史 reset 从不清空，
        # get_causal_chain 会把两个 run 的链焊成一条。
        started_run = self._start_run(
            scenario_id=spec.id if spec is not None else None,
            seed=resolved_seed,
            baseline_policy=(
                policy_selection.baseline_policy if policy_selection is not None else None
            ),
            recording_source_run_id=(
                policy_selection.recording_source_run_id
                if policy_selection is not None
                else None
            ),
            duration_seconds=duration_seconds,
            scenario_schema_version=(
                spec.scenario_schema_version if spec is not None else None
            ),
            scenario_contract_hash=(
                scenario_contract_fingerprint(spec) if spec is not None else None
            ),
        )
        # 产线绑定必须在 _start_run 之后：GenerationContext 盖的是**新** run 的章，
        # 随机子流取自新 run 的 SimRandom（一 run 一 seed）。没有场景就摘干净——
        # 留着上一个场景的产线，等于用旧场景的 timeline 驱动一个匿名的新实验。
        try:
            if spec is not None:
                self._install_generation_sources(spec, stochastic_overrides=stochastic_overrides)
            else:
                self.bind_generation_sources(None)
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
        except Exception as exc:
            # A post-commit failure must never leave a canonical current run
            # which blocks every subsequent launch/mutation.  The partial
            # artifact remains explicit and non-evaluable via end_reason.
            self.bind_generation_sources(None)
            current = self.run_manager.current
            if current is not None and current.run_id == started_run.run_id:
                self.run_manager.end_run("launch_failed")
            self.is_running = False
            self.state_manager.world.is_running = False
            log.error(
                "simulation_reset_commit_failed",
                run_id=started_run.run_id,
                scenario_id=started_run.scenario_id,
                error=str(exc),
            )
            raise
        log.info("sim_reset")

    async def close(self) -> None:
        await self.stop()
        self._unsubscribe_handlers()
        await self.agent_runtime.close()
        # run 必须显式收尾：此后 run_id 为 None，任何还在飞的产物一律判 stale
        # （进程关停时把变更写进一个已结束的 run，与写进另一个 run 一样错）。
        # end_run 已被 RunArtifactRecorder 包过，run.json 在这一步补上 ended_at/end_reason。
        self.run_manager.end_run("closed")
        # 兜底：万一 run 已被外部（场景 runner）结束，工件也要确保落盘关闭。
        self.run_artifacts.close()

    # §4.1 富用户根事件与旧 user.activity_change 共用同一台"位置写回"处理器：
    # 三个 data 键（user_id / from_room / to_room）同名，语义也同一件事——用户挪窝了。
    # 不给富事件单开一套写回逻辑，是为了避免同一条不变式（persons ↔ location）有两个实现。
    _USER_MOVEMENT_SUBSCRIPTIONS: tuple[str, ...] = (
        USER_ACTIVITY_CHANGE,
        *sorted(USER_MOVEMENT_EVENT_TYPES),
    )

    def _subscribe_handlers(self) -> None:
        if self._subscriptions_registered:
            return

        self.event_bus.subscribe(TIMER_TICK_EVENT_TYPE, self._handle_timer_tick)
        for event_type in self._USER_MOVEMENT_SUBSCRIPTIONS:
            self.event_bus.subscribe(event_type, self._handle_user_activity_change)
        self.event_bus.subscribe(ENVIRONMENT_STATE_REFRESH, self._handle_environment_refresh)
        # 工件写入订阅在总线的 wildcard 上（而不是包在 _publish_sim_event 里）：
        # 只有这样才能连"不经引擎、直接 bus.publish"的事件（main.py 的 UI 根事件）
        # 一起收进工件，且拿到的是已经盖好 seq/run_id 章的那一份。
        self.event_bus.subscribe("*", self._record_event_artifact)
        self._subscriptions_registered = True

    def _unsubscribe_handlers(self) -> None:
        if not self._subscriptions_registered:
            return

        self.event_bus.unsubscribe(TIMER_TICK_EVENT_TYPE, self._handle_timer_tick)
        for event_type in self._USER_MOVEMENT_SUBSCRIPTIONS:
            self.event_bus.unsubscribe(event_type, self._handle_user_activity_change)
        self.event_bus.unsubscribe(ENVIRONMENT_STATE_REFRESH, self._handle_environment_refresh)
        self.event_bus.unsubscribe("*", self._record_event_artifact)
        self._subscriptions_registered = False

    def _record_event_artifact(self, event: SimEvent) -> None:
        """把一条已发布事件追加进本 run 的 events.jsonl（同步、行缓冲）。

        写盘在事件热路径上是有意的：每 tick 的事件量级很小，而异步刷盘会引入
        "进程退出时最后几条事件还在队列里"的丢事件窗口。
        """

        self.run_artifacts.record(event)

    async def _handle_timer_tick(self, event: SimEvent) -> None:
        self._is_processing_timer_tick = True
        try:
            await self._handle_timer_tick_body(event)
            metadata = self.run_manager.current
            duration_seconds = (
                metadata.duration_seconds if metadata is not None else None
            )
            if (
                duration_seconds is not None
                and self.sim_time_s >= duration_seconds
            ):
                # Stop at the first tick whose t=(tick-1)*dt covers the deadline.
                # This only arms the loop stop flag: the current EventBus fan-out
                # (including wildcard artifact recording) still finishes fully.
                self.timer.request_stop_after_current_tick()
        finally:
            # Covers the full exact-handler body, including the final delta,
            # invariant and agent-status awaits. Timer.pause then drains the
            # surrounding EventBus fan-out before finalization closes artifacts.
            self._is_processing_timer_tick = False

    async def _handle_timer_tick_body(self, event: SimEvent) -> None:
        world = self.state_manager.world
        self._pending_deltas = []

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

        sim_time_s = self.sim_time_s
        sources = self.generation_sources

        # —— ① scripted：timeline 到点的根事件（+ 其设备命令，走 CommandExecutor）——
        if sources is not None and sources.scripted is not None:
            for generated in sources.scripted.emit(world, trigger=event, sim_time_s=sim_time_s):
                await self._dispatch_generated(generated, tick=timer_tick)

        # —— ② 用户日程：接了 rule 源就由它产出富根事件，否则沿用旧 user_sim ——
        if sources is not None and sources.rule_based is not None:
            for generated in sources.rule_based.emit_schedule_events(
                world, trigger=event, sim_time_s=sim_time_s
            ):
                user_event = await self._dispatch_generated(generated, tick=timer_tick)
                # critic 修正②：阈值规则在**每条用户事件写回世界之后**立刻评一次，
                # trigger 就是那条用户事件——于是"谁把读数推过阈值"是构造出来的事实，
                # 而不是"取最近一条用户事件"的猜测（两个用户先后动作时必然猜错）。
                for threshold in sources.rule_based.emit_threshold_events(
                    world, trigger=user_event, sim_time_s=sim_time_s
                ):
                    await self._dispatch_generated(threshold, tick=timer_tick)
        else:
            for user_event in self.user_sim.step(world):
                await self._publish_sim_event(
                    SimEvent.from_world_event(
                        user_event,
                        timestamp=float(world.simulation_tick),
                        wall_time=time.time(),
                        priority=2,
                    )
                )

        env_updates = self.env_sim.step(world, dt=simulated_dt)
        significant_change_reasons = self._collect_environment_change_reasons(
            world=world,
            updates=env_updates,
            previous_time_of_day=previous_time_of_day,
            previous_weather=previous_weather,
            next_time_of_day=time_of_day,
        )

        # 因果归属（critic 修正② / 审计§六⑤）：环境刷新的物理成因是**这一拍时钟推进了
        # simulated_dt**，不是"本 tick 里最后一个用户事件"。旧实现取最后一条用户事件当父，
        # 多用户同拍时把因果指给了一个根本没影响环境的人——S5 的因果树、S2-T9 的
        # canonical trace、S3 的多用户门场景全都建立在这条边上。
        await self._publish_sim_event(
            SimEvent(
                event_type=ENVIRONMENT_STATE_REFRESH,
                source="environment_sim",
                timestamp=float(world.simulation_tick),
                wall_time=time.time(),
                correlation_id=event.correlation_id,
                causal_parent=event.event_id,
                priority=1,
                event_generation_mode="rule_based",
                generation_rule_id="environment.state_refresh",
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

        # —— ③ 阈值规则：环境刷新之后再评一次，trigger 是本拍 tick 事件 ——
        if sources is not None and sources.rule_based is not None:
            for generated in sources.rule_based.emit_threshold_events(
                world, trigger=event, sim_time_s=sim_time_s
            ):
                await self._dispatch_generated(generated, tick=timer_tick)

        # —— ④ stochastic：seed 化的设备掉线/恢复（MVP 只此一种，critic 修正④）——
        if sources is not None and sources.stochastic is not None:
            for generated in sources.stochastic.emit(
                world, trigger=event, sim_time_s=sim_time_s
            ):
                await self._dispatch_generated(generated, tick=timer_tick)

        await self._flush_pending_deltas()
        # tick 收尾探测：本 tick 内的仿真写入（timer/user/env）是否把世界写成不一致。
        await self._report_world_invariants(
            phase="tick_end", attributed_to="simulator_timer", root_event=event
        )
        await self._broadcast_agent_status(world)

    # ------------------------------------------------------------------
    # §4.5 生成事件的统一落地：发布 → 命令（executor）→ 可用性写入（仿真侧）
    # ------------------------------------------------------------------

    async def _dispatch_generated(
        self, generated: GeneratedEvent, *, tick: int | None = None
    ) -> SimEvent:
        """把一条生成事件落到世界上。**三条产线唯一的执行权都在这里**。

        顺序是刻意的：先发根事件，再产生它的后果（§4.4「根事件必须先于其派生事件可见」）。
        设备命令一律 ``CommandExecutor.submit``——同一条六级校验、同一套十态生命周期、
        同一份失败词表（critic 修正①：没有 state_manager 兜底路径）。
        """

        event = await self._publish_sim_event(generated.event)
        if generated.availability_write is not None:
            self._apply_availability_write(generated.availability_write, event)
        if generated.device_command is not None:
            command = generated.device_command
            if tick is not None:
                command = command.model_copy(update={"issued_tick": int(tick)})
            await self.command_executor.submit(
                command,
                tick=int(tick if tick is not None else self.state_manager.world.simulation_tick),
                publish=self._collecting_publish(),
            )
        return event

    def _collecting_publish(self):
        """给 executor 的 publish 包装：照常外发，顺手把反馈 delta 攒进本 tick 的批次。

        不收集的话，场景命令改了世界但前端收不到 STATE_DELTA——世界与事件流分叉，
        正是 S1 根治的那类"看起来什么都没发生"。
        """

        async def publish(event: SimEvent) -> SimEvent:
            published = await self._publish_sim_event(event)
            if published.event_type == FEEDBACK_EVENT_TYPE:
                self._pending_deltas.append(DeltaChange.model_validate(published.data))
            return published

        return publish

    def _apply_availability_write(
        self, write: DeviceAvailabilityWrite, event: SimEvent
    ) -> None:
        """设备可用性（``state.extra.online``）的仿真侧写入（§13 故障注入）。

        为什么不走 CommandExecutor：§3.2 声明 ``online`` **不可写**——它是"设备还在不在线"
        这一世界事实，不是可下发的控制点；走命令路径只会拿到一条 read_only_capability 失败。
        这条写入照样是可归因的（``caused_by=failure_injector`` + ``caused_by_event_id``），
        因此工件里分得清"掉线是注入的"还是"物理演化的"。
        """

        device = self.state_manager.world.devices.get(write.device_id)
        if device is None:
            log.warning("availability_write_unknown_device", device_id=write.device_id)
            return
        # extra 里没有 online 键时先补位：apply_path_update 会先读旧值，
        # 缺键会 KeyError；补 True 恰好如实表达"此前视为在线"（见 validation.device_is_online）。
        device.state.extra.setdefault("online", True)
        self._pending_deltas.extend(
            self.state_manager.apply_path_update(
                caused_by=FAILURE_INJECTION_CAUSED_BY,
                path=f"devices[{write.device_id}].state.extra.online",
                new_value=write.online,
                reason=write.reason or "device availability change",
                caused_by_event_id=event.event_id,
            )
        )

    # ------------------------------------------------------------------
    # 主循环故障（critic 修正③：绝不假活）
    # ------------------------------------------------------------------

    async def _handle_tick_error(self, error: BaseException) -> None:
        """tick 体抛异常：停机 + 结构化事件 + WS 广播。

        旧实现里这条路径根本不存在：``SimulatorTimer._run_loop`` 只捕 CancelledError，
        任何 tick 异常都会杀死循环而 ``is_running`` 仍是 True——前端看着"运行中"、
        headless suite 则拿到一份被截断却毫无解释的 trace，最后被当成"指标莫名其妙变差"。
        """

        self.is_running = False
        self.state_manager.world.is_running = False
        detail = {
            "error": repr(error),
            "error_type": type(error).__name__,
            "phase": "timer_tick",
            "tick": self.timer.current_tick,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
        }
        self.last_engine_error = detail
        log.error("engine_tick_failed", **detail)
        try:
            await self._publish_sim_event(
                SimEvent(
                    event_type=ENGINE_ERROR_EVENT_TYPE,
                    source="simulation_engine",
                    timestamp=float(self.state_manager.world.simulation_tick),
                    wall_time=time.time(),
                    priority=3,
                    event_generation_mode="system",
                    data=dict(detail),
                )
            )
            await self.conn.broadcast(
                WSMessage(type=ENGINE_ERROR_WS_TYPE, payload=dict(detail))
            )
        except Exception as report_error:  # noqa: BLE001 - 上报失败也要留痕
            log.error("engine_error_report_failed", error=repr(report_error))

    async def _handle_user_activity_change(self, event: SimEvent) -> None:
        world = self.state_manager.world
        user_id = str(event.data.get("user_id") or "")
        # 富根事件（user.starts_activity 等）可能不带房间：此时只更新活动，不动位置。
        target_room = str(event.data.get("to_room") or "")
        activity = str(event.data.get("activity") or "")
        old_room = str(event.data.get("from_room") or "")

        user = world.users.get(user_id)
        if user is None:
            return

        updates: list[tuple[str, Any]] = []
        if target_room:
            updates.append((f"users[{user_id}].location", Location3D(room=target_room)))
        if activity:
            updates.append((f"users[{user_id}].activity", activity))

        # 只有"确实换了地方"才动 persons/occupancy：没带房间信息的富事件
        # （user.starts_activity 只声明活动）若也走这段，会把人从旧房间摘掉却不给新位置，
        # 直接违反 §2.2 的 persons ↔ location 一致性。
        if target_room and old_room and old_room in world.rooms:
            remaining = [person for person in world.rooms[old_room].persons if person != user_id]
            updates.extend(
                [
                    (f"rooms[{old_room}].persons", remaining),
                    (f"rooms[{old_room}].occupancy", bool(remaining)),
                ]
            )

        if target_room and target_room in world.rooms:
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

        async def broadcast_before_fan_out(visible: SimEvent) -> None:
            await self.conn.broadcast(
                WSMessage(type="SIM_EVENT", payload=visible.model_dump())
            )

        # Admission, stamping and WS visibility are one transaction.  The
        # before-fan-out hook preserves the product invariant that a root is on
        # the wire before synchronous subscribers emit derived events; a
        # depth-capped input is instead represented everywhere by the same
        # suppression notice.  Returning that notice also prevents callers from
        # extending a chain from a refused, non-existent parent.
        return await self.event_bus.publish_visible(
            sim_event,
            before_fan_out=broadcast_before_fan_out,
        )

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
        metadata = self.run_manager.current
        return {
            # run_id/scenario_id 纯增字段：前端 simulationStore 不解析未知键，
            # 但研究者从此能在任意一条状态消息上回答"我现在看的是哪次实验"（§18）。
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "seed": metadata.seed if metadata is not None else None,
            "baseline_policy": (
                metadata.baseline_policy.value
                if metadata is not None and metadata.baseline_policy is not None
                else None
            ),
            "llm_mode": metadata.llm_mode.value if metadata is not None else None,
            "duration_seconds": (
                metadata.duration_seconds if metadata is not None else None
            ),
            "recording_source_run_id": (
                metadata.recording_source_run_id if metadata is not None else None
            ),
            "finalized": False if metadata is not None else None,
            "ended_at": metadata.ended_at if metadata is not None else None,
            "end_reason": metadata.end_reason if metadata is not None else None,
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
