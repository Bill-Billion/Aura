# SmartHomeSim — gstack 最终讨论方案汇总

> 整合自 /office-hours + /autoplan (CEO/Eng/Design/DX Review) 的所有产出
> Branch: feat/mvp | Date: 2026-04-17

---

## 一、产品定位

**智能家居 Agent 行为可观测性平台** — "Weights & Biases for embodied home AI"

核心不是仿真引擎逼真度，而是让研究者**对比不同 Agent 策略在同一场景下的思考过程**。

关键洞察：**事件流即产品**。Agent 思考链路是第一类数据，可观测性是差异化卖点（AI2-THOR / Habitat 都没有）。

### 目标用户
正在用 RL/IL 方法训练家庭自动化策略的研究团队和 IoT 平台算法团队。

### 最窄楔子
一个能跑通完整事件驱动决策链路的 3D 可视化平台。研究者看到 Agent 每一步决策：
**感知事件 → 识别意图 → 分解任务 → 多Agent协调 → 执行操作 → 反馈**

### 商业模式
- SDK 开源获客（PyPI + GitHub）
- SaaS 可观测性平台收费（Docker 私有部署 + 云托管）
- 获客路径：GitHub demo → docker-compose 体验 → 平台

---

## 二、当前架构问题

```
SimulationEngine._tick() [100ms 循环]
    ├── world.simulation_tick += 1
    ├── _advance_time(world)
    ├── user_sim.step(world) → publish events
    ├── env_sim.step(world, dt=60.0)
    ├── agent_runtime.step(world) → actions
    ├── state_manager.apply_action() → deltas
    └── conn.broadcast(STATE_DELTA + AGENT_STATUS)
```

| 问题 | 影响 |
|------|------|
| Tick 循环限制响应速度 | Agent 每 100ms 被动轮询，无法即时响应关键事件 |
| 规则 Agent 无法产生思考链路 | 无状态 decide() 只能 if-else，无法展示意图识别 |
| EventBus 简单 pub/sub | 无优先级、路由、因果链追踪 |

---

## 三、目标架构

**Event trigger → Perception → Intent Recognition (LLM) → Task Decomposition → Multi-Agent Collaboration → Execution → Feedback Loop**

### 核心概念

#### Event Schema
```python
class SimEvent(BaseModel):
    event_id: str          # UUID v4
    event_type: str        # sensor / user / device / reasoning / action / feedback
    source: str            # agent_id 或 simulator 名称
    timestamp: float       # 模拟时间戳
    wall_time: float       # 墙钟时间
    correlation_id: str    # 同一触发事件链路共享
    causal_parent: str | None  # 因果父事件
    priority: int          # 0=低, 1=正常, 2=高, 3=紧急
    data: dict             # 载荷
```

#### Correlation ID 规则
- 用户行为事件产生新 correlation_id
- 该事件触发的所有后续事件共享同一 correlation_id
- 可通过 correlation_id 检索完整因果链

---

## 四、三阶段实施计划

### Phase 1: 事件 Schema + EventBus 升级

**目标**: 替换 tick 循环为事件驱动，保持所有现有功能不受影响。

渐进式迁移步骤：
1. 新增 SimEvent（继承 WorldEvent），保持兼容
2. 引入 SimulatorTimer 替代 tick 计数
3. user_sim 改为事件驱动
4. env_sim 改为事件驱动
5. 移除 _tick 循环，SimulationEngine 变为事件调度器
6. 每步验证 WebSocket 推送不受影响

**DX Review 额外要求**:
- README + 5 分钟 quickstart guide
- docker-compose.yml（单命令启动）
- SDK 接口定义（SimulationClient protocol）
- WS 协议文档
- 错误响应格式（ErrorMessage schema）
- 开放 device/event 类型枚举

**完成标准**:
- EventBus 支持优先级事件和 correlation ID 查询
- 无 _tick 循环，全部事件触发
- WebSocket 推送、设备状态变更不受影响

---

### Phase 2: Agent 重构 + LLM 意图识别

**目标**: Agent 从无状态规则引擎升级为事件驱动自主 Agent。

Agent 重构为 6 层架构：
1. **事件订阅** — 订阅特定 event types
2. **感知层** — 从事件流构建内部状态视图
3. **意图识别** — LLM 分析用户行为 + 环境状态
4. **任务分解** — 将意图拆解为可执行步骤
5. **推理发射** — 每一步发射 reasoning event
6. **记忆系统** — 短期：当前 episode 最近 N 个事件

**LLM 失败降级**: 超时/不可用时降级为规则引擎，标注 "fallback:rule_based"

**多 Agent 协作**:
- Agent 间通过 EventBus 协调
- 轻量级 Arbiter：检测设备冲突，按优先级仲裁（用户舒适 > 节能）
- Agent 策略注册机制：动态注册/切换 Agent 实现

**DX Review 额外要求**:
- LLMProvider ABC（provider 抽象层）

**完成标准**:
- Agent 每次决策发射 ≥ 3 个 reasoning event
- LLM 意图识别延迟 < 5s
- 降级路径可用
- 多 Agent 协作可处理冲突场景

---

### Phase 3: 可观测性前端

**目标**: 实时可视化 Agent 完整思考链路。

#### 集成策略
- 替换 AIChatPanel + AgentActionLog → ObservabilityPanel
- 侧边栏宽度 360px → 480px
- 复用 uiStore.sidebarOpen

#### 布局
```
┌──────────────────────────┬──────────────────────────┐
│                          │  ObservabilityPanel       │
│     3D Scene             │  ┌──────────────────────┐ │
│                          │  │ 筛选栏 + Episode 信息 │ │
│                          │  ├──────────────────────┤ │
│                          │  │ 事件流时间线 (60%)    │ │
│                          │  │  ├ root event        │ │
│                          │  │  ├─ causal child     │ │
│                          │  │  │  ├─ reasoning     │ │
│                          │  │  │  └─ action        │ │
│                          │  │  └─ feedback          │ │
│                          │  ├──────────────────────┤ │
│                          │  │ 推理详情 (40%)        │ │
│                          │  │  1.✅Perception 12ms  │ │
│                          │  │  2.✅Intent 1.8s LLM  │ │
│                          │  │  3.✅Decomposition     │ │
│                          │  │  4.✅Coordination      │ │
│                          │  │  5.🔄Execution        │ │
│                          │  │  6.⏳Feedback          │ │
│                          │  └──────────────────────┘ │
├──────────────────────────┴──────────────────────────┤
│ StatusBar: Sim时间 | Tick | Agent状态 | 设备高亮     │
└─────────────────────────────────────────────────────┘
```

#### 事件类型颜色系统
```typescript
const eventColors = {
  sensor:    '#4FC3F7',  // 蓝
  user:      '#4ade80',  // 绿
  reasoning: '#a78bfa',  // 紫
  action:    '#ffe74a',  // 黄
  feedback:  '#fb923c',  // 橙
}
```

#### 交互状态表
| 功能 | Loading | Empty | Error | Success |
|------|---------|-------|-------|---------|
| Event Timeline | skeleton | "点击 ▶ 开始模拟" | "连接断开" + retry | 自动滚动 |
| Reasoning Detail | 紫色脉冲 + 计时器 | "选择事件查看详情" | "LLM 超时 + fallback" | 6 步完整展示 |
| Event Filters | N/A | "无匹配事件" | N/A | 列表更新 |

#### 响应式策略
| Viewport | 布局 |
|----------|------|
| ≥1920px | ObservabilityPanel 480px + 3D 场景 |
| 1366-1919px | ObservabilityPanel 400px + 3D 场景 |
| 1024-1365px | ObservabilityPanel 全屏叠加 |
| <1024px | 不支持 |

**完成标准**:
- 实时展示完整因果链
- End-to-end latency < 200ms（不含 LLM 调用）
- WebSocket ≤ 50 msg/sec，单条 ≤ 64KB

---

## 五、前端视觉计划（当前正在执行）

完成 3D 场景与 gamemcu 对标收尾，与上述架构计划并行。

### Phase 4: Shader 微调（~1h CC）
- 色温偏冷调整
- 灯光范围收窄
- 墙体轮廓增强
- 页面背景渐变

### Phase 5: 设备动画与后端联动（~2h CC）
- worldStore → DeviceVisual 联动
- Agent 触发操作的视觉反馈
- 多楼层设备独立控制

### Phase 6: 地面反射系统（~2h CC）
- PlanarReflection
- 模糊反射
- 性能优化

### Phase 7: 清理集成（~30min CC）
- 删除废弃代码
- 端到端测试
- Git 清理

---

## 六、约束

- FastAPI 后端 + Vue 3/TresJS 前端 + WebSocket
- EventBus 升级不从零重建
- LLM 调用 1-5s 延迟，需异步，不可用时降级
- 前端 3D 渲染已 85% 完成，Phase 3 仅新增面板
- EventBus 吞吐 ≥ 1000 events/sec
- 并发 Agent ≤ 8
- 每个 episode LLM 成本 < $0.10

---

## 七、gstack 审查历史

### Review Pipeline 结果
| 阶段 | 状态 | 关键发现 |
|------|------|---------|
| CEO Review (autoplan) | ISSUES_OPEN | 6/6 consensus: scope risk, competitive threat |
| Eng Review (interactive) | CLEAN | 10 issues resolved |
| Design Review (interactive) | 3→8/10 | 10 design decisions approved |
| DX Review (autoplan) | 4/10 | No README, no Docker, no SDK interface |

### 22 个自动决策审计
| # | 阶段 | 决策 | 原则 |
|---|------|------|------|
| 1 | CEO | 接受已有 Eng+Design reviews | P3 Pragmatic |
| 2 | CEO | 保持 Approach A (full EDA) | P1 Completeness |
| 3 | CEO | **LLM 留在 Phase 2（用户挑战被拒绝）** | P6 用户判断 |
| 4-8 | CEO | 接受现有审查、保持 3 阶段 | P3 |
| 9-12 | Design/Eng | 接受交互式审查的 10+10 个决策 | P3 |
| 13-22 | DX | 自动加入 README/Docker/SDK/协议文档/错误格式/LLMProvider | P1 Completeness |

### 关键分歧
- **LLM 是否 MVP 必需**：两个独立审查模型都认为不是，创始人 3 次坚持认为是。最终接受创始人判断。
- **Approach A vs C**：两个模型推荐 Approach C（事件拦截层），创始人选择 Approach A（完整 EDA）。

### 竞争风险
- LangSmith / LangFuse 等通用可观测性栈是真实威胁
- 需要聚焦智能家居垂直场景的深度优势

---

## 八、开放问题

1. LLM Provider 选择：本地模型 vs API 调用？
2. 事件 Schema 标准化程度：参考 HearthNet 还是自研？
3. 多 Agent 冲突仲裁权重：用户舒适度 vs 能耗
4. 记忆系统持久化：内存 vs Redis vs 文件

---

## 九、The Assignment

**在写一行代码之前，先完成事件 Schema 的详细设计。** 定义所有事件类型、字段规范、correlation 规则、因果链追踪格式。拿着这个 schema 去找一个做智能家居研究的人验证。

---

## 十、创始人思维观察

- 对"为什么规则 Agent 不够"有具体产品判断：研究者需要意图识别的复杂度
- "Event stream IS the product" 翻转了工程直觉
- 学术品味：理解约束后重新定义，而非照搬
- 3 次面对独立审查挑战仍坚持 LLM 是必需品 — 基于直接用户反馈的产品判断
