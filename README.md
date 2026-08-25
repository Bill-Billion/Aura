# Aura: open-source smart home simulation platform

[![Version](https://img.shields.io/badge/version-0.1.3.12-0A84FF.svg)](./VERSION)
[![Frontend](https://img.shields.io/badge/frontend-Vue%203%20%2B%20Three.js-42b883.svg)](./frontend)
[![Backend](https://img.shields.io/badge/backend-FastAPI%20%2B%20WebSocket-009688.svg)](./backend)
[![Simulation](https://img.shields.io/badge/runtime-event--driven-6c5ce7.svg)](./docs/architecture/sim-event-schema.md)
[![Protocol](https://img.shields.io/badge/protocol-STATE__FULL%20%7C%20STATE__DELTA%20%7C%20SIM__EVENT-f39c12.svg)](./docs/architecture/ws-protocol.md)
[![Docker](https://img.shields.io/badge/dev-docker%20compose-2496ed.svg)](./docker-compose.yml)

Aura 提供一套完整的开发环境，用来模拟三层智能住宅、运行事件驱动 Agent，并观察每一次自动化动作背后的完整因果链。它把 3D 场景、canonical scenario、仿真时钟、结构化事件、多 Agent 编排、可复现 run 工件和前端研究工作台放到同一条工作流里，适合做智能家居 Agent 的联调、基线比较和产品验证。

- 查看多楼层 3D 住宅里的灯光、空调、窗帘、风扇、摄像头和环境传感器
- 直接手动控制设备，也可以让 Lighting / HVAC / Security / Energy / Scene Agent 协同参与链路
- 通过 `root -> reasoning -> action -> feedback` 的 episode 视图看清楚 Agent 为什么这么做
- 用同一个 canonical scenario 和 seed 运行两种 baseline policy，对比七项评估指标与两条原始因果时间线
- 保存每次运行的元数据与事件 JSONL；recorded 模式另存带完整性 manifest 的 LLM recording，并可从工件重建评估报告
- 用本地脚本或 Docker Compose 一键拉起前后端开发栈

## Start Aura

### Quick start

需要 Python 3.12、Node.js 22.12+ 与 npm；Docker Compose 仅在使用容器启动时需要。首次 clone 先安装依赖：

```bash
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
npm ci --prefix frontend
```

之后推荐使用统一起栈脚本：

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
ANTHROPIC_MODEL=MiniMax-M3
ANTHROPIC_MAX_TOKENS=1200
# OpenAI Responses 路径使用同一硬上限：
# OPENAI_MAX_OUTPUT_TOKENS=1200
# 只有明确同意产生外部调用/费用时才开启；仅配置 API key 不会自动打网。
AURA_ALLOW_LIVE_LLM=1
LLM_TIMEOUT_MS=12000
AGENT_EPISODE_TIMEOUT_MS=15000
AGENT_ENV_DEBOUNCE_MS=5000
AGENT_EPISODE_BUDGET_USD=0.10
ENV_EOF
```

如果没有显式设置 `LLM_PROVIDER`，Aura 会先尝试 `OPENAI_API_KEY`，再尝试 Anthropic-compatible 环境变量。

`AURA_ALLOW_LIVE_LLM` 是付费能力的服务端总闸门，缺省关闭；仅存在 key 不代表授权消费。`LLM_TIMEOUT_MS` 控制单次 provider 请求超时，`AGENT_EPISODE_TIMEOUT_MS` 控制单个 agent episode 的最长耗时，`AGENT_ENV_DEBOUNCE_MS` 控制环境事件触发 Agent 的防抖窗口，`AGENT_EPISODE_BUDGET_USD` 是每条 episode 的硬预算。OpenAI 与 Anthropic-compatible 请求都强制使用和预算预检一致的输出 token 上限，并优先按 provider 返回的真实 usage（包括已计费但输出解析失败的响应）记账；没有硬输出上限的付费 provider 会在发出前被拒绝，没有 usage 的响应按输入 UTF-8 字节数与输出上限保守记账。请求与结构化 decision 也有长度、列表数和总 JSON 大小边界。

研究运行按 run 显式选择 baseline policy，客户端不会接收或传递 provider 凭据：

- `rule_based`: 不调用 LLM
- `llm_mocked`: 使用确定性 fixture，不访问网络
- `llm_recorded`: 留空 recording source 时使用服务端 provider 首次录制；指定已完成、契约一致的原始 recorded run 时零网络回放
- `llm_live`: 使用服务端已配置的真实 provider，可能产生费用

浏览器控制面缺省只信任 `localhost / 127.0.0.1 / ::1` 的 HTTP(S) Origin。远程部署需用逗号分隔的 `AURA_ALLOWED_ORIGINS` 明确列出站点；远程启动 `llm_live` 或首次 `llm_recorded` 录制还必须配置 `AURA_RESEARCH_WRITE_TOKEN`，并在 REST 请求中携带 `Authorization: Bearer <token>`。不要把这个服务端 token 写入前端 bundle。任意网页 Origin、通配符 Origin 和未授权远程付费请求都会在启动前被拒绝。

## Running from Source

### Backend

```bash
python3.12 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
backend/.venv/bin/python -m uvicorn backend.main:app --reload --port 8000
```

### MiniMax-M3 research substudy

PR21 的 Option B 研究使用 8 个动态场景 × 3 个 seed，并为每个实例运行
`3 live + 1 recorded capture + 3 offline replay`，共 168 个 run。先按上方示例在
服务端配置 `MiniMax-M3`，再依次执行：

```bash
python -m backend.experiments resolve-llm-substudy \
  benchmarks/aurabench-m3-substudy/manifest.yaml --output output/aurabench-m3
python -m backend.experiments preflight-llm-substudy \
  output/aurabench-m3/resolved-substudy.json --output output/aurabench-m3
python -m backend.experiments run-llm-substudy \
  output/aurabench-m3/resolved-substudy.json --output output/aurabench-m3
python -m backend.experiments summarize-llm-substudy \
  output/aurabench-m3/resolved-substudy.json --output output/aurabench-m3
```

API key 只从服务端环境读取，不进入清单、预检回执或结果工件。当前研究使用 token plan，
因此美元成本仅作复核指标，不作为停机条件；失败或中断后重复 `run-llm-substudy` 会从已
验证的 admitted slot 继续。无效/失败 slot 为 create-only，不能在同一 output root 覆盖重抽。
模型返回非法结构化输出时，PR21 strict runner 会记录可见的零动作模型失败，不调用规则
fallback；这类失败计入结果而不是被选择性排除。该子研究还把
`https://api.minimaxi.com/anthropic` 冻结为唯一 endpoint；预检后更改 endpoint 会被拒绝，
API key 与家居轨迹不会被发送到其他 Anthropic-compatible 服务。协议版本、输出上限、
超时和 decision schema hash 也会冻结；token plan 使用只记账不拦截的成本策略，最终结果
必须引用通过验证的 sealed preflight。provider 回包中的实际模型也会逐次核对并留证，
因此“请求了 M3”不会被误写成“证明运行了 M3”。

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
- `system.episode_cancelled`
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

### Multi-agent orchestration and fallback

`HomeOrchestratorAgent` 会把根事件分解给五个域 Agent，并通过统一仲裁门处理优先级、冲突、能源约束和显式用户覆盖。运行时支持 `rule_based / mocked / recorded / live` 四种实际模式；provider 超时、输出异常或 episode 预算耗尽时会带证据地回退，陈旧决策会被显式丢弃而不是写入新世界。

### Observability panel

右滑侧栏已经由 `ObservabilityPanel` 接管。它会默认跟随最新活跃 episode，没有活跃 episode 时回退到最近完成的一条。面板里会把 root event、reasoning event、设备动作和状态反馈放在一条时间线上，而不是只显示零散日志。

### Canonical scenarios and run artifacts

`GET /api/scenarios` 暴露可运行的 canonical scenario 摘要。每次运行都记录 `run_id`、scenario、seed、baseline policy、实际 LLM 模式、provider/model、初态 hash、场景评估契约 hash、运行代码 `source_revision`，以及 scenario/event/command/device-registry 四个 schema version。发布环境可通过 `AURA_SOURCE_REVISION` 注入 commit/image digest；本地 dirty tree 会对后端运行源码与配置计算 SHA-256。公开 seed 限定在 `0..2^53-1`，确保 Python 与浏览器 JSON 往返时不丢精度。运行达到场景声明时长后会自动 finalized，稳定工件位于 `data/runs/{run_id}/`。live 与 headless 共用 `tick 1 = t0` 的截止规则：非整拍 deadline 会运行到第一拍覆盖它的模拟时刻，完整排空这一拍的事件后停止，既不会漏掉 deadline 上的 timeline 事件，也不会因服务器负载多跑一拍。

事件写入会核对每条记录的 `run_id`，跨 run 事件不会混入新工件；EventBus admission 决定 WebSocket、内存 history 与 recorder 共同看见的唯一事件，深度风暴只发布可审计的 suppression notice，前端按 canonical `seq` 呈现。finalize 按 `flush -> fsync -> close` 封口，并把 event count、末条 seq 和原始字节 SHA-256 写入 `run.json`。任何事件或 recording 写入失败、取消、删尾、换行或摘要不一致都会让稳定读侧 fail closed；active run 的分页 JSON 仍可用于 Live 观测，但 report、finalized JSON、raw/canonical attachment 都必须先通过封口校验。recorded source 还必须通过 recording manifest 数量、内容 hash、场景契约与代码/Agent 版本校验；同一请求 key 若录到互相矛盾的 decision 会被视为损坏工件。回放 miss 可以触发安全规则兜底，但该 run 不再具备可复现评估资格。

评估器只从 finalized run 的元数据、ScenarioSpec 和原始事件重建结果，并在报告中分别保留 run 的 `source_revision` 与当前评估代码的 `evaluator_source_revision`，输出以下七项 canonical metrics：

- `episode_complete`
- `first_action_latency_ms`
- `command_failure_count`
- `fallback_count`
- `conflict_count`
- `user_intent_satisfied`
- `device_state_match_rate`

命令失败按 `command_id` 的最终状态去重；episode 完整性要求同一条 feedback 祖先链依次穿过 perception、intent、decomposition、coordination、plan、approved 与 action，root 下彼此无关的 sibling 事件不能拼成成功证据；首动作延迟使用墙钟时间；预期失败必须被真实观察到；设备效果读取 `feedback.state_delta` 的 `path/new_value`，数值期望区间必须为有限数且 `min <= max`；用户意图还会核对禁止设备、稳定归一化 intent 与必需 Agent role；未知安全约束不会静默判为通过。只有 `end_reason=completed`、schema/契约一致且工件完整的 finalized run 才能产生有效报告。

### Research workspace

点击主界面左下角“研究运行”进入 Setup / Live / Compare 工作流：

1. 选择 canonical scenario、固定非负 seed，并启动 Run A。
2. Run A finalized 后复制它的 scenario + seed，只更换 baseline policy 启动 Run B。
3. Live 视图复用 episode 可观测面板；点击 3D 设备仍可过滤相关因果链。
4. Compare 视图展示七项指标、A/B 差值、判定依据和带显式 gap 的双轨 raw-event 时间线。
5. 单侧可下载服务端 byte-exact raw JSONL，也可以导出带 provenance、报告和按 `seq` 排序事件的对比包。

比较入口除硬性拒绝不同 scenario 或不同 seed，还会核对初态 hash、场景契约 hash、运行代码 source revision、仿真/Agent 版本、四类 schema version、报告 schema 与两份报告的 evaluator source revision；这些固定实验条件缺失或不一致时不会生成策略胜负。baseline policy 与实际生效的 `llm_mode` 分开展示，避免把请求条件误当成运行事实。

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
- `CMD_SCENE_APPLY`
- `CMD_RUN_SCENARIO`

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
- `simulation.run_id`
- `simulation.scenario_id`
- `simulation.seed`
- `simulation.baseline_policy`
- `simulation.llm_mode`
- `simulation.finalized`
- `simulation.ended_at`
- `simulation.end_reason`
- `llm.provider`
- `llm.model`
- `llm.configured`

更完整的字段说明见 `docs/architecture/ws-protocol.md`。

## Research REST API

```http
GET  /api/scenarios
POST /api/runs
GET  /api/runs?scenario_id=...&seed=...&baseline_policy=...&finalized=true
GET  /api/runs/{run_id}
GET  /api/runs/{run_id}/report
GET  /api/runs/{run_id}/events
GET  /api/runs/{run_id}/events?format=raw
GET  /api/runs/{run_id}/events?format=canonical
```

启动示例：

```json
{
  "scenario_id": "user_arrives_home_evening",
  "seed": 1001,
  "baseline_policy": "rule_based",
  "idempotency_key": "123e4567-e89b-42d3-a456-426614174000"
}
```

同一时刻只允许一个 active canonical run。前端会为一次启动意图生成 UUID `idempotency_key` 并在网络重试时复用：在同一服务进程且该 key 仍位于 1024 条有界缓存内时，即使首次 201 丢失或短 run 已结束，重试也会返回原 run；进程重启或缓存淘汰不提供恢复保证。同一 key 改变场景、seed、policy 或 recording source 会返回 `409 idempotency_conflict`。运行期间服务端会拒绝设备直控、场景切换、速度/模式和 reset 等会污染实验条件的交互命令；结束后继续交互会先创建新的匿名 ambient run。finalizer 异常会把工件标为 invalid、以 `finalization_failed` 收尾并释放 active slot。运行中的报告和 trace attachment 返回 `409 run_not_finalized`；invalid 工件返回 `422`；raw/canonical attachment 只导出完整 finalized trace，不接受分页或过滤参数。

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
├── tests/                   # 后端 pytest 测试
└── frontend/tests/          # 前端 Vitest 测试
```

## Documentation

- `docs/architecture/simulation-requirements-spec.md`
- `docs/architecture/ws-protocol.md`
- `docs/architecture/sim-event-schema.md`

## Testing

### Backend tests

```bash
backend/.venv/bin/python -m pytest tests/ --timeout=120
```

### Frontend tests

```bash
cd frontend
npm test
npm run build
```

### Compose config check

```bash
docker compose config
```

## Status

Aura 当前已经完成事件驱动主链、多 Agent 编排、四种 LLM 运行模式、canonical scenario/run 工件、七指标评估契约，以及从启动到同 seed A/B 对比的研究工作流。它已经可以用来调 Agent、审计事件链并保存可复核实验结果；更大规模的 failure suite/CLI 与发布级浏览器、容器门禁仍是后续独立工作。

## About Aura

Aura 的方向很明确，用一套可视化、可回放、可观察的智能家居仿真环境，把 Agent 产品开发里最难调的那段链路做清楚。场景在变，状态在变，Agent 在推理，前端能把这条链路讲明白，这就是它现在的价值。

## License

Source code is released under the [MIT License](LICENSE). Bundled media assets
have separate provenance — notably, the floor models under
`frontend/public/models/` are third-party and NOT covered by MIT. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for details.
