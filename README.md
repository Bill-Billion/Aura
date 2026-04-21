# SmartHomeSim

SmartHomeSim 是一个面向智能家居 Agent 的 3D 仿真与观测平台。当前版本已经完成 Phase 2 的主链改造，后端同时保留 `STATE_FULL / STATE_DELTA / AGENT_STATUS / SIM_EVENT` 兼容输出，并把 Lighting / HVAC Agent 升级成事件驱动运行时，支持 OpenAI Responses 和 Anthropic-compatible provider 两条 LLM 接入路径。

## 当前能力

- 多楼层 3D 展厅渲染，支持灯光、空调、窗帘、风扇、摄像头和环境传感器
- 事件驱动仿真内核，`SimEvent` 已覆盖 timer、环境刷新、用户活动、设备动作和状态反馈
- Lighting / HVAC Agent 以事件订阅方式运行，支持 `user.activity_change` 和显著 `environment.state_refresh` 触发
- OpenAI Responses 和 Anthropic-compatible provider 双路接入，推理结果统一落成 `reasoning.*` 事件，超时或异常会自动回退到现有规则逻辑
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

先启动后端：

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export OPENAI_API_KEY=your_key_here
export OPENAI_MODEL=gpt-5.4
export OPENAI_REASONING_EFFORT=medium
export LLM_TIMEOUT_MS=5000
export AGENT_EPISODE_TIMEOUT_MS=5000
uvicorn main:app --reload --port 8000
```

如果没有配置 `OPENAI_API_KEY`，Agent 会继续工作，但会直接回退到规则逻辑，不会调用 LLM。

如果要走 Anthropic-compatible provider，比如 MiniMax，可以改成：

```bash
export LLM_PROVIDER=anthropic_compatible
export ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic
export ANTHROPIC_API_KEY=your_key_here
export ANTHROPIC_MODEL=MiniMax-M2.7
export LLM_TIMEOUT_MS=15000
export AGENT_EPISODE_TIMEOUT_MS=15000
uvicorn main:app --reload --port 8000
```

如果没有显式设置 `LLM_PROVIDER`，运行时会优先尝试 `OPENAI_API_KEY`，否则再尝试 `ANTHROPIC_API_KEY / ANTHROPIC_COMPAT_API_KEY`。

`LLM_TIMEOUT_MS` 控制单次 provider HTTP 请求的超时，`AGENT_EPISODE_TIMEOUT_MS` 控制单个 agent episode 最长占用时间。默认情况下 episode timeout 会跟随 `LLM_TIMEOUT_MS`，这样兼容 provider 即使没有及时断开，也不会把整条事件链长时间卡住。

像 MiniMax 这类响应更慢的 Anthropic-compatible provider，如果继续用 `5000ms`，系统会更快回退到规则逻辑；如果希望真实走完 LLM 决策链，需要把两个 timeout 一起调高。

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

更完整的协议说明见 `docs/architecture/ws-protocol.md`。

## 当前开放枚举

### 设备类型

`light`、`hvac`、`curtain`、`sensor`、`fan`、`camera`

### 设备能力

`power`、`brightness`、`color_temp`、`target_temp`、`mode`、`speed`、`open_percent`、`shake`、`timeout`、`view`、`read`

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

Phase 2 完成后，下一步是进入 Phase 3，把右侧旧日志壳层替换成真正的 ObservabilityPanel，让前端按事件时间线和 reasoning detail 消费这批结构化事件。
