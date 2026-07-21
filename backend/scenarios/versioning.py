"""§14 schema 版本兼容判定——唯一实现。

【S4-T1 约定】按计划，本模块将被原样搬进 ``backend/models/versioning.py`` 供 event /
command / device_registry 三套 schema 复用，因此这里刻意保持**自包含、零依赖**：
只用标准库，不 import pydantic / structlog / 任何 backend.* 模块。搬迁时整文件移动即可，
不要在这里加日志或校验框架依赖（日志由调用方 loader 打，见 loader._validate_with_tolerance）。

§14 兼容规则落地为三个分支：
  - MAJOR 不同             → 拒绝（"Scenario loaders should reject unknown major versions"）
  - MINOR <= 支持版本      → 严格模式，未知字段一律报错（extra="forbid"）
  - MINOR >  支持版本      → 容忍模式，未知**可选**字段被丢弃并记日志；缺必填字段仍然失败
    （"Minor versions may add optional fields" + "fail on missing required fields"）
"""

from __future__ import annotations

from dataclasses import dataclass

# 当前后端实现的 ScenarioSpec 版本。加字段（可选）时抬 MINOR，改语义/删字段时抬 MAJOR。
#
# 变更史（抬 MINOR 必须在这里留一行，否则没人说得清 "1.1 到底多了什么"）：
#   1.0 —— §5.1 的必填九项 + 可选七项 + §5.3 ground_truth。
#   1.1 —— 追加可选 ``expected_failures``（§13 "The evaluator must distinguish expected
#          failures from regressions" 的数据落点，形状见 spec.py::ExpectedFailure）。
#          §14 "Minor versions may add optional fields" 正是为这类增量准备的：旧后端
#          读 1.1 文件时走高 MINOR 容忍分支丢字段并记日志，新后端把它当一等字段校验。
SUPPORTED_SCENARIO_SCHEMA_VERSION = "1.1"


class SchemaVersionError(ValueError):
    """结构化的版本不兼容错误。

    与裸 ValueError 的差别在于它带机器可读字段：调用方（loader / REST 层）能把
    declared / supported / reason 原样透给研究者，而不是让人去解析异常文本。
    """

    def __init__(self, *, declared: str, supported: str, reason: str, field: str = "scenario_schema_version") -> None:
        self.declared = declared
        self.supported = supported
        self.reason = reason
        self.field = field
        super().__init__(f"{field}={declared!r} 与本实现支持的 {supported!r} 不兼容：{reason}")

    def to_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "declared": self.declared,
            "supported": self.supported,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class VersionCompatibility:
    """一次版本比较的结论。

    ``strict``   —— 是否按 extra="forbid" 校验（未知字段=错误）。
    ``tolerated``—— 是否走高 MINOR 容忍路径（未知可选字段丢弃 + 记日志）。
    二者互斥；MAJOR 不兼容不会返回本对象，而是直接抛 SchemaVersionError。
    """

    declared: tuple[int, int]
    supported: tuple[int, int]
    strict: bool
    tolerated: bool
    reason: str


def parse_schema_version(raw: object) -> tuple[int, int]:
    """把 ``"1.0"`` / ``"2"`` / ``"1.3.7"`` 解析成 (MAJOR, MINOR)。

    注意 YAML 里未加引号的 ``scenario_schema_version: 1.0`` 会被 PyYAML 解析成 float，
    因此这里接受 int/float 并先转字符串。副作用是 ``1.10`` 会退化成 ``1.1``——场景作者
    应当给版本号加引号，spec.py 的字段注释里也写了这条。
    """
    if isinstance(raw, bool):  # bool 是 int 的子类，先挡掉
        raise SchemaVersionError(
            declared=str(raw), supported=SUPPORTED_SCENARIO_SCHEMA_VERSION, reason="版本号不能是布尔值"
        )
    if isinstance(raw, (int, float)):
        text = str(raw)
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        raise SchemaVersionError(
            declared=str(raw),
            supported=SUPPORTED_SCENARIO_SCHEMA_VERSION,
            reason=f"版本号类型不支持：{type(raw).__name__}",
        )

    parts = text.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        raise SchemaVersionError(
            declared=text,
            supported=SUPPORTED_SCENARIO_SCHEMA_VERSION,
            reason="版本号必须形如 MAJOR.MINOR（例：'1.0'）",
        ) from None
    if major < 0 or minor < 0:
        raise SchemaVersionError(
            declared=text, supported=SUPPORTED_SCENARIO_SCHEMA_VERSION, reason="版本号不能为负"
        )
    return major, minor


def check_schema_compatibility(
    declared: object,
    supported: str = SUPPORTED_SCENARIO_SCHEMA_VERSION,
) -> VersionCompatibility:
    """§14 兼容判定。MAJOR 不匹配抛 SchemaVersionError，其余返回 VersionCompatibility。"""
    declared_text = str(declared).strip()
    declared_pair = parse_schema_version(declared)
    supported_pair = parse_schema_version(supported)

    if declared_pair[0] != supported_pair[0]:
        raise SchemaVersionError(
            declared=declared_text,
            supported=supported,
            reason=(
                f"unknown MAJOR version {declared_pair[0]}（本实现为 {supported_pair[0]}）；"
                "MAJOR 变更意味着语义不兼容，必须由人工迁移场景文件"
            ),
        )

    if declared_pair[1] > supported_pair[1]:
        return VersionCompatibility(
            declared=declared_pair,
            supported=supported_pair,
            strict=False,
            tolerated=True,
            reason=(
                f"declared MINOR {declared_pair[1]} 高于支持的 {supported_pair[1]}："
                "按 §14 兼容规则接受未知可选字段（会被丢弃并记日志）"
            ),
        )

    return VersionCompatibility(
        declared=declared_pair,
        supported=supported_pair,
        strict=True,
        tolerated=False,
        reason="MINOR 不高于支持版本：严格校验，未知字段一律拒绝",
    )
