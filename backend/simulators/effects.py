"""最小设备效果模型：fan 体感 / camera 安防覆盖 / sensor 读数（spec §3.4）。

§3.4 的底线是「命令成功不等于家真的响应了」——只有效果模型能把「字段变了」变成
「世界变了」。本模块只覆盖 fan/camera/sensor 三类新设备的最小效果，light/hvac/curtain
的物理效果仍归 :mod:`backend.simulators.environment`：

- fan（perceived_comfort）：按档降**体感**温度，shake 放大房间级覆盖系数。
  绝不写 ``room.temperature``——那是物理 ground truth，§3.4 明确要求风扇不直接改它。
- camera（security_coverage）：房间内 online 摄像头占比归一为 coverage∈[0,1]。
- sensor（ui_observability）：读数由 ground truth 派生而非手写，落实 §2.3 的
  ground truth / observable 分离；``rng``/``delay``/``noise`` 是 S2 seed 化与
  §13 故障注入的接缝，本阶段默认确定性直传。

§3.4 末段强制每条效果声明作用域，故所有效果函数经 :func:`effect_model` 登记到
:data:`EFFECT_MODELS`，作用域取值复用能力矩阵的 ``EffectClass``（单一来源）。

体感系数无权威出处，标注为可调常量并在测试里锁定当前值，防 S4 评估期数值漂移无察觉。
"""

from __future__ import annotations

import random
from typing import Any, Callable, Mapping, TypeVar

from backend.config.capability_matrix import EffectClass
from backend.engine.state import DeviceState, RoomState, WorldState


# 风扇档位 → 体感温度偏移（°C，恒为负）。可调常量，测试锁定。
FAN_SPEED_PERCEIVED_OFFSET: dict[str, float] = {
    "low": -0.8,
    "medium": -1.5,
    "high": -2.2,
}
# 未知档位按最低档兜底（校验层已挡住非法枚举，这里只防未来矩阵扩档）。
FAN_DEFAULT_SPEED = "low"
# shake=true 提高房间级覆盖系数：同档位吹到的范围更大，体感降幅相应放大。
FAN_SHAKE_COVERAGE_FACTOR = 1.25

# sensor_type → 房间级 ground truth 字段。未登记的类型（如 air_quality）当前世界模型里
# 没有对应 ground truth，宁可不写也不编造读数。
SENSOR_GROUND_TRUTH_FIELDS: dict[str, str] = {
    "temperature": "temperature",
    "humidity": "humidity",
    "illuminance": "light_level",
    "light": "light_level",
}

# §3.4 强制声明：效果名 → 效果函数（函数上挂 ``effect_class`` 属性）。
EFFECT_MODELS: dict[str, Callable[..., Any]] = {}

F = TypeVar("F", bound=Callable[..., Any])


def effect_model(effect_class: EffectClass) -> Callable[[F], F]:
    """登记一条效果模型并声明其作用域（§3.4 末段的强制声明落点）。"""

    def decorate(fn: F) -> F:
        fn.effect_class = effect_class  # type: ignore[attr-defined]
        EFFECT_MODELS[fn.__name__] = fn
        return fn

    return decorate


@effect_model("perceived_comfort")
def calculate_perceived_temperature(
    room: RoomState, world: WorldState, *, temperature: float | None = None
) -> float:
    """房间体感温度 = 物理温度 + 房内运行风扇的偏移叠加（只读，不写世界）。

    ``temperature`` 允许传入本 tick 刚算出、尚未落地的物理温度，避免体感永远慢一拍。
    """

    base = room.temperature if temperature is None else float(temperature)
    offset = 0.0
    for device in world.devices.values():
        if device.type != "fan" or device.location.room != room.id:
            continue
        if not device.state.power:
            continue
        speed = str(device.state.extra.get("speed", FAN_DEFAULT_SPEED))
        tier = FAN_SPEED_PERCEIVED_OFFSET.get(
            speed, FAN_SPEED_PERCEIVED_OFFSET[FAN_DEFAULT_SPEED]
        )
        if bool(device.state.extra.get("shake", False)):
            tier *= FAN_SHAKE_COVERAGE_FACTOR
        offset += tier
    return round(base + offset, 2)


@effect_model("security_coverage")
def calculate_security_coverage(world: WorldState) -> dict[str, float]:
    """逐房间安防覆盖系数 ∈[0,1] = 在线摄像头 / 该房间摄像头总数。

    §3.4 只把 ``online=false`` 定义为覆盖下降来源，因此这里不掺 power 等其他条件；
    无摄像头的房间覆盖为 0（无覆盖，而非「未知」）。
    """

    total: dict[str, int] = {room_id: 0 for room_id in world.rooms}
    online: dict[str, int] = {room_id: 0 for room_id in world.rooms}
    for device in world.devices.values():
        if device.type != "camera":
            continue
        room_id = device.location.room
        if room_id not in total:
            continue
        total[room_id] += 1
        if bool(device.state.extra.get("online", True)):
            online[room_id] += 1

    return {
        room_id: round(online[room_id] / total[room_id], 4) if total[room_id] else 0.0
        for room_id in total
    }


@effect_model("ui_observability")
def generate_sensor_reading(
    ground_truth: float,
    rng: random.Random | None = None,
    delay: int = 0,
    noise: float = 0.0,
    previous_value: float | None = None,
) -> float:
    """由 ground truth 生成可观测读数（本阶段默认确定性直传）。

    - ``delay>0`` 且有上一次读数 → 保持旧值（读数滞后一拍）；
    - ``noise>0`` 且注入了 ``rng`` → 叠加高斯噪声。未注入 rng 时**不**退化到全局随机源：
      S2 之前不引入任何不可复现的随机性（§11 run 可复现）。
    """

    if delay > 0 and previous_value is not None:
        return previous_value
    if noise > 0.0 and rng is not None:
        return round(ground_truth + rng.gauss(0.0, noise), 4)
    return ground_truth


def sensor_ground_truth(
    world: WorldState,
    device: DeviceState,
    room_overrides: Mapping[str, Mapping[str, float]] | None = None,
) -> float | None:
    """取该 sensor 对应的房间级 ground truth；无对应量则 None（不编造读数）。"""

    field = SENSOR_GROUND_TRUTH_FIELDS.get(str(device.state.extra.get("sensor_type", "")))
    if field is None:
        return None
    room = world.rooms.get(device.location.room)
    if room is None:
        return None
    override = (room_overrides or {}).get(device.location.room, {})
    if field in override:
        return float(override[field])
    return float(getattr(room, field))


def derive_observable_updates(
    world: WorldState,
    room_overrides: Mapping[str, Mapping[str, float]] | None = None,
    *,
    rng: random.Random | None = None,
) -> dict[str, float]:
    """按 ground truth 重算三类派生量，返回 StateManager 路径→新值。

    只产出派生量（体感温度 / 安防覆盖 / 传感器读数），绝不回写物理量——调用方
    （EnvironmentSimulator.step）在同一批 updates 里已经给出物理量。
    """

    coverage = calculate_security_coverage(world)
    updates: dict[str, float] = {}
    for room_id, room in world.rooms.items():
        override = (room_overrides or {}).get(room_id, {})
        updates[f"rooms[{room_id}].perceived_temperature"] = calculate_perceived_temperature(
            room, world, temperature=override.get("temperature")
        )
        updates[f"rooms[{room_id}].security_coverage"] = coverage[room_id]

    for device in world.devices.values():
        if device.type != "sensor":
            continue
        ground_truth = sensor_ground_truth(world, device, room_overrides)
        if ground_truth is None:
            continue
        updates[f"devices[{device.id}].state.extra.value"] = generate_sensor_reading(
            ground_truth,
            rng=rng,
            previous_value=device.state.extra.get("value"),
        )
    return updates
