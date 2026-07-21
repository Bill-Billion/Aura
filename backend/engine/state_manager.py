from __future__ import annotations

import copy
import re

from pydantic import BaseModel, Field
from typing import Any, Sequence

from backend.execution.capability_matrix import get_capability_spec
from backend.execution.validation import device_is_online
from backend.engine.state import WorldState, DeviceState, DeviceStateValues, Location3D


# §2.2 第6条白名单：只读能力只能由仿真引擎/传感器事件源写入，agent/UI/场景命令一律不可。
# 口径先按 caused_by 字符串约定（environment.py / user_behavior.py 目前部分原地写），
# S2 把仿真写入也事件化之后再收紧成事件源判定。
SIMULATION_WRITE_SOURCES: frozenset[str] = frozenset(
    {"environment_sim", "user_behavior_sim", "simulator_timer", "system"}
)

# §2.2 不变式违规的结构化事件类型。命令路径（executor）与仿真写入路径（SimulationEngine）
# 共用同一类型，靠 data.attributed_to / data.pre_existing 区分归因——定义放在状态层，
# 避免 engine 反向依赖 execution 包。
INVARIANT_VIOLATION_EVENT_TYPE = "system.invariant_violation"


class InvariantViolation(RuntimeError):
    """§2.2 状态不变式被违反。

    抛出方只描述违反了哪条不变式；转成结构化失败事件（``system.invariant_violation``）
    与命令 failed 是 executor 的职责——spec §2.2 明确要求「违反不变式必须产生结构化失败
    事件，静默纠正必须留痕」。
    """

    def __init__(
        self, invariant: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.invariant = invariant
        self.message = message
        self.details = dict(details or {})


class DeltaChange(BaseModel):
    """Records a single state mutation."""

    path: str
    old_value: Any
    new_value: Any
    caused_by: str
    reason: str = ""
    # §2.2 第7条：所有 mutation 必须可归因到一条 SimEvent。命令路径由 executor 写入
    # action.device_control 的 event_id；尚未事件化的仿真写入暂留 None。
    caused_by_event_id: str | None = None


def verify_world_invariants(world: WorldState) -> None:
    """§2.2 世界级不变式后置校验；命中第一条违反即抛 :class:`InvariantViolation`。

    只覆盖可从 WorldState 判定的四条：用户单一位置、room.persons↔user.location 一致、
    occupancy 可由 persons 推导、设备属唯一（且已注册的）房间。run_id 相关两条由 S2 的
    run 模型接管。开销 O(rooms + persons + devices)：命令路径的 apply 前后各一次，仿真
    写入路径（SimulationEngine tick 收尾 / 用户活动 / 环境刷新）经
    :func:`find_world_invariant_violation` 各一次。
    """

    room_of_person: dict[str, str] = {}
    for room in world.rooms.values():
        if room.occupancy != bool(room.persons):
            raise InvariantViolation(
                "occupancy_derivable_from_persons",
                f"房间 {room.id} 的 occupancy={room.occupancy} 无法由 persons={room.persons} 推导",
                {"room_id": room.id, "occupancy": room.occupancy, "persons": list(room.persons)},
            )
        for person in room.persons:
            previous = room_of_person.get(person)
            if previous is not None:
                raise InvariantViolation(
                    "user_single_location",
                    f"用户 {person} 同时出现在房间 {previous} 与 {room.id}",
                    {"user_id": person, "rooms": [previous, room.id]},
                )
            room_of_person[person] = room.id

            user = world.users.get(person)
            located_in = user.location.room if user is not None and user.location else None
            if located_in != room.id:
                raise InvariantViolation(
                    "person_location_matches_room",
                    f"房间 {room.id} 的 persons 含 {person}，但其 location={located_in}",
                    {"user_id": person, "room_id": room.id, "user_location": located_in},
                )

    # 未注册任何房间的世界（最小夹具 / 尚未装载场景）不判设备归属，避免误报。
    if not world.rooms:
        return
    for device in world.devices.values():
        if device.location.room not in world.rooms:
            raise InvariantViolation(
                "device_single_room",
                f"设备 {device.id} 所属房间 {device.location.room!r} 未在世界中注册",
                {"device_id": device.id, "room_id": device.location.room},
            )


def find_world_invariant_violation(world: WorldState) -> InvariantViolation | None:
    """不抛出版的不变式探测：返回首条违规，世界一致时返回 None。

    仿真写入路径要的是「检测 + 归因」而不是「打断」——仿真产出的是 ground truth，回滚它
    会撕裂物理演化，所以那里拿到违规对象后只上报；命令路径继续用
    :func:`verify_world_invariants` 的抛出语义（回滚 + failed）。

    另一处用途是归因：命令 apply 之前先探一次，apply 之后若仍违规且 apply 前就已违规，
    说明责任不在这条命令（审计 finding 3：绝不让无辜命令替仿真背锅）。
    """

    try:
        verify_world_invariants(world)
    except InvariantViolation as violation:
        return violation
    return None


class StateManager:
    """Owns the canonical WorldState and exposes mutation + query helpers."""

    _PATH_TOKEN_RE = re.compile(r"([^\.\[\]]+)|\[([^\]]+)\]")

    def __init__(self, world: WorldState | None = None):
        self.world = world if world is not None else WorldState()

    # ------------------------------------------------------------------
    # Invariants
    # ------------------------------------------------------------------

    def check_command_invariants(
        self, device: DeviceState, capability: str, caused_by: str
    ) -> None:
        """§2.2 命令级不变式：apply 落地前的最后一道状态层守卫。

        与 §3.3 六级校验的重叠是有意的——校验守命令入口，这里守状态层：任何未来绕过
        入口的调用方（仿真器、脚本、迁移期旧代码）也不可能把世界改坏。

        - 只读能力只允许 :data:`SIMULATION_WRITE_SOURCES` 写入（§2.2 第6条）；
        - online=false 的设备不接受可写命令（§2.2 第5条）。

        未在能力矩阵登记的 capability 不在此判定（属 §3.3 第3级 capability_not_supported）。
        """

        spec = get_capability_spec(device.type, capability)
        if spec is None:
            return

        if not spec.writable:
            if caused_by in SIMULATION_WRITE_SOURCES:
                return
            raise InvariantViolation(
                "read_only_capability_write",
                f"只读能力 {capability} 不接受来自 {caused_by} 的写入",
                {"device_id": device.id, "capability": capability, "caused_by": caused_by},
            )

        if not device_is_online(device):
            raise InvariantViolation(
                "offline_device_write",
                f"设备 {device.id} 离线，不能执行可写命令 {capability}",
                {"device_id": device.id, "capability": capability, "caused_by": caused_by},
            )

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def apply_action(
        self,
        agent_id: str,
        device_id: str,
        property_path: str,
        new_value: Any,
        reason: str = "",
        *,
        caused_by_event_id: str | None = None,
    ) -> list[DeltaChange]:
        """Apply a change to a device's state via a dot-notation path.

        *property_path* examples: ``"power"``, ``"extra.brightness"``,
        ``"last_changed_by"``.

        Returns a list of :class:`DeltaChange` objects (one per changed leaf).
        """
        device = self.world.devices.get(device_id)
        if device is None:
            raise KeyError(f"Device '{device_id}' not found in world state")

        deltas = self.apply_path_update(
            caused_by=agent_id,
            path=f"devices[{device_id}].state.{property_path}",
            new_value=new_value,
            reason=reason,
            caused_by_event_id=caused_by_event_id,
        )
        if not deltas:
            return []

        # Always record who last changed the device
        if property_path != "last_changed_by":
            prev_agent = device.state.last_changed_by
            if prev_agent != agent_id:
                deltas.extend(
                    self.apply_path_update(
                        caused_by=agent_id,
                        path=f"devices[{device_id}].state.last_changed_by",
                        new_value=agent_id,
                        reason="auto-update on action",
                        caused_by_event_id=caused_by_event_id,
                    )
                )

        return deltas

    def apply_path_update(
        self,
        caused_by: str,
        path: str,
        new_value: Any,
        reason: str = "",
        *,
        caused_by_event_id: str | None = None,
    ) -> list[DeltaChange]:
        old_value = self._get_nested(self.world, path)
        if old_value == new_value:
            return []

        self._set_nested(self.world, path, new_value)
        return [
            DeltaChange(
                path=path,
                old_value=copy.deepcopy(old_value),
                new_value=copy.deepcopy(new_value),
                caused_by=caused_by,
                reason=reason,
                caused_by_event_id=caused_by_event_id,
            )
        ]

    def apply_updates(
        self,
        caused_by: str,
        updates: list[tuple[str, Any]],
        reason: str = "",
        *,
        caused_by_event_id: str | None = None,
    ) -> list[DeltaChange]:
        deltas: list[DeltaChange] = []
        for path, value in updates:
            deltas.extend(
                self.apply_path_update(
                    caused_by=caused_by,
                    path=path,
                    new_value=value,
                    reason=reason,
                    caused_by_event_id=caused_by_event_id,
                )
            )
        return deltas

    def revert(self, deltas: Sequence[DeltaChange]) -> list[str]:
        """按逆序把一批 delta 写回旧值，返回被回滚的 path 列表（不产生新 delta）。

        供不变式违规时撤销半成品变更——§2.2 允许纠正的前提是纠正留痕，痕迹由 executor
        的 ``system.invariant_violation`` 事件（含 reverted_paths）承载。
        """

        for delta in reversed(list(deltas)):
            self._set_nested(self.world, delta.path, copy.deepcopy(delta.old_value))
        return [delta.path for delta in deltas]

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_full_snapshot(self) -> dict:
        """Return a plain-dict deep copy of the entire world state."""
        return self.world.snapshot().model_dump()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tokenize_path(path: str) -> list[str]:
        return [match.group(1) or match.group(2) for match in StateManager._PATH_TOKEN_RE.finditer(path)]

    @classmethod
    def _get_nested(cls, obj: BaseModel | dict, path: str) -> Any:
        """Resolve a dot-notation *path* against a Pydantic model or dict."""
        parts = cls._tokenize_path(path)
        current: Any = obj
        for part in parts:
            if isinstance(current, dict):
                current = current[part]
            else:
                current = getattr(current, part)
        return current

    @classmethod
    def _set_nested(cls, obj: BaseModel | dict, path: str, value: Any) -> None:
        """Set a value at *path* (dot-notation) on a Pydantic model or dict."""
        parts = cls._tokenize_path(path)
        current: Any = obj
        for part in parts[:-1]:
            if isinstance(current, dict):
                current = current[part]
            else:
                current = getattr(current, part)
        last = parts[-1]
        if isinstance(current, dict):
            current[last] = value
        else:
            setattr(current, last, value)
