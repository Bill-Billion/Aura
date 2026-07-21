from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from backend.api.routes import configure_health_provider, router as api_router
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
from backend.engine.simulation import SimulationEngine
from backend.engine.state_manager import DeltaChange, StateManager
from backend.execution.command import CommandSource, DeviceCommand, PublishEvent
from backend.execution.executor import CommandExecutor, FEEDBACK_EVENT_TYPE
from backend.models.schemas import CmdDeviceControlPayload, ErrorMessage, WSMessage


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

manager = ConnectionManager()
event_bus = EventBus()
state_manager: StateManager | None = None
simulation_engine: SimulationEngine | None = None
load_local_env()


# WS 层结构性错误码，与 §10.2 命令失败码正交：这些描述"消息本身不合法"，
# 一旦命令成形，错误码一律取 CommandErrorCode（executor 拥有）。S5 可观测性 UI 直接消费。
WS_ERROR_MALFORMED_MESSAGE = "malformed_message"
WS_ERROR_INVALID_PAYLOAD = "invalid_payload"
WS_ERROR_INVALID_DEVICE_COMMAND = "invalid_device_command"
WS_ERROR_UNSUPPORTED_MESSAGE_TYPE = "unsupported_message_type"
WS_ERROR_INTERNAL = "internal_error"


async def _broadcast_sim_event(event: SimEvent) -> SimEvent:
    await event_bus.publish(event)
    await manager.broadcast(WSMessage(type="SIM_EVENT", payload=event.model_dump()))
    return event


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
            "is_running": False,
            "mode": "observe",
            "speed": 1.0,
            "wall_tick_ms": 2000,
            "simulated_dt_seconds": 10.0,
        }

    return {
        "is_running": simulation_engine.is_running,
        "mode": simulation_engine.mode,
        "speed": simulation_engine.speed,
        "wall_tick_ms": simulation_engine.wall_tick_ms,
        "simulated_dt_seconds": simulation_engine.simulated_dt_seconds,
    }


def _llm_health() -> dict[str, object]:
    if simulation_engine is None:
        return {
            "provider": "disabled",
            "model": "rule_based",
            "configured": False,
        }

    runtime = simulation_engine.agent_runtime
    provider = runtime.llm_provider
    return {
        "provider": getattr(provider, "provider_name", "disabled"),
        "model": getattr(provider, "model", "rule_based"),
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


def _init_default_state() -> StateManager:
    """Build the default apartment_v1 world state."""
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

    return StateManager(world)


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
        await simulation_engine.close()
    log.info("app_shutdown")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="SmartHomeSim", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
configure_health_provider(_runtime_health)


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
    # 先广播再入总线：根事件必须是这条命令外发的第一条 WS 消息（修顺序倒置）。
    await manager.broadcast(WSMessage(type="SIM_EVENT", payload=root_event.model_dump()))
    await event_bus.publish(root_event)

    collected: list[DeltaChange] = []
    # 共用引擎那台 executor；本次调用的 publish 包装按次传入（delta 聚合仍是每条消息一份）。
    executor = _resolve_command_executor()
    commands = [
        DeviceCommand(
            source=CommandSource.UI,
            device_id=payload.device_id,
            capability=capability,
            value=value,
            reason=f"ui {payload.action or payload.property or 'command'}",
            correlation_id=root_event.correlation_id,
            causal_parent=root_event.event_id,
            issued_tick=tick,
            priority=2,
        )
        for capability, value in targets
    ]
    records = await executor.submit_batch(
        commands, tick=tick, publish=_collecting_publish(collected)
    )

    if collected:
        await manager.broadcast(
            WSMessage(
                type="STATE_DELTA",
                payload={"deltas": [delta.model_dump() for delta in collected]},
            )
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


async def _handle_ws_message(ws: WebSocket, raw: Any) -> None:
    """处理单条入站消息；结构问题一律回 ERROR，不抛异常杀连接。"""

    global state_manager

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

    if msg_type == "CMD_DEVICE_CONTROL":
        await _handle_device_control(ws, payload)

    elif msg_type == "CMD_SIM_START":
        if simulation_engine is not None:
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
            await simulation_engine.reset(new_state_manager=state_manager)
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

    full_state = state_manager.get_full_snapshot()
    await manager.connect(ws, full_state)

    try:
        while True:
            # 逐消息 try/except：一条坏消息（非法 JSON / 结构错 / 处理时内部异常）只换来一条
            # ERROR，绝不落入外层 except 把整条连接拆掉——观察者会话不该被单条坏帧终结。
            try:
                raw = await ws.receive_json()
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
        manager.disconnect(ws)
    except Exception:
        log.exception("ws_error")
        manager.disconnect(ws)
