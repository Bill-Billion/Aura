# Changelog

All notable changes to this project will be documented in this file.

## [0.1.3.5] - 2026-04-20

### Added

- **Phase 1 protocol docs**: 新增 `/docs/architecture/ws-protocol.md`，把 WebSocket 命令、服务端消息、`SIM_EVENT` 类型和 `ERROR` 格式收敛成一份公共契约
- **SimulationClient contract**: 新增 `backend/models/simulation_client.py`，定义 `connect / disconnect / send_command / iter_events / get_snapshot` 最小协议
- **Compose dev stack**: 新增 `docker-compose.yml`、`backend/Dockerfile` 和 `frontend/Dockerfile`，提供一键起前后端的联调环境

### Changed

- **Event-driven runtime**: `SimulationEngine` 改成 `SimulatorTimer + EventBus` 驱动，移除集中 `_tick` 主循环，`system.timer_tick` 和 `environment.state_refresh` 会实时外发
- **WebSocket error schema**: `ERROR` 消息统一改为 `{ code, message, details }`，设备控制失败会带明确上下文
- **Reset lifecycle**: `CMD_SIM_RESET` 改成复用现有引擎实例并替换 `StateManager`，不再重建整套 `SimulationEngine`
- **Frontend contract types**: 前端补齐 `SIM_EVENT`、`ERROR`、`SIMULATION_STATUS` 和 `users` 快照类型，`Vite` 代理目标改成环境变量可配置
- **README quickstart**: README 更新成本机直跑、统一脚本、Docker Compose 三种入口，并把公开 device/event/message 枚举写进文档

### Testing

- 后端 `pytest tests/test_simulator_timer.py tests/test_state_manager.py tests/test_user_behavior_sim.py tests/test_environment_sim.py tests/test_simulation.py tests/test_main.py -q` 通过
- 额外覆盖了 `system.timer_tick`、`environment.state_refresh` 和结构化 `ERROR.details` 的 WebSocket 回归

## [0.1.3.4] - 2026-04-20

### Changed

- **Upper-floor light source rendering**: 修正二层和三层灯位 uniform 没有真正绑定到 shader 的问题，楼层升降后灯光现在会跟着场景一起移动，不再靠墙体泛白伪装亮灯
- **Light source presence**: 为各层补了跟随灯状态的展厅灯源片，二层和三层点亮后会看到明确的光源位置，不再只有墙面和玻璃发白
- **Upper-floor tuning**: 下调二层和三层的光体积、发光片尺寸和上层增益，避免之前那种整屋过曝的白片感

### Testing

- 前端 `node --experimental-strip-types --test tests/*.test.ts` 通过
- 前端 `npm run build` 通过
- 本地浏览器完成 F2 / F3 开关灯截图回归，确认上层由“墙面泛白”改成“有可见灯源 + 房间受光”

## [0.1.3.3] - 2026-04-20

### Changed

- **Upper-floor lighting**: 二层和三层的 SDF 灯光现在会跟随楼层升降同步到世界坐标，不再停在 `y=0` 导致高楼层几乎没有受光
- **Light aggregation**: 同层多盏灯改成按最大亮度聚合，避免“一盏开一盏关”时后写入的设备把整层灯光误压暗
- **Shader response**: 为上层展厅补了更强的墙体、楼板和家具受光响应，二层和三层开关灯时会有明确的亮暗变化

### Testing

- 前端 `node --experimental-strip-types --test tests/*.test.ts` 通过，新增楼层灯光 uniform 回归测试
- 前端 `npm run build` 通过
- 本地开发环境完成 F2 / F3 开灯关灯截图对照，确认右侧状态与场景亮暗变化一致

## [0.1.3.2] - 2026-04-20

### Added

- **Dev stack entrypoint**: 新增 `scripts/dev-stack.sh` 和 `scripts/check_ws_keepalive.py`，统一处理本地前后端起栈、PID/日志记录、端口冲突检测和 `5173/ws/simulation` 的 5 秒保活校验

### Changed

- **Curtain runtime**: 窗帘位姿改成“外侧边缘固定、内侧边缘滑动”，打开时会收在窗口两侧，关闭时回到中间
- **Animation coverage**: 开发环境下如果设备声明了 `scene_bindings.animation` 却没有命中对应场景节点，会打印一次明确告警，避免出现“注册了设备但场景没有反应”的假绑定
- **Fan binding guard**: 为风扇 `rotor_nodes` / `head_nodes` 增加不重叠回归校验，锁住“扇叶转、机头摆”的绑定语义

### Testing

- 后端 `backend/.venv/bin/python -m pytest tests/test_main.py tests/test_device_registry.py -q` 通过，包含 5 秒 WebSocket longevity 校验
- 前端 `node --experimental-strip-types --test frontend/tests/deviceAnimationMath.test.ts` 通过
- `./scripts/dev-stack.sh start` 可完成 HTTP health 与代理 WebSocket keepalive 校验

## [0.1.3.1] - 2026-04-19

### Changed

- **Curtain runtime**: 二层和三层窗帘的绑定轴改回 GLB 本地 `z`，窗帘滑动距离改成按内边缘和父坐标系边界计算，打开时会沿正确轨道方向滑开
- **Fan runtime**: 客厅风扇重新绑定为 `fan01` 扇叶旋转、`fan02` 头部摇头，默认送风时只转扇叶，不再整颗风扇头一起自转

### Testing

- 后端 `backend/.venv/bin/python -m pytest tests -q` 通过，74 个测试全部通过
- 前端 `node --experimental-strip-types --test frontend/tests/*.test.ts` 和 `npm run build` 通过

## [0.1.3.0] - 2026-04-18

### Added

- **Device registry**: 新增统一设备注册表，默认场景一次性接入灯光、空调、窗帘、风扇、摄像头和环境传感器
- **New control surfaces**: 右侧 contextual panel 新增风扇控制、摄像头预览和传感器只读面板

### Changed

- **World state metadata**: `STATE_FULL` 里的设备现在自带 `display_name`、`floor_id`、`ui_group` 和 `capabilities`
- **Command validation**: WebSocket 设备控制按能力校验，风扇支持 `speed / shake / timeout`，传感器写入会返回明确错误
- **Showroom shell**: 右侧楼层摘要改成按“照明 / 设备 / 安防 / 环境”分组，设备列表不再依赖硬编码 ID
- **HVAC control**: 空调面板补齐风速和更多模式，与 gamemcu 的控制语义更接近

### Testing

- 后端 `pytest tests -v` 通过，73 个测试全部通过
- 前端 `npm run build` 通过

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
