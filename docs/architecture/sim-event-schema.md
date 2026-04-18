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
