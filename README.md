# Aura

Smart Home AI Agent Behavior Observability Platform — 智能家居 Agent 行为可观测性平台

Aura is a simulation and visualization platform for observing how AI agents control IoT devices in smart home environments. It provides a 3D real-time view of agent decision-making, device states, and environmental changes.

## Features

- **3D Smart Home Visualization** — Multi-floor apartment rendering with TresJS/Three.js, custom GLSL shaders (SDF area lights, matcap materials, Fresnel glass)
- **AI Agent Simulation** — Rule-based agents (Lighting, HVAC) with an extensible architecture for LLM-powered autonomous agents
- **Real-time Observability** — WebSocket-powered live updates showing agent reasoning chains and device state changes
- **Event-Driven Engine** — Async EventBus + StateManager with delta tracking and snapshot support
- **Interactive Dashboard** — Device control panels, agent action log, simulation controls, floor/scene selection

## Tech Stack

| Layer | Technologies |
|-------|-------------|
| Frontend | Vue 3.5 + TypeScript, TresJS 5.8 (Three.js), Pinia 3.0, TailwindCSS 4, GSAP 3.14 |
| 3D | Custom GLSL shaders, DRACO + Meshopt compressed GLB models |
| Backend | FastAPI, Pydantic v2, WebSocket, structlog |
| Testing | pytest + pytest-asyncio (22 backend tests) |

## Architecture

```
aura/
├── backend/
│   ├── api/          # FastAPI routes + WebSocket gateway
│   ├── agents/       # AI agents (Lighting, HVAC) + AgentRuntime
│   ├── engine/       # SimulationEngine, EventBus, StateManager
│   ├── models/       # Pydantic schemas (WorldState, WSMessage)
│   ├── simulators/   # Environment physics + User behavior
│   └── core/         # Logging
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   │   ├── scene/       # 3D rendering (SceneRenderer, FloorGroup, shaders)
│   │   │   └── dashboard/   # UI overlay (ControlPanel, AgentActionLog, etc.)
│   │   ├── composables/     # useWebSocket, useSphericalCamera, useShaderMaterials
│   │   ├── stores/          # Pinia stores (world, agent, simulation, ui)
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

### Running Tests

```bash
cd backend
pytest ../tests/ -v
```

## Roadmap

- [ ] Event-driven simulation engine (replace tick loop)
- [ ] LLM-powered autonomous agents with intent recognition
- [ ] Multi-agent collaboration and task decomposition
- [ ] User habit memory system
- [ ] Improved physics simulation (inter-room heat transfer)
- [ ] Frontend test suite

## License

MIT
