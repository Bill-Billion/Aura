# Aura: open-source smart home simulation platform

[![Version](https://img.shields.io/badge/version-0.1.3.11-0A84FF.svg)](./VERSION)
[![Frontend](https://img.shields.io/badge/frontend-Vue%203%20%2B%20Three.js-42b883.svg)](./frontend)
[![Backend](https://img.shields.io/badge/backend-FastAPI%20%2B%20WebSocket-009688.svg)](./backend)
[![Simulation](https://img.shields.io/badge/runtime-event--driven-6c5ce7.svg)](./docs/architecture/sim-event-schema.md)
[![Protocol](https://img.shields.io/badge/protocol-STATE__FULL%20%7C%20STATE__DELTA%20%7C%20SIM__EVENT-f39c12.svg)](./docs/architecture/ws-protocol.md)
[![Docker](https://img.shields.io/badge/dev-docker%20compose-2496ed.svg)](./docker-compose.yml)

Aura 提供一套完整的开发环境，用来模拟三层智能住宅、运行事件驱动 Agent，并观察每一次自动化动作背后的完整因果链。它把 3D 场景、仿真时钟、结构化事件、LLM Agent 和前端可观测性面板放到同一条工作流里，适合做智能家居 Agent 的联调、演示和产品验证。

- 查看多楼层 3D 住宅里的灯光、空调、窗帘、风扇、摄像头和环境传感器
- 直接手动控制设备，也可以让 Lighting / HVAC Agent 自动参与链路
- 通过 `root -> reasoning -> action -> feedback` 的 episode 视图看清楚 Agent 为什么这么做
- 用本地脚本或 Docker Compose 一键拉起前后端开发栈

## Start Aura

### Quick start

推荐直接使用统一起栈脚本：

```bash
./scripts/dev-stack.sh restart
```

启动完成后访问：

- 前端: [http://127.0.0.1:5173](http://127.0.0.1:5173)
- 后端健康检查: [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health)

停止服务：

```bash
./scripts/dev-stack.sh stop
```

### Configure an LLM provider

在仓库根目录创建 `.env.local`：

```bash
cat > .env.local <<'ENV_EOF'
LLM_PROVIDER=anthropic_compatible
ANTHROPIC_BASE_URL=https://api.minimaxi.com/anthropic
ANTHROPIC_API_KEY=your_key_here
ANTHROPIC_MODEL=MiniMax-M2.7
LLM_TIMEOUT_MS=12000
AGENT_EPISODE_TIMEOUT_MS=15000
AGENT_ENV_DEBOUNCE_MS=5000
ENV_EOF
```

如果没有显式设置 `LLM_PROVIDER`，Aura 会先尝试 `OPENAI_API_KEY`，再尝试 Anthropic-compatible 环境变量。

`LLM_TIMEOUT_MS` 控制单次 provider 请求超时，`AGENT_EPISODE_TIMEOUT_MS` 控制单个 agent episode 的最长耗时，`AGENT_ENV_DEBOUNCE_MS` 控制环境事件触发 Agent 的防抖窗口。

## Running from Source

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Docker Compose

```bash
docker compose up --build
```

Compose 环境下前端代理会自动指向 `http://backend:8000`。

## What Aura currently does

### 3D smart home simulation

Aura 现在会渲染一套三层住宅场景，并把设备状态、房间 occupancy、天气和时间变化映射到画面里。当前已经接入灯光、空调、窗帘、风扇、摄像头和环境传感器。

### Event-driven runtime

仿真内核已经切成事件驱动结构，核心事件包括：

- `system.timer_tick`
- `system.simulation_started`
- `system.simulation_paused`
- `system.simulation_reset`
- `environment.state_refresh`
- `user.command`
- `user.activity_change`
- `action.device_control`
- `feedback.state_delta`

### Agent reasoning and fallback

Lighting / HVAC Agent 已经跑在事件订阅式运行时上。它们可以使用真实 LLM provider，也可以在 provider 超时或输出异常时自动回退到规则链路。前端可以直接看到 `reasoning.perception_snapshot`、`reasoning.intent_recognized`、`reasoning.task_decomposition`、`reasoning.coordination_decision`、`reasoning.execution_plan` 和 `reasoning.fallback_rule_based`。

### Observability panel

右滑侧栏已经由 `ObservabilityPanel` 接管。它会默认跟随最新活跃 episode，没有活跃 episode 时回退到最近完成的一条。面板里会把 root event、reasoning event、设备动作和状态反馈放在一条时间线上，而不是只显示零散日志。

## Simulation modes

Aura 现在使用固定墙钟节拍，默认每 2 秒发一次 tick。模式决定每个 tick 推进多少模拟时间：

- `observe`: 每 2 秒推进 10 秒模拟时间
- `demo`: 每 2 秒推进 60 秒模拟时间

页面首次进入不会自动开始仿真。暂停态会明确显示“仿真未开始”、当前模式和启动后的推进节奏。

## WebSocket protocol

### Client commands

- `CMD_SIM_START`
- `CMD_SIM_PAUSE`
- `CMD_SIM_RESET`
- `CMD_SIM_MODE`
- `CMD_SIM_SPEED`
- `CMD_DEVICE_CONTROL`

### Server messages

- `STATE_FULL`
- `STATE_DELTA`
- `AGENT_STATUS`
- `SIM_EVENT`
- `SIMULATION_STATUS`
- `ERROR`

### Health endpoint

`/api/health` 除了 `status: ok`，还会返回：

- `simulation.is_running`
- `simulation.mode`
- `simulation.speed`
- `simulation.wall_tick_ms`
- `simulation.simulated_dt_seconds`
- `llm.provider`
- `llm.model`
- `llm.configured`

更完整的字段说明见 `docs/architecture/ws-protocol.md`。

## Supported devices

### Device types

`light`、`hvac`、`curtain`、`sensor`、`fan`、`camera`

### Device capabilities

`power`、`brightness`、`color_temp`、`target_temp`、`mode`、`speed`、`open_percent`、`shake`、`timeout`、`view`、`read`

## Project structure

```text
Aura/
├── backend/                 # FastAPI、SimulationEngine、Agent runtime、协议模型
├── frontend/                # Vue 3、Three.js、TresJS、Pinia 前端
├── docs/architecture/       # 事件 schema、WS 协议、设备注册相关文档
├── scripts/                 # 本地起栈和联调脚本
└── tests/                   # 后端与前端测试
```

## Documentation

- `GSTACK_FINAL_PLAN.md`
- `docs/architecture/ws-protocol.md`
- `docs/architecture/sim-event-schema.md`
- `docs/architecture/gamemcu-device-registration-plan.md`

## Testing

### Backend tests

```bash
pytest tests -q
```

### Frontend tests

```bash
cd frontend
node --experimental-strip-types --test tests/*.test.ts
npm run build
```

### Compose config check

```bash
docker compose config
```

## Status

Aura 当前已经完成了事件驱动主链、双模式仿真、真实 LLM provider 联调、前端 episode 级可观测性和房间级场景反馈。现在它已经不是一个只会动模型的 demo，而是一套可以用来调 Agent、看事件链、查状态同步问题的开发平台。

## About Aura

Aura 的方向很明确，用一套可视化、可回放、可观察的智能家居仿真环境，把 Agent 产品开发里最难调的那段链路做清楚。场景在变，状态在变，Agent 在推理，前端能把这条链路讲明白，这就是它现在的价值。

## License

Source code is released under the [MIT License](LICENSE). Bundled media assets
have separate provenance — notably, the floor models under
`frontend/public/models/` are third-party and NOT covered by MIT. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.
