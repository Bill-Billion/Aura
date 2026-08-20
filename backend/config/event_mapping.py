"""§7 事件→设备映射的加载器：把那张表当**数据**读，而不是当分支写。

审计（2026-07-16 codex 复核 §六）确认 §7 的映射当前散在 agent 代码里：``lighting.py`` 与
``hvac.py`` 各抄了一份几乎相同的 ``_focus_rooms`` / ``get_relevant_devices`` 分支。后果是
研究者想改"到家该不该动窗帘"必须改 Python，而两份分支迟早分叉。spec §7 原文要求的是
另一种形态：

    *The implementation should expose this mapping as data, not hard-coded branching
    inside individual agents.*

本模块只做三件事：读 ``event_device_mapping.yaml``、**在加载期**把它和设备注册表对齐、
给 agent 一个按事件查搜索空间的纯函数。

三条边界：

1. **加载期失败优于运行期静默漏配**。表里写了注册表不认识的设备类型 / 能力 / 房间，或者
   漏掉了 §4.1 十七个根事件里的任何一个，都是 :class:`EventMappingError`。少一行的代价是
   "那类事件对所有 agent 静默失明"——比"多开一轮推理"难查得多。
2. **搜索面 ≠ 必须执行**。这里回答"这类事件默认与哪些设备相关"，不回答"该发什么命令"
   （后者仍归各 agent 的 ``get_allowed_command_specs`` 与 §9 仲裁）。
3. **不碰世界状态**。解析 ``device.offline`` 那种"affected device"行时，设备类型由调用方
   传入或由事件 ``data.device_type`` 提供；本模块不查世界、不改世界。

与 :mod:`backend.agents.relevance` 的关系：那个模块是 S2 时期把 §7 收口进 ``is_relevant``
的最小实现（只有"事件→设备类型集合"一列，且写死在 Python 里）。本模块是它的数据化超集，
多出 primary/secondary 之分、default_policy、房间限定与活动限定键。S3-T3/T4 改写 agent 时
由那边切到这边（本任务按 FILE SCOPE 不动 agent）。
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.config.device_registry import build_default_rooms
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES
from backend.execution.capability_matrix import CAPABILITY_MATRIX, all_device_types

if TYPE_CHECKING:  # 只为类型标注；运行期按鸭子类型取 event_type/data，避免引入运行期依赖。
    from backend.engine.event_bus import SimEvent


__all__ = [
    "DEFAULT_MAPPING_PATH",
    "SUPPORTED_SCHEMA_VERSIONS",
    "ACTIVITY_KEY_SEPARATOR",
    "EventMappingErrorCode",
    "EventMappingError",
    "EventDeviceMappingRow",
    "DeviceSearchSpace",
    "EventDeviceMapping",
    "load_event_device_mapping",
    "get_event_device_mapping",
    "get_device_search_space",
    "clear_mapping_cache",
]


DEFAULT_MAPPING_PATH: Path = Path(__file__).resolve().parent / "event_device_mapping.yaml"
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
# 活动限定键的分隔符（spec §7 的 ``user.starts_activity:sleeping`` 写法）。
ACTIVITY_KEY_SEPARATOR = ":"

# 行数据由 YAML 提供，这三个字段由表键推导——YAML 里再写一遍就有了第二份真相。
_DERIVED_ROW_FIELDS = frozenset({"event_key", "event_type", "activity"})

_ResolvedFrom = Literal["exact", "activity", "base", "dynamic", "dynamic_unresolved", "unmapped"]


class EventMappingErrorCode(str, Enum):
    """加载期失败原因（与 :class:`~backend.scenarios.loader.ScenarioLoadErrorCode` 同风格）。"""

    FILE_NOT_FOUND = "file_not_found"
    NOT_A_MAPPING = "not_a_mapping"
    INVALID_DOCUMENT = "invalid_document"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_ROW = "invalid_row"
    UNKNOWN_EVENT_TYPE = "unknown_event_type"
    MISSING_EVENT_COVERAGE = "missing_event_coverage"
    UNKNOWN_DEVICE_TYPE = "unknown_device_type"
    UNKNOWN_CAPABILITY = "unknown_capability"
    CAPABILITY_TYPE_MISMATCH = "capability_type_mismatch"
    UNKNOWN_ROOM_ID = "unknown_room_id"


class EventMappingError(Exception):
    """§7 映射表加载失败。带 code 是为了让调用方能区分"表写错了"和"文件没找到"。"""

    def __init__(
        self,
        code: EventMappingErrorCode,
        message: str,
        *,
        path: Path | None = None,
        event_key: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.event_key = event_key
        location = f" [{event_key}]" if event_key else ""
        where = f" ({path})" if path else ""
        super().__init__(f"{code.value}{location}: {message}{where}")


class EventDeviceMappingRow(BaseModel):
    """§7 表里的一行（不可变）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # —— 由表键推导 ——
    event_key: str  # 原始键，可能带活动限定，如 "user.starts_activity:sleeping"
    event_type: str  # 键的事件类型部分
    activity: str | None = None  # 键的活动限定部分（无限定为 None）

    # —— YAML 列 ——
    source: str = "derived"
    primary_device_types: tuple[str, ...] = ()
    secondary_device_types: tuple[str, ...] = ()
    room_scope: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    default_policy: str = ""
    # §7「affected device | alternatives」：搜索面由事件里那台设备现场决定。
    dynamic: Literal["affected_device"] | None = None
    alternatives: dict[str, tuple[str, ...]] = {}
    notes: str = ""

    @property
    def is_spec_row(self) -> bool:
        """是否是 §7 表里的原文行（相对于按同族补齐的 derived / 迁移期 compat 行）。"""

        return self.source.startswith("spec")

    @property
    def declared_device_types(self) -> frozenset[str]:
        return frozenset(self.primary_device_types) | frozenset(self.secondary_device_types)


class DeviceSearchSpace(BaseModel):
    """一次查表的结果：这条事件默认与哪些设备相关（§7 "search space for agents"）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str
    # 实际命中的表键；未命中任何行时等于 event_type。
    event_key: str
    activity: str | None = None
    resolved_from: _ResolvedFrom = "unmapped"
    primary: tuple[str, ...] = ()
    secondary: tuple[str, ...] = ()
    room_scope: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    default_policy: str = ""
    source: str = ""

    @classmethod
    def empty(
        cls,
        event_type: str,
        *,
        activity: str | None = None,
        resolved_from: _ResolvedFrom = "unmapped",
        default_policy: str = "",
        source: str = "",
    ) -> DeviceSearchSpace:
        return cls(
            event_type=event_type,
            event_key=event_type,
            activity=activity,
            resolved_from=resolved_from,
            default_policy=default_policy,
            source=source,
        )

    @property
    def device_types(self) -> tuple[str, ...]:
        """primary ∪ secondary，**升序**——canonical trace 不能有集合迭代序泄漏。"""

        return tuple(sorted(set(self.primary) | set(self.secondary)))

    @property
    def is_empty(self) -> bool:
        return not self.device_types

    def includes_device_type(self, device_type: str) -> bool:
        return device_type in self.primary or device_type in self.secondary

    def intersect_controlled(self, controlled_types: Any) -> tuple[str, ...]:
        """与某个 agent 控的设备类型求交（升序）。S3-T3/T4 的 ``is_relevant`` 入口。"""

        return tuple(sorted(set(self.device_types) & set(controlled_types)))


class EventDeviceMapping(BaseModel):
    """整张 §7 表（不可变）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    rows: dict[str, EventDeviceMappingRow]
    path: Path | None = None

    def event_keys(self) -> tuple[str, ...]:
        """表键（保持 YAML 声明顺序——确定性门要求可枚举顺序稳定）。"""

        return tuple(self.rows)

    def spec_row_keys(self) -> tuple[str, ...]:
        return tuple(key for key, row in self.rows.items() if row.is_spec_row)

    def resolve(
        self,
        event_type: str,
        *,
        activity: str | None = None,
        affected_device_type: str | None = None,
    ) -> DeviceSearchSpace:
        row: EventDeviceMappingRow | None = None
        resolved_from: _ResolvedFrom = "exact"

        if activity:
            row = self.rows.get(f"{event_type}{ACTIVITY_KEY_SEPARATOR}{activity}")
            if row is not None:
                resolved_from = "activity"

        if row is None:
            row = self.rows.get(event_type)
            # 带了活动名却只命中基础行 = 回落（未知活动不该报错，也不该变空）。
            resolved_from = "base" if activity else "exact"

        if row is None:
            # 未登记的事件类型：返回空搜索面而不是抛 KeyError，也不是"全集"——
            # 编排层据此判定"没有默认相关设备"，由它决定要不要兜底。
            return DeviceSearchSpace.empty(event_type, activity=activity)

        if row.dynamic == "affected_device":
            return self._resolve_dynamic(row, activity=activity, affected_device_type=affected_device_type)

        return DeviceSearchSpace(
            event_type=event_type,
            event_key=row.event_key,
            activity=activity,
            resolved_from=resolved_from,
            primary=row.primary_device_types,
            secondary=row.secondary_device_types,
            room_scope=row.room_scope,
            capabilities=row.capabilities,
            default_policy=row.default_policy,
            source=row.source,
        )

    def _resolve_dynamic(
        self,
        row: EventDeviceMappingRow,
        *,
        activity: str | None,
        affected_device_type: str | None,
    ) -> DeviceSearchSpace:
        """§7「affected device | alternatives」行的现场解析。"""

        if not affected_device_type:
            # 连是哪台设备都说不出 → 没有任何 agent 能定位替代品；给空面并保留 policy，
            # 让上层仍能解释"fail closed and explain"。
            return DeviceSearchSpace.empty(
                row.event_type,
                activity=activity,
                resolved_from="dynamic_unresolved",
                default_policy=row.default_policy,
                source=row.source,
            )

        alternatives = tuple(
            candidate
            for candidate in row.alternatives.get(affected_device_type, ())
            if candidate != affected_device_type
        )
        return DeviceSearchSpace(
            event_type=row.event_type,
            event_key=row.event_key,
            activity=activity,
            resolved_from="dynamic",
            primary=(affected_device_type,),
            secondary=alternatives,
            room_scope=row.room_scope,
            capabilities=row.capabilities,
            default_policy=row.default_policy,
            source=row.source,
        )


# --------------------------------------------------------------------------- #
# 加载与校验
# --------------------------------------------------------------------------- #


def _split_key(event_key: str) -> tuple[str, str | None]:
    if ACTIVITY_KEY_SEPARATOR in event_key:
        event_type, _, activity = event_key.partition(ACTIVITY_KEY_SEPARATOR)
        return event_type, (activity or None)
    return event_key, None


def _known_capabilities_by_type() -> dict[str, frozenset[str]]:
    return {
        device_type: frozenset(spec.name for spec in specs)
        for device_type, specs in CAPABILITY_MATRIX.items()
    }


def _check_device_types(
    types: tuple[str, ...],
    *,
    known: frozenset[str],
    event_key: str,
    path: Path | None,
    column: str,
) -> None:
    unknown = [t for t in types if t not in known]
    if unknown:
        raise EventMappingError(
            EventMappingErrorCode.UNKNOWN_DEVICE_TYPE,
            f"{column} 里有设备注册表不认识的设备类型 {unknown}；已知类型 {sorted(known)}",
            path=path,
            event_key=event_key,
        )


def _validate_row(
    row: EventDeviceMappingRow,
    *,
    path: Path | None,
    known_types: frozenset[str],
    known_rooms: frozenset[str],
    caps_by_type: dict[str, frozenset[str]],
) -> None:
    if row.event_type not in ALL_ROOT_EVENT_TYPES:
        raise EventMappingError(
            EventMappingErrorCode.UNKNOWN_EVENT_TYPE,
            f"{row.event_type!r} 不是 §4.1 的根事件类型（backend/engine/event_types.py 是单一来源）",
            path=path,
            event_key=row.event_key,
        )

    _check_device_types(
        row.primary_device_types,
        known=known_types,
        event_key=row.event_key,
        path=path,
        column="primary_device_types",
    )
    _check_device_types(
        row.secondary_device_types,
        known=known_types,
        event_key=row.event_key,
        path=path,
        column="secondary_device_types",
    )
    _check_device_types(
        tuple(row.alternatives),
        known=known_types,
        event_key=row.event_key,
        path=path,
        column="alternatives 的键",
    )
    for affected, candidates in row.alternatives.items():
        _check_device_types(
            candidates,
            known=known_types,
            event_key=row.event_key,
            path=path,
            column=f"alternatives[{affected}]",
        )

    unknown_rooms = [room for room in row.room_scope if room not in known_rooms]
    if unknown_rooms:
        raise EventMappingError(
            EventMappingErrorCode.UNKNOWN_ROOM_ID,
            f"room_scope 里有注册表不存在的房间 {unknown_rooms}；已知房间 {sorted(known_rooms)}",
            path=path,
            event_key=row.event_key,
        )

    if row.dynamic is None and not row.declared_device_types:
        raise EventMappingError(
            EventMappingErrorCode.INVALID_ROW,
            "行既没有 primary/secondary 设备类型，也没有 dynamic 解析方式——等于静默漏配",
            path=path,
            event_key=row.event_key,
        )

    # 能力校验分两级：能力名本身要存在；且必须至少属于本行声明的某个设备类型——
    # 否则就是"写了但永远匹配不到"，与漏配等价。dynamic 行没有声明类型，按全类型判定。
    scope_types = row.declared_device_types or frozenset(caps_by_type)
    all_known_caps = frozenset().union(*caps_by_type.values()) if caps_by_type else frozenset()
    for capability in row.capabilities:
        if capability not in all_known_caps:
            raise EventMappingError(
                EventMappingErrorCode.UNKNOWN_CAPABILITY,
                f"能力 {capability!r} 不在能力矩阵里（backend/execution/capability_matrix.py）",
                path=path,
                event_key=row.event_key,
            )
        if not any(capability in caps_by_type.get(t, frozenset()) for t in scope_types):
            raise EventMappingError(
                EventMappingErrorCode.CAPABILITY_TYPE_MISMATCH,
                (
                    f"能力 {capability!r} 不属于本行声明的任何设备类型 {sorted(scope_types)}"
                    "——写了也永远匹配不到"
                ),
                path=path,
                event_key=row.event_key,
            )


def _build_row(event_key: str, raw: Any, *, path: Path | None) -> EventDeviceMappingRow:
    if not isinstance(raw, dict):
        raise EventMappingError(
            EventMappingErrorCode.INVALID_ROW,
            "行必须是映射（key: value）",
            path=path,
            event_key=event_key,
        )

    collided = sorted(_DERIVED_ROW_FIELDS & set(raw))
    if collided:
        raise EventMappingError(
            EventMappingErrorCode.INVALID_ROW,
            f"字段 {collided} 由表键推导，不要在 YAML 里重复声明",
            path=path,
            event_key=event_key,
        )

    event_type, activity = _split_key(event_key)
    try:
        return EventDeviceMappingRow(
            event_key=event_key,
            event_type=event_type,
            activity=activity,
            **raw,
        )
    except ValidationError as exc:
        raise EventMappingError(
            EventMappingErrorCode.INVALID_ROW,
            f"行结构非法：{exc.errors()[0].get('loc')} {exc.errors()[0].get('msg')}",
            path=path,
            event_key=event_key,
        ) from exc


def load_event_device_mapping(path: Path | str | None = None) -> EventDeviceMapping:
    """读一份 §7 映射表并做全部加载期校验；任何问题都抛 :class:`EventMappingError`。

    不带缓存——测试与热更新需要能对同一路径重复加载。生产路径请走
    :func:`get_event_device_mapping`（默认表带进程级缓存）。
    """

    path = Path(path) if path is not None else DEFAULT_MAPPING_PATH
    if not path.is_file():
        raise EventMappingError(
            EventMappingErrorCode.FILE_NOT_FOUND, "映射表文件不存在", path=path
        )

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise EventMappingError(
            EventMappingErrorCode.NOT_A_MAPPING, f"YAML 解析失败：{exc}", path=path
        ) from exc

    if not isinstance(raw, dict):
        raise EventMappingError(
            EventMappingErrorCode.NOT_A_MAPPING, "文档顶层必须是映射", path=path
        )

    unknown_top = sorted(set(raw) - {"schema_version", "events"})
    if unknown_top:
        raise EventMappingError(
            EventMappingErrorCode.INVALID_DOCUMENT,
            f"顶层出现未知键 {unknown_top}（只允许 schema_version / events）",
            path=path,
        )

    schema_version = raw.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise EventMappingError(
            EventMappingErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            f"schema_version={schema_version!r}，本代码支持 {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
            path=path,
        )

    events = raw.get("events")
    if not isinstance(events, dict) or not events:
        raise EventMappingError(
            EventMappingErrorCode.INVALID_DOCUMENT, "events 必须是非空映射", path=path
        )

    known_types = frozenset(all_device_types())
    known_rooms = frozenset(build_default_rooms())
    caps_by_type = _known_capabilities_by_type()

    rows: dict[str, EventDeviceMappingRow] = {}
    for event_key, raw_row in events.items():
        row = _build_row(str(event_key), raw_row, path=path)
        _validate_row(
            row,
            path=path,
            known_types=known_types,
            known_rooms=known_rooms,
            caps_by_type=caps_by_type,
        )
        rows[row.event_key] = row

    # §4.1 十七个根事件一个都不能缺：漏一行 = 那类事件对所有 agent 静默失明。
    covered = {row.event_type for row in rows.values() if row.activity is None}
    missing = sorted(ALL_ROOT_EVENT_TYPES - covered)
    if missing:
        raise EventMappingError(
            EventMappingErrorCode.MISSING_EVENT_COVERAGE,
            f"§4.1 根事件缺少映射行：{missing}",
            path=path,
        )

    return EventDeviceMapping(schema_version=int(schema_version), rows=rows, path=path)


@lru_cache(maxsize=1)
def get_event_device_mapping() -> EventDeviceMapping:
    """进程级缓存的默认表。agent 每条事件都会查表，不能每次读盘。"""

    return load_event_device_mapping(DEFAULT_MAPPING_PATH)


def clear_mapping_cache() -> None:
    """丢弃默认表缓存（改了 YAML 想热加载、或测试需要重读时用）。"""

    get_event_device_mapping.cache_clear()


def get_device_search_space(
    event: "SimEvent | str",
    *,
    activity: str | None = None,
    affected_device_type: str | None = None,
    mapping: EventDeviceMapping | None = None,
) -> DeviceSearchSpace:
    """§7 查表入口：给一条根事件（或事件类型名），返回它的默认设备搜索空间。

    - ``activity`` 不传时自动读事件 ``data.activity``（``user.starts_activity:sleeping``
      这类活动限定键靠它命中）；显式传参优先。
    - ``affected_device_type`` 不传时自动读事件 ``data.device_type``（``device.offline``
      的「affected device」行靠它现场解析）；调用方若已从世界里查到设备类型，显式传更准。
    - 未登记的事件类型返回空搜索面，**不抛 KeyError**。
    """

    if isinstance(event, str):
        event_type = event
        data: dict[str, Any] = {}
    else:
        event_type = str(getattr(event, "event_type", "") or "")
        data = getattr(event, "data", None) or {}

    if activity is None:
        raw_activity = data.get("activity")
        activity = str(raw_activity) if raw_activity else None

    if affected_device_type is None:
        raw_device_type = data.get("device_type")
        affected_device_type = str(raw_device_type) if raw_device_type else None

    table = mapping if mapping is not None else get_event_device_mapping()
    return table.resolve(
        event_type,
        activity=activity,
        affected_device_type=affected_device_type,
    )
