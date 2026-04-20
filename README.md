# SmartHomeSim

SmartHomeSim 是一个面向智能家居 Agent 的 3D 仿真与观测平台。当前版本已经完成 Phase 1 的事件驱动迁移，后端会同时外发兼容旧界面的状态消息和给后续可观测性面板使用的结构化事件流。

## 当前能力

- 多楼层 3D 展厅渲染，支持灯光、空调、窗帘、风扇、摄像头和环境传感器
- 规则型 Lighting / HVAC Agent 与用户行为模拟
- `STATE_FULL + STATE_DELTA + AGENT_STATUS + SIM_EVENT` 并行输出
- 事件链字段 `event_id / correlation_id / causal_parent / priority`
- 统一设备注册表与场景绑定
- 一键本地起栈脚本和 Docker Compose 联调入口

## 技术栈

| 层 | 技术 |
| --- | --- |
| Frontend | Vue 3.5、TypeScript、TresJS、Three.js、Pinia、TailwindCSS、GSAP |
| Backend | FastAPI、Pydantic v2、WebSocket、structlog |
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
uvicorn main:app --reload --port 8000
```

再启动前端：

```bash
cd frontend
npm install
npm run dev
```

浏览器打开 [http://localhost:5173](http://localhost:5173)。

### 方式二：用统一起栈脚本

如果本机已经有旧实例，优先用仓库脚本。它会检查端口占用、记录 PID/日志，并补一条代理 WebSocket 保活校验。

```bash
./scripts/dev-stack.sh start
./scripts/dev-stack.sh verify
./scripts/dev-stack.sh stop
```

### 方式三：用 Docker Compose

```bash
docker compose up --build
```

Compose 环境下前端代理会自动指向 `http://backend:8000`，不再把 `/ws` 和 `/api` 打回容器里的自己。

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

Phase 1 对外开放的 `SIM_EVENT.event_type`：

- `system.timer_tick`
- `system.simulation_started`
- `system.simulation_paused`
- `system.simulation_reset`
- `environment.state_refresh`
- `user.command`
- `user.activity_change`
- `action.device_control`
- `feedback.state_delta`

### 错误格式

```json
{
  "code": "DEVICE_NOT_CONTROLLABLE",
  "message": "客厅温度传感器不支持修改: value",
  "details": {
    "device_id": "sensor_living_temp_01",
    "action": "set_state"
  }
}
```

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
│   ├── agents/        # 规则型 Agent
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

Phase 1 已收口完成。下一步会沿着 `GSTACK_FINAL_PLAN.md` 进入事件流消费侧，把右侧可观测性面板从旧日志模式切到事件时间线和 reasoning detail，而不是提前跳到 LLM Agent。
