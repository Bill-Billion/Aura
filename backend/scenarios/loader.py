"""场景 YAML 加载与校验（§5 + §14）。

职责边界：形状归 spec.py，本模块只做 IO、§14 版本分支、跨文件唯一性、以及需要外部
上下文（设备/房间注册表）才能做的引用完整性校验。

**目录契约（跨阶段硬约定）**：``load_library(dirs)`` 显式吃一个目录列表，默认
``backend/scenarios/library/``。S3 的 ``backend/scenarios/eval/``、S4 的
``backend/scenarios/failures/`` 与 ``backend/scenarios/suites/`` 都走这同一个 loader ——
不要为新目录再写第二个加载函数，也不要把目录硬编码进调用方。缺失的目录被静默跳过，
因此后续阶段的目录还没建时整库加载不会中断。
"""

from __future__ import annotations

import copy
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

import yaml
from pydantic import ValidationError

from backend.config.device_registry import build_default_rooms, get_default_device_registry
from backend.core.logging import log
from backend.models.versioning import (
    SUPPORTED_SCENARIO_SCHEMA_VERSION,
    SchemaVersionError,
    check_schema_compatibility,
)
from backend.scenarios.spec import ScenarioSpec

# 默认场景库目录。列表形式是有意的：S3/S4 追加目录时只改调用方传入的列表。
DEFAULT_LIBRARY_DIRS: tuple[Path, ...] = (Path(__file__).resolve().parent / "library",)

_YAML_SUFFIXES = (".yaml", ".yml")


class ScenarioLoadErrorCode(str, Enum):
    """加载期失败词表（面向研究者：坏在哪个文件、坏在哪一条）。"""

    FILE_NOT_FOUND = "file_not_found"
    INVALID_YAML = "invalid_yaml"
    NOT_A_MAPPING = "not_a_mapping"
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    INVALID_SPEC = "invalid_spec"
    UNKNOWN_DEVICE_ID = "unknown_device_id"
    UNKNOWN_ROOM_ID = "unknown_room_id"
    DUPLICATE_SCENARIO_ID = "duplicate_scenario_id"


class ScenarioLoadError(Exception):
    """结构化加载失败。``code`` 机器可读，``details`` 带定位信息，消息永远带文件路径。"""

    def __init__(
        self,
        code: ScenarioLoadErrorCode,
        message: str,
        *,
        path: Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.details = details or {}
        prefix = f"{path}: " if path is not None else ""
        super().__init__(f"{prefix}{message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "path": str(self.path) if self.path is not None else None,
            "details": dict(self.details),
        }


# ------------------------------------------------------------------ 注册表引用校验


def registry_device_ids() -> frozenset[str]:
    return frozenset(entry.id for entry in get_default_device_registry())


def registry_room_ids() -> frozenset[str]:
    return frozenset(build_default_rooms())


def _check_registry_references(spec: ScenarioSpec, path: Path | None) -> None:
    known_devices = registry_device_ids()
    unknown_devices = sorted(spec.referenced_device_ids() - known_devices)
    if unknown_devices:
        raise ScenarioLoadError(
            ScenarioLoadErrorCode.UNKNOWN_DEVICE_ID,
            f"场景引用了默认设备注册表里不存在的设备：{', '.join(unknown_devices)}",
            path=path,
            details={"scenario_id": spec.id, "unknown_device_ids": unknown_devices},
        )

    known_rooms = registry_room_ids()
    unknown_rooms = sorted(spec.referenced_room_ids() - known_rooms)
    if unknown_rooms:
        raise ScenarioLoadError(
            ScenarioLoadErrorCode.UNKNOWN_ROOM_ID,
            f"场景引用了默认房间注册表里不存在的房间：{', '.join(unknown_rooms)}",
            path=path,
            details={"scenario_id": spec.id, "unknown_room_ids": unknown_rooms},
        )


# ------------------------------------------------------- §14 版本分支下的模型校验


def _pop_path(data: Any, loc: Sequence[Any]) -> bool:
    """按 pydantic 的 error loc 定位并删除一个键。删掉返回 True。"""
    cursor = data
    for key in loc[:-1]:
        if isinstance(cursor, dict) and key in cursor:
            cursor = cursor[key]
        elif isinstance(cursor, list) and isinstance(key, int) and 0 <= key < len(cursor):
            cursor = cursor[key]
        else:
            return False
    last = loc[-1]
    if isinstance(cursor, dict) and last in cursor:
        del cursor[last]
        return True
    return False


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<root>"
        lines.append(f"{location}: {err['msg']}")
    return "; ".join(lines)


def _validate_with_tolerance(
    data: dict[str, Any],
    *,
    strict: bool,
    path: Path | None,
) -> ScenarioSpec:
    """严格模式直接校验；容忍模式（高 MINOR）剥离未知**可选**字段后重试并记日志。

    容忍分支刻意只吃 ``extra_forbidden`` 这一类错误：缺必填字段依然失败（§14
    "fail on missing required fields"）。这样高 MINOR 的场景文件能在旧后端上跑，但
    "被丢了什么"永远有日志可查，不会静默变成一个语义被削掉的 run。
    """
    working = copy.deepcopy(data)
    dropped: list[str] = []

    for _ in range(8):  # 每轮会一次性剥掉本轮报出的全部未知字段，正常 1-2 轮收敛
        try:
            spec = ScenarioSpec.model_validate(working)
        except ValidationError as exc:
            if strict:
                raise ScenarioLoadError(
                    ScenarioLoadErrorCode.INVALID_SPEC,
                    _format_validation_error(exc),
                    path=path,
                    details={"errors": exc.errors(include_url=False)},
                ) from exc
            extra_locs = [err["loc"] for err in exc.errors() if err["type"] == "extra_forbidden"]
            if not extra_locs:
                raise ScenarioLoadError(
                    ScenarioLoadErrorCode.INVALID_SPEC,
                    _format_validation_error(exc),
                    path=path,
                    details={"errors": exc.errors(include_url=False)},
                ) from exc
            removed_any = False
            for loc in extra_locs:
                if _pop_path(working, loc):
                    dropped.append(".".join(str(part) for part in loc))
                    removed_any = True
            if not removed_any:
                raise ScenarioLoadError(
                    ScenarioLoadErrorCode.INVALID_SPEC,
                    _format_validation_error(exc),
                    path=path,
                    details={"errors": exc.errors(include_url=False)},
                ) from exc
            continue
        if dropped:
            log.warning(
                "scenario.schema_forward_compat_fields_dropped",
                path=str(path) if path is not None else None,
                scenario_id=spec.id,
                declared_schema_version=spec.scenario_schema_version,
                supported_schema_version=SUPPORTED_SCENARIO_SCHEMA_VERSION,
                dropped_fields=dropped,
            )
        return spec

    raise ScenarioLoadError(
        ScenarioLoadErrorCode.INVALID_SPEC,
        "未知字段剥离未在预期轮数内收敛，疑似 schema 版本声明与内容严重不符",
        path=path,
        details={"dropped_fields": dropped},
    )


# ------------------------------------------------------------------------- 入口


def parse_scenario_mapping(
    data: dict[str, Any],
    *,
    path: Path | None = None,
    check_registry: bool = True,
) -> ScenarioSpec:
    """从已解析的 YAML/JSON 映射构造 ScenarioSpec（供 loader 与测试/程序化构造共用）。"""
    if not isinstance(data, dict):
        raise ScenarioLoadError(
            ScenarioLoadErrorCode.NOT_A_MAPPING,
            f"场景文件顶层必须是映射（mapping），实际是 {type(data).__name__}",
            path=path,
        )

    declared = data.get("scenario_schema_version", SUPPORTED_SCENARIO_SCHEMA_VERSION)
    try:
        compatibility = check_schema_compatibility(declared)
    except SchemaVersionError as exc:
        raise ScenarioLoadError(
            ScenarioLoadErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            str(exc),
            path=path,
            details=exc.to_dict(),
        ) from exc

    spec = _validate_with_tolerance(data, strict=compatibility.strict, path=path)
    if check_registry:
        _check_registry_references(spec, path)
    return spec


def load_scenario_file(path: Path | str, *, check_registry: bool = True) -> ScenarioSpec:
    """加载单个场景 YAML 文件。

    ``check_registry=False`` 供合成/失败注入场景使用（S4 的 failures/ 可能刻意引用
    不在默认注册表里的设备）；库场景一律开启。
    """
    path = Path(path)
    if not path.is_file():
        raise ScenarioLoadError(
            ScenarioLoadErrorCode.FILE_NOT_FOUND, "场景文件不存在", path=path
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioLoadError(
            ScenarioLoadErrorCode.INVALID_YAML,
            f"YAML 解析失败：{exc}",
            path=path,
        ) from exc
    if raw is None:
        raise ScenarioLoadError(
            ScenarioLoadErrorCode.NOT_A_MAPPING, "场景文件为空", path=path
        )
    return parse_scenario_mapping(raw, path=path, check_registry=check_registry)


def iter_scenario_files(dirs: Iterable[Path | str] | None = None) -> list[Path]:
    """按目录顺序、目录内按文件名排序枚举场景文件；不存在的目录跳过。"""
    target_dirs = [Path(d) for d in (dirs if dirs is not None else DEFAULT_LIBRARY_DIRS)]
    files: list[Path] = []
    for directory in target_dirs:
        if not directory.is_dir():
            # 缺目录不是错误：S3/S4 的 eval/failures/suites 会陆续出现
            continue
        for suffix in _YAML_SUFFIXES:
            files.extend(sorted(directory.glob(f"*{suffix}")))
    return files


def load_library(
    dirs: Iterable[Path | str] | None = None,
    *,
    check_registry: bool = True,
) -> dict[str, ScenarioSpec]:
    """加载若干目录下的全部场景，返回按 id 排序的 {scenario_id: ScenarioSpec}。

    不做缓存：场景文件是小体量、低频读的 git 资产，缓存带来的陈旧风险大于收益
    （S2-T8 往 library/ 里加文件、测试在 tmp_path 里造文件都不该被缓存挡住）。
    """
    library: dict[str, ScenarioSpec] = {}
    origin: dict[str, Path] = {}
    for file_path in iter_scenario_files(dirs):
        spec = load_scenario_file(file_path, check_registry=check_registry)
        if spec.id in library:
            raise ScenarioLoadError(
                ScenarioLoadErrorCode.DUPLICATE_SCENARIO_ID,
                f"场景 id {spec.id!r} 重复，已在 {origin[spec.id]} 定义过",
                path=file_path,
                details={"scenario_id": spec.id, "first_seen": str(origin[spec.id])},
            )
        library[spec.id] = spec
        origin[spec.id] = file_path
    return {scenario_id: library[scenario_id] for scenario_id in sorted(library)}


def get_scenario(
    scenario_id: str,
    dirs: Iterable[Path | str] | None = None,
) -> ScenarioSpec | None:
    return load_library(dirs).get(scenario_id)


def list_scenario_summaries(dirs: Iterable[Path | str] | None = None) -> list[dict[str, Any]]:
    """GET /api/scenarios 的投影列表（不含 ground_truth，见 ScenarioSpec.summary）。"""
    return [spec.summary() for spec in load_library(dirs).values()]
