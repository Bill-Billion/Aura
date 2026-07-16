from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import configure_health_provider, router as api_router
from backend.api.ws import ConnectionManager
from backend.config.device_registry import (
    build_default_devices,
    build_default_rooms,
    validate_device_command,
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
from backend.engine.state_manager import StateManager
from backend.models.schemas import ErrorMessage, WSMessage
from backend.simulators.environment import calculate_room_light_level


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

manager = ConnectionManager()
event_bus = EventBus()
state_manager: StateManager | None = None
simulation_engine: SimulationEngine | None = None
load_local_env()


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


def _command_affects_room_light(
    device,
    *,
    action: str,
    params: dict[str, object],
    property_path: str,
) -> bool:
    normalized_property = property_path.removeprefix("extra.")

    if device.type == "light":
        return (
            action in {"turn_on", "turn_off"}
            or "brightness" in params
            or normalized_property in {"power", "brightness"}
        )

    if device.type == "curtain":
        return "open_percent" in params or normalized_property == "open_percent"

    return False


def _append_room_light_feedback(device, deltas: list) -> None:
    assert state_manager is not None
    room_id = device.location.room
    if room_id not in state_manager.world.rooms:
        return

    deltas.extend(
        state_manager.apply_path_update(
            caused_by="user",
            path=f"rooms[{room_id}].light_level",
            new_value=calculate_room_light_level(state_manager.world, room_id),
            reason="apply device light feedback",
        )
    )


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


@app.websocket("/ws/simulation")
async def ws_simulation(ws: WebSocket) -> None:
    global state_manager, simulation_engine
    assert state_manager is not None

    full_state = state_manager.get_full_snapshot()
    await manager.connect(ws, full_state)

    try:
        while True:
            raw = await ws.receive_json()

            msg_type = raw.get("type", "")
            payload = raw.get("payload", {})

            if msg_type == "CMD_DEVICE_CONTROL":
                device_id = payload.get("device_id", "")
                action = payload.get("action", "")
                params = payload.get("params", {})
                # Legacy format support
                prop = payload.get("property", "")
                value = payload.get("value")

                deltas: list = []
                device = state_manager.world.devices.get(device_id) if device_id else None
                if device_id and device is None:
                    await _send_ws_error(
                        ws,
                        "UNKNOWN_DEVICE",
                        f"设备 {device_id} 不存在",
                        details={"device_id": device_id},
                    )
                    continue

                if device and action:
                    error_code, error_message = validate_device_command(
                        device,
                        action=action,
                        params=params,
                    )
                    if error_code:
                        await _send_ws_error(
                            ws,
                            error_code,
                            error_message,
                            details={"device_id": device_id, "action": action},
                        )
                        continue

                    # action/params format (from frontend UI)
                    if action == "turn_on":
                        deltas = state_manager.apply_action(
                            "user", device_id, "power", True
                        )
                    elif action == "turn_off":
                        deltas = state_manager.apply_action(
                            "user", device_id, "power", False
                        )
                    elif action == "set_state" and params:
                        for k, v in params.items():
                            deltas.extend(
                                state_manager.apply_action(
                                    "user", device_id, f"extra.{k}", v
                                )
                            )
                    if _command_affects_room_light(
                        device,
                        action=action,
                        params=params,
                        property_path="",
                    ):
                        _append_room_light_feedback(device, deltas)
                elif device and prop and value is not None:
                    error_code, error_message = validate_device_command(
                        device,
                        action="",
                        property_path=prop,
                    )
                    if error_code:
                        await _send_ws_error(
                            ws,
                            error_code,
                            error_message,
                            details={"device_id": device_id, "action": prop},
                        )
                        continue

                    # property/value format (legacy)
                    deltas = state_manager.apply_action(
                        "user", device_id, prop, value
                    )
                    if _command_affects_room_light(
                        device,
                        action="",
                        params={},
                        property_path=prop,
                    ):
                        _append_room_light_feedback(device, deltas)
                else:
                    await _send_ws_error(
                        ws,
                        "INVALID_DEVICE_COMMAND",
                        "设备控制命令缺少 action 或 property",
                        details={"device_id": device_id},
                    )
                    continue

                root_event = event_bus.coerce_event(
                    SimEvent(
                        event_type="user.command",
                        source="user_ui",
                        timestamp=float(state_manager.world.simulation_tick),
                        wall_time=time.time(),
                        priority=2,
                        data={
                            "message_type": msg_type,
                            "device_id": device_id,
                            "action": action or prop,
                            "params": params if params else {"value": value},
                        },
                    )
                )
                await event_bus.publish(root_event)

                if deltas:
                    delta_payload = {
                        "deltas": [d.model_dump() for d in deltas],
                    }
                    await manager.broadcast(
                        WSMessage(type="STATE_DELTA", payload=delta_payload)
                    )
                    await manager.broadcast(
                        WSMessage(type="SIM_EVENT", payload=root_event.model_dump())
                    )
                    for delta in deltas:
                        await _broadcast_sim_event(
                            SimEvent(
                                event_type="feedback.state_delta",
                                source="state_manager",
                                timestamp=float(state_manager.world.simulation_tick),
                                wall_time=time.time(),
                                correlation_id=root_event.correlation_id,
                                causal_parent=root_event.event_id,
                                priority=1,
                                data=delta.model_dump(),
                            )
                        )
                else:
                    await manager.broadcast(
                        WSMessage(type="SIM_EVENT", payload=root_event.model_dump())
                    )

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
                await manager.broadcast(
                    WSMessage(type="STATE_FULL", payload=full)
                )
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

    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        log.exception("ws_error")
        manager.disconnect(ws)
