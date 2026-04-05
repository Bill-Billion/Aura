# SmartHomeSim MVP 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个可运行的多智能体智能家居仿真平台 MVP，包含后端仿真引擎、2个规则Agent、3D场景可视化和实时控制面板。

**Architecture:** FastAPI 后端维护内存级 WorldState，通过 EventBus (Pub/Sub) 解耦 Agent 通信，WebSocket 实时推送增量状态到前端。前端使用 TresJS 渲染等距视角3D公寓，GSAP 驱动设备状态动画，TailwindCSS 磨砂玻璃风格面板。

**Tech Stack:** Python 3.11+ / FastAPI / Pydantic / asyncio / pytest | Vue 3 / TresJS / Three.js / GSAP / Pinia / VueUse / TailwindCSS / Vite

**MVP 简化决策（延后项）：**
- 插件体系 → 硬编码 3 种设备 + 2 个 Agent
- LLM 集成 → 纯规则策略
- 学习型世界模型 → 简化物理模拟
- 研究工具/实验管理 → 仅演示
- GLB 模型 → 程序化几何体（无外部资源依赖）
- Agent 协商协议 → 无需协商，各管各域

---

## File Structure

```
SmartHomeSim/
├── .gitignore
├── docs/superpowers/specs/          # 已有
├── docs/superpowers/plans/          # 本文件
│
├── backend/
│   ├── requirements.txt
│   ├── main.py                      # FastAPI 入口
│   ├── config.toml                  # 运行配置
│   ├── api/
│   │   ├── __init__.py
│   │   ├── routes.py                # HTTP REST
│   │   └── ws.py                    # WebSocket + ConnectionManager
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py                # 配置加载
│   │   └── logging.py               # structlog
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── state.py                 # WorldState Pydantic 模型
│   │   ├── event_bus.py             # EventBus Pub/Sub
│   │   ├── state_manager.py         # StateManager
│   │   └── simulation.py            # SimulationEngine 主循环
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── base.py                  # BaseAgent ABC
│   │   ├── runtime.py               # AgentRuntime
│   │   ├── lighting.py              # LightingAgent
│   │   └── hvac.py                  # HVACAgent
│   ├── simulators/
│   │   ├── __init__.py
│   │   ├── environment.py           # 物理环境模拟
│   │   └── user_behavior.py         # 用户行为模拟
│   └── models/
│       ├── __init__.py
│       └── schemas.py               # 消息 Schema
│
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tsconfig.node.json
│   ├── tailwind.config.ts
│   ├── postcss.config.js
│   ├── index.html
│   └── src/
│       ├── main.ts
│       ├── App.vue
│       ├── types/
│       │   ├── websocket.ts
│       │   └── world-state.ts
│       ├── stores/
│       │   ├── worldStore.ts
│       │   ├── agentStore.ts
│       │   └── simulationStore.ts
│       ├── composables/
│       │   ├── useWebSocket.ts
│       │   └── useAnimatedDevice.ts
│       ├── components/
│       │   ├── scene/
│       │   │   ├── SceneRenderer.vue
│       │   │   └── DeviceMesh.vue
│       │   └── dashboard/
│       │       ├── DashboardOverlay.vue
│       │       ├── ControlPanel.vue
│       │       ├── AgentActionLog.vue
│       │       └── StatusBar.vue
│       └── styles/
│           ├── main.css
│           └── glassmorphism.css
│
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_state.py
    ├── test_event_bus.py
    ├── test_state_manager.py
    ├── test_environment_sim.py
    ├── test_user_behavior_sim.py
    ├── test_agents.py
    └── test_simulation.py
```

---

## Task 1: Project Scaffolding

**Files:**
- Create: `.gitignore`
- Create: `backend/requirements.txt`
- Create: `backend/config.toml`
- Create: all `__init__.py` files
- Create: `tests/conftest.py`

- [ ] **Step 1: Initialize git repo + .gitignore**

```bash
cd /Users/yanghaoran/Code/SmartHomeSim && git init
```

`.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
.venv/
node_modules/
dist/
.DS_Store
*.egg-info/
.env
```

- [ ] **Step 2: Create backend directory structure + __init__.py**

```bash
mkdir -p backend/{api,core,engine,agents,simulators,models} tests
touch backend/__init__.py backend/{api,core,engine,agents,simulators,models}/__init__.py
touch tests/__init__.py
```

- [ ] **Step 3: Create backend/requirements.txt**

```
fastapi>=0.110.0
uvicorn[standard]>=0.27.0
pydantic>=2.6.0
structlog>=24.1.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
httpx>=0.27.0
```

- [ ] **Step 4: Create backend/config.toml**

```toml
[server]
host = "0.0.0.0"
port = 8000

[simulation]
tick_interval = 0.1
physics_substeps = 3
default_speed = 1.0

[scene]
default_scene = "apartment_v1"
```

- [ ] **Step 5: Create tests/conftest.py**

```python
import pytest

@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 6: Install deps + verify**

```bash
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

- [ ] **Step 7: Commit**

```bash
git add .gitignore backend/ tests/conftest.py && git commit -m "chore: project scaffolding"
```

---

## Task 2: Backend Data Models (WorldState)

**Files:**
- Create: `backend/engine/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write test_state.py**

```python
# tests/test_state.py
from backend.engine.state import (
    WorldState, DeviceState, RoomState, EnvironmentState,
    Location3D, DeviceStateValues
)


def test_device_state_creation():
    ds = DeviceState(
        id="light_01", type="light",
        location=Location3D(room="living_room", x=1.0, y=0.0, z=2.0),
        state=DeviceStateValues(power=True, extra={"brightness": 80})
    )
    assert ds.state.power is True
    assert ds.state.extra["brightness"] == 80


def test_world_state_defaults():
    ws = WorldState()
    assert ws.simulation_tick == 0
    assert ws.is_running is False
    assert len(ws.devices) == 0


def test_world_state_snapshot_isolation():
    ws = WorldState()
    ws.devices["light_01"] = DeviceState(
        id="light_01", type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True)
    )
    snap = ws.snapshot()
    snap.devices["light_01"].state.power = False
    assert ws.devices["light_01"].state.power is True


def test_world_state_serialization_roundtrip():
    ws = WorldState()
    ws.rooms["living_room"] = RoomState(id="living_room", temperature=26.0)
    json_str = ws.model_dump_json()
    ws2 = WorldState.model_validate_json(json_str)
    assert ws2.rooms["living_room"].temperature == 26.0
```

- [ ] **Step 2: Run test → expect FAIL**

```bash
pytest tests/test_state.py -v
```

- [ ] **Step 3: Implement backend/engine/state.py**

```python
# backend/engine/state.py
"""WorldState — 全局单一事实源"""
from pydantic import BaseModel, Field
from typing import Literal, Any


class Location3D(BaseModel):
    room: str
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


class DeviceStateValues(BaseModel):
    power: bool = False
    last_changed_by: str = "system"
    extra: dict[str, Any] = Field(default_factory=dict)


class DeviceState(BaseModel):
    id: str
    type: Literal["light", "hvac", "curtain", "sensor"]
    location: Location3D
    state: DeviceStateValues


class RoomState(BaseModel):
    id: str
    temperature: float = 25.0
    humidity: float = 0.5
    light_level: float = 300.0
    occupancy: bool = False
    persons: list[str] = Field(default_factory=list)


class EnvironmentState(BaseModel):
    time_of_day: str = "12:00"
    outdoor_temp: float = 25.0
    outdoor_humidity: float = 0.5
    weather: Literal["clear", "cloudy", "rainy", "snowy"] = "clear"


class AgentRuntimeState(BaseModel):
    id: str
    name: str
    status: Literal["idle", "active", "thinking"] = "idle"
    current_strategy: str = ""
    confidence: float = 0.5
    last_action: str = ""


class UserState(BaseModel):
    id: str
    name: str = "User"
    location: Location3D | None = None
    activity: str = "idle"
    comfort_score: float = 0.8


class WorldState(BaseModel):
    simulation_tick: int = 0
    simulation_speed: float = 1.0
    is_running: bool = False
    scene_id: str = ""
    environment: EnvironmentState = Field(default_factory=EnvironmentState)
    devices: dict[str, DeviceState] = Field(default_factory=dict)
    rooms: dict[str, RoomState] = Field(default_factory=dict)
    agents: dict[str, AgentRuntimeState] = Field(default_factory=dict)
    users: dict[str, UserState] = Field(default_factory=dict)

    def snapshot(self) -> "WorldState":
        return self.model_copy(deep=True)
```

- [ ] **Step 4: Run test → expect PASS**

```bash
pytest tests/test_state.py -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/engine/state.py tests/test_state.py && git commit -m "feat: WorldState Pydantic models with snapshot"
```

---

## Task 3: EventBus

**Files:**
- Create: `backend/engine/event_bus.py`
- Create: `tests/test_event_bus.py`

- [ ] **Step 1: Write test_event_bus.py**

```python
# tests/test_event_bus.py
import pytest
from backend.engine.event_bus import EventBus, WorldEvent


@pytest.mark.asyncio
async def test_publish_notifies_subscriber():
    bus = EventBus()
    received = []
    bus.subscribe("test_event", lambda e: received.append(e))
    event = WorldEvent(event_type="test_event", source="unit_test", timestamp=0.0, data={"key": "val"})
    count = await bus.publish(event)
    assert count == 1
    assert len(received) == 1
    assert received[0].data["key"] == "val"


@pytest.mark.asyncio
async def test_wildcard_subscriber():
    bus = EventBus()
    received = []
    bus.subscribe("*", lambda e: received.append(e))
    await bus.publish(WorldEvent(event_type="anything", source="t", timestamp=0.0, data={}))
    assert len(received) == 1


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = EventBus()
    handler = lambda e: None
    bus.subscribe("evt", handler)
    bus.unsubscribe("evt", handler)
    count = await bus.publish(WorldEvent(event_type="evt", source="t", timestamp=0.0, data={}))
    assert count == 0


@pytest.mark.asyncio
async def test_history():
    bus = EventBus()
    await bus.publish(WorldEvent(event_type="a", source="t", timestamp=1.0, data={}))
    await bus.publish(WorldEvent(event_type="b", source="t", timestamp=2.0, data={}))
    assert len(bus.get_history()) == 2
    assert len(bus.get_history(event_type="a")) == 1
    assert len(bus.get_history(since=1.5)) == 1
```

- [ ] **Step 2: Run test → expect FAIL**

- [ ] **Step 3: Implement backend/engine/event_bus.py**

```python
# backend/engine/event_bus.py
"""EventBus — 内存级异步 Pub/Sub"""
import asyncio
from collections import defaultdict
from typing import Callable, Awaitable
from pydantic import BaseModel


class WorldEvent(BaseModel):
    event_type: str
    source: str
    timestamp: float
    data: dict


EventHandler = Callable[[WorldEvent], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)
        self._history: list[WorldEvent] = []
        self._max_history = 1000

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        if event_type in self._subscribers:
            self._subscribers[event_type] = [
                h for h in self._subscribers[event_type] if h != handler
            ]

    async def publish(self, event: WorldEvent) -> int:
        self._history.append(event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        handlers = list(self._subscribers.get(event.event_type, []))
        handlers += list(self._subscribers.get("*", []))
        if handlers:
            await asyncio.gather(*[h(event) for h in handlers])
        return len(handlers)

    def get_history(self, event_type: str | None = None, since: float | None = None) -> list[WorldEvent]:
        result = self._history
        if event_type:
            result = [e for e in result if e.event_type == event_type]
        if since:
            result = [e for e in result if e.timestamp >= since]
        return result
```

- [ ] **Step 4: Run test → expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/engine/event_bus.py tests/test_event_bus.py && git commit -m "feat: EventBus async pub/sub with history"
```

---

## Task 4: Message Schemas + StateManager

**Files:**
- Create: `backend/models/schemas.py`
- Create: `backend/engine/state_manager.py`
- Create: `tests/test_state_manager.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_state_manager.py
import pytest
from backend.engine.state import WorldState, DeviceState, Location3D, DeviceStateValues
from backend.engine.state_manager import StateManager, DeltaChange


def test_apply_action_produces_delta():
    sm = StateManager()
    sm.state.devices["light_01"] = DeviceState(
        id="light_01", type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=False, extra={"brightness": 0})
    )
    deltas = sm.apply_action("lighting_agent", "light_01", "extra.brightness", 80)
    assert len(deltas) == 1
    assert deltas[0].old_value == 0
    assert deltas[0].new_value == 80
    assert sm.state.devices["light_01"].state.extra["brightness"] == 80


def test_get_full_snapshot():
    sm = StateManager()
    snap = sm.get_full_snapshot()
    assert isinstance(snap, dict)
    assert "simulation_tick" in snap


def test_delta_change_serialization():
    dc = DeltaChange(
        path="devices[light_01].state.extra.brightness",
        old_value=0, new_value=80,
        caused_by="lighting_agent", reason="auto_adjust"
    )
    d = dc.model_dump()
    assert d["path"] == "devices[light_01].state.extra.brightness"
```

- [ ] **Step 2: Implement backend/models/schemas.py**

```python
# backend/models/schemas.py
"""WebSocket 消息 Schema"""
from pydantic import BaseModel
from typing import Literal, Any


class WSMessage(BaseModel):
    type: str
    id: str = ""
    timestamp: float = 0.0
    payload: dict[str, Any] = {}


# Client → Server
class SimCommand(BaseModel):
    command: Literal[
        "CMD_SIM_START", "CMD_SIM_PAUSE", "CMD_SIM_RESET",
        "CMD_SIM_SPEED", "CMD_DEVICE_CONTROL", "CMD_TRIGGER_EVENT"
    ]
    params: dict[str, Any] = {}
```

- [ ] **Step 3: Implement backend/engine/state_manager.py**

```python
# backend/engine/state_manager.py
"""StateManager — 状态变更 + Delta 追踪"""
from pydantic import BaseModel
from backend.engine.state import WorldState


class DeltaChange(BaseModel):
    path: str
    old_value: Any = None
    new_value: Any = None
    caused_by: str = ""
    reason: str = ""


class StateManager:
    def __init__(self) -> None:
        self.state = WorldState()

    def apply_action(self, agent_id: str, device_id: str, property_path: str, new_value: Any) -> list[DeltaChange]:
        device = self.state.devices.get(device_id)
        if not device:
            return []
        old_value = self._get_nested(device, property_path)
        self._set_nested(device, property_path, new_value)
        device.state.last_changed_by = agent_id
        return [DeltaChange(
            path=f"devices[{device_id}].state.{property_path}",
            old_value=old_value, new_value=new_value,
            caused_by=agent_id
        )]

    def get_full_snapshot(self) -> dict:
        return self.state.model_dump()

    def _get_nested(self, obj: Any, path: str) -> Any:
        parts = path.split(".")
        current = obj
        for part in parts:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)
            if current is None:
                return None
        return current

    def _set_nested(self, obj: Any, path: str, value: Any) -> None:
        parts = path.split(".")
        current = obj
        for part in parts[:-1]:
            if isinstance(current, dict):
                current = current[part]
            else:
                current = getattr(current, part)
        last = parts[-1]
        if isinstance(current, dict):
            current[last] = value
        else:
            setattr(current, last, value)
```

- [ ] **Step 4: Run tests → expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/models/schemas.py backend/engine/state_manager.py tests/test_state_manager.py && git commit -m "feat: StateManager with delta tracking + message schemas"
```

---

## Task 5: FastAPI + WebSocket Gateway

**Files:**
- Create: `backend/main.py`
- Create: `backend/api/ws.py`
- Create: `backend/api/routes.py`
- Create: `backend/core/logging.py`

- [ ] **Step 1: Implement backend/core/logging.py**

```python
# backend/core/logging.py
import structlog

structlog.configure(processors=[
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.dev.ConsoleRenderer(),
])
```

- [ ] **Step 2: Implement backend/api/ws.py**

```python
# backend/api/ws.py
"""WebSocket endpoint + ConnectionManager"""
import asyncio
import json
import structlog
from fastapi import WebSocket, WebSocketDisconnect
from typing import List
from backend.models.schemas import WSMessage

logger = structlog.get_logger()


class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket, full_state: dict):
        await ws.accept()
        self.active.append(ws)
        await self.send(ws, WSMessage(type="STATE_FULL", payload=full_state))
        logger.info("client_connected", total=len(self.active))

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
        logger.info("client_disconnected", total=len(self.active))

    async def send(self, ws: WebSocket, msg: WSMessage):
        try:
            await ws.send_json(msg.model_dump())
        except Exception:
            self.disconnect(ws)

    async def broadcast(self, msg: WSMessage):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(msg.model_dump())
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)
```

- [ ] **Step 3: Implement backend/api/routes.py**

```python
# backend/api/routes.py
"""HTTP REST endpoints"""
from fastapi import APIRouter

router = APIRouter()


@router.get("/api/scenes")
async def list_scenes():
    return {"scenes": [{"id": "apartment_v1", "name": "测试公寓"}]}


@router.get("/api/health")
async def health():
    return {"status": "ok"}
```

- [ ] **Step 4: Implement backend/main.py**

```python
# backend/main.py
"""FastAPI 入口"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import structlog

from backend.api.routes import router
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, WorldEvent
from backend.engine.state_manager import StateManager
from backend.models.schemas import WSMessage

logger = structlog.get_logger()

# Global instances (wired in lifespan)
event_bus = EventBus()
state_manager = StateManager()
connection_manager = ConnectionManager()
simulation_engine = None  # set in lifespan


def _init_default_state():
    """初始化默认场景数据"""
    from backend.engine.state import DeviceState, RoomState, Location3D, DeviceStateValues
    s = state_manager.state
    s.scene_id = "apartment_v1"
    # Rooms
    for rid in ["living_room", "bedroom", "kitchen", "bathroom"]:
        s.rooms[rid] = RoomState(id=rid)
    # Devices
    devices = [
        ("light_living_01", "light", "living_room", {"brightness": 80, "color_temp": 4000}),
        ("light_bedroom_01", "light", "bedroom", {"brightness": 60, "color_temp": 3000}),
        ("ac_living_01", "hvac", "living_room", {"target_temp": 24.0, "mode": "cool", "fan_speed": "auto"}),
        ("curtain_living_01", "curtain", "living_room", {"open_percent": 70}),
    ]
    for did, dtype, room, extra in devices:
        s.devices[did] = DeviceState(
            id=did, type=dtype,
            location=Location3D(room=room),
            state=DeviceStateValues(power=True, extra=extra)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global simulation_engine
    _init_default_state()
    from backend.engine.simulation import SimulationEngine
    simulation_engine = SimulationEngine(
        event_bus=event_bus,
        state_manager=state_manager,
        connection_manager=connection_manager,
    )
    logger.info("server_started")
    yield
    if simulation_engine:
        await simulation_engine.stop()
    logger.info("server_stopped")


app = FastAPI(title="SmartHomeSim", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(router)


@app.websocket("/ws/simulation")
async def ws_simulation(websocket: WebSocket):
    await connection_manager.connect(websocket, state_manager.get_full_snapshot())
    try:
        while True:
            data = await asyncio.wait_for(websocket.receive_text(), timeout=60)
            msg = json.loads(data)
            await _handle_client_message(msg)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        connection_manager.disconnect(websocket)


async def _handle_client_message(msg: dict):
    cmd = msg.get("type", "")
    payload = msg.get("payload", {})
    if cmd == "CMD_SIM_START":
        await simulation_engine.start()
    elif cmd == "CMD_SIM_PAUSE":
        await simulation_engine.pause()
    elif cmd == "CMD_SIM_RESET":
        await simulation_engine.reset()
        _init_default_state()
    elif cmd == "CMD_SIM_SPEED":
        simulation_engine.speed = payload.get("speed", 1.0)
    elif cmd == "CMD_DEVICE_CONTROL":
        device_id = payload["device_id"]
        prop = payload["property"]
        value = payload["value"]
        deltas = state_manager.apply_action("user", device_id, prop, value)
        if deltas:
            await connection_manager.broadcast(WSMessage(
                type="STATE_DELTA",
                payload={"changes": [d.model_dump() for d in deltas]}
            ))
    else:
        logger.warning("unknown_command", cmd=cmd)
```

- [ ] **Step 5: Manual test — start server**

```bash
cd backend && source .venv/bin/activate
uvicorn backend.main:app --reload --port 8000
```
Verify: `curl http://localhost:8000/api/health` → `{"status":"ok"}`

- [ ] **Step 6: Commit**

```bash
git add backend/ && git commit -m "feat: FastAPI + WebSocket gateway with ConnectionManager"
```

---

## Task 6: EnvironmentSimulator

**Files:**
- Create: `backend/simulators/environment.py`
- Create: `tests/test_environment_sim.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_environment_sim.py
from backend.engine.state import WorldState, DeviceState, RoomState, Location3D, DeviceStateValues, EnvironmentState
from backend.simulators.environment import EnvironmentSimulator


def _make_state() -> WorldState:
    ws = WorldState()
    ws.environment = EnvironmentState(outdoor_temp=35.0)
    ws.rooms["living_room"] = RoomState(id="living_room", temperature=28.0)
    ws.rooms["bedroom"] = RoomState(id="bedroom", temperature=26.0)
    ws.devices["ac_living_01"] = DeviceState(
        id="ac_living_01", type="hvac",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"target_temp": 24.0, "mode": "cool"})
    )
    return ws


def test_temperature_towards_target():
    sim = EnvironmentSimulator()
    ws = _make_state()
    ws.rooms["living_room"].temperature = 28.0
    sim.step(ws, dt=1.0)
    # AC cooling: temp should move toward 24.0
    assert ws.rooms["living_room"].temperature < 28.0


def test_outdoor_heat_diffusion():
    sim = EnvironmentSimulator()
    ws = _make_state()
    ws.devices["ac_living_01"].state.power = False  # AC off
    old_temp = ws.rooms["living_room"].temperature
    sim.step(ws, dt=10.0)
    # Without AC, temp moves toward outdoor (35.0)
    assert ws.rooms["living_room"].temperature > old_temp


def test_light_level_with_curtain():
    sim = EnvironmentSimulator()
    ws = WorldState()
    ws.environment = EnvironmentState(time_of_day="12:00", weather="clear")
    ws.rooms["living_room"] = RoomState(id="living_room")
    ws.devices["light_living_01"] = DeviceState(
        id="light_living_01", type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"brightness": 50})
    )
    ws.devices["curtain_living_01"] = DeviceState(
        id="curtain_living_01", type="curtain",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"open_percent": 80})
    )
    sim.step(ws, dt=1.0)
    # Light level = artificial + natural
    assert ws.rooms["living_room"].light_level > 0
```

- [ ] **Step 2: Implement backend/simulators/environment.py**

```python
# backend/simulators/environment.py
"""简化物理环境模拟：温度扩散、光照衰减"""
import math


class EnvironmentSimulator:
    AC_COOL_RATE = 0.05      # per dt, per degree difference
    AC_HEAT_RATE = 0.03
    OUTDOOR_DIFFUSION = 0.002
    LIGHT_CONTRIBUTION = 8.0  # lux per brightness unit
    NATURAL_LIGHT_MAX = 500.0

    def step(self, state, dt: float = 1.0) -> list[dict]:
        changes = []
        # Build room→devices mapping
        room_devices: dict[str, list] = {}
        for dev in state.devices.values():
            room = dev.location.room
            room_devices.setdefault(room, []).append(dev)

        for room_id, room in state.rooms.items():
            # --- Temperature ---
            temp = room.temperature
            # Outdoor diffusion
            outdoor = state.environment.outdoor_temp
            temp += self.OUTDOOR_DIFFUSION * (outdoor - temp) * dt
            # HVAC effect
            for dev in room_devices.get(room_id, []):
                if dev.type == "hvac" and dev.state.power:
                    target = dev.state.extra.get("target_temp", 25.0)
                    mode = dev.state.extra.get("mode", "cool")
                    diff = target - temp
                    if mode == "cool" and diff < 0:
                        temp += self.AC_COOL_RATE * diff * dt
                    elif mode == "heat" and diff > 0:
                        temp += self.AC_HEAT_RATE * diff * dt
            room.temperature = round(temp, 2)

            # --- Light level ---
            light_level = 0.0
            for dev in room_devices.get(room_id, []):
                if dev.type == "light" and dev.state.power:
                    light_level += dev.state.extra.get("brightness", 0) * self.LIGHT_CONTRIBUTION
                if dev.type == "curtain" and dev.state.power:
                    open_pct = dev.state.extra.get("open_percent", 0) / 100.0
                    hour = self._parse_hour(state.environment.time_of_day)
                    natural = self._sunlight(hour) * open_pct
                    light_level += natural
            room.light_level = round(light_level, 1)

        return changes

    def _parse_hour(self, time_str: str) -> float:
        parts = time_str.split(":")
        return int(parts[0]) + int(parts[1]) / 60.0

    def _sunlight(self, hour: float) -> float:
        """简化的日照模型：6-18点有光，12点最高"""
        if 6 <= hour <= 18:
            peak = 12.0
            return self.NATURAL_LIGHT_MAX * max(0, 1 - abs(hour - peak) / 6.0)
        return 0.0
```

- [ ] **Step 3: Run tests → expect PASS**

- [ ] **Step 4: Commit**

```bash
git add backend/simulators/environment.py tests/test_environment_sim.py && git commit -m "feat: EnvironmentSimulator with temp/light physics"
```

---

## Task 7: UserBehaviorSimulator

**Files:**
- Create: `backend/simulators/user_behavior.py`
- Create: `tests/test_user_behavior_sim.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_user_behavior_sim.py
from backend.engine.state import WorldState, EnvironmentState
from backend.simulators.user_behavior import UserBehaviorSimulator


def test_user_moves_on_schedule():
    sim = UserBehaviorSimulator()
    ws = WorldState()
    ws.environment = EnvironmentState(time_of_day="08:00")
    events = sim.step(ws)
    assert len(events) > 0
    # 8:00 should trigger morning routine
```

- [ ] **Step 2: Implement backend/simulators/user_behavior.py**

```python
# backend/simulators/user_behavior.py
"""基于日程表的用户行为模拟器"""
from backend.engine.event_bus import WorldEvent


# 简化的用户日程：hour → (room, activity)
DAILY_SCHEDULE = {
    7:  ("bedroom",   "waking_up"),
    8:  ("kitchen",   "breakfast"),
    9:  ("living_room", "idle"),
    12: ("kitchen",   "lunch"),
    14: ("living_room", "working"),
    18: ("kitchen",   "dinner"),
    19: ("living_room", "watching_tv"),
    22: ("bedroom",   "sleeping"),
}


class UserBehaviorSimulator:
    def __init__(self):
        self._last_hour = -1

    def step(self, state) -> list[WorldEvent]:
        events = []
        hour_str = state.environment.time_of_day
        hour = int(hour_str.split(":")[0])
        if hour == self._last_hour:
            return events
        self._last_hour = hour

        if hour in DAILY_SCHEDULE:
            room, activity = DAILY_SCHEDULE[hour]
            events.append(WorldEvent(
                event_type="user.activity_change",
                source="user_behavior_sim",
                timestamp=float(state.simulation_tick),
                data={"user_id": "user_01", "room": room, "activity": activity}
            ))
            # Update user state
            if "user_01" in state.users:
                from backend.engine.state import Location3D
                state.users["user_01"].activity = activity
                state.users["user_01"].location = Location3D(room=room)
            # Update room occupancy
            for r in state.rooms.values():
                if r.id == room:
                    r.occupancy = True
                    if "user_01" not in r.persons:
                        r.persons.append("user_01")
                else:
                    r.occupancy = False
                    r.persons = [p for p in r.persons if p != "user_01"]

        return events
```

- [ ] **Step 3: Run tests → expect PASS**

- [ ] **Step 4: Commit**

```bash
git add backend/simulators/user_behavior.py tests/test_user_behavior_sim.py && git commit -m "feat: UserBehaviorSimulator with daily schedule"
```

---

## Task 8: BaseAgent + LightingAgent + HVACAgent

**Files:**
- Create: `backend/agents/base.py`
- Create: `backend/agents/lighting.py`
- Create: `backend/agents/hvac.py`
- Create: `tests/test_agents.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_agents.py
import pytest
from backend.engine.state import WorldState, DeviceState, RoomState, Location3D, DeviceStateValues, EnvironmentState
from backend.agents.lighting import LightingAgent
from backend.agents.hvac import HVACAgent


def _make_state() -> WorldState:
    ws = WorldState()
    ws.environment = EnvironmentState(time_of_day="19:00", outdoor_temp=30.0)
    ws.rooms["living_room"] = RoomState(id="living_room", temperature=28.0, occupancy=True, persons=["user_01"])
    ws.rooms["bedroom"] = RoomState(id="bedroom", temperature=26.0)
    ws.devices["light_living_01"] = DeviceState(
        id="light_living_01", type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"brightness": 100, "color_temp": 5000})
    )
    ws.devices["ac_living_01"] = DeviceState(
        id="ac_living_01", type="hvac",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"target_temp": 26.0, "mode": "cool", "fan_speed": "auto"})
    )
    return ws


def test_lighting_agent_dim_at_night():
    agent = LightingAgent()
    ws = _make_state()
    actions = agent.decide(ws)
    # 19:00 evening → should dim to ~60 and warm color
    light_actions = [a for a in actions if a["device_id"] == "light_living_01"]
    assert len(light_actions) > 0
    brightness_action = next(a for a in light_actions if a["property"] == "extra.brightness")
    assert brightness_action["new_value"] < 100


def test_hvac_agent_cools_when_hot():
    agent = HVACAgent()
    ws = _make_state()
    ws.rooms["living_room"].temperature = 30.0
    actions = agent.decide(ws)
    ac_actions = [a for a in actions if a["device_id"] == "ac_living_01"]
    assert len(ac_actions) > 0
    temp_action = next(a for a in ac_actions if a["property"] == "extra.target_temp")
    assert temp_action["new_value"] < 28.0
```

- [ ] **Step 2: Implement backend/agents/base.py**

```python
# backend/agents/base.py
"""BaseAgent — 所有 Agent 的基类"""
from abc import ABC, abstractmethod
from backend.engine.state import WorldState


class BaseAgent(ABC):
    def __init__(self, agent_id: str, name: str):
        self.id = agent_id
        self.name = name
        self.status: str = "idle"
        self.confidence: float = 0.5

    @abstractmethod
    def get_controlled_device_types(self) -> list[str]:
        """声明本 Agent 控制的设备类型"""
        ...

    @abstractmethod
    def decide(self, world_state: WorldState) -> list[dict]:
        """
        基于世界状态生成动作列表。
        每个动作: {"device_id": str, "property": str, "value": any, "reason": str}
        """
        ...

    def _get_my_devices(self, world_state: WorldState) -> list:
        """获取本 Agent 控制的设备列表"""
        types = self.get_controlled_device_types()
        return [d for d in world_state.devices.values() if d.type in types]
```

- [ ] **Step 3: Implement backend/agents/lighting.py**

```python
# backend/agents/lighting.py
"""LightingAgent — 规则驱动的照明策略"""
from backend.agents.base import BaseAgent
from backend.engine.state import WorldState


class LightingAgent(BaseAgent):
    def __init__(self):
        super().__init__("lighting_agent", "照明管家")

    def get_controlled_device_types(self) -> list[str]:
        return ["light"]

    def decide(self, ws: WorldState) -> list[dict]:
        actions = []
        hour = int(ws.environment.time_of_day.split(":")[0])

        for dev in self._get_my_devices(ws):
            if not dev.state.power:
                continue
            room = ws.rooms.get(dev.location.room)
            if not room:
                continue

            brightness = dev.state.extra.get("brightness", 0)
            color_temp = dev.state.extra.get("color_temp", 4000)
            target_brightness = brightness
            target_color = color_temp

            # 时间段策略
            if 6 <= hour < 9:     # 早晨：明亮冷白光
                target_brightness = 90
                target_color = 5000
            elif 9 <= hour < 17:  # 白天：自然光（有人时补光）
                target_brightness = 40 if room.occupancy else 10
                target_color = 4500
            elif 17 <= hour < 21: # 傍晚：暖光，适度亮度
                target_brightness = 70 if room.occupancy else 20
                target_color = 3000
            elif 21 <= hour < 23: # 晚间：暗暖光
                target_brightness = 30 if room.occupancy else 5
                target_color = 2700
            else:                 # 深夜：极暗
                target_brightness = 5
                target_color = 2700

            if abs(brightness - target_brightness) > 5:
                actions.append({
                    "device_id": dev.id,
                    "property": "extra.brightness",
                    "value": target_brightness,
                    "reason": f"时间策略({hour}:00), 有人={room.occupancy}"
                })
            if abs(color_temp - target_color) > 200:
                actions.append({
                    "device_id": dev.id,
                    "property": "extra.color_temp",
                    "value": target_color,
                    "reason": f"色温策略({hour}:00)"
                })

        return actions
```

- [ ] **Step 4: Implement backend/agents/hvac.py**

```python
# backend/agents/hvac.py
"""HVACAgent — 规则驱动的暖通策略"""
from backend.agents.base import BaseAgent
from backend.engine.state import WorldState


class HVACAgent(BaseAgent):
    COMFORT_TEMP_MIN = 22.0
    COMFORT_TEMP_MAX = 26.0

    def __init__(self):
        super().__init__("hvac_agent", "暖通管家")

    def get_controlled_device_types(self) -> list[str]:
        return ["hvac"]

    def decide(self, ws: WorldState) -> list[dict]:
        actions = []
        for dev in self._get_my_devices(ws):
            if not dev.state.power:
                continue
            room = ws.rooms.get(dev.location.room)
            if not room or not room.occupancy:
                continue

            current_temp = room.temperature
            target = dev.state.extra.get("target_temp", 25.0)
            mode = dev.state.extra.get("mode", "cool")
            new_target = target

            if current_temp > self.COMFORT_TEMP_MAX and mode == "cool":
                new_target = max(22.0, current_temp - 3.0)
            elif current_temp < self.COMFORT_TEMP_MIN and mode == "heat":
                new_target = min(28.0, current_temp + 3.0)

            if abs(new_target - target) > 0.5:
                actions.append({
                    "device_id": dev.id,
                    "property": "extra.target_temp",
                    "value": round(new_target, 1),
                    "reason": f"室温{current_temp}°C超舒适区, 调整目标温度"
                })

        return actions
```

- [ ] **Step 5: Run tests → expect PASS**

- [ ] **Step 6: Commit**

```bash
git add backend/agents/ tests/test_agents.py && git commit -m "feat: LightingAgent + HVACAgent with rule-based strategies"
```

---

## Task 9: AgentRuntime + SimulationEngine

**Files:**
- Create: `backend/agents/runtime.py`
- Create: `backend/engine/simulation.py`
- Create: `tests/test_simulation.py`

- [ ] **Step 1: Implement backend/agents/runtime.py**

```python
# backend/agents/runtime.py
"""AgentRuntime — 管理所有 Agent 的生命周期"""
from backend.agents.base import BaseAgent
from backend.engine.state import WorldState


class AgentRuntime:
    def __init__(self):
        self.agents: list[BaseAgent] = []

    def register(self, agent: BaseAgent):
        self.agents.append(agent)

    async def step(self, world_state: WorldState) -> list[dict]:
        """每个 tick 调用所有 Agent 的 decide()，返回动作列表"""
        all_actions = []
        for agent in self.agents:
            agent.status = "thinking"
            actions = agent.decide(world_state)
            if actions:
                agent.status = "active"
                for action in actions:
                    action["agent_id"] = agent.id
                    action["agent_name"] = agent.name
                all_actions.extend(actions)
            else:
                agent.status = "idle"
        return all_actions
```

- [ ] **Step 2: Implement backend/engine/simulation.py**

```python
# backend/engine/simulation.py
"""SimulationEngine — 仿真主循环"""
import asyncio
import time
import structlog
from backend.engine.event_bus import EventBus, WorldEvent
from backend.engine.state_manager import StateManager
from backend.agents.runtime import AgentRuntime
from backend.agents.lighting import LightingAgent
from backend.agents.hvac import HVACAgent
from backend.simulators.environment import EnvironmentSimulator
from backend.simulators.user_behavior import UserBehaviorSimulator
from backend.models.schemas import WSMessage

logger = structlog.get_logger()

TICK_INTERVAL = 0.1      # 100ms
SIMULATED_DT = 60.0      # 每个 tick 推进 60 秒仿真时间


class SimulationEngine:
    def __init__(self, event_bus: EventBus, state_manager: StateManager, connection_manager):
        self.event_bus = event_bus
        self.state_manager = state_manager
        self.conn = connection_manager
        self.is_running = False
        self.speed = 1.0
        self._task: asyncio.Task | None = None

        # Subsystems
        self.agent_runtime = AgentRuntime()
        self.agent_runtime.register(LightingAgent())
        self.agent_runtime.register(HVACAgent())
        self.env_sim = EnvironmentSimulator()
        self.user_sim = UserBehaviorSimulator()

    async def start(self):
        if self.is_running:
            return
        self.is_running = True
        self.state_manager.state.is_running = True
        self._task = asyncio.create_task(self._main_loop())
        logger.info("simulation_started")

    async def pause(self):
        self.is_running = False
        self.state_manager.state.is_running = False
        if self._task:
            self._task.cancel()
        logger.info("simulation_paused")

    async def stop(self):
        await self.pause()

    async def reset(self):
        await self.pause()
        self.state_manager.state.simulation_tick = 0
        logger.info("simulation_reset")

    async def _main_loop(self):
        while self.is_running:
            tick_start = time.monotonic()
            state = self.state_manager.state
            state.simulation_tick += 1

            # Advance simulated time
            self._advance_time(state, SIMULATED_DT)

            # 1. User behavior
            user_events = self.user_sim.step(state)
            for ev in user_events:
                await self.event_bus.publish(ev)

            # 2. Environment physics
            self.env_sim.step(state, dt=SIMULATED_DT)

            # 3. Agent decisions
            actions = await self.agent_runtime.step(state)
            all_deltas = []
            for action in actions:
                device_id = action["device_id"]
                prop = action["property"]
                value = action["value"]
                deltas = self.state_manager.apply_action(
                    action["agent_id"], device_id, prop, value
                )
                for d in deltas:
                    d.reason = action.get("reason", "")
                all_deltas.extend(deltas)

            # 4. Broadcast
            if all_deltas:
                await self.conn.broadcast(WSMessage(
                    type="STATE_DELTA",
                    payload={"changes": [d.model_dump() for d in all_deltas]}
                ))

            # 5. Broadcast agent status
            await self.conn.broadcast(WSMessage(
                type="STATE_DELTA",
                payload={"changes": [
                    {
                        "path": f"agents[{a.id}]",
                        "new_value": {"status": a.status, "confidence": a.confidence},
                        "caused_by": a.id
                    }
                    for a in self.agent_runtime.agents
                ]}
            ))

            # 6. Frame rate control
            elapsed = time.monotonic() - tick_start
            sleep_time = max(0, TICK_INTERVAL / self.speed - elapsed)
            await asyncio.sleep(sleep_time)

    def _advance_time(self, state, dt_seconds: float):
        """推进仿真时间"""
        parts = state.environment.time_of_day.split(":")
        h, m = int(parts[0]), int(parts[1])
        total_min = h * 60 + m + int(dt_seconds / 60)
        total_min %= 1440  # wrap around 24h
        state.environment.time_of_day = f"{total_min // 60:02d}:{total_min % 60:02d}"
```

- [ ] **Step 3: Write minimal test**

```python
# tests/test_simulation.py
import pytest
from unittest.mock import AsyncMock
from backend.engine.simulation import SimulationEngine
from backend.engine.event_bus import EventBus
from backend.engine.state_manager import StateManager
from backend.engine.state import DeviceState, RoomState, Location3D, DeviceStateValues, UserState


def _make_engine():
    eb = EventBus()
    sm = StateManager()
    sm.state.rooms["living_room"] = RoomState(id="living_room", temperature=28.0, occupancy=True, persons=["user_01"])
    sm.state.devices["light_01"] = DeviceState(
        id="light_01", type="light",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"brightness": 100, "color_temp": 5000})
    )
    sm.state.devices["ac_01"] = DeviceState(
        id="ac_01", type="hvac",
        location=Location3D(room="living_room"),
        state=DeviceStateValues(power=True, extra={"target_temp": 26.0, "mode": "cool"})
    )
    sm.state.users["user_01"] = UserState(id="user_01")

    mock_conn = AsyncMock()
    engine = SimulationEngine(eb, sm, mock_conn)
    return engine, sm


def test_engine_init():
    engine, sm = _make_engine()
    assert len(engine.agent_runtime.agents) == 2


@pytest.mark.asyncio
async def test_engine_main_loop_runs_one_tick():
    engine, sm = _make_engine()
    engine.is_running = True
    # Run one iteration manually
    await engine._main_loop()
    assert sm.state.simulation_tick == 1
    # Should have called broadcast (agent actions or status)
    assert engine.conn.broadcast.called
```

- [ ] **Step 4: Run tests → expect PASS**

- [ ] **Step 5: Commit**

```bash
git add backend/agents/runtime.py backend/engine/simulation.py tests/test_simulation.py && git commit -m "feat: SimulationEngine main loop with agent runtime"
```

---

## Task 10: Frontend Project Setup

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/tsconfig.node.json`, `frontend/tailwind.config.ts`, `frontend/postcss.config.js`, `frontend/index.html`
- Create: `frontend/src/main.ts`, `frontend/src/App.vue`
- Create: `frontend/src/types/websocket.ts`, `frontend/src/types/world-state.ts`
- Create: `frontend/src/styles/main.css`, `frontend/src/styles/glassmorphism.css`

- [ ] **Step 1: Scaffold Vue 3 project**

```bash
cd /Users/yanghaoran/Code/SmartHomeSim
npm create vite@latest frontend -- --template vue-ts
```

- [ ] **Step 2: Install dependencies**

```bash
cd frontend && npm install
npm install @tresjs/core @tresjs/cientos three gsap pinia @vueuse/core echarts tailwindcss postcss autoprefixer
npm install -D @types/three @tailwindcss/vite
```

- [ ] **Step 3: Configure vite.config.ts**

```typescript
// frontend/vite.config.ts
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: 'http://localhost:8000', ws: true },
      '/api': 'http://localhost:8000',
    }
  }
})
```

- [ ] **Step 4: Create TypeScript types**

`frontend/src/types/websocket.ts`:
```typescript
export interface WSMessage<T = any> {
  type: string
  id?: string
  timestamp?: number
  payload: T
}

export type MessageType =
  | 'STATE_FULL' | 'STATE_DELTA'
  | 'EVENT_NOTIFICATION' | 'AGENT_STATUS'
  | 'SIMULATION_STATUS' | 'HEARTBEAT_PING'
  | 'CMD_SIM_START' | 'CMD_SIM_PAUSE'
  | 'CMD_SIM_RESET' | 'CMD_SIM_SPEED'
  | 'CMD_DEVICE_CONTROL' | 'CMD_TRIGGER_EVENT'
```

`frontend/src/types/world-state.ts`:
```typescript
export interface Location3D { room: string; x: number; y: number; z: number }

export interface DeviceState {
  id: string; type: string; location: Location3D
  state: { power: boolean; last_changed_by: string; extra: Record<string, any> }
}

export interface RoomState {
  id: string; temperature: number; humidity: number
  light_level: number; occupancy: boolean; persons: string[]
}

export interface EnvironmentState {
  time_of_day: string; outdoor_temp: number
  outdoor_humidity: number; weather: string
}

export interface AgentState {
  id: string; name: string; status: string
  current_strategy: string; confidence: number; last_action: string
}

export interface WorldStateSnapshot {
  simulation_tick: number; simulation_speed: number
  is_running: boolean; scene_id: string
  environment: EnvironmentState
  devices: Record<string, DeviceState>
  rooms: Record<string, RoomState>
  agents: Record<string, AgentState>
}

export interface DeltaChange {
  path: string; old_value?: any; new_value: any
  caused_by?: string; reason?: string
}
```

- [ ] **Step 5: Create styles**

`frontend/src/styles/main.css`:
```css
@import "tailwindcss";
@import "./glassmorphism.css";

body { margin: 0; background: #0a0a0f; color: #e0e0e0; font-family: 'Inter', system-ui, sans-serif; }
```

`frontend/src/styles/glassmorphism.css`:
```css
.glass-panel {
  background: rgba(15, 15, 25, 0.75);
  backdrop-filter: blur(16px);
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 12px;
}
```

- [ ] **Step 6: Update main.ts with Pinia**

```typescript
// frontend/src/main.ts
import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import './styles/main.css'

createApp(App).use(createPinia()).mount('#app')
```

- [ ] **Step 7: Verify frontend starts**

```bash
cd frontend && npm run dev
```
Open http://localhost:5173 → should see blank Vue app

- [ ] **Step 8: Commit**

```bash
git add frontend/ && git commit -m "feat: Vue 3 frontend scaffold with TresJS + Pinia + TailwindCSS"
```

---

## Task 11: Pinia Stores

**Files:**
- Create: `frontend/src/stores/worldStore.ts`
- Create: `frontend/src/stores/agentStore.ts`
- Create: `frontend/src/stores/simulationStore.ts`

- [ ] **Step 1: Implement worldStore.ts**

```typescript
// frontend/src/stores/worldStore.ts
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { DeviceState, RoomState, EnvironmentState, WorldStateSnapshot, DeltaChange } from '../types/world-state'

export const useWorldStore = defineStore('world', () => {
  const environment = ref<EnvironmentState | null>(null)
  const devices = ref<Map<string, DeviceState>>(new Map())
  const rooms = ref<Map<string, RoomState>>(new Map())
  const simulationTick = ref(0)
  const sceneId = ref('')

  const getDevice = computed(() => (id: string) => devices.value.get(id))
  const activeDevices = computed(() => [...devices.value.values()].filter(d => d.state.power))

  function applyFullState(snap: WorldStateSnapshot) {
    simulationTick.value = snap.simulation_tick
    sceneId.value = snap.scene_id
    environment.value = snap.environment
    const devMap = new Map<string, DeviceState>()
    Object.entries(snap.devices).forEach(([k, v]) => devMap.set(k, v))
    devices.value = devMap
    const roomMap = new Map<string, RoomState>()
    Object.entries(snap.rooms).forEach(([k, v]) => roomMap.set(k, v))
    rooms.value = roomMap
  }

  function applyDelta(changes: DeltaChange[]) {
    for (const c of changes) {
      // Parse path like "devices[light_01].state.extra.brightness"
      const devMatch = c.path.match(/devices\[(\w+)\]/)
      if (devMatch) {
        const devId = devMatch[1]
        const dev = devices.value.get(devId)
        if (!dev) continue
        if (c.path.includes('.extra.')) {
          const key = c.path.split('.extra.')[1]
          dev.state.extra[key] = c.new_value
        } else if (c.path.includes('.power')) {
          dev.state.power = c.new_value
        }
        devices.value = new Map(devices.value) // trigger reactivity
      }
      const roomMatch = c.path.match(/rooms\[(\w+)\]/)
      if (roomMatch) {
        const roomId = roomMatch[1]
        const room = rooms.value.get(roomId)
        if (room && c.path.includes('.temperature')) room.temperature = c.new_value
        if (room && c.path.includes('.light_level')) room.light_level = c.new_value
        rooms.value = new Map(rooms.value)
      }
    }
  }

  return { environment, devices, rooms, simulationTick, sceneId, getDevice, activeDevices, applyFullState, applyDelta }
})
```

- [ ] **Step 2: Implement agentStore.ts**

```typescript
// frontend/src/stores/agentStore.ts
import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface AgentLogEntry {
  timestamp: number
  agent_name: string
  action: string
  reason: string
}

export const useAgentStore = defineStore('agents', () => {
  const agentStatuses = ref<Record<string, { status: string; confidence: number }>>({})
  const actionLog = ref<AgentLogEntry[]>([])
  const maxLog = 100

  function updateStatus(agentId: string, data: { status?: string; confidence?: number }) {
    agentStatuses.value[agentId] = { ...agentStatuses.value[agentId], ...data }
  }

  function appendLog(entry: AgentLogEntry) {
    actionLog.value.unshift(entry)
    if (actionLog.value.length > maxLog) actionLog.value.pop()
  }

  return { agentStatuses, actionLog, updateStatus, appendLog }
})
```

- [ ] **Step 3: Implement simulationStore.ts**

```typescript
// frontend/src/stores/simulationStore.ts
import { defineStore } from 'pinia'
import { ref } from 'vue'

export const useSimulationStore = defineStore('simulation', () => {
  const isRunning = ref(false)
  const speed = ref(1.0)
  const connectionStatus = ref<'connected' | 'reconnecting' | 'disconnected'>('disconnected')

  function setRunning(v: boolean) { isRunning.value = v }
  function setSpeed(v: number) { speed.value = v }
  function setConnectionStatus(v: typeof connectionStatus.value) { connectionStatus.value = v }

  return { isRunning, speed, connectionStatus, setRunning, setSpeed, setConnectionStatus }
})
```

- [ ] **Step 4: Verify no TS errors**

```bash
cd frontend && npx vue-tsc --noEmit
```

- [ ] **Step 5: Commit**

```bash
git add frontend/src/stores/ && git commit -m "feat: Pinia stores for world/agent/simulation state"
```

---

## Task 12: WebSocket Composable

**Files:**
- Create: `frontend/src/composables/useWebSocket.ts`

- [ ] **Step 1: Implement useWebSocket.ts**

```typescript
// frontend/src/composables/useWebSocket.ts
import { ref, onUnmounted } from 'vue'
import { useWorldStore } from '../stores/worldStore'
import { useAgentStore } from '../stores/agentStore'
import { useSimulationStore } from '../stores/simulationStore'
import type { WSMessage } from '../types/websocket'

export function useSimulationWebSocket() {
  const simStore = useSimulationStore()
  const worldStore = useWorldStore()
  const agentStore = useAgentStore()
  const ws = ref<WebSocket | null>(null)
  let reconnectTimer: number | null = null
  let reconnectDelay = 1000

  function connect(url: string = 'ws://localhost:8000/ws/simulation') {
    ws.value = new WebSocket(url)

    ws.value.onopen = () => {
      simStore.setConnectionStatus('connected')
      reconnectDelay = 1000
    }

    ws.value.onmessage = (event) => {
      const msg: WSMessage = JSON.parse(event.data)
      handleMessage(msg)
    }

    ws.value.onclose = () => {
      simStore.setConnectionStatus('disconnected')
      scheduleReconnect(url)
    }

    ws.value.onerror = () => {
      simStore.setConnectionStatus('disconnected')
    }
  }

  function handleMessage(msg: WSMessage) {
    switch (msg.type) {
      case 'STATE_FULL':
        worldStore.applyFullState(msg.payload)
        simStore.setRunning(msg.payload.is_running)
        break
      case 'STATE_DELTA': {
        const changes = msg.payload.changes
        // Check if it's agent status update
        const agentChanges = changes.filter((c: any) => c.path?.startsWith('agents['))
        const deviceChanges = changes.filter((c: any) => !c.path?.startsWith('agents['))

        if (deviceChanges.length) worldStore.applyDelta(deviceChanges)
        for (const ac of agentChanges) {
          const match = ac.path.match(/agents\[(\w+)\]/)
          if (match) agentStore.updateStatus(match[1], ac.new_value)
        }
        // Log agent actions
        for (const c of changes) {
          if (c.caused_by && c.caused_by !== 'system' && c.caused_by !== 'user') {
            agentStore.appendLog({
              timestamp: Date.now(),
              agent_name: c.caused_by,
              action: `${c.path} → ${JSON.stringify(c.new_value)}`,
              reason: c.reason || ''
            })
          }
        }
        break
      }
      case 'HEARTBEAT_PING':
        send({ type: 'HEARTBEAT_PONG' })
        break
    }
  }

  function send(msg: Record<string, any>) {
    if (ws.value?.readyState === WebSocket.OPEN) {
      ws.value.send(JSON.stringify(msg))
    }
  }

  function scheduleReconnect(url: string) {
    if (reconnectTimer) return
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      reconnectDelay = Math.min(reconnectDelay * 2, 30000)
      connect(url)
    }, reconnectDelay)
  }

  function sendCommand(type: string, payload: Record<string, any> = {}) {
    send({ type, payload })
  }

  onUnmounted(() => {
    if (reconnectTimer) clearTimeout(reconnectTimer)
    ws.value?.close()
  })

  return { connect, sendCommand, ws }
}
```

- [ ] **Step 2: Commit**

```bash
git add frontend/src/composables/useWebSocket.ts && git commit -m "feat: WebSocket composable with auto-reconnect"
```

---

## Task 13: 3D Scene Renderer (Procedural Apartment)

**Files:**
- Create: `frontend/src/components/scene/SceneRenderer.vue`
- Create: `frontend/src/components/scene/DeviceMesh.vue`
- Create: `frontend/src/composables/useAnimatedDevice.ts`

- [ ] **Step 1: Implement SceneRenderer.vue**

This is the main 3D canvas with procedural apartment geometry:

```vue
<!-- frontend/src/components/scene/SceneRenderer.vue -->
<template>
  <div class="w-full h-full absolute inset-0">
    <TresCanvas>
      <TresOrthographicCamera :position="[12, 12, 12]" :zoom="45" :lookAt="[0, 0, 0]" />
      <TresAmbientLight :intensity="0.4" />
      <TresDirectionalLight :position="[5, 10, 5]" :intensity="0.6" color="#ffffff" />

      <!-- Floor -->
      <TresMesh :position="[0, -0.05, 0]" receiveShadow>
        <TresBoxGeometry :args="[10, 0.1, 8]" />
        <TresMeshStandardMaterial color="#1a1a2e" />
      </TresMesh>

      <!-- Walls -->
      <TresMesh :position="[-5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 8]" />
        <TresMeshStandardMaterial color="#16213e" />
      </TresMesh>
      <TresMesh :position="[5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 8]" />
        <TresMeshStandardMaterial color="#16213e" />
      </TresMesh>
      <TresMesh :position="[0, 1.5, -4]">
        <TresBoxGeometry :args="[10, 3, 0.1]" />
        <TresMeshStandardMaterial color="#0f3460" />
      </TresMesh>
      <TresMesh :position="[0, 1.5, 4]">
        <TresBoxGeometry :args="[10, 3, 0.1]" />
        <TresMeshStandardMaterial color="#0f3460" />
      </TresMesh>

      <!-- Room dividers -->
      <TresMesh :position="[-2.5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 5]" />
        <TresMeshStandardMaterial color="#1a1a3e" transparent :opacity="0.6" />
      </TresMesh>
      <TresMesh :position="[2.5, 1.5, 0]">
        <TresBoxGeometry :args="[0.1, 3, 5]" />
        <TresMeshStandardMaterial color="#1a1a3e" transparent :opacity="0.6" />
      </TresMesh>

      <!-- Furniture: Sofa (living room, left area) -->
      <TresMesh :position="[-3.5, 0.25, -1]">
        <TresBoxGeometry :args="[2, 0.5, 0.8]" />
        <TresMeshStandardMaterial color="#2d3436" />
      </TresMesh>

      <!-- Furniture: Table -->
      <TresMesh :position="[0, 0.3, -2]">
        <TresBoxGeometry :args="[1.5, 0.05, 0.8]" />
        <TresMeshStandardMaterial color="#636e72" />
      </TresMesh>

      <!-- Furniture: Bed (bedroom, right area) -->
      <TresMesh :position="[3.5, 0.2, -2]">
        <TresBoxGeometry :args="[1.8, 0.4, 2]" />
        <TresMeshStandardMaterial color="#2d3436" />
      </TresMesh>

      <!-- Devices -->
      <DeviceMesh v-for="dev in devices" :key="dev.id" :device="dev" />
    </TresCanvas>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { TresCanvas } from '@tresjs/core'
import { useWorldStore } from '../../stores/worldStore'
import DeviceMesh from './DeviceMesh.vue'
import type { DeviceState } from '../../types/world-state'

const worldStore = useWorldStore()
const devices = computed(() => [...worldStore.devices.values()])
</script>
```

- [ ] **Step 2: Implement DeviceMesh.vue with GSAP animation**

```vue
<!-- frontend/src/components/scene/DeviceMesh.vue -->
<template>
  <!-- Light device -->
  <TresGroup v-if="device.type === 'light'" :position="devicePosition">
    <TresMesh ref="bodyRef">
      <TresCylinderGeometry :args="[0.15, 0.15, 0.08, 16]" />
      <TresMeshStandardMaterial :emissive="emissiveColor" :emissiveIntensity="emissiveIntensity" color="#333" />
    </TresMesh>
    <TresPointLight :intensity="lightIntensity" :color="lightColor" :distance="5" :position="[0, -0.3, 0]" />
  </TresGroup>

  <!-- HVAC device -->
  <TresGroup v-else-if="device.type === 'hvac'" :position="devicePosition">
    <TresMesh>
      <TresBoxGeometry :args="[0.8, 0.2, 0.3]" />
      <TresMeshStandardMaterial :color="device.state.power ? '#0984e3' : '#636e72'" />
    </TresMesh>
    <TresMesh :position="[0, 0, 0]">
      <TresBoxGeometry :args="[0.6, 0.05, 0.1]" />
      <TresMeshStandardMaterial color="#dfe6e9" transparent :opacity="0.5" />
    </TresMesh>
  </TresGroup>

  <!-- Curtain device -->
  <TresGroup v-else-if="device.type === 'curtain'" :position="devicePosition">
    <TresMesh :position="[0, 1, 0]">
      <TresBoxGeometry :args="[2, 2, 0.05]" />
      <TresMeshStandardMaterial :color="curtainColor" transparent :opacity="curtainOpacity" :side="2" />
    </TresMesh>
  </TresGroup>
</template>

<script setup lang="ts">
import { computed, watch, ref } from 'vue'
import gsap from 'gsap'
import type { DeviceState } from '../../types/world-state'

const props = defineProps<{ device: DeviceState }>()

const bodyRef = ref<any>(null)

// Map room name to 3D position
const roomPositions: Record<string, [number, number, number]> = {
  living_room: [-3.5, 2.5, -1],
  bedroom: [3.5, 2.5, -1],
  kitchen: [-3.5, 2.5, 2],
  bathroom: [3.5, 2.5, 2],
}

const devicePosition = computed<[number, number, number]>(() => {
  return roomPositions[props.device.location.room] || [0, 2.5, 0]
})

// Light properties
const emissiveIntensity = ref(0)
const lightIntensity = ref(0)
const lightColor = ref('#fff5e6')

const emissiveColor = computed(() => {
  const ct = props.device.state.extra.color_temp || 4000
  return ct < 3500 ? '#ff9f43' : ct < 4500 ? '#fff5e6' : '#dfe6e9'
})

// Curtain properties
const curtainOpacity = computed(() => {
  const open = props.device.state.extra.open_percent ?? 100
  return 1.0 - (open / 100) * 0.8
})
const curtainColor = computed(() => props.device.state.power ? '#2d3436' : '#636e72')

// Watch brightness changes → GSAP animate
watch(
  () => props.device.state.extra.brightness,
  (newVal) => {
    const target = (newVal || 0) / 100
    gsap.to(emissiveIntensity, { value: target * 2, duration: 0.8, ease: 'power2.out' })
    gsap.to(lightIntensity, { value: target * 3, duration: 0.8, ease: 'power2.out' })
  },
  { immediate: true }
)
</script>
```

- [ ] **Step 3: Verify frontend compiles**

```bash
cd frontend && npm run build
```

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/scene/ frontend/src/composables/ && git commit -m "feat: 3D procedural apartment scene with animated device meshes"
```

---

## Task 14: Dashboard UI Panels

**Files:**
- Create: `frontend/src/components/dashboard/DashboardOverlay.vue`
- Create: `frontend/src/components/dashboard/ControlPanel.vue`
- Create: `frontend/src/components/dashboard/AgentActionLog.vue`
- Create: `frontend/src/components/dashboard/StatusBar.vue`

- [ ] **Step 1: Implement StatusBar.vue**

```vue
<!-- frontend/src/components/dashboard/StatusBar.vue -->
<template>
  <div class="glass-panel absolute bottom-4 left-1/2 -translate-x-1/2 px-6 py-2 flex gap-8 items-center text-sm font-mono">
    <span>🕐 {{ timeOfDay }}</span>
    <span>🌡️ 室外 {{ outdoorTemp }}°C</span>
    <span :class="statusColor">● {{ statusText }}</span>
    <span>Tick: {{ tick }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useWorldStore } from '../../stores/worldStore'
import { useSimulationStore } from '../../stores/simulationStore'

const world = useWorldStore()
const sim = useSimulationStore()

const timeOfDay = computed(() => world.environment?.time_of_day || '--:--')
const outdoorTemp = computed(() => world.environment?.outdoor_temp?.toFixed(1) || '--')
const tick = computed(() => world.simulationTick)
const statusText = computed(() => sim.connectionStatus === 'connected' ? (sim.isRunning ? '运行中' : '已暂停') : '未连接')
const statusColor = computed(() => sim.isRunning ? 'text-green-400' : 'text-yellow-400')
</script>
```

- [ ] **Step 2: Implement ControlPanel.vue**

```vue
<!-- frontend/src/components/dashboard/ControlPanel.vue -->
<template>
  <div class="glass-panel absolute top-4 left-4 w-72 p-4 flex flex-col gap-3 max-h-[90vh] overflow-y-auto">
    <h2 class="text-sm font-bold text-blue-400 tracking-wider uppercase">仿真控制</h2>

    <div class="flex gap-2">
      <button @click="startSim" class="btn" :disabled="sim.isRunning">▶ 启动</button>
      <button @click="pauseSim" class="btn" :disabled="!sim.isRunning">⏸ 暂停</button>
      <button @click="resetSim" class="btn">↺ 重置</button>
    </div>

    <div class="flex items-center gap-2 text-sm">
      <span class="text-gray-400">速率:</span>
      <input type="range" min="0.5" max="10" step="0.5" v-model.number="speed" @change="changeSpeed" class="flex-1 accent-blue-500" />
      <span class="font-mono">{{ speed }}x</span>
    </div>

    <hr class="border-white/10" />
    <h3 class="text-xs font-bold text-gray-400 uppercase">设备控制</h3>

    <div v-for="dev in devices" :key="dev.id" class="text-xs space-y-1">
      <div class="flex justify-between items-center">
        <span class="text-gray-300">{{ dev.id }}</span>
        <span :class="dev.state.power ? 'text-green-400' : 'text-gray-500'">
          {{ dev.state.power ? 'ON' : 'OFF' }}
        </span>
      </div>
      <div v-if="dev.type === 'light' && dev.state.power">
        <label class="text-gray-500">亮度: {{ dev.state.extra.brightness }}</label>
        <input type="range" min="0" max="100" :value="dev.state.extra.brightness"
          @input="controlDevice(dev.id, 'extra.brightness', +$event.target.value)" class="w-full accent-yellow-500" />
      </div>
      <div v-if="dev.type === 'hvac' && dev.state.power">
        <label class="text-gray-500">目标温度: {{ dev.state.extra.target_temp }}°C</label>
        <input type="range" min="16" max="30" step="0.5" :value="dev.state.extra.target_temp"
          @input="controlDevice(dev.id, 'extra.target_temp', +$event.target.value)" class="w-full accent-blue-500" />
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useWorldStore } from '../../stores/worldStore'
import { useSimulationStore } from '../../stores/simulationStore'
import { useSimulationWebSocket } from '../../composables/useWebSocket'

const world = useWorldStore()
const sim = useSimulationStore()
const { sendCommand } = useSimulationWebSocket()

const speed = ref(1.0)
const devices = computed(() => [...world.devices.values()])

function startSim() { sendCommand('CMD_SIM_START') }
function pauseSim() { sendCommand('CMD_SIM_PAUSE') }
function resetSim() { sendCommand('CMD_SIM_RESET') }
function changeSpeed() { sendCommand('CMD_SIM_SPEED', { speed: speed.value }) }
function controlDevice(deviceId: string, property: string, value: number) {
  sendCommand('CMD_DEVICE_CONTROL', { device_id: deviceId, property, value })
}
</script>

<style scoped>
.btn { @apply px-3 py-1 text-xs rounded bg-white/5 hover:bg-white/10 border border-white/10 transition-colors disabled:opacity-30; }
</style>
```

- [ ] **Step 3: Implement AgentActionLog.vue**

```vue
<!-- frontend/src/components/dashboard/AgentActionLog.vue -->
<template>
  <div class="glass-panel absolute top-4 right-4 w-80 p-4 max-h-[80vh] flex flex-col">
    <h2 class="text-sm font-bold text-blue-400 tracking-wider uppercase mb-2">Agent 决策流</h2>
    <div class="flex-1 overflow-y-auto space-y-1 text-xs font-mono">
      <div v-for="(entry, i) in agentStore.actionLog" :key="i"
        class="p-2 rounded bg-white/5 border-l-2"
        :class="entry.agent_name.includes('照明') ? 'border-yellow-400' : 'border-blue-400'">
        <div class="flex justify-between text-gray-500">
          <span class="text-gray-300">{{ entry.agent_name }}</span>
          <span>{{ new Date(entry.timestamp).toLocaleTimeString() }}</span>
        </div>
        <div class="text-gray-400 mt-1">{{ entry.action }}</div>
        <div v-if="entry.reason" class="text-gray-600 mt-0.5 italic">{{ entry.reason }}</div>
      </div>
      <div v-if="!agentStore.actionLog.length" class="text-gray-600 text-center py-4">等待 Agent 决策...</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { useAgentStore } from '../../stores/agentStore'
const agentStore = useAgentStore()
</script>
```

- [ ] **Step 4: Implement DashboardOverlay.vue**

```vue
<!-- frontend/src/components/dashboard/DashboardOverlay.vue -->
<template>
  <div class="absolute inset-0 pointer-events-none z-10">
    <div class="pointer-events-auto">
      <ControlPanel />
      <AgentActionLog />
      <StatusBar />
    </div>
  </div>
</template>

<script setup lang="ts">
import ControlPanel from './ControlPanel.vue'
import AgentActionLog from './AgentActionLog.vue'
import StatusBar from './StatusBar.vue'
</script>
```

- [ ] **Step 5: Update App.vue to wire everything**

```vue
<!-- frontend/src/App.vue -->
<template>
  <div class="w-screen h-screen relative overflow-hidden bg-[#0a0a0f]">
    <SceneRenderer />
    <DashboardOverlay />
    <div v-if="sim.connectionStatus === 'disconnected'"
      class="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 glass-panel p-8 text-center z-20">
      <p class="text-gray-400 mb-4">未连接到仿真服务器</p>
      <button @click="connectWS" class="px-4 py-2 bg-blue-600 rounded text-sm hover:bg-blue-500">连接</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useSimulationStore } from './stores/simulationStore'
import { useSimulationWebSocket } from './composables/useWebSocket'
import SceneRenderer from './components/scene/SceneRenderer.vue'
import DashboardOverlay from './components/dashboard/DashboardOverlay.vue'

const sim = useSimulationStore()
const { connect, sendCommand } = useSimulationWebSocket()

function connectWS() {
  connect('ws://localhost:8000/ws/simulation')
}
</script>
```

- [ ] **Step 6: Verify frontend builds**

```bash
cd frontend && npm run build
```

- [ ] **Step 7: Commit**

```bash
git add frontend/src/ && git commit -m "feat: dashboard UI with control panel, agent log, status bar"
```

---

## Task 15: End-to-End Integration + Demo

**Files:**
- Modify: `backend/main.py` (ensure _init_default_state includes users + agents)

- [ ] **Step 1: Update _init_default_state in main.py to include user and agent state**

In `backend/main.py` `_init_default_state()`, add after existing devices:

```python
    # Users
    from backend.engine.state import UserState, Location3D
    s.users["user_01"] = UserState(
        id="user_01", name="Alice",
        location=Location3D(room="living_room"),
        activity="idle"
    )
    # Agent status
    from backend.engine.state import AgentRuntimeState
    s.agents["lighting_agent"] = AgentRuntimeState(id="lighting_agent", name="照明管家", status="idle")
    s.agents["hvac_agent"] = AgentRuntimeState(id="hvac_agent", name="暖通管家", status="idle")
```

- [ ] **Step 2: Start backend**

```bash
cd backend && source .venv/bin/activate && uvicorn backend.main:app --reload --port 8000
```

- [ ] **Step 3: Start frontend (new terminal)**

```bash
cd frontend && npm run dev
```

- [ ] **Step 4: Verify integration checklist**

Open http://localhost:5173:
- [ ] Click "连接" → WebSocket connects → 3D scene appears
- [ ] Click "▶ 启动" → Simulation starts → StatusBar shows "运行中"
- [ ] Time advances → StatusBar shows changing time
- [ ] Agent decisions appear in right panel log
- [ ] Device brightness/temp sliders work
- [ ] GSAP animation: light glow changes smoothly
- [ ] Temperature changes reflected in environment

- [ ] **Step 5: Final commit**

```bash
git add -A && git commit -m "feat: complete MVP — multi-agent smart home simulation"
```

---

## Summary

| Task | Description | Key Files |
|------|-------------|-----------|
| 1 | Project Scaffolding | .gitignore, requirements.txt, config.toml |
| 2 | WorldState Models | engine/state.py |
| 3 | EventBus | engine/event_bus.py |
| 4 | StateManager + Schemas | engine/state_manager.py, models/schemas.py |
| 5 | FastAPI + WebSocket | main.py, api/ws.py, api/routes.py |
| 6 | EnvironmentSimulator | simulators/environment.py |
| 7 | UserBehaviorSimulator | simulators/user_behavior.py |
| 8 | Agents (Lighting + HVAC) | agents/base.py, lighting.py, hvac.py |
| 9 | SimulationEngine | agents/runtime.py, engine/simulation.py |
| 10 | Frontend Setup | package.json, types/, styles/ |
| 11 | Pinia Stores | stores/*.ts |
| 12 | WebSocket Composable | composables/useWebSocket.ts |
| 13 | 3D Scene + Devices | components/scene/*.vue |
| 14 | Dashboard UI | components/dashboard/*.vue |
| 15 | Integration + Demo | App.vue, main.py update |

**Deliverable:** 打开浏览器 → 连接服务器 → 启动仿真 → 看到3D公寓中灯光和温度随时间自动调节 → Agent决策流实时滚动 → 可手动控制设备

