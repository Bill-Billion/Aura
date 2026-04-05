# 多智能体协同调度智能家居仿真平台 — 系统设计规格

> **Date**: 2026-04-05
> **Status**: Draft — 待用户审核
> **Version**: 1.0

---

## 0. 设计约束与元原则

### Agent友好型设计（贯穿全文的元约束）

所有设计必须满足以下原则，确保AI Agent（如Claude）能高效理解、导航和操作代码库：

1. **自描述** — 每个文件/模块/插件的头注释说明"我是什么、谁用我、怎么用"
2. **约定优于配置** — 目录结构即注册，不需要额外的注册表或import链
3. **扁平优于嵌套** — 避免3层以上的继承/目录嵌套
4. **工具可发现** — 所有能力通过JSON Schema描述，LLM无需读源码即可调用
5. **单一职责** — 一个文件只做一件事，一个函数只干一活
6. **最小惊喜** — 命名和行为完全一致，没有隐式魔法
7. **示例即文档** — 每个插槽自带最小实现示例

### 关键决策记录

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 项目定位 | 学术研究验证工具 | 核心价值是算法透明度、可观测性、数据可导出 |
| LLM策略 | 一开始就接入API | 真实展示意图解析能力，异步非阻塞+降级策略 |
| Agent策略 | 规则+LLM混合 | 常规场景走规则快速路径，复杂/模糊场景调用LLM |
| 3D模型 | 现成免费GLB | 从Sketchfab获取，聚焦功能开发 |
| 世界模型 | 学习型（神经网络） | 学术价值高，MLP预测状态转移 |
| 架构模式 | 平台层+研究层分离 | 研究者只触碰研究层，无需改平台代码 |
| Agent设计 | 工具驱动（借鉴learn-claude-code） | Agent通过Tools与世界交互，LLM通过JSON Schema理解工具 |

---

## 1. 系统分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│                     用户交互层 (Browser)                          │
│   ┌──────────────────────┐  ┌──────────────────────────────┐   │
│   │   3D渲染层 (TresJS)   │  │   2D覆盖层 (Dashboard)       │   │
│   │   OrthographicCam    │  │   研究模式 / 演示模式 切换    │   │
│   │   GLB Scene + Bloom  │  │   实验管理面板               │   │
│   │   GSAP Tweens        │  │   数据导出按钮               │   │
│   └──────────┬───────────┘  └──────────────┬───────────────┘   │
│              └──────────┬──────────────────┘                    │
│                    Pinia Reactive Bridge                        │
├─────────────────────────────────────────────────────────────────┤
│                     通信层 (WebSocket)                           │
│              vue-use/useWebSocket + JSON                        │
├──────────────────────┬──────────────────────────────────────────┤
│                      │                                          │
│   ┌──────────────────▼──────────────────────┐                  │
│   │         平台层 (Platform Layer)           │                  │
│   │                                          │                  │
│   │  ┌────────────┐ ┌──────────────────────┐ │                  │
│   │  │ EventBus   │ │ SimulationEngine     │ │                  │
│   │  │ (Pub/Sub)  │ │ (asyncio主循环)       │ │                  │
│   │  └────────────┘ └──────────────────────┘ │                  │
│   │  ┌────────────┐ ┌──────────────────────┐ │                  │
│   │  │ StateMgr   │ │ WebSocket Gateway    │ │                  │
│   │  │ (单一事实源)│ │ (ConnectionManager)  │ │                  │
│   │  └────────────┘ └──────────────────────┘ │                  │
│   │  ┌────────────┐ ┌──────────────────────┐ │                  │
│   │  │ EnvSim     │ │ UserBehaviorSim      │ │                  │
│   │  │ (物理环境)  │ │ (用户行为模拟)        │ │                  │
│   │  └────────────┘ └──────────────────────┘ │                  │
│   └──────────────────────────────────────────┘                  │
│                                                                  │
│   ┌──────────────────────────────────────────┐                  │
│   │         研究层 (Research Layer)            │                  │
│   │                                          │                  │
│   │  ┌────────────────────────────────────┐  │                  │
│   │  │ PluginManager                      │  │                  │
│   │  │  - Agent/Device/Scene插件发现加载    │  │                  │
│   │  │  - ToolRegistry 工具注册表          │  │                  │
│   │  └────────────────────────────────────┘  │                  │
│   │  ┌────────────────────────────────────┐  │                  │
│   │  │ WorldModelFactory                  │  │                  │
│   │  │  - PhysicsModel (简化物理)          │  │                  │
│   │  │  - LearnedModel (神经网络)          │  │                  │
│   │  └────────────────────────────────────┘  │                  │
│   │  ┌────────────────────────────────────┐  │                  │
│   │  │ RewardFunction (可插拔)             │  │                  │
│   │  └────────────────────────────────────┘  │                  │
│   │  ┌────────────────────────────────────┐  │                  │
│   │  │ ExperimentManager                  │  │                  │
│   │  │  - 实验配置加载 (YAML)              │  │                  │
│   │  │  - 对比实验管理                     │  │                  │
│   │  │  - 数据记录与导出 (JSON/CSV)        │  │                  │
│   │  └────────────────────────────────────┘  │                  │
│   └──────────────────────────────────────────┘                  │
│                                                                  │
│   ┌──────────────────────────────────────────┐                  │
│   │         基础设施层 (Infrastructure)        │                  │
│   │  LLM API Client │ Config(TOML) │ structlog│                  │
│   └──────────────────────────────────────────┘                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. 后端引擎核心

### 2.1 SimulationEngine — 仿真主循环

```
SimulationEngine._main_loop():
  while is_running:
    tick_start = now()

    ① 用户行为模拟 → UserEvent[] → EventBus.publish()
    ② 环境物理模拟 (子步) → EnvDelta[] → 收集
    ③ Agent感知-决策-执行循环:
       for agent in plugin_manager.agents:
         snapshot = agent.perceive(world_state)
         actions = agent.decide(snapshot)
         for action in actions:
           deltas = state_manager.apply(action)
           event_bus.publish(action.to_event())
    ④ 世界模型推演 (异步，不阻塞主循环)
    ⑤ 状态广播 → broadcast(STATE_DELTA)
    ⑥ 实验数据记录 → experiment_manager.record_tick(...)
    ⑦ 帧率控制 → sleep(max(0, TICK_INTERVAL/speed - elapsed))
```

### 2.2 EventBus — 事件类型体系

```
system.*              simulation.tick, simulation.status
environment.*         env.temperature_change, env.weather_change, env.light_change
user.*                user.location_change, user.activity_change,
                      user.device_override, user.preference_drift
agent.*               agent.action_executed, agent.status_change,
                      agent.confidence_update, agent.learning_signal
negotiation.*         negotiation.proposal, negotiation.counter,
                      negotiation.accept, negotiation.reject, negotiation.resolved
llm.*                 llm.parse_request, llm.parse_result, llm.fallback
experiment.*          experiment.started, experiment.tick_recorded,
                      experiment.completed
```

### 2.3 WorldState — 补全版Pydantic模型

```python
class WorldState(BaseModel):
    tick: int = 0
    speed: float = 1.0
    is_running: bool = False
    scene_id: str = ""
    environment: EnvironmentState
    devices: dict[str, DeviceState]
    rooms: dict[str, RoomState]
    agents: dict[str, AgentRuntimeState]       # 补充
    users: dict[str, UserState]                # 补充
    preference_weights: PreferenceWeights      # 补充

    def snapshot(self) -> "WorldState":
        return self.model_copy(deep=True)
```

---

## 3. 插件插槽体系

### 3.1 DevicePlugin 插槽

```python
class DevicePlugin(Protocol):
    device_type: str
    display_name: str
    version: str

    def get_state_schema(self) -> type[BaseModel]: ...
    def get_default_state(self) -> BaseModel: ...
    def get_tools(self) -> list[ToolDefinition]: ...
    def get_emitted_events(self) -> list[str]: ...
    def compute_env_impact(self, state, dt) -> dict[str, float]: ...
    def get_render_config(self) -> RenderConfig: ...
```

### 3.2 设备工具体系（按域组织）

```
照明域:      control_light_power, adjust_brightness, adjust_color_temp, adjust_color_rgb, set_lighting_scene
暖通域:      control_hvac_power, set_temperature, set_hvac_mode, set_fan_speed, control_humidifier, control_fresh_air
窗帘/窗户域: set_curtain_open, set_blind_tilt, control_window
娱乐域:      control_tv_power, set_tv_input, control_speaker_power, set_volume, play_media, control_projector
安防域:      control_door_lock, arm_alarm, control_camera, trigger_emergency
家电域:      control_robot_vacuum, control_washer, control_dryer, control_dishwasher, control_oven, control_coffee_maker
卫浴域:      set_water_heater, control_bath_fan, control_floor_heating, control_shower
通用域:      control_smart_plug, control_smart_switch, set_timer
传感域(只读): query_temperature, query_humidity, query_light_level, query_air_quality,
              query_motion, query_smoke_gas, query_power_consumption, query_door_window_status
场景宏:      execute_scene, create_custom_scene, schedule_action
Agent协作:   propose_action, respond_proposal, request_negotiation,
             consult_world_model, broadcast_intention
研究工具:    log_decision, record_reward, export_state, mark_experiment_event
```

### 3.3 ScenePlugin 插槽

```python
class ScenePlugin(Protocol):
    scene_id: str
    display_name: str
    def get_model_path(self) -> str: ...
    def get_room_layout(self) -> list[RoomDef]: ...
    def get_device_placements(self) -> list[DevicePlacement]: ...
    def get_initial_environment(self) -> EnvironmentState: ...
    def get_user_profiles(self) -> list[UserProfile]: ...
```

### 3.4 其他插槽

- **WorldModelPlugin**: `predict(state, actions, horizon) → WorldModelResult`; `train(dataset) → Report`
- **RewardPlugin**: `compute(state, action, predicted_state) → RewardBreakdown`; `get_dimensions() → list[str]`
- **UserProfilePlugin**: `get_initial_preferences()`, `get_daily_schedule()`, `get_drift_model()`

### 3.5 插件发现机制

```
plugins/{type}/{name}/plugin.py    → 自动发现，每个子目录包含plugin.py
plugin.py 暴露 register() 函数     → 返回插件实例
实验配置 YAML 指定加载哪些插件      → PluginManager按配置加载
```

### 3.6 新增X的成本

| 新增什么 | 做法 | 改现有代码 |
|---------|------|----------|
| 新设备类型 | `plugins/devices/新类型/plugin.py` | 零 |
| 新场景 | `plugins/scenes/新场景/plugin.py` + GLB | 零 |
| 新Agent | `plugins/agents/新Agent/plugin.py` + TOML | 零 |
| 新世界模型 | `plugins/world_models/新模型/plugin.py` | 零 |
| 新Reward函数 | `plugins/rewards/新函数/plugin.py` | 零 |
| 新实验 | `config/experiments/新实验.yaml` | 零 |

---

## 4. Agent系统设计

### 4.1 Agent核心循环

```python
class BaseAgent:
    async def run_cycle(self, world_snapshot: WorldState):
        perception = self.perceive(world_snapshot)
        if not self.should_act(perception): return
        context = self.build_context(perception)
        if self.can_rule_decide(perception):
            actions = self.rule_decide(perception)
        else:
            actions = await self.llm_decide(context)
        for action in actions:
            result = await self.execute_tool(action)
        self.learn(perception, actions, results)
        for action in actions:
            await self.event_bus.publish(action.to_event())
```

关键约束：
- Agent之间绝对不直接调用，只通过EventBus交换消息
- Agent通过工具操作设备，不直接修改WorldState
- Agent内部维护自己的记忆，不污染全局状态

### 4.2 LLM工具调用

Agent走LLM路径时，构建的prompt包含：
- System Prompt: Agent身份、管理设备、可用工具(JSON Schema)、决策约束
- User Message: 环境状态JSON、近期事件、用户偏好、上次决策、冲突历史

LLM通过JSON Schema理解工具，通过工具输出理解世界，不需要读源码。

### 4.3 Agent协商协议

```python
class NegotiationProposal(BaseModel):
    proposal_id: str
    proposer: str
    intent: str
    affected_devices: list[str]
    proposed_actions: list[dict]
    confidence: float
    expected_reward: float
    conflicts_with: list[str] | None
    deadline_tick: int

class NegotiationResponse(BaseModel):
    proposal_id: str
    responder: str
    stance: Literal["accept", "counter", "reject"]
    counter_actions: list[dict] | None
    reason: str

class NegotiationResolution(BaseModel):
    proposal_id: str
    final_actions: list[dict]
    participants: list[str]
    consensus_score: float
    selected_by: str
```

### 4.4 Agent记忆系统

```python
class AgentMemory:
    recent_decisions: deque[DecisionRecord]     # maxlen=20, 原始保留
    preference_updates: list[PreferenceUpdate]  # 自动压缩
    learned_patterns: list[str]                 # 自然语言总结
    # 例: "用户晚上10点后偏好卧室亮度<10%"
    # 例: "夏天用户对空调温度的偏好比冬天低2度"

    def compress(self):
        """将最老的决策记录压缩为learned_patterns"""
```

### 4.5 Agent插件模板

新增Agent只需实现：
- `agent_id`, `display_name` — 身份
- `get_controlled_domains()` — 控制的设备域
- `get_observed_events()` — 关注的事件
- `should_act(perception)` — 是否需要行动
- `rule_decide(perception)` — 快速规则路径
- `can_rule_decide(perception)` — 判断是否需要LLM

BaseAgent提供默认实现：`perceive()`（按域自动裁剪）、`llm_decide()`（自动构建prompt）、`learn()`、`execute_tool()`

### 4.6 Agent配置示例

```toml
[agent]
class = "LightingAgent"
name = "照明管家"
strategy = "rule_llm_hybrid"

[agent.domains]
control = ["lighting"]
observe = ["sensor", "curtain"]

[agent.events.subscribe]
events = ["environment.*", "user.location_change", "user.device_override", "negotiation.*"]

[agent.llm]
model = "gpt-4o-mini"
fallback_to_rule = true
```

---

## 5. 世界模型与训练管线

### 5.1 双引擎架构

- **PhysicsModel**: 简化物理方程（温度扩散、光照衰减、空气流通），零训练，用于基线推演和生成训练数据
- **LearnedModel**: 2层MLP（128→64），输入状态向量+动作向量，输出下一状态+reward预测

两者共享 `WorldModelPlugin` 接口，Agent不感知底层引擎。

### 5.2 PhysicsModel规则

```
temperature_diffusion:  室内温度向室外温度线性趋近，受空调影响
humidity_diffusion:     湿度受加湿器和通风影响
light_decay:            光照 = 灯光贡献 + 自然光 × 窗帘开合度
air_quality:            CO2随人数增加，通风时降低
inter_room_diffusion:   相邻房间温度和湿度相互扩散
```

### 5.3 LearnedModel状态编码

```python
STATE_ENCODING = {
    "rooms.*.temperature":            (15.0, 35.0),
    "rooms.*.humidity":               (0.0, 1.0),
    "rooms.*.light_level":            (0.0, 1000.0),
    "rooms.*.co2_level":              (300.0, 2000.0),
    "rooms.*.occupancy":              (0, 1),
    "environment.outdoor_temp":       (-10.0, 45.0),
    "devices.*.state.power":          (0, 1),
    "devices.*.state.brightness":     (0, 100),
    "devices.*.state.target_temp":    (16.0, 30.0),
    "devices.*.state.open_percent":   (0, 100),
    "users.*.comfort_score":          (0.0, 1.0),
}
```

### 5.4 训练管线（4步）

1. **数据生成**: PhysicsModel运行1000+次随机仿真，记录(state, action, next_state, reward)四元组
2. **训练**: MLP，loss = MSE(状态预测) + MSE(reward预测)
3. **验证**: 对比两个引擎的预测精度（MSE/MAE/方向正确率）
4. **部署**: 修改实验配置 `world_model: "learned_model"`

### 5.5 Reward系统

```python
class ComfortFirstReward(RewardPlugin):
    weights = {"comfort": 0.4, "safety": 0.3, "energy": 0.2, "privacy": 0.1}

    def compute(self, state, action, predicted_state) -> RewardBreakdown:
        scores = {
            "comfort": ...,  # 温度/湿度/光照在用户偏好范围内的程度
            "safety": ...,   # 安全隐患检测（全黑走廊、过高温度）
            "energy": ...,   # 能源消耗惩罚
            "privacy": ...,  # 窗帘/摄像头隐私保护
        }
        return RewardBreakdown(
            total=加权求和,
            breakdown=scores,
            explanation=自然语言解释,  # Agent友好：理解"为什么不好"
        )
```

---

## 6. 前端渲染与可视化

### 6.1 核心原则

**数据驱动渲染**：前端不硬编码设备逻辑，一切由后端数据和插件配置驱动。

### 6.2 渲染注册表（前端"插槽"）

```typescript
const RENDERER_REGISTRY: Record<string, RendererDefinition> = {
  "point_light":       { component: LightRenderer, ... },
  "hvac_unit":         { component: HVACRenderer, ... },
  "curtain_rail":      { component: CurtainRenderer, ... },
  "speaker_device":    { component: SpeakerRenderer, ... },
  "tv_screen":         { component: TVRenderer, ... },
  "particle_emitter":  { component: ParticleEmitterRenderer, ... },
  "door_lock":         { component: LockRenderer, ... },
  "sensor_dot":        { component: SensorRenderer, ... },
  // 新增设备 → 加一条映射
}
```

DevicePlugin.get_render_config() 返回 mesh_type，前端查注册表找到对应组件。

### 6.3 双模式设计

**研究模式**: 左侧研究面板(实验配置/Agent检查器/Reward图表/对比/导出) + 右侧全量日志 + 右下偏好漂移3D散点图 + 右上世界模型面板

**演示模式**: 底部极简控制条 + 浮空信息气泡 + 全屏心智之眼叠加

### 6.4 动画系统

全局AnimationOrchestrator统一编排，参数来自DevicePlugin.get_render_config()：
- 设备属性变化 → 300~800ms补间（easing来自插件配置）
- 用户位置变化 → 1000ms路径动画
- 世界模型推演 → 500ms子场景淡入
- 意图热力图 → 2000ms指数衰减
- Agent协商 → 贝塞尔曲线粒子流

### 6.5 Pinia Store

- worldStore: 设备/房间/环境状态，响应式
- agentStore: Agent状态 + 决策日志
- simulationStore: 仿真控制（启停/速率/场景）
- experimentStore: 实验数据 + 模式切换 + 对比实验 + 导出

---

## 7. 通信协议

### 7.1 消息类型

**上行 (Client → Server)**:
- 仿真控制: CMD_SIM_START/PAUSE/RESET/SPEED, CMD_SCENE_LOAD
- 设备操作: CMD_DEVICE_CONTROL, CMD_USER_PREFERENCE, CMD_TRIGGER_EVENT
- LLM交互: CMD_USER_INPUT
- 研究功能: CMD_EXPERIMENT_LOAD/START/STOP, CMD_EXPERIMENT_COMPARE, CMD_DATA_EXPORT, CMD_MODE_SWITCH
- 心跳: HEARTBEAT_PONG

**下行 (Server → Client)**:
- 状态同步: STATE_FULL, STATE_DELTA (10Hz)
- 事件通知: EVENT_NOTIFICATION, AGENT_STATUS, NEGOTIATION_UPDATE
- LLM/世界模型: LLM_PARSE_RESULT, WORLD_MODEL_RESULT
- 研究数据: EXPERIMENT_STATUS, REWARD_TICK, PREFERENCE_DRIFT, AGENT_MEMORY_SNAPSHOT, COMPARISON_RESULT, DATA_EXPORT_READY
- 心跳: HEARTBEAT_PING

### 7.2 推送频率策略

| 数据类型 | 研究模式 | 演示模式 |
|---------|---------|---------|
| STATE_DELTA | 10Hz | 10Hz |
| EVENT_NOTIFICATION | 不限频 | 节流2Hz |
| REWARD_TICK | 10Hz | 聚合1Hz |
| AGENT_MEMORY | 每次变化 | 关闭 |
| PREFERENCE_DRIFT | 每次变化 | 关闭 |
| NEGOTIATION | 实时 | 仅最终结果 |
| WORLD_MODEL | 全量 | 全量 |

### 7.3 连接管理

- 连接建立 → 立即发送STATE_FULL
- 心跳: 30s PING / 即时PONG / 60s无PONG断开
- 断线重连: 指数退避(1s→2s→4s→8s→max 30s)，重连后请求STATE_FULL

---

## 8. 实施阶段

### Phase 0: 骨架搭建

后端: FastAPI + WebSocket + WorldState + EventBus + SimulationEngine空循环
前端: Vue3项目 + Pinia stores + WebSocket连接
交付物: 浏览器打开 → WebSocket连接成功 → 收到STATE_FULL

### Phase 1: 3D场景 + 基础渲染

前端: TresJS Canvas + OrthographicCamera + GLB加载 + LightRenderer/HVACRenderer/CurtainRenderer + Bloom
后端: PluginManager + ToolRegistry + 3个设备插件 + 1个场景插件
交付物: 3D公寓可见 → 设备有初始状态 → 开灯有发光效果

### Phase 2: 仿真引擎 + 基础Agent

后端: EnvironmentSimulator + UserBehaviorSimulator + LightingAgent/HVACAgent(规则策略) + 完整主循环
前端: StatusBar + AgentActionLog + 用户手动覆盖
交付物: Agent自动调温调光 → 日志实时滚动

### Phase 3: LLM集成 + 世界模型

后端: LLMIntegrator + Agent LLM路径 + PhysicsModel + 协商引擎
前端: 自然语言输入框 + 心智之子场景 + 热力图 + 协商粒子流
交付物: 输入"屋里有点闷" → 推演过程可视化 → Agent选择最优方案

### Phase 4: 学习 + 偏好系统

后端: AgentMemory + 偏好学习(on_user_override → 权重调整)
前端: AgentInspector + PreferenceDriftChart(3D散点图)
交付物: 手动覆盖 → 偏好图变化 → Agent下次策略调整

### Phase 5: 研究工具

后端: ExperimentManager + RewardPlugin体系 + 数据导出 + 训练数据生成 + LearnedModel训练
前端: ModeSwitch + ResearchPanel + RewardChart + ComparisonPanel + DataExport + WorldModelPanel
交付物: 完整研究工作流 — 配置 → 运行 → 分析 → 导出

### Phase 6: 视觉打磨

演示模式UI + 更多设备渲染器 + 场景切换动画 + 更多GLB模型 + 特效优化

---

## 9. 项目目录结构

```
SmartHomeSim/
├── docs/superpowers/specs/        # 设计文档
├── config/experiments/            # 实验配置 (YAML)
├── slots/                         # 插槽接口定义 (Protocol/ABC)
├── plugins/                       # 插件实现
│   ├── devices/                   # 设备插件 (light/hvac/curtain/speaker/tv/...)
│   ├── agents/                    # Agent插件 (lighting/hvac/curtain/scene_mode/...)
│   ├── scenes/                    # 场景插件 (apartment_v1/...)
│   ├── world_models/              # 世界模型插件 (physics_model/learned_model)
│   ├── rewards/                   # Reward插件 (comfort_first/energy_saving)
│   └── user_profiles/             # 用户画像插件
├── backend/                       # 后端平台层
│   ├── main.py
│   ├── api/                       # routes.py + ws.py
│   ├── core/                      # config.py + logging.py
│   ├── engine/                    # simulation.py + event_bus.py + state.py + state_manager.py
│   ├── research/                  # plugin_manager.py + tool_registry.py + experiment_manager.py + negotiation.py + data_recorder.py
│   ├── services/                  # llm.py + training.py
│   └── models/                    # schemas.py
├── frontend/                      # 前端
│   └── src/
│       ├── types/                 # TypeScript类型
│       ├── stores/                # Pinia stores (world/agent/simulation/experiment)
│       ├── composables/           # useWebSocket + useAnimationOrchestrator + useRenderRegistry
│       ├── renderers/             # 设备渲染器 (LightRenderer/HVACRenderer/...)
│       ├── components/
│       │   ├── scene/             # 3D场景组件
│       │   └── dashboard/         # UI面板 (research/ + demo/ + shared/)
│       ├── styles/
│       └── utils/
└── tools/                         # 开发辅助 (generate_training_data/train_world_model/eval/create_plugin)
```

---

## 10. 关键依赖

**后端 (Python 3.11+)**:
fastapi, uvicorn, pydantic, httpx, structlog, torch, numpy, pyyaml, tomli

**前端 (Node 18+)**:
vue@3, @tresjs/core, @tresjs/cientos, three, gsap, pinia, @vueuse/core, echarts, echarts-gl, tailwindcss
