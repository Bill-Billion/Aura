"""§5.2 ``initial_state`` → 世界的确定性覆盖。

一个 run 的可复现性从"起始世界是怎么来的"开始。直接 ``setattr`` 一遍世界能跑，但它
制造的是一个**没有出处**的起始状态：§11 的 ``initial_state_hash`` 只能证明两个 run 从
同一个世界出发，证明不了那个世界是被哪一条场景声明改成这样的。因此本模块的全部写入
都走 :meth:`StateManager.apply_updates`，归因到 ``caused_by="scenario_loader"``
（§2.2 第7条"所有变更可归因"）。

三条刻意的纪律：

1. **确定性**：所有映射按 key 升序遍历，同内容不同 YAML 键序产生逐条相同的 delta 序列。
   否则作者调换两行 YAML 就会让"同场景同 seed"的工件对不上。
2. **不静默**：未知房间/设备/位置一律抛 :class:`InitialStateApplyError`，不跳过；
   ``extra`` 里注册表默认没有的键（如给一台灯写 ``online``）显式创建并留 delta
   （``old_value=None`` 表示"此前没有这个读数"），不 KeyError 也不静默吞。
3. **不越过不变式**：§2.2 要求 ``room.occupancy == bool(room.persons)``、用户单一位置。
   本模块从用户位置**推导** occupancy，场景里显式写的 ``occupancy`` 只当作断言：
   与推导结果不一致就报错，绝不"帮作者改成一致"——那是在起始状态里就开始撒谎。

应用顺序（有意固定）：环境 → 房间标量 → 用户位置/活动（顺带推导 persons/occupancy）
→ 房间 occupancy 断言 → 设备 → 全局不变式复查。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.engine.state import Location3D, UserState
from backend.engine.state_manager import (
    DeltaChange,
    InvariantViolation,
    StateManager,
    verify_world_invariants,
)
from backend.scenarios.spec import InitialState, InitialUserState, ScenarioSpec

__all__ = [
    "SCENARIO_INITIAL_STATE_CAUSED_BY",
    "SCENARIO_INITIAL_STATE_REASON",
    "OUTSIDE_LOCATION_ALIASES",
    "STATIC_ROOM_FIELDS",
    "InitialStateApplication",
    "InitialStateApplyError",
    "InitialStateApplyErrorCode",
    "apply_initial_state",
    "apply_scenario_initial_state",
]


# 归因标签。与 backend/engine/state_manager.py::SIMULATION_WRITE_SOURCES 刻意**不重合**：
# 场景装载不是仿真写入，它发生在 run 开始之前；只读能力的白名单不该顺手把它放进去。
# （initial_state 走 apply_path_update，本就不过 check_command_invariants 那道命令级闸。）
SCENARIO_INITIAL_STATE_CAUSED_BY = "scenario_loader"
SCENARIO_INITIAL_STATE_REASON = "scenario initial_state"

# §5.2 里"人不在家"的语义位置写法。它们都落到 UserState.location = None。
OUTSIDE_LOCATION_ALIASES = frozenset({"outside", "away", "out", "none", "off_site"})

# 场景写得进、传感器读得到、但**没有任何仿真器演化**的房间字段。
# 【S2-T6 更新】``humidity`` 已从本元组摘除：EnvironmentSimulator 现在按
# ``HUMIDITY_OUTDOOR_DIFFUSION_RATE`` 让房间湿度向室外湿度扩散（§6.7 雨天场景依赖它）。
# 元组现在为空，但**保留**这个契约位：下一个"写得进却不演化"的字段必须登记在这里，
# 由 runner 写进 run 工件——研究者看曲线时才知道哪条线是"平的因为没人动它"。
STATIC_ROOM_FIELDS: tuple[str, ...] = ()


class InitialStateApplyErrorCode(str, Enum):
    """应用期失败词表（面向场景作者：错在哪一条声明）。"""

    UNKNOWN_ROOM_ID = "unknown_room_id"
    UNKNOWN_DEVICE_ID = "unknown_device_id"
    UNKNOWN_USER_LOCATION = "unknown_user_location"
    PRESENCE_LOCATION_CONFLICT = "presence_location_conflict"
    OCCUPANCY_CONFLICT = "occupancy_conflict"
    INVARIANT_VIOLATION = "invariant_violation"


class InitialStateApplyError(Exception):
    """initial_state 无法被一致地应用到世界上。

    抛出而不是"尽力而为"：起始状态错了，这个 run 的每一条结论都建立在错误前提上；
    半应用的世界比明确失败危险得多。
    """

    def __init__(
        self,
        code: InitialStateApplyErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": dict(self.details)}


@dataclass(frozen=True)
class InitialStateApplication:
    """一次 initial_state 应用的结果（S2-T6 runner 与 S2-T9 工件都消费它）。"""

    deltas: tuple[DeltaChange, ...]
    created_users: tuple[str, ...] = ()
    # 本次声明触及的、世界里存在但无仿真写入方的字段（见 STATIC_ROOM_FIELDS）。
    # runner 应当把它写进 run 工件：研究者看曲线时才知道哪条线是"平的因为没人动它"。
    static_fields: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.deltas)


# --------------------------------------------------------------------- helpers


def _resolve_user_room(
    user_id: str,
    declared: InitialUserState,
    known_rooms: set[str],
) -> str | None:
    """把 §5.2 的语义位置字符串 + presence_state 解析成房间 id（None = 不在任何房间）。

    ``presence_state`` 在世界模型里**没有存储位**（``UserState`` 只有 location/activity/
    comfort_score）。与其静默丢掉，不如把它能表达的那部分落到 location 上：
    ``away`` ⇒ 不在任何房间。其余取值（``home`` / ``sleeping`` / ``unknown``）不携带
    房间信息，只能由 ``location`` 决定；``away`` 与一个房间 location 同时出现是自相矛盾
    的声明，直接报错而不是挑一个赢家。
    """

    location = declared.location.strip() if declared.location else None
    presence = declared.presence_state

    room: str | None = None
    if location:
        if location.lower() in OUTSIDE_LOCATION_ALIASES:
            room = None
        elif location in known_rooms:
            room = location
        else:
            raise InitialStateApplyError(
                InitialStateApplyErrorCode.UNKNOWN_USER_LOCATION,
                f"用户 {user_id} 的 location {location!r} 既不是已注册房间，"
                f"也不是 {sorted(OUTSIDE_LOCATION_ALIASES)} 中的"
                "户外别名",
                details={"user_id": user_id, "location": location},
            )

    if presence == "away":
        if location and room is not None:
            raise InitialStateApplyError(
                InitialStateApplyErrorCode.PRESENCE_LOCATION_CONFLICT,
                f"用户 {user_id} 同时声明了 presence_state='away' 与房间 location={location!r}",
                details={"user_id": user_id, "location": location, "presence_state": presence},
            )
        return None

    return room


def _device_state_updates(
    device_id: str,
    declared_state: Any,
    world_devices: dict[str, Any],
) -> list[tuple[str, Any]]:
    """一台设备的 (path, value) 覆盖列表；顺带为 extra 中缺失的键补位。"""

    device = world_devices[device_id]
    updates: list[tuple[str, Any]] = []
    if declared_state.power is not None:
        updates.append((f"devices[{device_id}].state.power", declared_state.power))
    for key in sorted(declared_state.extra):
        if key not in device.state.extra:
            # apply_path_update 先读旧值，extra 里没有这个键会 KeyError。补一个 None
            # 占位，delta 的 old_value=None 恰好如实表达"此前没有这个读数"。
            device.state.extra[key] = None
        updates.append((f"devices[{device_id}].state.extra.{key}", declared_state.extra[key]))
    return updates


# ----------------------------------------------------------------------- 主入口


def apply_initial_state(
    state_manager: StateManager,
    initial_state: InitialState,
    *,
    caused_by: str = SCENARIO_INITIAL_STATE_CAUSED_BY,
    reason: str = SCENARIO_INITIAL_STATE_REASON,
    caused_by_event_id: str | None = None,
) -> InitialStateApplication:
    """把 ``initial_state`` 覆盖到 ``state_manager`` 的世界上。

    返回全部 :class:`DeltaChange`（未声明或与现值相同的字段不产生 delta，因此重复应用
    是幂等的）。任何一致性问题抛 :class:`InitialStateApplyError`。
    """

    world = state_manager.world
    known_rooms = set(world.rooms)
    known_devices = set(world.devices)

    # —— 0. 引用完整性先于任何写入：宁可一条都不写，也不要留一个半成品世界 ——
    unknown_rooms = sorted(set(initial_state.rooms) - known_rooms)
    if unknown_rooms:
        raise InitialStateApplyError(
            InitialStateApplyErrorCode.UNKNOWN_ROOM_ID,
            f"initial_state 引用了未注册的房间：{unknown_rooms}",
            details={"room_ids": unknown_rooms, "known_room_ids": sorted(known_rooms)},
        )
    unknown_devices = sorted(set(initial_state.devices) - known_devices)
    if unknown_devices:
        raise InitialStateApplyError(
            InitialStateApplyErrorCode.UNKNOWN_DEVICE_ID,
            f"initial_state 引用了未注册的设备：{unknown_devices}",
            details={"device_ids": unknown_devices},
        )

    # 用户位置先解析一遍（可能抛错），再开始写世界。
    resolved_user_rooms: dict[str, str | None] = {}
    for user_id in sorted(initial_state.users):
        resolved_user_rooms[user_id] = _resolve_user_room(
            user_id, initial_state.users[user_id], known_rooms
        )

    updates: list[tuple[str, Any]] = []
    created_users: list[str] = []
    static_fields: list[str] = []

    # —— 1. 环境 ——
    for field in ("time_of_day", "weather", "outdoor_temp", "outdoor_humidity"):
        value = getattr(initial_state, field)
        if value is not None:
            updates.append((f"environment.{field}", value))

    # —— 2. 房间标量（occupancy 留到用户位置定下来之后再断言）——
    for room_id in sorted(initial_state.rooms):
        declared_room = initial_state.rooms[room_id]
        for field in ("temperature", "humidity", "light_level"):
            value = getattr(declared_room, field)
            if value is None:
                continue
            updates.append((f"rooms[{room_id}].{field}", value))
            if field in STATIC_ROOM_FIELDS:
                static_fields.append(f"rooms.*.{field}")

    # —— 3. 用户：位置 + 活动，并推导 persons/occupancy ——
    #   persons 的最终值必须从"所有用户的最终房间"整体推导，不能逐个用户增量改，
    #   否则一个用户搬走、另一个搬入时中间态会短暂违反不变式。
    for user_id in sorted(initial_state.users):
        if user_id not in world.users:
            # 场景可以引入默认世界里没有的住户（§6 的多人场景）。补一个最小 UserState，
            # 位置/活动随后与既有用户走同一条 delta 路径，保持可归因。
            world.users[user_id] = UserState(id=user_id, name=user_id, location=None)
            created_users.append(user_id)

        declared_user = initial_state.users[user_id]
        room = resolved_user_rooms[user_id]
        if declared_user.location is not None or declared_user.presence_state is not None:
            new_location = Location3D(room=room) if room is not None else None
            updates.append((f"users[{user_id}].location", new_location))
        if declared_user.activity is not None:
            updates.append((f"users[{user_id}].activity", declared_user.activity))

    # 声明生效后每个用户的最终房间（未声明的沿用世界现值）。
    final_room_of_user: dict[str, str | None] = {}
    for user_id, user in world.users.items():
        if user_id in resolved_user_rooms:
            declared_user = initial_state.users[user_id]
            if declared_user.location is not None or declared_user.presence_state is not None:
                final_room_of_user[user_id] = resolved_user_rooms[user_id]
                continue
        final_room_of_user[user_id] = user.location.room if user.location else None

    derived_persons: dict[str, list[str]] = {room_id: [] for room_id in world.rooms}
    for user_id in sorted(final_room_of_user):
        room = final_room_of_user[user_id]
        if room is not None:
            derived_persons[room].append(user_id)

    for room_id in sorted(derived_persons):
        persons = derived_persons[room_id]
        if list(world.rooms[room_id].persons) != persons:
            updates.append((f"rooms[{room_id}].persons", persons))
        occupancy = bool(persons)
        if world.rooms[room_id].occupancy != occupancy:
            updates.append((f"rooms[{room_id}].occupancy", occupancy))

    # —— 4. 显式 occupancy 只当断言（§2.2：occupancy 必须可由 persons 推导）——
    for room_id in sorted(initial_state.rooms):
        declared_occupancy = initial_state.rooms[room_id].occupancy
        if declared_occupancy is None:
            continue
        derived = bool(derived_persons[room_id])
        if declared_occupancy != derived:
            raise InitialStateApplyError(
                InitialStateApplyErrorCode.OCCUPANCY_CONFLICT,
                f"房间 {room_id} 声明 occupancy={declared_occupancy}，但按 initial_state.users "
                f"推导出的在场人员是 {derived_persons[room_id]}（occupancy={derived}）。"
                "§2.2 要求 occupancy 可由 persons 推导——请改为声明用户位置，"
                "而不是直接写 occupancy。",
                details={
                    "room_id": room_id,
                    "declared_occupancy": declared_occupancy,
                    "derived_persons": list(derived_persons[room_id]),
                },
            )

    # —— 5. 设备 ——
    for device_id in sorted(initial_state.devices):
        updates.extend(
            _device_state_updates(
                device_id, initial_state.devices[device_id].state, world.devices
            )
        )

    deltas = state_manager.apply_updates(
        caused_by=caused_by,
        updates=updates,
        reason=reason,
        caused_by_event_id=caused_by_event_id,
    )

    # —— 6. 全局复查：起始世界必须自洽，否则第一次 tick 就会报一条谁也解释不了的违规 ——
    try:
        verify_world_invariants(world)
    except InvariantViolation as violation:
        raise InitialStateApplyError(
            InitialStateApplyErrorCode.INVARIANT_VIOLATION,
            f"应用 initial_state 后世界违反 §2.2 不变式：{violation.message}",
            details={"invariant": violation.invariant, **violation.details},
        ) from violation

    return InitialStateApplication(
        deltas=tuple(deltas),
        created_users=tuple(created_users),
        static_fields=tuple(dict.fromkeys(static_fields)),
    )


def apply_scenario_initial_state(
    state_manager: StateManager,
    spec: ScenarioSpec,
    *,
    caused_by: str = SCENARIO_INITIAL_STATE_CAUSED_BY,
    caused_by_event_id: str | None = None,
) -> InitialStateApplication:
    """:func:`apply_initial_state` 的 ScenarioSpec 入口（S2-T6 runner 的调用点）。

    ``reason`` 带上场景 id：delta 流里一眼看得出这个起始世界是哪个场景摆出来的。
    """

    return apply_initial_state(
        state_manager,
        spec.initial_state,
        caused_by=caused_by,
        reason=f"{SCENARIO_INITIAL_STATE_REASON}:{spec.id}",
        caused_by_event_id=caused_by_event_id,
    )
