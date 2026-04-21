# Changelog

All notable changes to this project will be documented in this file.

## [0.1.3.9] - 2026-04-21

### Added

- **ObservabilityPanel**: 新增真正的右滑观测侧栏，顶部展示 episode 摘要和筛选，中段展示因果链时间线，底部展示 reasoning / action / feedback 细节
- **Episode view model**: 前端新增 episode 聚合、事件分类、详情映射和面板状态推导工具，默认按 `correlation_id` 把 `SIM_EVENT` 组织成 episode 视图
- **Observability tests**: 新增 `frontend/tests/observability.test.ts`，覆盖 episode 分组、根事件识别、默认选中、详情映射和 500 条窗口截断回归

### Changed

- **Sidebar replacement**: `DashboardOverlay` 已经用 `ObservabilityPanel` 替换旧 `AIChatPanel`，底部开关文案从“日志”切到“观测”
- **Event store upgrade**: `eventStore` 从单纯 `events[]` 升级成带 `episodes`、筛选、选中 episode / event 和连接派生状态的前端观测 store
- **Responsive sidebar**: 观测侧栏现在按视口分档，桌面宽屏用 480px，常规桌面用 400px，中等桌面改为全屏叠加

### Testing

- 前端 `node --experimental-strip-types --test tests/*.test.ts` 通过
- 前端 `npm run build` 通过
- 构建结果继续保持现有大包 warning，没有新增 TypeScript 或 Vue 编译错误

## [0.1.3.8] - 2026-04-20

### Changed

- **Agent episode concurrency**: `AgentRuntime` 现在会并发评估同一根事件下的多个 agent，避免兼容 provider 串行阻塞把整条推理链拖慢
- **Episode timeout fallback**: 新增 `AGENT_EPISODE_TIMEOUT_MS` 配置，默认跟随 `LLM_TIMEOUT_MS`，慢 provider 会在 episode 级别快速超时并回退到规则逻辑
- **Provider timeout diagnostics**: OpenAI Responses 和 Anthropic-compatible provider 的 timeout 错误现在会生成明确消息，不再出现空白异常；Anthropic invalid output 仍会附带 raw preview

### Testing

- `pytest tests/test_openai_provider.py tests/test_anthropic_provider.py tests/test_phase2_runtime.py -q` 通过
- 后端 `pytest tests -q`、前端 `node --experimental-strip-types --test tests/*.test.ts` 和 `npm run build` 通过

## [0.1.3.7] - 2026-04-20

### Added

- **Anthropic-compatible provider**: 新增 `AnthropicCompatibleProvider`，可通过 Anthropic 风格的 `messages` 接口接 MiniMax 这类兼容服务，并统一映射成现有 `AgentLLMDecision`
- **Provider selection test**: 新增 `tests/test_anthropic_provider.py` 和运行时 provider 选择回归，锁住 Anthropic-compatible provider 的请求格式、超时映射和 JSON 解析行为

### Changed

- **Runtime provider selection**: `AgentRuntime` 现在支持通过 `LLM_PROVIDER=anthropic_compatible` 显式切到 Anthropic-compatible provider；未显式指定时，会先尝试 OpenAI，再尝试 Anthropic-compatible 环境变量
- **Decision parsing**: LLM 输出解析层现在会容忍 ```json fenced block``` 这类常见返回格式，避免兼容模型因为包了一层 markdown 就直接掉到 fallback
- **README setup**: README 增补了 MiniMax Anthropic-compatible 接入示例和相关环境变量说明

### Testing

- `pytest tests/test_anthropic_provider.py tests/test_openai_provider.py tests/test_phase2_components.py -q` 通过

## [0.1.3.6] - 2026-04-20

### Added

- **Phase 2 agent runtime**: 新增 `backend/agents/llm.py`、`backend/agents/memory.py`、`backend/agents/arbiter.py` 和 `backend/agents/types.py`，把事件驱动 Agent 运行时需要的 LLM provider、短期记忆、冲突仲裁和结构化契约收拢成独立模块
- **Reasoning event contract**: 新增 `reasoning.perception_snapshot`、`reasoning.intent_recognized`、`reasoning.task_decomposition`、`reasoning.coordination_decision`、`reasoning.execution_plan` 和 `reasoning.fallback_rule_based` 事件类型，并补齐前端 TS 类型

### Changed

- **Event-driven agents**: `LightingAgent` 和 `HVACAgent` 升级成事件驱动接口，保留旧 `decide(world)` 作为 fallback 规则逻辑
- **Simulation orchestration**: `SimulationEngine` 移除 timer tick 内的直接 `agent_runtime.step(world)` 调用，改成由 `AgentRuntime` 订阅 `user.activity_change` 和显著 `environment.state_refresh` 根事件异步起 episode
- **OpenAI integration**: 默认接入 OpenAI Responses API，模型配置由 `OPENAI_MODEL`、`OPENAI_REASONING_EFFORT`、`LLM_TIMEOUT_MS` 控制；缺失 key 或调用失败时自动发 `reasoning.fallback_rule_based`
- **Agent status fields**: `AgentRuntimeState` 与前端状态类型新增 `mode`、`active_correlation_id`、`last_reasoning_step`、`last_fallback_reason`
- **Protocol docs**: README 和 `docs/architecture/ws-protocol.md` 更新到 Phase 2，补充 reasoning 事件 payload、环境变量和 Agent 状态字段说明

### Testing

- `pytest tests/test_phase2_components.py tests/test_openai_provider.py tests/test_phase2_runtime.py -q` 通过
- `pytest tests/test_agents.py tests/test_simulation.py tests/test_main.py tests/test_ws.py tests/test_state.py -q` 通过

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
- **Frontend scaffold**: Vue 3 + Vite project structure with 3D scene and dashboard overlay

### Testing

- 后端测试套件初始化完成，覆盖 REST、WebSocket 和基础状态模型
