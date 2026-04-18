# Changelog

All notable changes to this project will be documented in this file.

## [0.1.2.0] - 2026-04-18

### Changed

- **Visual polish**: 继续按 gamemcu 参考站收紧总览镜头、楼层层次和右下车辆配重，让 F1 更像总览态视觉锚点
- **Left rail shell**: 左侧楼层切换改成更轻的细条导航，激活态改为黄线强调，不再用整块卡片高亮
- **Right panel shell**: 顶部天气卡改成展示型信息结构，右侧默认内容压成 live 预览、模式卡和单层摘要，减少驾驶舱式统计感
- **Bottom controls**: 底部控制条和日志入口缩成更细的辅助带，弱化调试台观感

### Testing

- 前端 `npm run build` 通过
- 重新启动本地前端开发服务并完成桌面端截图验收

## [0.1.1.0] - 2026-04-18

### Changed

- **Showroom rendering**: 统一 Three 颜色空间策略，重做玻璃、楼板、家具、家电、车辆和信息牌的材质职责，新增展厅级地面与反射层
- **Dashboard shell**: 将旧控制台式布局改成 gamemcu 风格壳层，右侧默认展示环境/摘要/模式卡，设备控制改为 contextual controller
- **Scene interaction**: 新增场景内 CSS2D 信息牌与对象选中链路，楼层镜头和权重重新调整为 F1 主锚点构图
- **WebSocket protocol**: 在保留 `STATE_FULL`、`STATE_DELTA`、`AGENT_STATUS` 的同时新增 `SIM_EVENT` 结构化事件通道

### Added

- **Event schema**: 引入 `SimEvent`，补齐 `event_id`、`wall_time`、`correlation_id`、`causal_parent`、`priority`
- **EventBus queries**: 支持按 `correlation_id`、`source`、`priority`、`causal_parent` 检索历史，并提供因果链查询
- **Structured event flow**: 为 `user.command`、`user.activity_change`、`action.device_control`、`feedback.state_delta` 建立迁移期结构化事件流
- **Frontend event store**: 前端新增轻量事件缓存，为下一阶段 ObservabilityPanel 留出接入点
- **Architecture docs**: 新增 `/docs/architecture/sim-event-schema.md`

### Testing

- 新增 `SimEvent` 升级、关联链过滤和因果链查询测试
- 新增 WebSocket 结构化事件与兼容旧消息并存测试
- 前端 `npm run build` 通过，后端结构化事件相关测试通过

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
