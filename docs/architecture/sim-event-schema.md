# SimEvent Schema 设计

Author: Bill Billion  
Date: 2026-04-18

这份文档定义 SmartHomeSim 在事件驱动迁移期使用的统一事件模型。目标不是一次性推翻 tick 引擎，而是先把“用户触发、Agent 动作、设备反馈”这三段链路标准化，让前端、后端和后续 LLM Agent 都围绕同一种事件语义工作。

## 1. 设计目标

事件必须能回答四件事。是谁触发的，发生在模拟时间的哪一刻，和哪一条因果链有关，以及它最终改了什么。

迁移期仍允许保留 `STATE_FULL`、`STATE_DELTA` 和 `AGENT_STATUS`。它们负责兼容旧前端。新增的 `SIM_EVENT` 通道负责承载结构化事件流，供下一阶段的可观测性面板直接消费。

## 2. 标准字段

`SimEvent` 使用以下字段：

- `event_id`: 全局唯一事件 ID，默认使用 UUID4 的十六进制字符串。
- `event_type`: 事件命名空间。采用 `family.detail` 形式，而不是只写宽泛类别。
- `source`: 事件源。可以是 `user_ui`、`user_behavior_sim`、`lighting_agent`、`hvac_agent`、`state_manager` 这类稳定标识。
- `timestamp`: 模拟时间戳。当前迁移期统一用 `simulation_tick` 的浮点值表达。
- `wall_time`: 墙钟时间。用于真实时序排序和日志对齐。
- `correlation_id`: 同一条因果链共享的关联 ID。
- `causal_parent`: 当前事件的直接父事件 ID。根事件填 `null`。
- `priority`: 事件优先级。`0=background`，`1=normal`，`2=high`，`3=critical`。
- `data`: 载荷。必须只放业务字段，不重复顶层元数据。

S2 之后追加的可复现元数据字段见第 11 节。它们全部可选、默认 `null`，Phase 1 的事件构造式不受影响。

## 3. event_type 命名规则

顶层 family 只允许以下几类：

- `user.*`: 用户主动行为或用户模拟器产出的触发事件。
- `sensor.*`: 传感器或环境感知事件。当前版本预留。
- `environment.*`: 环境仿真产生的状态变化。当前版本预留。
- `reasoning.*`: Agent 推理链路。Phase 2 才会真正启用。
- `action.*`: Agent 或系统发出的可执行动作。
- `feedback.*`: 动作落地后的设备或状态反馈。
- `system.*`: 生命周期、健康检查、重置这类系统级事件。

当前 Phase 1 已经落地的细分类型如下：

- `user.command`: 来自 WebSocket UI 指令的用户控制。
- `user.activity_change`: `user_behavior_sim` 产生的行为变化。
- `action.device_control`: Agent 触发的设备控制动作。
- `feedback.state_delta`: `state_manager` 对设备状态产生的结构化反馈。

## 4. correlation_id 规则

这部分是后续可观测性的基础，规则必须稳定。

用户命令和用户模拟器事件永远作为根事件。它们创建新的 `correlation_id`。

Agent 动作默认继承最近一个根事件的 `correlation_id`。如果当前 tick 没有根事件，但系统仍然产生动作，就由该动作自己开一条新的链。

设备反馈永远继承它所响应动作的 `correlation_id`。

同一条链上的事件，不允许中途改写 `correlation_id`。

## 5. causal_parent 规则

`causal_parent` 只记录“直接父事件”，不记录整条祖先路径。

- 根事件：`causal_parent = null`
- Agent 动作：`causal_parent = root_event.event_id`
- 设备反馈：`causal_parent = action_event.event_id`
- 未来 reasoning 事件：挂在触发它的根事件或上一步 reasoning 事件下面

前端要恢复完整链路时，只需要按 `correlation_id` 取全量事件，再用 `causal_parent` 重建树。

## 6. data 载荷约束

`data` 的写法保持扁平、可 JSON 序列化，不允许混入对象方法和不可序列化类型。

推荐字段：

- `user.command`
  - `message_type`
  - `device_id`
  - `action`
  - `params`
- `user.activity_change`
  - `user_id`
  - `from_room`
  - `to_room`
  - `activity`
- `action.device_control`
  - `agent_name`
  - `device_id`
  - `property`
  - `value`
  - `reason`
- `feedback.state_delta`
  - 直接复用 `DeltaChange.model_dump()` 的结果

## 7. WebSocket 通道

迁移期 WebSocket 同时保留旧消息和新事件流：

- `STATE_FULL`: 全量世界状态快照
- `STATE_DELTA`: 兼容旧前端的增量状态
- `AGENT_STATUS`: 兼容旧前端的 Agent 状态
- `SIM_EVENT`: 新增的结构化事件消息，`payload` 直接是一个 `SimEvent`

这样旧界面不回归，新界面也能从现在开始按事件链消费数据。

## 8. 查询接口要求

EventBus 必须至少支持这些查询维度：

- `event_type`
- `since`
- `correlation_id`
- `source`
- `min_priority`
- `causal_parent`

除此之外，还要提供 `get_causal_chain(root_event_id)`，让调试时能直接从根事件拿到完整因果链。

## 9. 一个标准示例

```json
{
  "event_id": "90f2e95b4f1d44e0b5c6a7b4cc76f11d",
  "event_type": "action.device_control",
  "source": "lighting_agent",
  "timestamp": 42.0,
  "wall_time": 1760762400.25,
  "correlation_id": "f5f7f26b2adf4c99a59d930302e2d33f",
  "causal_parent": "8a6072e1d2ef49a7b8bc0a1f18df3d25",
  "priority": 2,
  "data": {
    "agent_name": "Lighting Agent",
    "device_id": "light_living_01",
    "property": "extra.brightness",
    "value": 40,
    "reason": "occupied room at daytime"
  }
}
```

## 10. 当前边界

这份 schema 先服务 Phase 1，不提前引入 LLM 专属字段。推理时延、token 使用、fallback 原因这些字段留到 `reasoning.*` 正式落地时再补。

当前也不做事件持久化存储。EventBus 历史只负责进程内查询和前端链路联调。

---

## 11. S2 可复现元数据扩展

Updated: 2026-07-21（S2-T2）

第 1-10 节描述的是 Phase 1 的事件语义，仍然有效。这一节补的是把"跑仿真"变成"跑可复现实验"所需的元数据：一条事件必须能回答它属于哪个 run、哪个场景、由哪种机制生成，以及它在这个 run 里排第几。

### 11.1 新增字段

全部可选，默认 `null`。既有事件构造式一行不改仍然合法。

| 字段 | 类型 | 默认 | 谁来填 | 含义 |
|------|------|------|--------|------|
| `run_id` | `str \| null` | `null` | EventBus 盖章（缺失才填） | 事件所属的 run（§11 run 模型）。 |
| `scenario_id` | `str \| null` | `null` | EventBus 盖章（缺失才填） | 产生该 run 的 `ScenarioSpec.id`。 |
| `event_generation_mode` | 枚举 \| `null` | `null` | 事件生产方（只有根事件填） | `scripted` / `rule_based` / `stochastic` / `system`。 |
| `generation_rule_id` | `str \| null` | `null` | 事件生产方 | `rule_based` 事件必填：命中的是哪条规则。 |
| `rng_stream` | `str \| null` | `null` | 事件生产方 | `stochastic` 事件必填：抽样用的命名随机子流 id（对应 `SimRandom.derive(stream_id)`）。 |
| `seq` | `int \| null` | `null` | **只由 EventBus.publish 盖章** | run 内单调发布序号，从 `0` 起，每条 `+1`。 |
| `sim_time_s` | `float \| null` | `null` | 生产方优先，否则 EventBus 用注入的模拟时钟填 | run 起点起算的模拟秒。 |

`event_generation_mode` 回答的是一个窄问题：**这条根事件是怎么进世界的**。枚举只有四个值——`scripted` / `rule_based` / `stochastic` 是规格 §4.5 的三种生成产线，`system` 是本实现补的一种，指引擎自己发的 `system.timer_tick` / `system.simulation_reset` 这类生命周期根事件。

哪些事件**不带**生成模式（字段为 `null`），以及为什么：

| 事件 | 为什么是 `null` |
|------|----------------|
| `user.command`（UI 直控） | 是真人发的，不是平台生成的。它的来源写在 `source: "user_ui"` 里。 |
| `reasoning.*` / `command.lifecycle` / `action.device_control` / `feedback.state_delta` | 派生事件。它们不是"被生成"的，是被某条根事件**引出**的；来源由 `causal_parent`（引它出来的那条根事件）+ `source`（哪个 agent / executor）表达，顺着 `get_causal_chain()` 就能读到根事件的生成模式。 |

因此 `get_history(event_generation_mode=...)` 与 `/api/runs/{id}/events?generation_mode=` 筛的是**根事件**，不是"这条流里所有 agent 相关的事件"。一次典型 run 里大部分事件（S2 review 实测 138 条里的 97 条）该字段为 `null`，这是设计，不是漏填。

> 历史注记：枚举里曾有第五个成员 `agent`，文档也曾称它能把"agent 自己想出来的行为"从流里筛出来。`backend/` 里从来没有任何生产方写过它，S2 review 用 grep 证伪后删除。派生事件的来源问 `causal_parent`，不问生成模式。

### 11.2 盖章规则（EventBus 是权威）

`run_id` / `scenario_id` / `sim_time_s`：**缺失才填**。生产方显式给的值优先。这条不能反过来——旧 run 的在飞事件若被改写成当前 run，§2.2 的"旧 run_id 的变更不得应用到活跃 run"就再也判不出来了。

`seq`：**总线唯一权威，只分配一次**。它描述的是"这条事件在本 run 里第几个被发布"，不是生产方的主张（生产方不要自己填）。盖章在 `publish()` 内、写入历史与分发订阅者之前完成，且是**原地修改**——调用方手里的那条对象同步生效，不需要再查一次历史。同一条事件对象二次进总线不会换号：号码一旦分配就终身不变，否则同一条事件会带着两个不同的 `seq` 出现在 WS 与 `events.jsonl` 上。

`EventBus.stamp(event)`：给"必须先拿到盖章副本再外发"的调用方用的公开盖章入口，语义与 `publish()` 里那次盖章完全一致，只是不分发订阅者、不写历史。唯一的生产用例是 `backend/main.py` 的 `CMD_DEVICE_CONTROL`：那条根 `user.command` 必须是本命令外发的第一条 WS 消息（前端要先拿到因果头再拿状态变更），所以顺序是 **stamp → broadcast → publish**。此前的实现是 broadcast → publish，WS 上那份根事件 `seq` 是 `null` 而它的子事件带 `seq 1..N`，S5 的 `eventStore` 按 `seq` 排因果树时根节点排不进去——S2 review 修项，已闭环（`tests/test_main.py::test_ui_command_root_event_carries_seq_ahead_of_its_children`）。

`EventBus.clear()` 清空历史并把 `seq` 归零，但不动订阅者和 run 上下文；换 run 时由 `set_run_context()` + `clear()` 两步显式完成。

### 11.3 seq 存在的理由

`timestamp` 是 tick 计数，同一 tick 内产生的事件全部并列，排序完全依赖字典/协程调度顺序——这正是审计里"同 tick agent 事件顺序不可复现"的根。`seq` 是全局唯一严格递增的，因此：

- 它是 canonical trace（S2-T9 字节一致性门）的排序锚。
- `get_causal_chain()` 的排序键升级为 `(timestamp, wall_time, seq)`，同刻事件不再靠运气排序。
- 事件工件（S2-T7 的 `data/runs/{run_id}/events.jsonl`）按 `seq` 顺序逐行追加，读回即还原发布顺序。

### 11.4 sim_time_s 与既有三个时间域的关系

审计确认当前有三个时间域并存。这次**不删任何一个**（改动面太宽会砸穿前端 `eventStore` 的排序契约），只新增第四个数值字段做最小统一：

| 域 | 位置 | 单位 | 问题 |
|----|------|------|------|
| `timestamp` | `SimEvent.timestamp` | tick 计数（float） | 粒度粗、同 tick 并列；随 `mode` 改变对应的模拟秒数不同。 |
| `wall_time` | `SimEvent.wall_time` | Unix 秒 | 墙钟，跨 run 不可复现，必须排除出 canonical trace。 |
| `time_of_day` | `world.environment.time_of_day` | `"HH:MM"` 字符串 | 只精确到分钟，且真正的精度藏在 `SimulationEngine._time_of_day_seconds` 这个私有累加器里。 |
| **`sim_time_s`** | `SimEvent.sim_time_s` | 模拟秒（float） | 新增。run 起点为 0，单调递增。 |

换算关系（tick 速率恒定时）：

```
sim_time_s ≈ timestamp × SimulationEngine.simulated_dt_seconds
```

`simulated_dt_seconds` 由 `SimulatorTimer` 的模式决定（`observe` = 10 模拟秒/tick，`demo` = 60）。run 中途切模式会让这个乘法关系断掉，所以 `sim_time_s` 由时钟源逐 tick 累加得出，而不是每次现算——这也是它比 `timestamp` 更适合做回放对齐锚的原因。

一个已知的脏点：`backend/simulators/user_behavior.py` 产出的 `user.activity_change` 事件把 `timestamp` 填成了 `time.time()`（墙钟），随后由引擎覆盖成 tick 值。`sim_time_s` 不复制这个错误——生产方要么填真实模拟秒，要么留空让总线的时钟源填。

### 11.5 §4.5 五项元数据落点

规格要求"每条**被生成**的事件"都携带五项——即三条生成产线发出的根事件。`run_id` / `scenario_id` 由总线给全流盖章，后三项只有根事件有。落点如下：

| §4.5 要求 | 字段 |
|-----------|------|
| `run_id` | `SimEvent.run_id`（全流） |
| `scenario_id` | `SimEvent.scenario_id`（全流） |
| `event_generation_mode` | `SimEvent.event_generation_mode`（只有根事件；派生事件为 `null`，见 §11.1） |
| `generation_rule_id`（规则产出时） | `SimEvent.generation_rule_id` |
| seed 或确定性随机流标识（stochastic 时） | `SimEvent.rng_stream`（seed 本身记在 run 元数据里，不逐事件重复） |

### 11.6 因果链不跨 run

`get_causal_chain(root_event_id)` 只在与根事件同 `run_id` 的事件里找子节点。reset 之后新 run 可能复用 `correlation_id` 或 `causal_parent`（测试构造和录制回放都会），只按 `event_id` 认父会把两个 run 的链焊成一条。`get_history()` 同步新增 `run_id` 与 `event_generation_mode` 两个过滤维度。
