# Aura

Smart Home AI Agent Behavior Observability Platform — 智能家居 Agent 行为可观测性平台

Aura is a simulation and visualization platform for observing how AI agents control IoT devices in smart home environments. It provides a 3D real-time view of agent decision-making, device states, and environmental changes.

## Features

- **3D Showroom Visualization** — Multi-floor apartment rendering with TresJS/Three.js, custom GLSL shaders, world-floor reflection layers, and a tighter gamemcu-style shell
- **AI Agent Simulation** — Rule-based agents (Lighting, HVAC) with an extensible architecture for LLM-powered autonomous agents
- **Structured Event Flow** — `SimEvent` now carries `event_id`, `correlation_id`, `causal_parent`, and priority for user/action/feedback tracing
- **Registered Device Catalog** — Default scene now ships with lights, HVAC, curtains, fans, cameras, and environment sensors, all with explicit metadata and capability flags
- **Real-time Observability Foundation** — WebSocket now exposes legacy state sync and `SIM_EVENT` side by side, ready for the next-stage observability panel
- **Interactive Dashboard** — Slim left floor rail, lighter right showroom cards, grouped device summary, contextual device controller, compact simulation controls, and auxiliary event log

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| Frontend | Vue 3.5 + TypeScript, TresJS 5.8 (Three.js), Pinia 3.0, TailwindCSS 4, GSAP 3.14 |
| 3D | Custom GLSL shaders, DRACO + Meshopt compressed GLB models |
| Backend | FastAPI, Pydantic v2, WebSocket, structlog |
| Testing | pytest + pytest-asyncio (74 backend tests) |

## Architecture

```
aura/
├── backend/
│   ├── api/          # FastAPI routes + WebSocket gateway
│   ├── agents/       # AI agents (Lighting, HVAC) + AgentRuntime
│   ├── engine/       # SimulationEngine, EventBus, SimEvent, StateManager
│   ├── models/       # Pydantic schemas (WorldState, WSMessage)
│   ├── simulators/   # Environment physics + User behavior
│   └── core/         # Logging
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── scene/       # 3D rendering (SceneRenderer, shaders, CSS2D labels)
│   │   │   └── dashboard/   # Showroom shell, contextual controller, scene presets
│   │   ├── composables/     # useWebSocket, useSphericalCamera, useShaderMaterials
│   │   ├── stores/          # Pinia stores (world, agent, simulation, ui, events)
│   │   ├── shaders/         # GLSL vertex/fragment shaders
│   │   └── types/           # TypeScript type definitions
│   └── public/
│       ├── models/          # GLB 3D models (F1, F2, F3)
│       ├── scenes/          # Scene configuration JSON
│       └── textures/        # Matcap textures, HDR environment maps
└── tests/                   # Backend test suite
```

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 18+

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173

### Recommended: fresh local stack

调试时优先使用仓库根目录的统一起栈脚本，它会清理已记录的旧 PID、检查 `8000/5173` 端口占用，并在启动后额外校验一次通过前端代理的 WebSocket 保活。

```bash
./scripts/dev-stack.sh start
./scripts/dev-stack.sh status
./scripts/dev-stack.sh verify
./scripts/dev-stack.sh stop
```

### Running Tests

```bash
cd backend
pytest ../tests/ -v
```

前端构建验证：

```bash
cd frontend
npm run build
```

## Event Schema

结构化事件字段和关联规则写在 `/docs/architecture/sim-event-schema.md`。下一阶段的 ObservabilityPanel 会直接消费这条事件流，而不是继续依赖旧的 delta 文本日志。

## Device Registration

默认设备注册与接入主线写在 `/docs/architecture/gamemcu-device-registration-plan.md`。当前系统已经把设备能力显式写进 `STATE_FULL`，前端可以直接按 `ui_group` 和 `capabilities` 渲染交互，不需要再维护一套分散的硬编码映射。

## Roadmap

- [ ] Event-driven simulation engine (replace tick loop)
- [ ] LLM-powered autonomous agents with intent recognition
- [ ] Multi-agent collaboration and task decomposition
- [ ] User habit memory system
- [ ] Improved physics simulation (inter-room heat transfer)
- [ ] Frontend test suite

## License

MIT
