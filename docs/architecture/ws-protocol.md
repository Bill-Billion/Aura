# WebSocket 协议

Author: Bill Billion  
Date: 2026-04-22  
Updated: 2026-07-21（S1：命令生命周期事件、§10.2 失败语义、事件顺序承诺）

这份文档定义 SmartHomeSim 当前对外开放的 WebSocket 命令、服务端消息、结构化事件类型和错误格式。当前阶段已经进入 Phase 2，所以除了 Phase 1 的世界状态同步消息，还会通过 `SIM_EVENT` 实时外发 `reasoning.*` 事件。S1 之后，所有设备变更（UI / Agent / 场景脚本 / 规则降级四来源）都走同一台 `CommandExecutor`，因此 `SIM_EVENT` 里还会看到 `command.lifecycle`、`device.command_failed`、`system.invariant_violation` 三类新事件。

## 连接入口

WebSocket 入口固定为 `/ws/simulation`。

连接建立后，服务端会立刻下发一条 `STATE_FULL`。前端应先用这条快照初始化世界状态，再持续消费后续的 `STATE_DELTA`、`SIM_EVENT` 和 `AGENT_STATUS`。

## 客户端命令

当前开放的命令有六类：

- `CMD_SIM_START`
- `CMD_SIM_PAUSE`
- `CMD_SIM_RESET`
- `CMD_SIM_MODE`
- `CMD_SIM_SPEED`
- `CMD_DEVICE_CONTROL`

命令信封格式统一如下：

```json
{
  "type": "CMD_DEVICE_CONTROL",
  "timestamp": 1760762400.25,
  "payload": {
    "device_id": "light_living_01",
    "action": "turn_off"
  }
}
```

`CMD_DEVICE_CONTROL` 兼容两种载荷：

- 新格式：`device_id + action + params`
- 兼容格式：`device_id + property + value`

## 服务端消息

当前保留六类公开消息：

- `STATE_FULL`：全量世界状态快照
- `STATE_DELTA`：批量状态增量，`payload.deltas` 为数组
- `AGENT_STATUS`：Agent 运行状态快照
- `SIM_EVENT`：结构化事件流
- `SIMULATION_STATUS`：仿真运行状态，例如 `is_running`、`speed`
- `ERROR`：结构化错误

### `STATE_FULL`

`payload` 对应当前世界快照，至少包含这些根字段：

- `simulation_tick`
- `simulation_speed`
- `simulation_mode`
- `wall_tick_ms`
- `simulated_dt_seconds`
- `is_running`
- `scene_id`
- `environment`
- `devices`
- `rooms`
- `agents`
- `users`

### `STATE_DELTA`

`payload.deltas` 是一组路径更新。当前路径语义覆盖五类根路径：

- `simulation_*`
- `environment.*`
- `rooms[...]`
- `users[...]`
- `devices[...]`

示例：

```json
{
  "type": "STATE_DELTA",
  "payload": {
    "deltas": [
      {
        "path": "users[user_01].activity",
        "old_value": "idle",
        "new_value": "watching_tv",
        "caused_by": "user_behavior_sim",
        "reason": "apply user activity change"
      }
    ]
  }
}
```

命令引发的 delta 里，`caused_by` 取命令来源（`ui` / `agent` / `scenario` / `rule_fallback`），`caused_by_event_id` 指向那条 `action.device_control`。同一条命令派生的效果 delta（房间 `light_level`、`perceived_temperature`、`security_coverage`）与直接 delta 归在同一条 `STATE_DELTA` 里，并共享同一个 `caused_by_event_id`。

### `AGENT_STATUS`

`payload.agents` 是一个以 `agent_id` 为 key 的状态表。当前字段包括：

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

### `SIM_EVENT`

`payload` 直接是一个 `SimEvent`。当前对外开放的事件类型如下：

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
- `command.lifecycle`
- `action.device_control`
- `feedback.state_delta`
- `device.command_failed`
- `system.invariant_violation`

说明：

- `system.timer_tick` 和 `environment.state_refresh` 继续实时外发
- `reasoning.*` 不新增新的 envelope，全部通过 `SIM_EVENT` 输出
- S1 起 `CMD_DEVICE_CONTROL` 也走 `CommandExecutor`：与 Agent 路径同一条六级校验、同一套十态生命周期、同一份失败词表，不再有"UI 直控专用链路"

### `SIMULATION_STATUS`

当前会下发这些字段：

- `is_running`
- `speed`
- `mode`
- `wall_tick_ms`
- `simulated_dt_seconds`

### `ERROR`

错误格式固定为：

```json
{
  "type": "ERROR",
  "payload": {
    "code": "capability_not_supported",
    "message": "sensor 设备不支持能力 value",
    "details": {
      "device_id": "sensor_living_temp_01",
      "capability": "value",
      "command_id": "0f2c…",
      "status": "failed"
    }
  }
}
```

`details` 可选，但一旦携带，就必须是对象。

`code` 取自两套正交词表：

**消息层错误码**（消息本身不合法，命令还没成形）：

| code | 触发条件 |
| --- | --- |
| `malformed_message` | 帧不是合法 JSON，或不是 JSON 对象 |
| `invalid_payload` | `payload` 不是对象，或字段结构校验失败（`details.issues` 列出逐条问题） |
| `invalid_device_command` | 载荷结构合法但没有可执行的 `action` / `property` |
| `unsupported_message_type` | 未知的 `type`（`details.type` 回显原值） |
| `internal_error` | 处理该消息时服务端内部异常 |

**命令层失败码**（命令已成形，被 `CommandExecutor` 拒绝或执行失败，spec §10.2 十类）：

| code | 语义 |
| --- | --- |
| `unknown_device` | 设备不存在（含校验通过后设备并发消失） |
| `device_offline` | 设备离线，不接受可写命令 |
| `capability_not_supported` | 该设备类型没有这条能力，或这台设备没声明这条能力位 |
| `read_only_capability` | 能力存在但只读（如 `sensor.value` / `camera.view`） |
| `invalid_value_type` | 值类型与能力矩阵声明不符 |
| `invalid_value_range` | 数值越界或枚举取值非法 |
| `policy_denied` | 场景策略禁止操作该设备 |
| `execution_timeout` | 动作已下发但状态反馈超出预算窗口 |
| `state_feedback_missing` | 执行后没有拿到状态反馈 —— **保留码，当前版本不会出现在线上**（见下） |
| `superseded_by_newer_command` | 同一控制点被更新的命令取代，本命令不执行 |

> `state_feedback_missing` 目前**没有产出方**：同步 StateManager 下 `apply_action` 立即返回，唯一的反馈类失败路径（超出预算窗口）报 `execution_timeout`。该码列在这里只为保持 §10.2 十类词表完整——客户端应认得它，但不要等它出现。引入异步 episode（S2/S3）后才会有真实产出方。

此外 `system.invariant_violation` 使用独立错误码 `invariant_violation`：它描述"世界差点被改坏"（系统级故障），不属于上面十类"命令为什么没被执行"。

一条坏消息只换来一条 `ERROR`，连接保持存活；同一连接后续的合法命令照常执行。

## `SimEvent` 公共字段

所有结构化事件都共享下面的顶层字段：

- `event_id`
- `event_type`
- `source`
- `timestamp`
- `wall_time`
- `correlation_id`
- `causal_parent`
- `priority`
- `data`

字段约束和因果链规则以 `docs/architecture/sim-event-schema.md` 为准。

## Phase 2 reasoning payload

### `reasoning.perception_snapshot`

```json
{
  "agent_id": "lighting_agent",
  "trigger_event_type": "user.activity_change",
  "world_summary": "event=user.activity_change; time=19:00; weather=clear; ...",
  "relevant_devices": ["light_living_01"],
  "relevant_rooms": ["living_room"]
}
```

### `reasoning.intent_recognized`

```json
{
  "agent_id": "lighting_agent",
  "intent": "light occupied room",
  "confidence": 0.94,
  "explanation": "Occupancy increased in the living room during the evening",
  "provider": "openai_responses",
  "model": "gpt-5.4",
  "latency_ms": 320
}
```

### `reasoning.task_decomposition`

```json
{
  "agent_id": "lighting_agent",
  "intent": "light occupied room",
  "task_steps": ["raise brightness", "warm color"]
}
```

### `reasoning.coordination_decision`

```json
{
  "agent_id": "lighting_agent",
  "outcome": "approved",
  "priority": "user_comfort",
  "conflicts": [],
  "winning_commands": [
    {
      "device_id": "light_living_01",
      "property": "extra.brightness",
      "value": 70,
      "reason": "occupied evening lighting"
    }
  ]
}
```

### `reasoning.execution_plan`

```json
{
  "agent_id": "lighting_agent",
  "execution_mode": "llm",
  "commands": [
    {
      "device_id": "light_living_01",
      "property": "extra.brightness",
      "value": 70,
      "reason": "occupied evening lighting"
    }
  ]
}
```

### `reasoning.fallback_rule_based`

```json
{
  "agent_id": "lighting_agent",
  "reason": "timeout",
  "failed_step": "intent_generation",
  "fallback_strategy": "rule_based"
}
```

## S1 命令生命周期 payload

四来源（`ui` / `agent` / `scenario` / `rule_fallback`）的每条设备命令都会外发同一组事件，前端只需消费 `SIM_EVENT` 即可重建"命令为何影响 / 未影响世界"。

### `command.lifecycle`

`source` 固定为 `command_executor`。每次状态迁移发一条：

```json
{
  "event_type": "command.lifecycle",
  "source": "command_executor",
  "correlation_id": "…",
  "causal_parent": "<根事件 event_id>",
  "data": {
    "command_id": "…",
    "device_id": "light_living_01",
    "capability": "power",
    "from_status": "validated",
    "to_status": "executing",
    "source": "ui",
    "detail": null,
    "failure_code": null
  }
}
```

`to_status` 取 spec §10.1 十态：`proposed`、`approved`、`rejected`、`validated`、`executing`、`succeeded`、`failed`、`timed_out`、`cancelled`、`superseded`。其中 `rejected`、`succeeded`、`failed`、`timed_out`、`cancelled`、`superseded` 是终态（吸收态，不再有后续迁移）。`from_status` 只有出生事件为 `null`。`failure_code` 仅在带失败语义的迁移上出现，取值见上面的命令层失败码表。

一条成功命令的典型序列：`proposed → approved → validated → executing → succeeded`。

### `device.command_failed`

命令被拒绝或执行失败时，与 `command.lifecycle` 的失败迁移同码派发一条：

```json
{
  "event_type": "device.command_failed",
  "source": "command_executor",
  "data": {
    "command_id": "…",
    "device_id": "light_living_01",
    "capability": "brightness",
    "value": 999,
    "source": "ui",
    "error_code": "invalid_value_range",
    "reason": "brightness 超出上界 100（收到 999）"
  }
}
```

UI 来源的命令在此之外还会收到一条同码的 `ERROR` 消息。

### `system.invariant_violation`

spec §2.2 不变式被破坏时发出，`priority` 恒为 `3`（最高），且本命令已落地的 delta 会被逆序回滚——回滚路径列在 `reverted_paths` 里，禁止静默纠正：

```json
{
  "event_type": "system.invariant_violation",
  "source": "state_manager",
  "priority": 3,
  "data": {
    "invariant": "…",
    "message": "…",
    "details": {},
    "command_id": "…",
    "device_id": "light_living_01",
    "capability": "power",
    "source": "agent",
    "reverted_paths": ["devices[light_living_01].state.power"]
  }
}
```

### 顺序承诺

同一条命令的消息顺序是稳定的，前端可以据此建因果树：

1. 根事件（UI 命令为 `user.command`，Agent 命令为其推理链根事件）先于任何派生事件与 `STATE_DELTA` 外发
2. `command.lifecycle` 的 `executing` 先于 `action.device_control`
3. `action.device_control` 先于由它派生的 `feedback.state_delta`（后者的 `causal_parent` 即前者的 `event_id`）
4. 终态 `command.lifecycle` 最后到达
5. 校验失败的命令绝不产生 `action.device_control` / `feedback.state_delta`，世界零变更

## 当前开放的设备类型

默认场景对外暴露的 `device.type` 固定为：

- `light`
- `hvac`
- `curtain`
- `sensor`
- `fan`
- `camera`

对应能力枚举为：

- `power`
- `brightness`
- `color_temp`
- `target_temp`
- `mode`
- `speed`
- `open_percent`
- `shake`
- `timeout`
- `view`
- `online`（只读，camera）
- `value`（只读，sensor；spec §3.2 表里旧名为 `read`，S1 已统一改名，见该节实现说明）

## 连接生命周期

正常链路是这样：

1. 客户端连接 `/ws/simulation`
2. 服务端返回 `STATE_FULL`
3. 客户端按需发送控制命令
4. 服务端持续推送 `SIM_EVENT`、`STATE_DELTA`、`AGENT_STATUS`
5. 仿真状态变化时补发 `SIMULATION_STATUS`
6. 命令非法时返回 `ERROR`

服务端不设空闲踢线：只观察不操作的会话（可观测性面板的常态）会一直保持连接。客户端的 `HEARTBEAT_PONG` 只作保活，不会触发 `ERROR`。断线后前端按自己的重连策略重连即可。
