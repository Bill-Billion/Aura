from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from backend.api.routes import (
    configure_health_provider,
    configure_scenario_launcher,
    get_scenario_dirs,
    router as api_router,
)
from backend.api.access_control import (
    LOCAL_ORIGIN_REGEX,
    ResearchAccessError,
    authorize_run_launch,
    configured_allowed_origins,
    origin_is_trusted,
)
from backend.agents.arbiter import (
    ARBITER_ID,
    COORDINATION_DECISION_EVENT_TYPE,
    UI_ACTOR_ID,
    ArbitrationGate,
)
from backend.agents.contracts import AgentProposal, PriorityLevel
from backend.agents.llm_modes import llm_mode_health
from backend.agents.runtime import BaselinePolicyUnavailableError
from backend.agents.scene import (
    SCENE_APPLY_MESSAGE_TYPE,
    SceneApplyPayload,
    get_scene_definitions,
)
from backend.agents.types import AgentCommandProposal
from backend.api.ws import ConnectionManager
from backend.config.device_registry import (
    build_default_devices,
    build_default_rooms,
)
from backend.core.logging import log
from backend.core.local_env import load_local_env
from backend.engine.state import (
    AgentRuntimeState,
    Location3D,
    UserState,
    WorldState,
)
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.event_log import artifacts_enabled, read_run_metadata, run_dir
from backend.engine.run_manager import (
    RunMetadata,
    RunManager,
    RunProvenanceError,
    RunProvenanceErrorCode,
    baseline_policy_for_llm_mode,
)
from backend.engine.simulation import (
    PerturbationRuntimeUnavailableError,
    SimulationEngine,
)
from backend.engine.state_manager import DeltaChange, StateManager
from backend.execution.command import CommandSource, DeviceCommand, PublishEvent
from backend.execution.executor import CommandExecutor, FEEDBACK_EVENT_TYPE
from backend.models.schemas import (
    CmdDeviceControlPayload,
    ErrorMessage,
    RunScenarioPayload,
    ScenarioLaunchError,
    ScenarioLaunchErrorCode,
    WSMessage,
)
from backend.scenarios.apply import InitialStateApplyError, apply_initial_state
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import ScenarioLoadError, load_library
from backend.scenarios.runner import scenario_duration_seconds, scenario_world_mode
from backend.scenarios.spec import InitialState


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

manager = ConnectionManager()
event_bus = EventBus()
state_manager: StateManager | None = None
simulation_engine: SimulationEngine | None = None
# REST 与 WS 共用这把锁保护 active-check + reset + monitor 安装，杜绝两个并发请求
# 互相 supersede。匿名 ambient run 不占用这把产品层的 canonical run 槽位。
_scenario_launch_lock = asyncio.Lock()
_scenario_finalizer_task: asyncio.Task[None] | None = None


@dataclass(frozen=True, slots=True)
class _ScenarioLaunchRecord:
    """Process-local result for one client launch intention.

    The metadata object is deliberately retained rather than copying the first
    201 response: ``RunManager.end_run`` finalizes that same object in place, so
    a retry whose original response was lost can recover the final state without
    creating a second paid/recorded run.
    """

    fingerprint: tuple[str, int | None, str, str | None]
    metadata: RunMetadata
    mode: str


_SCENARIO_LAUNCH_IDEMPOTENCY_LIMIT = 1024
_scenario_launch_idempotency: OrderedDict[str, _ScenarioLaunchRecord] = OrderedDict()
load_local_env()


# WS 层结构性错误码，与 §10.2 命令失败码正交：这些描述"消息本身不合法"，
# 一旦命令成形，错误码一律取 CommandErrorCode（executor 拥有）。S5 可观测性 UI 直接消费。
WS_ERROR_MALFORMED_MESSAGE = "malformed_message"
WS_ERROR_INVALID_PAYLOAD = "invalid_payload"
WS_ERROR_INVALID_DEVICE_COMMAND = "invalid_device_command"
WS_ERROR_UNKNOWN_SCENE = "unknown_scene"
WS_ERROR_UNSUPPORTED_MESSAGE_TYPE = "unsupported_message_type"
WS_ERROR_INTERNAL = "internal_error"
WS_ERROR_RESEARCH_RUN_LOCKED = "research_run_locked"

# Canonical scenario artifacts are compared under a fixed scenario/seed/policy
# contract.  Accepting any of these interactive commands from another tab while
# the run is active would change the world or clock without changing provenance,
# producing a trace that looks reproducible but is not.
_RESEARCH_RUN_MUTATION_MESSAGES = frozenset(
    {
        "CMD_DEVICE_CONTROL",
        SCENE_APPLY_MESSAGE_TYPE,
        "CMD_SIM_START",
        "CMD_SIM_PAUSE",
        "CMD_SIM_RESET",
        "CMD_SIM_SPEED",
        "CMD_SIM_MODE",
    }
)
_AMBIENT_RUN_REQUIRED_MESSAGES = frozenset(
    {
        "CMD_DEVICE_CONTROL",
        SCENE_APPLY_MESSAGE_TYPE,
        "CMD_SIM_SPEED",
        "CMD_SIM_MODE",
    }
)


async def _broadcast_sim_event(event: SimEvent) -> SimEvent:
    async def broadcast_before_fan_out(visible: SimEvent) -> None:
        await manager.broadcast(
            WSMessage(type="SIM_EVENT", payload=visible.model_dump())
        )

    return await event_bus.publish_visible(
        event,
        before_fan_out=broadcast_before_fan_out,
    )


async def _send_ws_error(
    ws: WebSocket,
    code: str,
    message: str,
    details: dict[str, object] | None = None,
) -> None:
    await manager.send(
        ws,
        WSMessage(
            type="ERROR",
            payload=ErrorMessage(code=code, message=message, details=details).model_dump(),
        ),
    )


def _simulation_health() -> dict[str, object]:
    if simulation_engine is None:
        return {
            "run_id": None,
            "scenario_id": None,
            "run_artifact_dir": None,
            "baseline_policy": None,
            "llm_mode": None,
            "duration_seconds": None,
            "is_running": False,
            "mode": "observe",
            "speed": 1.0,
            "wall_tick_ms": 2000,
            "simulated_dt_seconds": 10.0,
        }

    run_id = simulation_engine.run_id
    metadata = simulation_engine.run_manager.current
    return {
        # §11/§18：健康检查即可回答"当前在跑哪个 run / 哪个场景"，
        # 不必等 /api/runs 才能定位一份事件流的归属。
        "run_id": run_id,
        "scenario_id": simulation_engine.scenario_id,
        # 工件落在哪：uvicorn 的工作目录随启动方式漂移，直接把绝对路径报出来，
        # 免得研究者去猜 events.jsonl 到底写进了哪个 data/runs/。
        "run_artifact_dir": (
            str(run_dir(run_id)) if run_id is not None and artifacts_enabled() else None
        ),
        "baseline_policy": (
            metadata.baseline_policy.value
            if metadata is not None and metadata.baseline_policy is not None
            else None
        ),
        "llm_mode": metadata.llm_mode.value if metadata is not None else None,
        "duration_seconds": metadata.duration_seconds if metadata is not None else None,
        "is_running": simulation_engine.is_running,
        "mode": simulation_engine.mode,
        "speed": simulation_engine.speed,
        "wall_tick_ms": simulation_engine.wall_tick_ms,
        "simulated_dt_seconds": simulation_engine.simulated_dt_seconds,
    }


def _llm_health() -> dict[str, object]:
    """§11.1：健康检查必须回答"这台实例现在跑的是哪种模式"。

    ``mode`` / ``benchmark_safe``（以及 replay 时的 ``recordings_path``）由
    :func:`llm_mode_health` 提供，与启动日志、run 工件同一份判据——
    只报 provider/model/configured 的话，研究者无法从运行中的实例上区分
    live / mocked / recorded / rule_based，而这正是模式系统存在的理由
    （DECISION #7：只有非 live 的模式能拿去做 benchmark 声明）。
    """

    if simulation_engine is None:
        return {**llm_mode_health(None), "configured": False}

    runtime = simulation_engine.agent_runtime
    return {
        **llm_mode_health(runtime.llm_provider),
        "configured": runtime.is_provider_configured,
    }


def _runtime_health() -> dict[str, object]:
    return {
        "status": "ok",
        "simulation": _simulation_health(),
        "llm": _llm_health(),
    }


def _collecting_publish(collected: list[DeltaChange]) -> PublishEvent:
    """给 executor 的 publish 包装：照常外发事件，顺手把反馈里的 delta 攒起来。

    房间光照重算已随 executor 的 room_light_effect 统一（main.py 那份重复判定随之退役），
    收集在这里做是为了把一条 WS 消息的所有命令的 delta 聚合成单条 STATE_DELTA 广播，
    保持前端"一条命令一次状态刷新"的既有口径。
    """

    async def publish(event: SimEvent) -> SimEvent:
        published = await _broadcast_sim_event(event)
        if published.event_type == FEEDBACK_EVENT_TYPE:
            collected.append(DeltaChange.model_validate(published.data))
        return published

    return publish


def _resolve_command_executor() -> CommandExecutor:
    """取引擎持有的那台唯一 executor（S1 review finding-8）。

    每条入站消息新造一台会让 ``_pending`` 注册表没有生产寿命：reset 取消不到在飞命令、
    后一条命令也看不见前一条（跨消息取代不成立）。引擎在 CMD_SIM_RESET 时把它换绑到
    新世界，因此这里每次现取、绝不缓存。引擎尚未起来（lifespan 之外）才临时兜底一台。
    """

    assert state_manager is not None
    if simulation_engine is not None:
        return simulation_engine.command_executor
    return CommandExecutor(state_manager)


def _resolve_arbitration_gate() -> ArbitrationGate:
    """取 runtime 持有的那台仲裁门（UI 腿与 agent 腿必须共用同一台）。

    共用是硬要求：用户占用登记在这台门上，agent episode 的仲裁又从这台门读占用。
    各持一台的话，"用户覆盖 agent" 会重新退回审计里那种名义状态——两边根本看不见对方。
    引擎没起来（lifespan 之外的兜底路径）才临时造一台。
    """

    if simulation_engine is not None:
        return simulation_engine.agent_runtime.arbitration_gate
    return ArbitrationGate()


def _ui_command_targets(payload: CmdDeviceControlPayload) -> list[tuple[str, Any]]:
    """四种入站格式（turn_on / turn_off / set_state / 旧 property-value）归一为 (能力, 值) 对。"""

    if payload.action == "turn_on":
        return [("power", True)]
    if payload.action == "turn_off":
        return [("power", False)]
    if payload.action == "set_state":
        return list(payload.params.items())
    if payload.property:
        # 旧格式 property 是点路径（power / extra.brightness），能力名去掉 extra. 前缀。
        return [(payload.property.removeprefix("extra."), payload.value)]
    return []


# ---------------------------------------------------------------------------
# Default state initialisation
# ---------------------------------------------------------------------------


def _init_default_state(initial_state: InitialState | None = None) -> StateManager:
    """Build the default apartment_v1 world state.

    ``initial_state`` 非空时在默认世界之上叠加一份场景覆盖（§5.2），走
    :func:`backend.scenarios.apply.apply_initial_state`——每一条覆盖都是一条
    caused_by="scenario_loader" 的 delta（§2.2 可归因），而不是无出处的 setattr。
    S2-T6 的场景 runner 用这一个入口拿到"默认世界 + 场景起始状态"，不要各自再拼一遍；
    覆盖不自洽（未知设备/房间、occupancy 与在场人员矛盾）会抛 InitialStateApplyError，
    起始状态错了的 run 不该被放出去跑。
    """
    world = WorldState(scene_id="apartment_v1")

    # Rooms
    rooms = build_default_rooms()
    world.rooms = rooms  # type: ignore[assignment]

    # Devices
    world.devices = build_default_devices()

    # Users
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="living_room"),
            activity="idle",
        ),
    }
    # Mark living_room as occupied
    world.rooms["living_room"].occupancy = True
    world.rooms["living_room"].persons = ["user_01"]

    # Agents
    world.agents = {
        "lighting_agent": AgentRuntimeState(
            id="lighting_agent",
            name="Lighting Agent",
            status="idle",
        ),
        "hvac_agent": AgentRuntimeState(
            id="hvac_agent",
            name="HVAC Agent",
            status="idle",
        ),
    }

    manager = StateManager(world)
    if initial_state is not None:
        apply_initial_state(manager, initial_state)
    return manager


# ---------------------------------------------------------------------------
# 场景启动（S2 review major-1）
# ---------------------------------------------------------------------------


async def _cancel_scenario_finalizer() -> None:
    """取消旧 monitor；调用方必须持有 ``_scenario_launch_lock``。"""

    global _scenario_finalizer_task
    task = _scenario_finalizer_task
    _scenario_finalizer_task = None
    if task is None or task.done() or task is asyncio.current_task():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _scenario_launch_fingerprint(
    payload: RunScenarioPayload,
) -> tuple[str, int | None, str, str | None]:
    """Return the exact semantic request bound to an idempotency key."""

    return (
        payload.scenario_id,
        payload.seed,
        payload.baseline_policy.value,
        payload.recording_source_run_id,
    )


def _scenario_run_response(
    metadata: RunMetadata,
    *,
    mode: str,
    engine: SimulationEngine,
) -> dict[str, Any]:
    """Build both initial and replayed launch responses from live metadata."""

    active = engine.run_manager.current
    return {
        **metadata.model_dump(mode="json"),
        "finalized": metadata.ended_at is not None,
        "is_running": (
            metadata.ended_at is None
            and active is not None
            and active.run_id == metadata.run_id
            and engine.is_running
        ),
        "mode": mode,
        "run_artifact_dir": (
            str(run_dir(metadata.run_id)) if artifacts_enabled() else None
        ),
    }


def _remember_scenario_launch(
    key: str,
    *,
    fingerprint: tuple[str, int | None, str, str | None],
    metadata: RunMetadata,
    mode: str,
) -> None:
    _scenario_launch_idempotency[key] = _ScenarioLaunchRecord(
        fingerprint=fingerprint,
        metadata=metadata,
        mode=mode,
    )
    _scenario_launch_idempotency.move_to_end(key)
    while len(_scenario_launch_idempotency) > _SCENARIO_LAUNCH_IDEMPOTENCY_LIMIT:
        _scenario_launch_idempotency.popitem(last=False)


def _terminal_simulation_status(
    engine: SimulationEngine,
    metadata: RunMetadata | None,
) -> dict[str, Any]:
    status = engine.build_simulation_status_payload()
    if metadata is None:
        return status
    status.update(
        {
            "run_id": metadata.run_id,
            "scenario_id": metadata.scenario_id,
            "seed": metadata.seed,
            "baseline_policy": (
                metadata.baseline_policy.value
                if metadata.baseline_policy is not None
                else None
            ),
            "llm_mode": metadata.llm_mode.value,
            "duration_seconds": metadata.duration_seconds,
            "recording_source_run_id": metadata.recording_source_run_id,
            "finalized": True,
            "ended_at": metadata.ended_at,
            "end_reason": metadata.end_reason,
            "is_running": False,
        }
    )
    return status


async def _finalize_scenario_run(run_id: str, duration_seconds: float) -> None:
    """按模拟时长收尾 canonical run；只允许结束自己捕获的 run_id。"""

    global _scenario_finalizer_task
    this_task = asyncio.current_task()
    end_reason = "completed"
    terminal_status: dict[str, Any] | None = None
    try:
        while True:
            engine = simulation_engine
            if engine is None or engine.run_id != run_id:
                return
            if engine.last_engine_error is not None:
                end_reason = "engine_error"
                break
            # SimulationEngine 在第一个覆盖 duration 的 tick 收尾时原子地置下
            # timer stop 信号。finalizer 必须同时观察到该信号与 handler 退出；仅轮询
            # sim_time 会在 pause 抢锁前留下启动下一拍的窗口。
            if (
                engine.sim_time_s >= duration_seconds
                and not engine.timer.is_running
                and not engine._is_processing_timer_tick
            ):
                break
            poll_s = min(max(float(engine.timer.tick_interval) / 4.0, 0.01), 0.25)
            await asyncio.sleep(poll_s)

        async with _scenario_launch_lock:
            engine = simulation_engine
            if engine is None or engine.run_id != run_id:
                return
            await engine.pause()
            settled = await engine.agent_runtime.wait_for_idle(timeout=30.0)
            cleanup_reason = (
                "run_duration_elapsed" if end_reason == "completed" else end_reason
            )
            if not settled:
                await engine.agent_runtime.cancel_active_episodes(cleanup_reason)
            await engine.command_executor.cancel_pending(cleanup_reason)
            engine.command_executor.device_runtime.reset()
            if engine.run_id != run_id:
                return
            metadata = engine.run_manager.end_run(end_reason)
            terminal_status = _terminal_simulation_status(engine, metadata)
            log.info(
                "scenario_run_finalized",
                run_id=run_id,
                scenario_id=metadata.scenario_id if metadata is not None else None,
                duration_seconds=duration_seconds,
                end_reason=end_reason,
            )
        if terminal_status is not None:
            await manager.broadcast(
                WSMessage(type="SIMULATION_STATUS", payload=terminal_status)
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # A background monitor must never strand the canonical slot.  The
        # original failure may have occurred in pause, idle draining, command
        # cancellation, artifact close, or status publication; all are outside
        # the request that created the task, so without this recovery every
        # later launch would remain 409 until the process restarted.
        log.error(
            "scenario_run_finalization_failed",
            run_id=run_id,
            duration_seconds=duration_seconds,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        recovery_status: dict[str, Any] | None = None
        try:
            async with _scenario_launch_lock:
                engine = simulation_engine
                if engine is not None and engine.run_id == run_id:
                    failure = f"finalization_failed: {type(exc).__name__}: {exc}"
                    try:
                        engine._mark_run_artifact_invalid(run_id, failure)
                    except Exception as marker_exc:  # pragma: no cover - disk failure
                        log.error(
                            "scenario_finalization_failure_marker_failed",
                            run_id=run_id,
                            error=str(marker_exc),
                        )

                    # Retry the graceful path first.  If the failing operation
                    # was persistent, force the clock/world flags down before
                    # releasing the run identity so stale work cannot publish
                    # into the next run.
                    try:
                        await engine.pause()
                    except Exception as pause_exc:
                        log.error(
                            "scenario_finalization_pause_recovery_failed",
                            run_id=run_id,
                            error=str(pause_exc),
                        )
                        try:
                            await engine.timer.pause()
                        except Exception as timer_exc:  # pragma: no cover - defensive
                            log.error(
                                "scenario_finalization_timer_recovery_failed",
                                run_id=run_id,
                                error=str(timer_exc),
                            )
                        engine.is_running = False
                        engine.state_manager.world.is_running = False

                    for operation, name in (
                        (
                            lambda: engine.agent_runtime.cancel_active_episodes(
                                "scenario_finalization_failed"
                            ),
                            "episodes",
                        ),
                        (
                            lambda: engine.command_executor.cancel_pending(
                                "scenario_finalization_failed"
                            ),
                            "commands",
                        ),
                    ):
                        try:
                            await operation()
                        except Exception as cleanup_exc:  # pragma: no cover - defensive
                            log.error(
                                "scenario_finalization_cleanup_failed",
                                run_id=run_id,
                                cleanup=name,
                                error=str(cleanup_exc),
                            )

                    engine.command_executor.device_runtime.reset()

                    current = engine.run_manager.current
                    metadata: RunMetadata | None = None
                    if current is not None and current.run_id == run_id:
                        try:
                            metadata = engine.run_manager.end_run("finalization_failed")
                        except Exception as end_exc:  # pragma: no cover - defensive
                            log.error(
                                "scenario_finalization_end_recovery_failed",
                                run_id=run_id,
                                error=str(end_exc),
                            )
                            # The artifact wrapper calls the real RunManager
                            # before closing its writer.  If it failed earlier,
                            # clear the lifecycle directly and best-effort close
                            # the recorder so the service is never left locked.
                            metadata = current
                            if engine.run_manager.current is not None:
                                metadata = RunManager.end_run(
                                    engine.run_manager, "finalization_failed"
                                )
                            try:
                                engine.run_artifacts._close_writer()
                            except Exception as close_exc:
                                log.error(
                                    "scenario_finalization_writer_recovery_failed",
                                    run_id=run_id,
                                    error=str(close_exc),
                                )
                    recovery_status = _terminal_simulation_status(engine, metadata)
        except Exception as recovery_exc:  # pragma: no cover - last-resort logging
            log.error(
                "scenario_run_finalization_recovery_failed",
                run_id=run_id,
                error_type=type(recovery_exc).__name__,
                error=str(recovery_exc),
            )
        if recovery_status is not None:
            try:
                await manager.broadcast(
                    WSMessage(type="SIMULATION_STATUS", payload=recovery_status)
                )
            except Exception as broadcast_exc:  # pragma: no cover - transport boundary
                log.error(
                    "scenario_finalization_status_broadcast_failed",
                    run_id=run_id,
                    error=str(broadcast_exc),
                )
    finally:
        if _scenario_finalizer_task is this_task:
            _scenario_finalizer_task = None


async def start_scenario_run(payload: RunScenarioPayload) -> dict[str, Any]:
    """在**跑着的服务端**上按场景开一个新 run —— REST 与 WS 共用的唯一实现。

    S2 收工时 ``bind_generation_sources`` 只有 ScenarioRunner 一个调用方：活着的服务端
    永远装不上 §4.5 三条产线，tick 里走的是未打标的旧 ``user_sim`` 分支，于是
    "前端连上后跑一个场景 run"这条手动验收根本无从演示，S5 也没有东西可驱动。

    这条路径本身**不新写任何生命周期逻辑**：换 run / 取消在飞 episode 与命令 / 清事件
    历史 / 摆 initial_state / 装产线全在 :meth:`SimulationEngine.reset` 里（S0-5 那条
    cancel-before-swap 纪律的唯一实现）。停止与重置同理走既有的 ``CMD_SIM_PAUSE`` /
    ``CMD_SIM_RESET``——第二套取消机制就是第二套语义。

    失败一律抛 :class:`ScenarioLaunchError`（结构化、REST/WS 同码），但**并非全部发生在
    换世界之前**，别按"失败即无副作用"来用：

    - ``ENGINE_UNAVAILABLE`` / ``SCENARIO_NOT_FOUND`` / ``SCENARIO_LIBRARY_INVALID``
      在解析阶段抛出，旧 run 原封不动——拼错 scenario_id 不会拆掉正在跑的 run。
    - ``INITIAL_STATE_INVALID`` 由 reset 内部的 initial_state 应用阶段抛出，此时旧 run
      已被取消、世界已被换掉。调用方要把它当作"新 run 起了一半失败"，而不是 no-op。
    """

    global state_manager, _scenario_finalizer_task

    if simulation_engine is None or state_manager is None:
        raise ScenarioLaunchError(
            ScenarioLaunchErrorCode.ENGINE_UNAVAILABLE,
            "仿真引擎尚未启动，无法运行场景",
            details={"scenario_id": payload.scenario_id},
        )

    async with _scenario_launch_lock:
        engine = simulation_engine
        assert engine is not None
        launch_fingerprint = _scenario_launch_fingerprint(payload)
        if payload.idempotency_key is not None:
            previous = _scenario_launch_idempotency.get(payload.idempotency_key)
            if previous is not None:
                if previous.fingerprint != launch_fingerprint:
                    raise ScenarioLaunchError(
                        ScenarioLaunchErrorCode.IDEMPOTENCY_CONFLICT,
                        "idempotency_key 已绑定到另一组场景启动参数",
                        details={
                            "idempotency_key": payload.idempotency_key,
                            "original_run_id": previous.metadata.run_id,
                        },
                    )
                _scenario_launch_idempotency.move_to_end(payload.idempotency_key)
                return _scenario_run_response(
                    previous.metadata,
                    mode=previous.mode,
                    engine=engine,
                )

        current = engine.run_manager.current
        if current is not None and current.scenario_id is not None:
            raise ScenarioLaunchError(
                ScenarioLaunchErrorCode.RUN_ALREADY_ACTIVE,
                "已有 canonical scenario run 尚未 finalized",
                details={
                    "active_run_id": current.run_id,
                    "scenario_id": current.scenario_id,
                },
            )

        # 先完成所有可失败的场景/provider 校验；未通过时匿名 ambient run 保持原样。
        try:
            library = load_library(get_scenario_dirs())
        except ScenarioLoadError as exc:
            raise ScenarioLaunchError(
                ScenarioLaunchErrorCode.SCENARIO_LIBRARY_INVALID,
                f"场景库加载失败：{exc}",
                details=exc.to_dict(),
            ) from exc
        spec = library.get(payload.scenario_id)
        if spec is None:
            raise ScenarioLaunchError(
                ScenarioLaunchErrorCode.SCENARIO_NOT_FOUND,
                f"场景 {payload.scenario_id!r} 不在已加载的场景库中",
                details={
                    "scenario_id": payload.scenario_id,
                    "known_ids": sorted(library),
                },
            )
        try:
            selection = engine.agent_runtime.prepare_baseline_policy(
                payload.baseline_policy,
                recording_source_run_id=payload.recording_source_run_id,
            )
        except BaselinePolicyUnavailableError as exc:
            reason_code = str(exc.details.get("reason_code") or "")
            launch_code = {
                "recording_source_not_found": ScenarioLaunchErrorCode.RECORDING_SOURCE_NOT_FOUND,
                "recording_source_not_finalized": ScenarioLaunchErrorCode.RECORDING_SOURCE_NOT_FINALIZED,
                "recording_source_mode_mismatch": ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH,
                "recording_artifact_missing": ScenarioLaunchErrorCode.RECORDING_SOURCE_INVALID,
                "recording_artifact_invalid": ScenarioLaunchErrorCode.RECORDING_SOURCE_INVALID,
                "recording_artifact_empty": ScenarioLaunchErrorCode.RECORDING_SOURCE_INVALID,
            }.get(reason_code, ScenarioLaunchErrorCode.BASELINE_POLICY_UNAVAILABLE)
            raise ScenarioLaunchError(
                launch_code,
                exc.reason,
                details=exc.details,
            ) from exc
        if payload.recording_source_run_id is not None:
            source = read_run_metadata(payload.recording_source_run_id)
            target_seed = spec.seed if payload.seed is None else payload.seed
            if source.get("scenario_id") != spec.id:
                raise ScenarioLaunchError(
                    ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH,
                    "recorded 来源与目标场景不一致",
                    details={
                        "baseline_policy": payload.baseline_policy.value,
                        "reason_code": "recording_source_scenario_mismatch",
                        "recording_source_run_id": payload.recording_source_run_id,
                        "source_scenario_id": source.get("scenario_id"),
                        "target_scenario_id": spec.id,
                    },
                )
            if source.get("seed") != target_seed:
                raise ScenarioLaunchError(
                    ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH,
                    "recorded 来源与目标 seed 不一致",
                    details={
                        "baseline_policy": payload.baseline_policy.value,
                        "reason_code": "recording_source_seed_mismatch",
                        "recording_source_run_id": payload.recording_source_run_id,
                        "source_seed": source.get("seed"),
                        "target_seed": target_seed,
                    },
                )
            expected_contract_hash = scenario_contract_fingerprint(spec)
            if source.get("scenario_contract_hash") != expected_contract_hash:
                raise ScenarioLaunchError(
                    ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH,
                    "recorded 来源与当前场景评估契约不一致",
                    details={
                        "baseline_policy": payload.baseline_policy.value,
                        "reason_code": "recording_source_contract_mismatch",
                        "recording_source_run_id": payload.recording_source_run_id,
                        "source_scenario_contract_hash": source.get(
                            "scenario_contract_hash"
                        ),
                        "target_scenario_contract_hash": expected_contract_hash,
                    },
                )
            expected_agent_versions = {
                agent.agent_id: str(
                    getattr(agent, "agent_version", engine.run_manager.sim_version)
                )
                for agent in engine.agent_runtime.agents
            }
            if (
                source.get("sim_version") != engine.run_manager.sim_version
                or source.get("source_revision")
                != engine.run_manager.source_revision
                or source.get("agent_versions") != expected_agent_versions
            ):
                raise ScenarioLaunchError(
                    ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH,
                    "recorded 来源与当前仿真/Agent 版本不一致",
                    details={
                        "baseline_policy": payload.baseline_policy.value,
                        "reason_code": "recording_source_code_version_mismatch",
                        "recording_source_run_id": payload.recording_source_run_id,
                        "source_sim_version": source.get("sim_version"),
                        "target_sim_version": engine.run_manager.sim_version,
                        "source_revision": source.get("source_revision"),
                        "target_source_revision": engine.run_manager.source_revision,
                        "source_agent_versions": source.get("agent_versions"),
                        "target_agent_versions": expected_agent_versions,
                    },
                )

        duration = scenario_duration_seconds(spec)
        launch_state = _init_default_state()
        try:
            await engine.reset(
                new_state_manager=launch_state,
                scenario=spec,
                seed=payload.seed,
                scenario_dirs=get_scenario_dirs(),
                policy_selection=selection,
                duration_seconds=duration,
            )
        except RunProvenanceError as exc:
            code = (
                ScenarioLaunchErrorCode.SCENARIO_NOT_FOUND
                if exc.code is RunProvenanceErrorCode.SCENARIO_NOT_FOUND
                else ScenarioLaunchErrorCode.SCENARIO_LIBRARY_INVALID
            )
            raise ScenarioLaunchError(code, exc.message, details=exc.details) from exc
        except InitialStateApplyError as exc:
            raise ScenarioLaunchError(
                ScenarioLaunchErrorCode.INITIAL_STATE_INVALID,
                f"场景 {payload.scenario_id} 的 initial_state 无法一致地应用：{exc.message}",
                details={"scenario_id": payload.scenario_id, **exc.to_dict()},
            ) from exc
        except PerturbationRuntimeUnavailableError as exc:
            raise ScenarioLaunchError(
                ScenarioLaunchErrorCode.PERTURBATION_RUNTIME_UNAVAILABLE,
                exc.message,
                details=exc.to_dict()["details"],
            ) from exc
        except (TypeError, ValueError) as exc:  # validate_seed
            raise ScenarioLaunchError(
                ScenarioLaunchErrorCode.INVALID_SEED,
                f"seed 非法：{exc}",
                details={"scenario_id": payload.scenario_id, "seed": payload.seed},
            ) from exc

        state_manager = launch_state
        engine.mode = scenario_world_mode(spec.mode)
        await engine.start()

        metadata = engine.run_manager.current
        assert metadata is not None
        await _cancel_scenario_finalizer()
        _scenario_finalizer_task = asyncio.create_task(
            _finalize_scenario_run(metadata.run_id, duration),
            name=f"finalize-{metadata.run_id}",
        )
        launch_mode = engine.mode
        if payload.idempotency_key is not None:
            _remember_scenario_launch(
                payload.idempotency_key,
                fingerprint=launch_fingerprint,
                metadata=metadata,
                mode=launch_mode,
            )

    # 前端在一条消息里拿齐"世界换了"与"现在跑的是哪个 run"（SIMULATION_STATUS 已含
    # run_id/scenario_id），不为场景启动新增 WS 消息类型。
    await manager.broadcast(WSMessage(type="STATE_FULL", payload=state_manager.get_full_snapshot()))
    await manager.broadcast(
        WSMessage(
            type="SIMULATION_STATUS",
            payload=engine.build_simulation_status_payload(),
        )
    )

    log.info(
        "scenario_run_started",
        run_id=metadata.run_id,
        scenario_id=payload.scenario_id,
        seed=metadata.seed,
        baseline_policy=metadata.baseline_policy.value,
        llm_mode=metadata.llm_mode.value,
        duration_seconds=metadata.duration_seconds,
    )
    # ``metadata`` is the live object finalized in place.  Rebuild after the
    # (bounded) WS sends so a very short run cannot return a stale
    # ``finalized=false / is_running=true`` snapshot after it already ended.
    return _scenario_run_response(metadata, mode=launch_mode, engine=engine)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global state_manager, simulation_engine
    state_manager = _init_default_state()
    simulation_engine = SimulationEngine(
        event_bus=event_bus,
        state_manager=state_manager,
        connection_manager=manager,
    )
    log.info("app_started", scene=state_manager.world.scene_id)
    yield
    # Gracefully stop the simulation if running
    if simulation_engine is not None:
        async with _scenario_launch_lock:
            await _cancel_scenario_finalizer()
            await simulation_engine.close()
    log.info("app_shutdown")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="SmartHomeSim", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(configured_allowed_origins()),
    allow_origin_regex=LOCAL_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
configure_health_provider(_runtime_health)
# POST /api/runs 与 WS CMD_RUN_SCENARIO 共用同一条实现（routes 不能反向 import main）。
configure_scenario_launcher(start_scenario_run)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


async def _handle_device_control(ws: WebSocket, raw_payload: dict) -> None:
    """CMD_DEVICE_CONTROL：入站结构守卫 → 根事件 → CommandExecutor（spec §10 步骤 4-8）。

    审计必修①：UI 直控不再自己 apply_action/自己拼校验分支，与 agent 路径共用同一台
    executor（同一六级校验、同一十态生命周期、同一失败词表）。
    审计§六⑤：根 user.command 事件先于任何 STATE_DELTA 外发，前端拿到状态变更时因果头已在手。
    """

    assert state_manager is not None

    try:
        payload = CmdDeviceControlPayload.model_validate(raw_payload)
    except ValidationError as exc:
        await _send_ws_error(
            ws,
            WS_ERROR_INVALID_PAYLOAD,
            "设备控制载荷结构非法",
            details={
                "issues": [
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                ]
            },
        )
        return

    targets = _ui_command_targets(payload)
    if not targets:
        await _send_ws_error(
            ws,
            WS_ERROR_INVALID_DEVICE_COMMAND,
            "设备控制命令缺少可执行的 action 或 property",
            details={"device_id": payload.device_id, "action": payload.action},
        )
        return

    tick = int(state_manager.world.simulation_tick)
    root_event = event_bus.coerce_event(
        SimEvent(
            event_type="user.command",
            source="user_ui",
            timestamp=float(tick),
            wall_time=time.time(),
            priority=2,
            data={
                "message_type": "CMD_DEVICE_CONTROL",
                "device_id": payload.device_id,
                "action": payload.action or payload.property or "",
                "params": payload.params if payload.params else {"value": payload.value},
            },
        )
    )
    # Admission/盖章/WS 可见性由总线统一完成；hook 仍保证根事件先于同步订阅者
    # 派生的动作/反馈出现在前端。
    root_event = await _broadcast_sim_event(root_event)

    collected: list[DeltaChange] = []
    # 共用引擎那台 executor；本次调用的 publish 包装按次传入（delta 聚合仍是每条消息一份）。
    executor = _resolve_command_executor()
    gate = _resolve_arbitration_gate()
    gate.bind(executor)
    publish = _collecting_publish(collected)

    # —— 用户命令进仲裁（S3-T5）——————————————————————————————————
    # 审计 §六：过去 main.py 直接落地 UI 命令、runtime 又显式跳过 CMD_DEVICE_CONTROL，
    # 于是 §9.1 的 explicit_user 档从来没被走过——"用户覆盖 agent"只是名义上的。
    # 现在 UI 命令以 explicit_user 档进同一台仲裁器，并发出与 agent episode 同格式的
    # 一条 reasoning.coordination_decision。
    #
    # 按 decision #4，它**不**触发一轮完整 agent 推理（即时反馈延迟不能被 LLM 拖长）：
    # 仲裁在这里是同步纯函数，只多一条事件。
    user_proposal = AgentProposal(
        agent_id=UI_ACTOR_ID,
        agent_role="user",
        intent=f"user {payload.action or payload.property or 'command'}",
        priority=PriorityLevel.EXPLICIT_USER,
        confidence=1.0,
        commands=[
            AgentCommandProposal(
                device_id=payload.device_id,
                property=capability if capability == "power" else f"extra.{capability}",
                value=value,
                reason=f"ui {payload.action or payload.property or 'command'}",
            )
            for capability, value in targets
        ],
    )
    arbitration = gate.arbiter.resolve(
        [user_proposal],
        root_event,
        state_manager.world,
        user_claims=gate.claims_since(float(root_event.wall_time or 0.0)),
    )
    coordination_event = await publish(
        SimEvent(
            event_type=COORDINATION_DECISION_EVENT_TYPE,
            source=ARBITER_ID,
            timestamp=float(tick),
            wall_time=time.time(),
            correlation_id=root_event.correlation_id,
            causal_parent=root_event.event_id,
            priority=2,
            data=arbitration.event_data(),
        )
    )

    commands = [
        DeviceCommand(
            source=CommandSource.UI,
            device_id=approved.device_id,
            capability=approved.capability,
            value=approved.value,
            reason=approved.reason,
            correlation_id=root_event.correlation_id,
            causal_parent=coordination_event.event_id,
            issued_tick=tick,
            priority=2,
        )
        for approved in arbitration.approved_commands
    ]
    # pre_submit 是 S1 预留的那个接缝（出厂 no-op）；仲裁门在这里登记 explicit_user 占用，
    # 并点名要取代的在飞目标——包括还停在仲裁窗口里等批准的 agent 命令。
    records = await executor.submit_batch(
        commands, tick=tick, publish=publish, pre_submit=gate.pre_submit
    )

    if collected:
        await manager.broadcast(
            WSMessage(
                type="STATE_DELTA",
                payload={"deltas": [delta.model_dump() for delta in collected]},
            )
        )

    # 被仲裁否掉的用户命令**不会**静默消失。今天 explicit_user 档豁免全部单边拒绝、
    # 同一提案者内部也不判互斥，因此这条分支正常跑不到；留着是因为"命令没执行却什么都
    # 不说"正是 S1 全程根治的那类缺陷——将来若新增一条对用户也生效的仲裁规则，
    # 用户会立刻收到解释，而不是面对一个没反应的开关。
    for rejected in arbitration.rejected_commands:
        await _send_ws_error(
            ws,
            WS_ERROR_INVALID_DEVICE_COMMAND,
            rejected.rejection_reason,
            details={
                "device_id": rejected.device_id,
                "capability": rejected.capability,
                "conflict_class": rejected.conflict_class.value,
            },
        )

    for record in records:
        if record.failure_code is None:
            continue
        # 失败码直接取 §10.2 词表，和 agent 路径同码（审计必修①奇偶校验）。
        await _send_ws_error(
            ws,
            record.failure_code,
            record.detail or "设备命令未能执行",
            details={
                "device_id": record.command.device_id,
                "capability": record.command.capability,
                "command_id": record.command.command_id,
                "status": record.status.value,
            },
        )


async def _handle_scene_apply(ws: WebSocket, raw_payload: dict) -> None:
    """CMD_SCENE_APPLY：一条消息 = 一次场景切换（S3-T4 的前门）。

    被推翻的现状：SceneSelector.vue 在浏览器里循环发 2×N 条 ``CMD_DEVICE_CONTROL``。
    后端因此**看不见**"这是一次场景切换"——事件流里只有 N 条互不相干的直控，可观测性
    面板拼不出一条完整因果链，而"看得见 Agent 的推理链路"正是这个平台的产品本身。
    场景语义同时被锁在 .vue 的 switch 里：headless 脚本与 S4 评估器复用不了，命令也
    绕过编排与仲裁，§9 的优先级全序对场景完全不生效。

    这里**只做翻译**：消息 → 一条带 ``scene_id`` 的 ``user.command`` 根事件。展开成哪些
    设备命令是 :class:`~backend.agents.scene.SceneAgent` 的事（§9.1 ambience 档），随后
    与其他 agent 走同一条编排 → 仲裁 → CommandExecutor 的腿。所以本函数**不**碰
    executor、也不自己 apply——那正是它要取代的形态。

    与 :func:`_handle_device_control` 的两处刻意不同：

    - **不同步执行**。直控要求即时反馈，故那条路径同步跑完仲裁与 executor；场景是一轮
      真正的 agent episode（可能带 LLM），由 runtime 在后台跑完并自行广播事件与增量。
    - **不占 explicit_user 档**。场景是一整套氛围预设，理应让位于安全/安防/舒适；
      "用户点名某台设备"才是 explicit_user（见 SceneAgent.proposal_priority）。

    未知 ``scene_id`` 当场回 ERROR，而不是放一条只会 no-op 的 episode 出去：点错场景的
    人应该立刻知道，而不是盯着一个没反应的按钮去翻事件流。
    """

    assert state_manager is not None

    try:
        payload = SceneApplyPayload.model_validate(raw_payload)
    except ValidationError as exc:
        await _send_ws_error(
            ws,
            WS_ERROR_INVALID_PAYLOAD,
            "场景应用载荷结构非法",
            details={
                "issues": [
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                ]
            },
        )
        return

    library = get_scene_definitions()
    if library.get(payload.scene_id) is None:
        await _send_ws_error(
            ws,
            WS_ERROR_UNKNOWN_SCENE,
            f"未知场景 id '{payload.scene_id}'",
            details={
                "scene_id": payload.scene_id,
                # 点名已知场景：拼错一个 id 的人不必去翻后端 YAML 才知道有哪些。
                "known_scenes": sorted(library.scenes),
            },
        )
        return

    tick = int(state_manager.world.simulation_tick)
    root_event = event_bus.coerce_event(
        SimEvent(
            event_type="user.command",
            source="user_ui",
            timestamp=float(tick),
            wall_time=time.time(),
            priority=2,
            data={
                "message_type": SCENE_APPLY_MESSAGE_TYPE,
                "scene_id": payload.scene_id,
            },
        )
    )
    # 与设备直控共用同一 admission + before-fan-out WS 边界。
    await _broadcast_sim_event(root_event)


async def _handle_run_scenario(ws: WebSocket, raw_payload: dict) -> None:
    """CMD_RUN_SCENARIO：结构守卫 → :func:`start_scenario_run`。

    与 ``POST /api/runs`` 同一条实现、同一份错误词表；成功后的状态广播由启动路径统一发出
    （STATE_FULL + SIMULATION_STATUS），本处不另外回一条确认消息。
    """

    try:
        payload = RunScenarioPayload.model_validate(raw_payload)
    except ValidationError as exc:
        await _send_ws_error(
            ws,
            WS_ERROR_INVALID_PAYLOAD,
            "场景运行载荷结构非法",
            details={
                "issues": [
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    for error in exc.errors()
                ]
            },
        )
        return

    try:
        headers = getattr(ws, "headers", {})
        client = getattr(ws, "client", None)
        authorize_run_launch(
            payload,
            headers=headers,
            client_host=getattr(client, "host", None),
        )
    except ResearchAccessError as exc:
        await _send_ws_error(ws, exc.code, exc.message, details=exc.details)
        return

    try:
        await start_scenario_run(payload)
    except ScenarioLaunchError as exc:
        await _send_ws_error(ws, exc.code.value, exc.message, details=exc.details)


def _ambient_mutation_access_error(
    ws: WebSocket,
    engine: SimulationEngine | None,
) -> ResearchAccessError | None:
    """Authorize any interactive command that could reach a paid provider.

    Canonical launches have their own explicit policy check.  Ambient commands
    are less obvious: start/device/scene/timing can trigger the server-default
    provider on the next root/tick, including immediately after a canonical run
    when reset restores that default.  Derive the effective ambient policy and
    apply the same REST/WS capability boundary before any state mutation.
    """

    if engine is None:
        return None
    current = engine.run_manager.current
    if current is not None and current.scenario_id is None:
        policy = current.baseline_policy or baseline_policy_for_llm_mode(
            current.llm_mode
        )
        recording_source_run_id = current.recording_source_run_id
    else:
        selection = engine.agent_runtime.prepare_baseline_policy(None)
        policy = selection.baseline_policy
        recording_source_run_id = selection.recording_source_run_id

    try:
        headers = getattr(ws, "headers", {})
        client = getattr(ws, "client", None)
        authorize_run_launch(
            RunScenarioPayload(
                scenario_id="ambient_interaction",
                baseline_policy=policy,
                recording_source_run_id=recording_source_run_id,
            ),
            headers=headers,
            client_host=getattr(client, "host", None),
        )
    except ResearchAccessError as exc:
        return exc
    return None


async def _handle_ws_message(
    ws: WebSocket,
    raw: Any,
    *,
    _mutation_lock_held: bool = False,
) -> None:
    """处理单条入站消息；结构问题一律回 ERROR，不抛异常杀连接。"""

    global state_manager

    # The research-run guard and the mutation itself are one critical section.
    # Checking before an ``await`` is insufficient: a concurrent POST /api/runs
    # could otherwise reset into a canonical run while an ambient command is
    # paused mid-flight, then let that old command mutate the new world.
    if (
        not _mutation_lock_held
        and isinstance(raw, dict)
        and raw.get("type") in _RESEARCH_RUN_MUTATION_MESSAGES
    ):
        locked_run: dict[str, object] | None = None
        access_error: ResearchAccessError | None = None
        async with _scenario_launch_lock:
            engine = simulation_engine
            current_run = engine.run_manager.current if engine is not None else None
            if current_run is not None and current_run.scenario_id is not None:
                locked_run = {
                    "type": str(raw.get("type", "")),
                    "run_id": current_run.run_id,
                    "scenario_id": current_run.scenario_id,
                }
            else:
                access_error = _ambient_mutation_access_error(ws, engine)
                if access_error is None:
                    await _handle_ws_message(ws, raw, _mutation_lock_held=True)
        if locked_run is not None:
            # Keep the state decision atomic, but never wait on a slow/dead
            # client while holding the global launch/mutation boundary.
            await _send_ws_error(
                ws,
                WS_ERROR_RESEARCH_RUN_LOCKED,
                "canonical scenario run 运行期间不接受交互式状态变更",
                details=locked_run,
            )
        elif access_error is not None:
            await _send_ws_error(
                ws,
                access_error.code,
                access_error.message,
                details=access_error.details,
            )
        return

    if not isinstance(raw, dict):
        await _send_ws_error(ws, WS_ERROR_MALFORMED_MESSAGE, "消息必须是 JSON 对象")
        return

    msg_type = raw.get("type", "")
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        await _send_ws_error(
            ws,
            WS_ERROR_INVALID_PAYLOAD,
            "payload 必须是 JSON 对象",
            details={"type": str(msg_type)},
        )
        return

    engine = simulation_engine
    current_run = engine.run_manager.current if engine is not None else None
    if (
        engine is not None
        and current_run is None
        and msg_type in _AMBIENT_RUN_REQUIRED_MESSAGES
    ):
        # A finalized canonical writer is immutable and its EventBus context is
        # cleared. Continue interactive use in a fresh anonymous run before any
        # command can change the world, so STATE_DELTA and observability evidence
        # never diverge into an unowned run_id=None stream.
        await _cancel_scenario_finalizer()
        await engine.reset(new_state_manager=state_manager)
        current_run = engine.run_manager.current
        await manager.broadcast(
            WSMessage(type="STATE_FULL", payload=state_manager.get_full_snapshot())
        )
        await manager.broadcast(
            WSMessage(
                type="SIMULATION_STATUS",
                payload=engine.build_simulation_status_payload(),
            )
        )

    if msg_type == "CMD_DEVICE_CONTROL":
        await _handle_device_control(ws, payload)

    elif msg_type == SCENE_APPLY_MESSAGE_TYPE:
        await _handle_scene_apply(ws, payload)

    elif msg_type == "CMD_RUN_SCENARIO":
        await _handle_run_scenario(ws, payload)

    elif msg_type == "CMD_SIM_START":
        if simulation_engine is not None:
            # A canonical run becomes immutable at auto-finalize.  Starting the
            # wall clock afterwards must first open a fresh anonymous run; it may
            # never resume with ``RunManager.current is None`` or append events
            # under the finalized run's old EventBus context.
            if simulation_engine.run_manager.current is None:
                await _cancel_scenario_finalizer()
                await simulation_engine.reset(new_state_manager=state_manager)
            await simulation_engine.start()
            await manager.broadcast(
                WSMessage(
                    type="SIMULATION_STATUS",
                    payload=simulation_engine.build_simulation_status_payload(),
                )
            )

    elif msg_type == "CMD_SIM_PAUSE":
        if simulation_engine is not None:
            await simulation_engine.pause()
            await manager.broadcast(
                WSMessage(
                    type="SIMULATION_STATUS",
                    payload=simulation_engine.build_simulation_status_payload(),
                )
            )

    elif msg_type == "CMD_SIM_RESET":
        state_manager = _init_default_state()
        if simulation_engine is not None:
            # 引擎的 reset 负责：取消在飞 episode/命令 → 换世界 → 开新 run
            # （新 run_id + 清空事件历史，§11 + 审计发现③）。
            await _cancel_scenario_finalizer()
            await simulation_engine.reset(new_state_manager=state_manager)
        else:
            # 引擎未起（lifespan 之外的兜底路径）：run 上下文与历史仍必须切干净，
            # 否则重置前的事件会以同一个 run 的形态混进新世界。
            event_bus.set_run_context(None)
            event_bus.clear()
        full = state_manager.get_full_snapshot()
        await manager.broadcast(WSMessage(type="STATE_FULL", payload=full))
        if simulation_engine is not None:
            await manager.broadcast(
                WSMessage(
                    type="SIMULATION_STATUS",
                    payload=simulation_engine.build_simulation_status_payload(),
                )
            )

    elif msg_type == "CMD_SIM_SPEED":
        speed = payload.get("speed", 1.0)
        if simulation_engine is not None:
            simulation_engine.apply_legacy_speed(float(speed))
        await manager.broadcast(
            WSMessage(
                type="SIMULATION_STATUS",
                payload=(
                    simulation_engine.build_simulation_status_payload()
                    if simulation_engine is not None
                    else {"speed": float(speed)}
                ),
            )
        )

    elif msg_type == "CMD_SIM_MODE":
        mode = str(payload.get("mode", "observe"))
        if simulation_engine is not None:
            simulation_engine.mode = mode
            await manager.broadcast(
                WSMessage(
                    type="SIMULATION_STATUS",
                    payload=simulation_engine.build_simulation_status_payload(),
                )
            )

    elif msg_type == "HEARTBEAT_PONG":
        # 前端心跳应答（useWebSocket.ts 收到 PING 才发）。后端当前不发 PING，
        # 收到也只作保活，不能当未知类型回 ERROR，否则前端会被自己的心跳刷屏。
        return

    else:
        await _send_ws_error(
            ws,
            WS_ERROR_UNSUPPORTED_MESSAGE_TYPE,
            f"不支持的消息类型 {msg_type}",
            details={"type": str(msg_type)},
        )


@app.websocket("/ws/simulation")
async def ws_simulation(ws: WebSocket) -> None:
    assert state_manager is not None

    if not origin_is_trusted(ws.headers.get("origin")):
        # Reject before accept: an arbitrary web page must not gain a control
        # channel to a localhost simulator merely because it can reach the port.
        await ws.close(code=1008, reason="origin_not_allowed")
        return

    await manager.accept(ws)

    def initial_messages() -> list[WSMessage]:
        # STATE_FULL describes the world, not the experiment that owns it.  Build
        # both messages only after the socket is registered so later broadcasts
        # queue behind this batch instead of racing ahead of the snapshot.
        messages = [
            WSMessage(type="STATE_FULL", payload=state_manager.get_full_snapshot())
        ]
        engine = simulation_engine
        current_run = engine.run_manager.current if engine is not None else None
        if (
            engine is not None
            and current_run is not None
            and current_run.scenario_id is not None
        ):
            messages.append(
                WSMessage(
                    type="SIMULATION_STATUS",
                    payload=engine.build_simulation_status_payload(),
                )
            )
        return messages

    # Registration and snapshot/status capture share the canonical launch lock;
    # the ordered network writes happen only after that global lock is released.
    # Per-socket serialization still queues later broadcasts behind the pair.
    initialized = await manager.initialize(
        ws,
        initial_messages_factory=initial_messages,
        registration_lock=_scenario_launch_lock,
    )
    if not initialized:
        await manager.close(ws, code=1011)
        return

    try:
        while True:
            # 逐消息 try/except：一条坏消息（非法 JSON / 结构错 / 处理时内部异常）只换来一条
            # ERROR，绝不落入外层 except 把整条连接拆掉——观察者会话不该被单条坏帧终结。
            try:
                raw = await manager.receive_json(ws)
            except WebSocketDisconnect:
                raise
            except (ValueError, TypeError, KeyError):
                await _send_ws_error(
                    ws,
                    WS_ERROR_MALFORMED_MESSAGE,
                    "消息不是合法的 JSON 文本",
                )
                continue

            try:
                await _handle_ws_message(ws, raw)
            except WebSocketDisconnect:
                raise
            except Exception:
                log.exception("ws_message_error")
                await _send_ws_error(
                    ws,
                    WS_ERROR_INTERNAL,
                    "处理消息时发生内部错误",
                    details={"type": str(raw.get("type", "")) if isinstance(raw, dict) else ""},
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws_error")
    finally:
        await manager.close(ws)
