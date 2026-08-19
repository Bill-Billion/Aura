# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- **S3 multi-agent orchestration**: 加入 `HomeOrchestratorAgent`、Security / Energy / Scene 域 Agent、结构化任务分解、统一仲裁门、能源约束否决、显式用户覆盖与 stale-decision 丢弃证据。
- **Per-run LLM policies**: canonical run 现在可显式选择 `rule_based / llm_mocked / llm_recorded / llm_live` baseline；服务端单独记录实际 `llm_mode`，recorded 支持受控首次录制与契约一致的原始来源回放，客户端不能提交 key、provider URL 或文件路径。
- **Recording integrity manifest**: recorded capture 同步保存请求数、成功数与内容 hash；不完整录制、写入失败、回放 miss、链式 source、契约/代码版本漂移，或同一请求 key 对应互相矛盾的 decision，都不能作为可复现来源。
- **Versioned run provenance**: `run.json` 增加 baseline/source、场景时长、场景评估契约 hash 和 scenario/event/command/device-registry 四个 schema version；事件与命令生命周期也携带对应版本。
- **Source revision provenance**: run 同时持久化构建注入的 commit/image revision，或本地后端源码与配置的稳定 SHA-256；A/B 比较与 recorded source admission 不再把相同手工版本号误当成相同代码。
- **Finalized trace seal**: event writer 在 flush/fsync/close 后持久化 event count、final seq 与 byte-exact SHA-256；finalized JSON、报告和 raw/canonical 导出统一验证，删尾与篡改明确失败。
- **Research Workspace**: 主界面新增 Setup / Live / Compare 完整入口，支持 canonical scenario 启动、可复制 seed、A/B 独立状态与重试、同 scenario+seed 硬约束、策略 provenance、七指标表和双轨因果时间线。
- **Stable experiment exports**: finalized run 可导出 byte-exact raw JSONL 或 canonical JSONL；前端可导出包含两侧 provenance、报告和按 `seq` 排序原始事件的 comparison bundle。
- **S5 backend lifecycle**: REST 与 WebSocket 共用场景启动契约；并发启动只有一个请求成功，客户端可用 UUID `idempotency_key` 在同一进程的 1024 条有界缓存内从丢失的 201 恢复同一个 active/finalized run（不承诺跨重启恢复），canonical run 按场景模拟时长自动 finalized，报告与稳定 trace 在结束前返回结构化 `run_not_finalized`。

### Changed

- **S4 metric contract repaired**: 评估报告改为 `episode_complete`、`first_action_latency_ms`、`command_failure_count`、`fallback_count`、`conflict_count`、`user_intent_satisfied`、`device_state_match_rate` 七项 canonical 指标，替换此前语义不一致的替代指标。
- **Evidence-grounded evaluation**: episode 的同一条 feedback 祖先链必须完整穿过 perception、intent、decomposition、coordination、plan、approved 与 action，不能用 root 的 sibling 事件拼出假完整链；延迟使用 root 到首个动作的 wall time；命令按最终态去重；预期失败必须真实发生；设备效果读取 flat `path/new_value` 并按字段与 deadline 计算，数值区间拒绝非有限数和倒置边界；禁止设备、归一化 intent、必需 Agent role 与未知安全约束全部 fail closed。
- **Single report path**: API、离线评估与 suite 都从 run metadata 解析同一份 ScenarioSpec 和 persisted events，输出 `report_schema_version`、`failed_metrics` 与 provenance；suite 的 seed override 不再改变场景契约 hash，三条入口不会对同一 run 给出不同结论。动态历史报告另记当前 `evaluator_source_revision`，不再把运行代码版本冒充成报告生成代码版本。
- **Observability truth**: episode cancellation 只由 `system.episode_cancelled` 标记，不再把 stale `reasoning.decision_discarded` 错当成整条 episode 取消；3D 设备点击继续联动 Live/Compare 过滤。
- **Frontend unit runner**: README 和 CI 口径统一为 Vitest `npm test`；Research Workspace 关闭后保留运行状态并把焦点还给入口。
- **Typed generation configuration**: scenario 的噪声与设备掉线注入改为严格嵌套契约，非法概率、未知字段和反向舒适区在换 run 前拒绝；罕见 post-commit 失败也会以 `launch_failed` 收尾，不再永久锁住控制面。

### Fixed

- **Rule-based isolation**: 显式 `rule_based` 即使服务器配置了真实 LLM key 也不会构造 live provider，消除基线静默打网和计费风险。
- **Run/report race boundaries**: active canonical run 不再被第二个启动静默 supersede；旧 finalizer 不能结束新 run；raw/canonical trace 不再读取仍在追加的事件文件。
- **Experiment isolation**: canonical run 期间拒绝会改写世界或时钟的交互命令；交互 mutation 与场景启动共用原子边界，事件 recorder 也拒绝跨 run 追加。
- **Finalization and reconnect ordering**: timer tick 会完整 drain 后再关闭工件；finalizer 自身异常会把 run 标成 `finalization_failed` 并释放 active slot；连续 engine-error run 不会卡死后续 finalizer；WebSocket 重连的 `STATE_FULL + SIMULATION_STATUS` 作为有序批次先于后续增量发送。
- **Exact duration boundary**: live 与 headless 统一使用 `tick 1 = t0` 的截止语义；非整拍时长运行到首个覆盖 deadline 的完整 tick，当前 fan-out/工件写入排空后立即停止，不会漏掉 deadline 事件或随负载多跑一拍。
- **Event admission consistency**: 深度风暴中被拒事件不再占用 `seq`、从未知 parent 重启深度或先泄漏到 WebSocket；Live、EventBus history 与 finalized JSONL 只看到同一条 accepted event / suppression notice，前端也以 canonical `seq` 排序。
- **WebSocket slow-client isolation**: 每个 socket 的发送有界超时；失败连接进入不可复活终态、唤醒 receive loop 并 best-effort 关闭，初始化和 canonical 锁定错误的实际网络发送不再占用全局场景锁。
- **Cross-runtime seed safety**: 公开场景与启动 API 的 seed 收窄到 JavaScript JSON 安全整数域，避免相邻大 seed 在浏览器折成同一个实验。
- **Paid-provider access boundary**: API key 不再隐式开启 live；`AURA_ALLOW_LIVE_LLM=1` 才授权外部调用，本机浏览器 Origin 缺省可信，远程付费启动额外要求显式 Origin allowlist 与 Bearer token，REST/WS 均在修改状态前校验。
- **Hard LLM cost boundary**: OpenAI Responses 与 Anthropic-compatible provider 强制声明/发送输出 token 上限并记录真实 usage；同 episode 并发 Agent 会先原子预留 worst-case cost，已计费但解析失败的响应仍入账，缺 usage 时使用保守上界，无硬 cap 的付费 provider 在网络调用前拒绝，request/decision 同时限制字符串、列表与 JSON 总大小。
- **Per-call usage and recording cancellation**: 共享 provider 的 usage 改为 task-local，失败调用不会误读并发兄弟请求的账；recorded capture 被 timeout/reset 取消时会写入失败 manifest、标记 run 工件无效并阻止后续报告或 source admission。

### Testing

- 后端 `pytest tests/ --timeout=120` 通过。
- 前端 `npm test` 与 `npm run build` 通过。
- 前端构建仍有既有主 chunk 大于 500 kB 的提示；本条目不声明 Playwright 或 Docker 容器冒烟结果。

## [0.1.3.12] - 2026-04-28

### Fixed

- **Light control feedback**: 灯光和窗帘直控后会立即重算对应房间 `light_level`，F1/F2/F3 灯光不再只更新设备状态而不驱动 3D 房间光源。
- **MiniMax output compatibility**: Anthropic-compatible provider 现在会把 MiniMax 常见的百分制 `confidence` 归一化到 `0..1`，避免 `confidence: 95` 被误判为非法输出并触发规则回退。
- **WebSocket idle stability**: 移除 60 秒空闲关闭，前端不再因为观察侧栏或暂停态长时间无命令而反复重连。

### Changed

- **Simulation cadence**: 默认节奏改为真实墙钟每 2 秒一个 tick；`observe` 每 tick 推进 10 秒，`demo` 每 tick 推进 60 秒，让事件流和 LLM episode 更容易跟上。

### Testing

- 后端 `pytest tests -q` 通过
- 前端 `node --experimental-strip-types --test tests/*.test.ts` 通过
- 前端 `npm run build` 通过
- 本地 `./scripts/dev-stack.sh restart` 通过，`/api/health` 返回 `wall_tick_ms=2000`、`simulated_dt_seconds=10.0`
- WebSocket smoke 验证 `light_loft_01` 直控会返回 `rooms[loft].light_level` delta，真实 MiniMax episode 能产出非 fallback 的 `reasoning.intent_recognized`

## [0.1.3.11] - 2026-04-22

### Added

- **Simulation mode contract**: 新增 `observe / demo` 双模式、`CMD_SIM_MODE` 命令、`simulation_mode / wall_tick_ms / simulated_dt_seconds` 世界状态字段，以及 `/api/health` 的运行时摘要
- **Agent diagnostics**: `AGENT_STATUS` 和 `STATE_FULL.agents` 补充 `provider`、`provider_configured`、`last_latency_ms`、`last_trigger_event`，现在可以直接看出 MiniMax 是否真的在线
- **Room-level scene feedback**: 3D 场景新增房间局部 halo、房间点光源和环境光调色，开始消费 `rooms.*` 与 `environment.*`，不再只有设备动画在动

### Changed

- **Local env loading**: 后端、本地起栈脚本和 Docker Compose 统一改成从仓库根目录 `.env.local / .env` 读取本地 provider 配置，不再依赖手工 shell export
- **Simulation cadence**: 仿真改成固定墙钟节拍并引入 `observe / demo` 双模式；reset 也会把模式与显示状态一起归位。当前默认节奏见最新版本记录。
- **User/environment scripts**: 用户行为改成半小时粒度日常脚本，环境仿真改成确定性的天气/室外温度日变化，并把显著环境变化阈值收紧到更适合 LLM 的级别
- **Status UX**: 前端状态条和底部控制条现在会明确显示“仿真未开始 / 观察模式 / 演示模式 / Agent 是否在线”，暂停态不再像系统坏掉
- **Dev stack startup**: `scripts/dev-stack.sh` 现在会优先使用宿主机 Node 启动前端，并在每次启动时清空本轮日志，修掉 Codex 内置 Node 触发的 rolldown 原生 binding 签名冲突
- **MiniMax stability**: Anthropic-compatible MiniMax 路径增加了超时缓冲、episode 外层缓冲和更紧凑的 LLM 上下文，Lighting / HVAC 在本地联调里都能稳定进入真实 LLM 链路

### Testing

- 后端 `pytest tests -q` 通过
- 前端 `node --experimental-strip-types --test tests/*.test.ts` 通过
- 前端 `PATH=\"/opt/homebrew/bin:$PATH\" npm run build` 通过
- 本地 `./scripts/dev-stack.sh restart` 通过，`/api/health` 正常返回 `anthropic_compatible + MiniMax-M2.7 + configured=true`

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
