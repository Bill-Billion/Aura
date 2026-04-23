# SmartHomeSim

SmartHomeSim 是一个面向智能家居 Agent 的 3D 仿真与观测平台。当前版本已经完成 Phase 3 的前端观测侧栏替换，后端继续保留 `STATE_FULL / STATE_DELTA / AGENT_STATUS / SIM_EVENT` 兼容输出，前端则把 episode 级可观测性正式接到右滑侧栏上。

## 当前能力

- 多楼层 3D 展厅渲染，支持灯光、空调、窗帘、风扇、摄像头和环境传感器
- 事件驱动仿真内核，`SimEvent` 已覆盖 timer、环境刷新、用户活动、设备动作和状态反馈
- Lighting / HVAC Agent 以事件订阅方式运行，支持 `user.activity_change` 和显著 `environment.state_refresh` 触发
- OpenAI Responses 和 Anthropic-compatible provider 双路接入，推理结果统一落成 `reasoning.*` 事件，超时或异常会自动回退到现有规则逻辑
- 右滑 `ObservabilityPanel` 默认按 episode 展示 `root -> reasoning -> action -> feedback` 链路，并支持 agent / 类别 / fallback 本地筛选
- 一键本地起栈脚本和 Docker Compose 联调入口

## 技术栈

| 层 | 技术 |
| --- | --- |
| Frontend | Vue 3.5、TypeScript、TresJS、Three.js、Pinia、TailwindCSS、GSAP |
| Backend | FastAPI、Pydantic v2、WebSocket、structlog、httpx |
| Runtime | Python 3.10+、Node.js 18+ |
| Testing | pytest、node:test、Vue TSC、Vite build |

## 5 分钟快速开始

### 方式一：直接在本机运行

先在仓库根目录创建本地环境文件：

```bash
cat > .env.local <<'EOF'
LLM_PROVIDER=anthropic_compatible
ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic
ANTHROPIC_API_KEY=your_key_here
ANTHROPIC_MODEL=MiniMax-M2.7
LLM_TIMEOUT_MS=12000
AGENT_EPISODE_TIMEOUT_MS=15000
AGENT_ENV_DEBOUNCE_MS=5000
EOF
```

后端和本地起栈脚本都会自动读取仓库根目录的 `.env.local` / `.env`，不需要再手工 `export`。

再启动后端：

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

如果没有显式设置 `LLM_PROVIDER`，运行时会优先尝试 `OPENAI_API_KEY`，否则再尝试 `ANTHROPIC_API_KEY / ANTHROPIC_COMPAT_API_KEY`。

`LLM_TIMEOUT_MS` 控制单次 provider HTTP 请求的超时，`AGENT_EPISODE_TIMEOUT_MS` 控制单个 agent episode 最长占用时间，`AGENT_ENV_DEBOUNCE_MS` 控制环境刷新触发 Agent 的防抖窗口。

像 MiniMax 这类响应更慢的 Anthropic-compatible provider，如果继续用过短超时，系统会更快回退到规则逻辑；示例仍推荐从 `12000 / 15000ms` 起步，但运行时会给 MiniMax 自动补一层超时缓冲，避免刚好卡在线上。

再启动前端：

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 [http://localhost:5173](http://localhost:5173)。

### 方式二：用统一起栈脚本

```bash
./scripts/dev-stack.sh start
./scripts/dev-stack.sh verify
./scripts/dev-stack.sh stop
```

### 方式三：用 Docker Compose

```bash
docker compose up --build
```

Compose 环境下前端代理会自动指向 `http://backend:8000`。

## WebSocket 公共契约

### 客户端命令

当前开放的命令类型：

- `CMD_SIM_START`
- `CMD_SIM_PAUSE`
- `CMD_SIM_RESET`
- `CMD_SIM_MODE`
- `CMD_SIM_SPEED`
- `CMD_DEVICE_CONTROL`

### 服务端消息

当前公开的消息类型：

- `STATE_FULL`
- `STATE_DELTA`
- `AGENT_STATUS`
- `SIM_EVENT`
- `SIMULATION_STATUS`
- `ERROR`

### 结构化事件

当前公开的 `SIM_EVENT.event_type`：

- `system.timer_tick`
- `system.simulation_started`
- `system.simulation_paused`
- `system.simulation_reset`
- `environment.state_refresh`
- `user.command`
- `user.activity_change`
- `reasoning.perception_snapshot`
- `reasoning.intent_recognized`
- `reasoning.task_decomposition`
- `reasoning.coordination_decision`
- `reasoning.execution_plan`
- `reasoning.fallback_rule_based`
- `action.device_control`
- `feedback.state_delta`

### Agent 状态字段

`AGENT_STATUS` 和 `STATE_FULL.agents` 目前包含这些字段：

- `id`
- `name`
- `status`
- `current_strategy`
- `confidence`
- `last_action`
- `mode`
- `active_correlation_id`
- `last_reasoning_step`
- `last_fallback_reason`
- `provider`
- `provider_configured`
- `last_latency_ms`
- `last_trigger_event`

`/api/health` 现在除了 `status: ok`，还会补充：

- `simulation.is_running`
- `simulation.mode`
- `simulation.speed`
- `simulation.wall_tick_ms`
- `simulation.simulated_dt_seconds`
- `llm.provider`
- `llm.model`
- `llm.configured`

更完整的协议说明见 `docs/architecture/ws-protocol.md`。

## 当前开放枚举

### 设备类型

`light`、`hvac`、`curtain`、`sensor`、`fan`、`camera`

### 设备能力

`power`、`brightness`、`color_temp`、`target_temp`、`mode`、`speed`、`open_percent`、`shake`、`timeout`、`view`、`read`

## 前端观测侧栏

侧栏开关仍然使用底部的 `sidebarOpen`，但主内容已经从旧动作日志切成 episode 视图。当前侧栏会：

- 默认跟随最新活跃 episode，没有活跃链路时回退到最近完成的一条
- 按 `correlation_id` 组织时间线，自动识别根事件、reasoning 过程、设备动作和状态反馈
- 在详情区用固定布局展示 `reasoning.*`、`action.device_control`、`feedback.state_delta` 和 `user.*` payload
- 在 fallback episode 上显示明确提示，便于区分真实 LLM 链路和规则回退链路

## 目录结构

```text
SmartHomeSim/
├── backend/
│   ├── agents/        # Lighting / HVAC agent runtime、LLM provider、arbiter、memory
│   ├── api/           # FastAPI 路由与 WebSocket 网关
│   ├── config/        # 默认设备与场景配置
│   ├── engine/        # EventBus、SimulationEngine、SimulatorTimer、StateManager
│   ├── models/        # 协议 schema 与 SimulationClient protocol
│   └── simulators/    # 用户行为与环境仿真
├── docs/architecture/
│   ├── sim-event-schema.md
│   └── ws-protocol.md
├── frontend/
│   └── src/
│       ├── components/
│       ├── composables/
│       ├── stores/
│       └── types/
└── tests/
```

## 开发验证

后端测试：

```bash
pytest tests -q
```

前端测试：

```bash
cd frontend
node --experimental-strip-types --test tests/*.test.ts
npm run build
```

Compose 配置检查：

```bash
docker compose config
```

## 下一阶段

Phase 3 收口后，下一步会继续补 episode 历史查询、跨链路检索和更完整的观测分析面板，而不是再回到旧日志侧栏。
