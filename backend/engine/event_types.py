"""§4.1 事件分类学的**单一来源**（S2-T6）。

在 S2 之前，仿真只会发三种根事件（``user.activity_change`` / ``user.command`` /
``environment.state_refresh``）。它们能跑，但表达不了"发生了什么"——"用户活动变了"
既可能是下班到家，也可能是半夜去洗手间，agent 只能从房间 id 反推语义。§4.1 给出的
十四个富根事件把语义搬回事件名上，场景 YAML 从此可以直接声明"用户到家了"。

三条边界：

1. **单一来源**：``backend/scenarios/spec.py`` 的 ``ROOT_EVENT_TYPES`` 直接 re-export
   本模块的常量（同一个 frozenset 对象，不是内容相同的第二份）。两份分类学 = 两套真相，
   场景校验放行的事件类型和运行期识别的事件类型迟早会分叉。
2. **本模块零依赖**：只放字符串常量与纯函数，不 import 任何 engine/scenarios 模块，
   因此 agent 层、场景层、执行层都能安全引用它。
3. **兼容期不砍旧名**：§4.1 明确"current event names may remain during migration"，
   因此旧生产者仍在使用的三个根事件继续属于运行期分类学。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "USER_ARRIVES_HOME",
    "USER_LEAVES_HOME",
    "USER_ENTERS_ROOM",
    "USER_EXITS_ROOM",
    "USER_STARTS_ACTIVITY",
    "USER_ENDS_ACTIVITY",
    "ENVIRONMENT_WEATHER_CHANGE",
    "ENVIRONMENT_TEMPERATURE_THRESHOLD",
    "ENVIRONMENT_LIGHT_LEVEL_THRESHOLD",
    "SECURITY_PRESENCE_DETECTED",
    "SECURITY_DOOR_OPENED",
    "SAFETY_SMOKE_DETECTED",
    "DEVICE_OFFLINE",
    "DEVICE_RECOVERED",
    "USER_ACTIVITY_CHANGE",
    "USER_COMMAND",
    "ENVIRONMENT_STATE_REFRESH",
    "TIMER_TICK_EVENT_TYPE",
    "ENGINE_ERROR_EVENT_TYPE",
    "ROOT_EVENT_TYPES",
    "COMPAT_ROOT_EVENT_TYPES",
    "ALLOWED_TIMELINE_EVENT_TYPES",
    "ALL_ROOT_EVENT_TYPES",
    "USER_MOVEMENT_EVENT_TYPES",
    "ENVIRONMENT_ROOT_EVENT_TYPES",
    "DEVICE_AVAILABILITY_EVENT_TYPES",
    "is_root_event",
    "starts_agent_episode",
    "OUTSIDE_ROOM_ID",
]


# —— §4.1 十四个富根事件 ——
USER_ARRIVES_HOME = "user.arrives_home"
USER_LEAVES_HOME = "user.leaves_home"
USER_ENTERS_ROOM = "user.enters_room"
USER_EXITS_ROOM = "user.exits_room"
USER_STARTS_ACTIVITY = "user.starts_activity"
USER_ENDS_ACTIVITY = "user.ends_activity"
ENVIRONMENT_WEATHER_CHANGE = "environment.weather_change"
ENVIRONMENT_TEMPERATURE_THRESHOLD = "environment.temperature_threshold"
ENVIRONMENT_LIGHT_LEVEL_THRESHOLD = "environment.light_level_threshold"
SECURITY_PRESENCE_DETECTED = "security.presence_detected"
SECURITY_DOOR_OPENED = "security.door_opened"
SAFETY_SMOKE_DETECTED = "safety.smoke_detected"
DEVICE_OFFLINE = "device.offline"
DEVICE_RECOVERED = "device.recovered"

# —— §4.1 "current compatibility namespace"（迁移期保留，新场景不应再用）——
USER_ACTIVITY_CHANGE = "user.activity_change"
USER_COMMAND = "user.command"
ENVIRONMENT_STATE_REFRESH = "environment.state_refresh"

# —— 非根事件里被跨模块引用的两个系统事件 ——
TIMER_TICK_EVENT_TYPE = "system.timer_tick"
# 引擎主循环因未捕获异常而停摆（critic 修正③"假活"的可观测化落点）。
# 刻意不叫 system.error：它专指"仿真主循环已经不再推进"，前端要能与普通错误区分。
ENGINE_ERROR_EVENT_TYPE = "system.engine_error"

# 用户"不在任何房间"的语义房间 id。世界模型里没有这个房间，因此它只出现在事件 data 里
# （与 backend/scenarios/apply.py::OUTSIDE_LOCATION_ALIASES 同义，那边落到 location=None）。
OUTSIDE_ROOM_ID = "outside"


ROOT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        USER_ARRIVES_HOME,
        USER_LEAVES_HOME,
        USER_ENTERS_ROOM,
        USER_EXITS_ROOM,
        USER_STARTS_ACTIVITY,
        USER_ENDS_ACTIVITY,
        ENVIRONMENT_WEATHER_CHANGE,
        ENVIRONMENT_TEMPERATURE_THRESHOLD,
        ENVIRONMENT_LIGHT_LEVEL_THRESHOLD,
        SECURITY_PRESENCE_DETECTED,
        SECURITY_DOOR_OPENED,
        SAFETY_SMOKE_DETECTED,
        DEVICE_OFFLINE,
        DEVICE_RECOVERED,
    }
)

COMPAT_ROOT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        USER_ACTIVITY_CHANGE,
        USER_COMMAND,
        ENVIRONMENT_STATE_REFRESH,
    }
)

ALL_ROOT_EVENT_TYPES: frozenset[str] = ROOT_EVENT_TYPES | COMPAT_ROOT_EVENT_TYPES
# 场景 timeline 允许声明的事件类型（spec.py re-export 同一个对象）。
ALLOWED_TIMELINE_EVENT_TYPES: frozenset[str] = ALL_ROOT_EVENT_TYPES

# 会改变 ``user.location`` / ``user.activity`` 的富用户事件。SimulationEngine 用同一个
# 处理器把它们写回世界（data 键与 user.activity_change 同名：user_id/from_room/to_room/activity）。
USER_MOVEMENT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        USER_ARRIVES_HOME,
        USER_LEAVES_HOME,
        USER_ENTERS_ROOM,
        USER_EXITS_ROOM,
        USER_STARTS_ACTIVITY,
        USER_ENDS_ACTIVITY,
    }
)

ENVIRONMENT_ROOT_EVENT_TYPES: frozenset[str] = frozenset(
    {
        ENVIRONMENT_WEATHER_CHANGE,
        ENVIRONMENT_TEMPERATURE_THRESHOLD,
        ENVIRONMENT_LIGHT_LEVEL_THRESHOLD,
        ENVIRONMENT_STATE_REFRESH,
    }
)

DEVICE_AVAILABILITY_EVENT_TYPES: frozenset[str] = frozenset({DEVICE_OFFLINE, DEVICE_RECOVERED})


def is_root_event(event_type: str) -> bool:
    """是否是一条会开启新因果链的根事件（§4.4）。"""

    return event_type in ALL_ROOT_EVENT_TYPES


def starts_agent_episode(event_type: str, data: Mapping[str, Any] | None = None) -> bool:
    """Return whether an event opens an agent episode.

    This semantic trigger is intentionally independent of ``causal_parent``:
    compatibility ``environment.state_refresh`` events are children of timer
    ticks, yet significant ones still open real runtime episodes.
    """

    payload = data or {}
    if event_type in ENVIRONMENT_ROOT_EVENT_TYPES:
        reasons = payload.get("significant_change_reasons")
        return isinstance(reasons, list) and bool(reasons)
    if event_type == USER_COMMAND:
        if payload.get("message_type") == "CMD_DEVICE_CONTROL":
            return False
        if payload.get("device_id") and (payload.get("capability") or payload.get("property")):
            return False
    return event_type in ALL_ROOT_EVENT_TYPES
