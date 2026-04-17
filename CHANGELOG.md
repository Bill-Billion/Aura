# Changelog

All notable changes to this project will be documented in this file.

## [0.1.0.0] - 2026-04-17

### Added

- **Backend core**: EventBus (async pub/sub with history), StateManager (delta tracking + snapshots), FastAPI + WebSocket gateway with ConnectionManager
- **Simulation engine**: SimulationEngine tick loop with agent runtime, EnvironmentSimulator (temperature/light physics), UserBehaviorSimulator with daily schedules
- **Agent system**: LightingAgent + HVACAgent with rule-based strategies, AgentRuntime for multi-agent orchestration
- **World models**: Pydantic WorldState models with room/device/agent schemas, WSMessage protocol types
- **REST API**: `/api/scenes` endpoint for scene listing, `/api/health` health check
- **Frontend scaffold**: Vue 3 + TresJS + Pinia + TailwindCSS project structure
- **State management**: Pinia stores for world state (delta updates), agent logs, simulation control, and UI state
- **WebSocket client**: Auto-reconnect composable with exponential backoff and message routing
- **3D scene**: Procedural apartment rendering with animated device meshes (lights, HVAC, curtains)
- **Dashboard UI**: Control panel for device commands, agent action log, simulation control bar, status bar
- **Design system**: CSS variables, glassmorphism theme, GSAP animations
- **GLSL shaders**: SDF area lights, mathematical matcap materials, Fresnel glass effect
- **3D composables**: Spherical camera with spring damping, shader materials, GLB loader (DRACO + Meshopt), device animations
- **Config-driven rooms**: SceneConfig JSON types, useSceneConfig composable, RoomModule + SceneManager
- **Mi Home scene**: Apartment_v1 scene configuration, multi-floor GLB models (F1/F2/F3), gamemcu-style rendering
- **Dashboard overlay**: Floor selector, home panel groups, AI chat panel, scene selector
- **App layout**: Integrated SceneRenderer + DashboardOverlay with sidebar navigation

### Testing

- Backend test suite: 22 tests across test_main.py (WebSocket handler), test_ws.py (ConnectionManager), test_routes.py (REST API)
- All tests passing with pytest + pytest-asyncio + anyio

### Infrastructure

- Project scaffolding for backend (FastAPI) and frontend (Vue 3 + Vite)
- gstack skill routing rules in CLAUDE.md
