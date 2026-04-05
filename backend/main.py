from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from backend.api.routes import router as api_router
from backend.api.ws import ConnectionManager
from backend.core.logging import log
from backend.engine.state import (
    AgentRuntimeState,
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    UserState,
    WorldState,
)
from backend.engine.event_bus import EventBus
from backend.engine.simulation import SimulationEngine
from backend.engine.state_manager import StateManager
from backend.models.schemas import WSMessage


# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

manager = ConnectionManager()
event_bus = EventBus()
state_manager: StateManager | None = None
simulation_engine: SimulationEngine | None = None


# ---------------------------------------------------------------------------
# Default state initialisation
# ---------------------------------------------------------------------------


def _init_default_state() -> StateManager:
    """Build the default apartment_v1 world state."""
    world = WorldState(scene_id="apartment_v1")

    # Rooms
    rooms = {
        "living_room": RoomState(id="living_room", temperature=25.0, humidity=0.5),
        "bedroom": RoomState(id="bedroom", temperature=24.0, humidity=0.5),
        "kitchen": RoomState(id="kitchen", temperature=26.0, humidity=0.6),
        "bathroom": RoomState(id="bathroom", temperature=25.0, humidity=0.7),
    }
    world.rooms = rooms  # type: ignore[assignment]

    # Devices
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True,
                extra={"brightness": 80, "color_temp": 4000},
            ),
        ),
        "light_bedroom_01": DeviceState(
            id="light_bedroom_01",
            type="light",
            location=Location3D(room="bedroom"),
            state=DeviceStateValues(
                power=True,
                extra={"brightness": 60, "color_temp": 3500},
            ),
        ),
        "ac_living_01": DeviceState(
            id="ac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True,
                extra={"target_temp": 24, "mode": "cool"},
            ),
        ),
        "curtain_living_01": DeviceState(
            id="curtain_living_01",
            type="curtain",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=True,
                extra={"open_percent": 70},
            ),
        ),
    }

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
        await simulation_engine.stop()
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
            try:
                raw = await asyncio.wait_for(ws.receive_json(), timeout=60.0)
            except asyncio.TimeoutError:
                log.warning("ws_timeout")
                await ws.close(code=1000, reason="idle timeout")
                break

            msg_type = raw.get("type", "")
            payload = raw.get("payload", {})

            if msg_type == "CMD_DEVICE_CONTROL":
                device_id = payload.get("device_id", "")
                prop = payload.get("property", "")
                value = payload.get("value")
                if device_id and prop and value is not None:
                    deltas = state_manager.apply_action(
                        agent_id="user",
                        device_id=device_id,
                        property_path=prop,
                        new_value=value,
                    )
                    if deltas:
                        delta_payload = {
                            "deltas": [d.model_dump() for d in deltas],
                        }
                        await manager.broadcast(
                            WSMessage(type="STATE_DELTA", payload=delta_payload)
                        )

            elif msg_type == "CMD_SIM_START":
                if simulation_engine is not None:
                    await simulation_engine.start()
                await manager.broadcast(
                    WSMessage(type="SIM_STATUS", payload={"is_running": True})
                )

            elif msg_type == "CMD_SIM_PAUSE":
                if simulation_engine is not None:
                    await simulation_engine.pause()
                await manager.broadcast(
                    WSMessage(type="SIM_STATUS", payload={"is_running": False})
                )

            elif msg_type == "CMD_SIM_RESET":
                if simulation_engine is not None:
                    await simulation_engine.reset()
                state_manager = _init_default_state()
                # Re-create the engine with the fresh state manager
                simulation_engine = SimulationEngine(
                    event_bus=event_bus,
                    state_manager=state_manager,
                    connection_manager=manager,
                )
                full = state_manager.get_full_snapshot()
                await manager.broadcast(
                    WSMessage(type="STATE_FULL", payload=full)
                )

            elif msg_type == "CMD_SIM_SPEED":
                speed = payload.get("speed", 1.0)
                if simulation_engine is not None:
                    simulation_engine.speed = float(speed)
                state_manager.world.simulation_speed = float(speed)
                await manager.broadcast(
                    WSMessage(
                        type="SIM_STATUS",
                        payload={"simulation_speed": float(speed)},
                    )
                )

    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:
        log.exception("ws_error")
        manager.disconnect(ws)
