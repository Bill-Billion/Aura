# WebSocket 协议

Author: Bill Billion  
Date: 2026-04-20

这份文档定义 SmartHomeSim Phase 1 对外开放的 WebSocket 命令、服务端消息、结构化事件类型和错误格式。目标只有一个，让旧前端继续可用，同时给事件驱动链路留出稳定的公共契约。

## 连接入口

WebSocket 入口固定为 `/ws/simulation`。

连接建立后，服务端会立刻下发一条 `STATE_FULL`。前端应先用这条快照初始化世界状态，再持续消费后续的 `STATE_DELTA`、`SIM_EVENT` 和 `AGENT_STATUS`。

## 客户端命令

当前开放的命令只有五类：

- `CMD_SIM_START`：启动仿真
- `CMD_SIM_PAUSE`：暂停仿真
- `CMD_SIM_RESET`：重置仿真到默认场景
- `CMD_SIM_SPEED`：调整仿真速度，`payload.speed` 为数字
- `CMD_DEVICE_CONTROL`：控制设备

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

Phase 1 保留六类公开消息：

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

### `SIM_EVENT`

`payload` 直接是一个 `SimEvent`。迁移期已经对外开放的事件类型如下：

- `system.timer_tick`
- `system.simulation_started`
- `system.simulation_paused`
- `system.simulation_reset`
- `environment.state_refresh`
- `user.command`
- `user.activity_change`
- `action.device_control`
- `feedback.state_delta`

其中 `system.timer_tick` 和 `environment.state_refresh` 现在也会实时外发，不再只存在后端内部。

### `SIMULATION_STATUS`

当前只会下发两类字段：

- `is_running`
- `speed`

### `ERROR`

错误格式固定为：

```json
{
  "type": "ERROR",
  "payload": {
    "code": "DEVICE_NOT_CONTROLLABLE",
    "message": "客厅温度传感器不支持修改: value",
    "details": {
      "device_id": "sensor_living_temp_01",
      "action": "set_state"
    }
  }
}
```

`details` 可选，但一旦携带，就必须是对象。

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
- `read`

## 连接生命周期

正常链路是这样：

1. 客户端连接 `/ws/simulation`
2. 服务端返回 `STATE_FULL`
3. 客户端按需发送控制命令
4. 服务端持续推送 `SIM_EVENT`、`STATE_DELTA`、`AGENT_STATUS`
5. 仿真状态变化时补发 `SIMULATION_STATUS`
6. 命令非法时返回 `ERROR`

如果 60 秒内没有收到客户端消息，服务端会主动关闭空闲连接。前端应该按自己的重连策略重新建立连接。
