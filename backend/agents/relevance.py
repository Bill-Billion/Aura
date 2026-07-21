"""§7 Event-To-Device Mapping —— 根事件 → 默认设备相关面（数据，不是分支）。

spec §7 原文两句话定了这个模块的存在方式：

    *Every root event type must declare its default device relevance. This mapping is
    not the same as mandatory execution. It defines the search space for agents.*

    *The implementation should expose this mapping as data, not hard-coded branching
    inside individual agents.*

为什么需要它：S2-T6 把 :attr:`BaseAgent.subscribed_event_types` 从三个旧事件放宽到
§4.1 全部十七个根事件（不放宽，场景 timeline 声明的 ``user.arrives_home`` 会一路走到
runtime 却在相关性判定里被判不相关），但兜底的相关性仍是 ``return True``。两者叠加的
结果是**每个** agent 对**每一条**根事件都开一条 episode——照明 agent 会为"一台摄像头
掉线"跑一轮完整推理。S3 再挂三个 domain agent 上来，成本按 agent 数线性放大。

边界（与 backend/engine/event_types.py 相同的三条自律）：

1. **只放数据与纯函数**：本模块不 import agent / engine 运行期对象，只依赖事件分类学
   与 :data:`~backend.engine.state.DeviceType` 这两份类型级单一来源。
2. **相关面 ≠ 必须执行**：这里回答的是"这类事件值不值得让我开一轮推理"，不回答
   "该发什么命令"——后者仍由各 agent 的 ``get_allowed_command_specs`` 决定。
3. **设备可用性事件不在表里定死**：§7 给的是"affected device | alternatives"，也就是
   由事件里那台设备的类型现场决定，见 :func:`is_device_availability_event` 的调用方
   （:meth:`BaseAgent.is_relevant`）。表里仍给它们登记全集，保证第 4 条"每个根事件类型
   都声明了相关面"对十七个类型无一例外成立。
"""

from __future__ import annotations

from typing import get_args

from backend.engine.event_types import (
    DEVICE_AVAILABILITY_EVENT_TYPES,
    DEVICE_OFFLINE,
    DEVICE_RECOVERED,
    ENVIRONMENT_LIGHT_LEVEL_THRESHOLD,
    ENVIRONMENT_STATE_REFRESH,
    ENVIRONMENT_TEMPERATURE_THRESHOLD,
    ENVIRONMENT_WEATHER_CHANGE,
    SAFETY_SMOKE_DETECTED,
    SECURITY_DOOR_OPENED,
    SECURITY_PRESENCE_DETECTED,
    USER_ACTIVITY_CHANGE,
    USER_ARRIVES_HOME,
    USER_COMMAND,
    USER_ENDS_ACTIVITY,
    USER_ENTERS_ROOM,
    USER_EXITS_ROOM,
    USER_LEAVES_HOME,
    USER_STARTS_ACTIVITY,
)
from backend.engine.state import DeviceType

__all__ = [
    "ALL_DEVICE_TYPES",
    "EVENT_ACTIONABLE_DEVICE_TYPES",
    "actionable_device_types",
    "is_device_availability_event",
]


# 设备类型的单一来源就是 DeviceType 本身；在这里再抄一份字符串，迟早与世界模型分叉。
ALL_DEVICE_TYPES: frozenset[str] = frozenset(get_args(DeviceType))


# §7 表逐行落数据（Primary ∪ Secondary = 搜索空间）。表里没写的四个根事件按同族补齐，
# 补齐依据写在各行注释里——留空会让它们走"未知类型 = 不设限"的兜底，等于没收口。
EVENT_ACTIONABLE_DEVICE_TYPES: dict[str, frozenset[str]] = {
    # | user.arrives_home | lights, HVAC | curtains, cameras |
    USER_ARRIVES_HOME: frozenset({"light", "hvac", "curtain", "camera"}),
    # | user.leaves_home | lights, HVAC, fans, cameras | curtains |
    USER_LEAVES_HOME: frozenset({"light", "hvac", "fan", "camera", "curtain"}),
    # 表里没有的房间级进出：与到家/离家同属"人在哪"的舒适面，去掉安防（进出房间本身
    # 不是安防信号，§6.8 的安防信号是 security.*）。
    USER_ENTERS_ROOM: frozenset({"light", "hvac", "curtain", "fan"}),
    USER_EXITS_ROOM: frozenset({"light", "hvac", "curtain", "fan"}),
    # 活动类事件按活动种类分流（sleeping / cooking 两行），事件本身不带活动名时无法再细分，
    # 故取两行的并集 = 全集；真正的取舍留给 agent 的 allowed_command_specs。
    USER_STARTS_ACTIVITY: ALL_DEVICE_TYPES,
    USER_ENDS_ACTIVITY: ALL_DEVICE_TYPES,
    # 表里没有的天气变化：它同时改室内热负荷与自然光，故覆盖冷热 + 采光两条链。
    ENVIRONMENT_WEATHER_CHANGE: frozenset({"hvac", "fan", "curtain", "light", "sensor"}),
    # | environment.temperature_threshold | HVAC, fans | curtains, sensors |
    ENVIRONMENT_TEMPERATURE_THRESHOLD: frozenset({"hvac", "fan", "curtain", "sensor"}),
    # | environment.light_level_threshold | lights, curtains | sensors |
    ENVIRONMENT_LIGHT_LEVEL_THRESHOLD: frozenset({"light", "curtain", "sensor"}),
    # | security.presence_detected | cameras, entry lights | sensors |
    SECURITY_PRESENCE_DETECTED: frozenset({"camera", "light", "sensor"}),
    # 表里没有的开门事件：与 presence_detected 同族（§6.8 同一条场景里并列出现）。
    SECURITY_DOOR_OPENED: frozenset({"camera", "light", "sensor"}),
    # §19「prefer fail-closed behavior for safety and security events」：烟雾是唯一一类
    # "宁可多开一轮推理"的事件——空调要停止循环、灯要亮、摄像头要留存，一个都不能漏。
    SAFETY_SMOKE_DETECTED: ALL_DEVICE_TYPES,
    # | device.offline | affected device | alternatives | —— 真正的判定在 is_relevant 里
    # 现场按事件那台设备的类型做，这里登记全集只为让"每个根事件类型都有声明"成立。
    DEVICE_OFFLINE: ALL_DEVICE_TYPES,
    DEVICE_RECOVERED: ALL_DEVICE_TYPES,
    # —— §4.1 迁移期兼容命名空间：语义故意保持宽，旧前端/旧场景的行为一字不改 ——
    USER_ACTIVITY_CHANGE: ALL_DEVICE_TYPES,
    USER_COMMAND: ALL_DEVICE_TYPES,
    ENVIRONMENT_STATE_REFRESH: ALL_DEVICE_TYPES,
}


def actionable_device_types(event_type: str) -> frozenset[str]:
    """该根事件类型默认与哪些设备类型相关（§7 搜索空间）。

    未登记的类型返回全集：新事件类型上线时宁可多开一轮推理，也不要静默地让所有 agent
    对它失明——"没人响应"比"多响应一次"难查得多。
    """

    return EVENT_ACTIONABLE_DEVICE_TYPES.get(event_type, ALL_DEVICE_TYPES)


def is_device_availability_event(event_type: str) -> bool:
    """是否是 §7「affected device | alternatives」那一行（device.offline/recovered）。"""

    return event_type in DEVICE_AVAILABILITY_EVENT_TYPES
