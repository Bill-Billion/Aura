"""§5.1 ScenarioSpec 数据模型 + §5.3 ground truth 标签。

场景是"可复现实验"的契约（决策#2：YAML 文件、进 git 版本化）。本模块是这个契约的
**唯一形状来源**：loader 只负责 IO + 跨文件/注册表校验，REST 与 S2-T5/T6、S3-T9、
S4-T3/T4 全部消费这里的模型，不得各自再定义一份场景形状。

三个刻意的形状决定：
  1. ``expected`` 的值同时容纳精确值与区间/枚举（§5.2 里 ``power: true`` 与
     ``extra.brightness: {min: 50}`` 并存）——统一成 ``ExpectedValue``，S4 评估器直接调
     ``matches()``，不要在评估侧重写一遍判定。
  2. ``timeline`` 的事件类型对齐 §4.1 根事件分类学（``ROOT_EVENT_TYPES``）。分类学本体
     现居 ``backend/engine/event_types.py``（运行期与场景层共用同一个对象），本模块 re-export
     它——绝不另立一份 enum，两份分类学=两套真相。
  3. 所有子模型 ``extra="forbid"``。§14 的高 MINOR 容忍不是靠放松模型，而是靠 loader
     在校验失败后按 ``extra_forbidden`` 定位剥离未知字段并记日志（见 loader）。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.engine.event_types import (
    ALLOWED_TIMELINE_EVENT_TYPES,
    COMPAT_ROOT_EVENT_TYPES,
    ROOT_EVENT_TYPES,
)
from backend.execution.validation import CommandErrorCode
from backend.scenarios.versioning import SUPPORTED_SCENARIO_SCHEMA_VERSION

# §10.2 失败码词表的字符串投影。场景层只需要"这个码是否合法"，不需要枚举成员本身；
# 唯一来源仍是 backend/execution/validation.py::CommandErrorCode，这里绝不再抄一份。
_COMMAND_ERROR_CODES: frozenset[str] = frozenset(code.value for code in CommandErrorCode)

# §4.1 分类学的定义已上移到 backend/engine/event_types.py（S2-T6）：运行期（引擎、agent
# 订阅、生成器）与场景校验必须共用**同一个对象**，否则"YAML 里合法"与"运行期认得"
# 会各自漂移。这里只做 re-export，既有 `from backend.scenarios.spec import ROOT_EVENT_TYPES`
# 的调用方一行不用改。
__all_taxonomy__ = (
    "ROOT_EVENT_TYPES",
    "COMPAT_ROOT_EVENT_TYPES",
    "ALLOWED_TIMELINE_EVENT_TYPES",
)

# 设备状态路径前缀：power 是 DeviceStateValues 的顶层布尔位，其余可写量都挂在 extra.*
# （含 online 语义位，见 backend/execution/validation.py::device_is_online）。
_TOP_LEVEL_DEVICE_PATHS = frozenset({"power"})
_TIME_OF_DAY_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ initial_state


class InitialUserState(_StrictModel):
    """§5.2 initial_state.users.*。

    ``location`` 这里是房间 id 或 ``outside`` 之类的语义位置字符串，与世界模型的
    ``UserState.location``（Location3D）不是同一形状——映射由 S2-T5 的 initial_state
    应用层负责，场景作者不需要写坐标。
    """

    location: str | None = None
    activity: str | None = None
    presence_state: Literal["home", "away", "sleeping", "unknown"] | None = None


class InitialRoomState(_StrictModel):
    occupancy: bool | None = None
    light_level: float | None = None
    temperature: float | None = None
    humidity: float | None = None


class InitialDeviceStateValues(_StrictModel):
    power: bool | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class InitialDeviceState(_StrictModel):
    state: InitialDeviceStateValues = Field(default_factory=InitialDeviceStateValues)


class InitialState(_StrictModel):
    """运行开始前施加的世界覆盖（§5.1 initial_state）。字段全可选：只写要改的部分。"""

    time_of_day: str | None = None
    # 与 backend/engine/state.py::EnvironmentState.weather 保持同一词表
    weather: Literal["clear", "cloudy", "rainy", "snowy"] | None = None
    outdoor_temp: float | None = None
    outdoor_humidity: float | None = None
    users: dict[str, InitialUserState] = Field(default_factory=dict)
    rooms: dict[str, InitialRoomState] = Field(default_factory=dict)
    devices: dict[str, InitialDeviceState] = Field(default_factory=dict)

    @field_validator("time_of_day")
    @classmethod
    def _check_time_of_day(cls, value: str | None) -> str | None:
        if value is not None and not _TIME_OF_DAY_RE.match(value):
            raise ValueError(f"time_of_day 必须是 24 小时制 'HH:MM'，收到 {value!r}")
        return value


# ---------------------------------------------------------------------- timeline


class TimelineEvent(_StrictModel):
    """§5.1 timeline 的一条脚本根事件（§4.5 scripted 模式的输入）。

    顶层只放跨事件类型通用的定位字段；事件类型特有的载荷（weather、threshold、activity
    的附加参数等）写进 ``payload``。extra="forbid" 是有意的：场景文件里的键名手滑
    （``rooom_id``）应当在加载期就炸，而不是变成一条谁也没消费的静默事件。
    """

    at: float = Field(description="相对 run 起点的秒偏移；同一 timeline 内必须非降序")
    type: str
    user_id: str | None = None
    room_id: str | None = None
    device_id: str | None = None
    activity: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if value not in ALLOWED_TIMELINE_EVENT_TYPES:
            raise ValueError(
                f"未知根事件类型 {value!r}；允许的取值见 spec §4.1 "
                f"（backend/scenarios/spec.py::ALLOWED_TIMELINE_EVENT_TYPES）"
            )
        return value

    @field_validator("at")
    @classmethod
    def _check_at(cls, value: float) -> float:
        if value < 0:
            raise ValueError(f"timeline.at 不能为负，收到 {value}")
        return value


# --------------------------------------------------------- expected_device_effects


class ExpectedValue(_StrictModel):
    """一条期望约束：精确值（equals）或区间（min/max）或枚举（one_of）。

    ``equals=None`` 表示"没有精确值约束"而非"期望为 null"——设备状态里没有有意义的
    null 期望，为此换取模型干净是划算的；空约束会被下面的校验直接拒绝。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    equals: Any = None
    min: float | None = None
    max: float | None = None
    one_of: list[Any] | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> "ExpectedValue":
        if self.equals is None and self.min is None and self.max is None and self.one_of is None:
            raise ValueError("expected 约束为空：至少给出精确值、min/max 或 one_of 之一")
        return self

    def matches(self, actual: Any) -> bool:
        """S4 评估器的判定入口。任一已声明的约束不满足即 False。"""
        if self.equals is not None:
            # bool 与数值不互认：True == 1 在 Python 里成立，但 power=True 不该匹配 equals=1
            if isinstance(self.equals, bool) != isinstance(actual, bool):
                return False
            if actual != self.equals:
                return False
        if self.one_of is not None and actual not in self.one_of:
            return False
        if self.min is not None or self.max is not None:
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                return False
            if self.min is not None and actual < self.min:
                return False
            if self.max is not None and actual > self.max:
                return False
        return True


class ExpectedDeviceEffect(_StrictModel):
    """§5.1 expected_device_effects 的一项：某设备在 within_seconds 内应达到的状态。"""

    device_id: str
    within_seconds: float = Field(default=10.0, ge=0)
    expected: dict[str, ExpectedValue]

    @model_validator(mode="before")
    @classmethod
    def _coerce_expected(cls, data: Any) -> Any:
        """把 §5.2 的两种写法归一：标量 → {equals: 标量}，映射 → ExpectedValue 原样。"""
        if not isinstance(data, dict):
            return data
        raw = data.get("expected")
        if not isinstance(raw, dict):
            return data
        coerced: dict[str, Any] = {}
        for path, value in raw.items():
            if isinstance(value, dict):
                coerced[path] = value
            else:
                coerced[path] = {"equals": value}
        return {**data, "expected": coerced}

    @field_validator("expected")
    @classmethod
    def _check_paths(cls, value: dict[str, ExpectedValue]) -> dict[str, ExpectedValue]:
        if not value:
            raise ValueError("expected 不能为空")
        for path in value:
            if path in _TOP_LEVEL_DEVICE_PATHS:
                continue
            if path.startswith("extra.") and len(path) > len("extra."):
                continue
            raise ValueError(
                f"expected 路径 {path!r} 非法：只能是 'power' 或 'extra.<capability>'"
                "（漏写 'extra.' 前缀是最常见的写法错误）"
            )
        return value


# --------------------------------------------------------------- success_criteria


class SuccessCriteria(_StrictModel):
    """§5.1 success_criteria（§5.2 四项）。S4 评估器按这些阈值判 pass/fail。"""

    require_complete_episode: bool = True
    max_first_action_latency_ms: int | None = Field(default=None, ge=0)
    max_command_failures: int | None = Field(default=None, ge=0)
    allow_fallback: bool = True


# -------------------------------------------------------------- expected_failures


# §13 "Required failure scenarios" 十条，逐条 slug 化。这是**词表本体**：S4-T3 为每一类
# 认领一个 scenarios/failures/*.yaml，S4-T2 的评估器按 category 把"预期内失败"从回归里扣掉。
# 新增类别属于语义扩张，必须连同 SUPPORTED_SCENARIO_SCHEMA_VERSION 的 MINOR 一起抬。
EXPECTED_FAILURE_CATEGORIES: frozenset[str] = frozenset(
    {
        "device_offline_before_command",       # Device offline before a command
        "device_offline_during_execution",     # Device goes offline during execution
        "sensor_drift_or_stale_reading",       # Sensor drift or stale readings
        "llm_timeout",                         # LLM timeout
        "llm_invalid_output",                  # LLM invalid output
        "websocket_reconnect_during_run",      # WebSocket reconnect during an active run
        "reset_during_in_flight_episode",      # Reset during an in-flight episode
        "user_override_of_agent_proposal",     # User manually overrides an agent proposal
        "user_activity_change_after_action",   # User changes activity shortly after an action
        "safety_event_interrupts_comfort",     # Safety event interrupts comfort/energy behavior
    }
)


class ExpectedFailure(_StrictModel):
    """§5.1 可选字段 ``expected_failures`` 的一项（schema 1.1 增量）。

    存在的理由是 §13 的一条恢复要求："The evaluator must distinguish expected failures
    from regressions"。一个正确处理掉线的 run **本来就应该**产生失败命令；没有这条声明，
    S4 的 command_failure_count 会把"正确行为"记成回归。

    两个词表都刻意不自己造：
      * ``category``   取 ``EXPECTED_FAILURE_CATEGORIES``（§13 十类）。
      * ``error_code`` 取 ``CommandErrorCode``（§10.2 十码，唯一来源在
        backend/execution/validation.py）——场景声明的失败码与执行期真正产出的失败码
        必须是同一套字符串，否则 S4 的比对永远匹配不上。

    ``device_id`` 可选：设备域失败（掉线/被覆盖）指名道姓，LLM 超时之类的没有设备主语。
    给出时会进 :meth:`ScenarioSpec.referenced_device_ids`，由 loader 对照注册表校验。
    """

    category: str
    error_code: str
    device_id: str | None = None
    description: str | None = None
    # 期望的恢复动作（自由文本，面向研究者）。S4 只统计"是否发生 + 是否预期内"，
    # 不按这段字符串做机器判定，因此这里不设词表。
    expected_recovery: str | None = None

    @field_validator("category")
    @classmethod
    def _check_category(cls, value: str) -> str:
        if value not in EXPECTED_FAILURE_CATEGORIES:
            raise ValueError(
                f"未知失败类别 {value!r}；取值须是 spec §13 十类之一："
                f"{', '.join(sorted(EXPECTED_FAILURE_CATEGORIES))}"
            )
        return value

    @field_validator("error_code")
    @classmethod
    def _check_error_code(cls, value: str) -> str:
        if value not in _COMMAND_ERROR_CODES:
            raise ValueError(
                f"未知失败码 {value!r}；取值须是 spec §10.2 十码之一"
                f"（backend/execution/validation.py::CommandErrorCode）："
                f"{', '.join(sorted(_COMMAND_ERROR_CODES))}"
            )
        return value


# ------------------------------------------------------------------ ground truth


class GroundTruth(_StrictModel):
    """§5.3 八个标签。刻意与 observable 分离：agent 不得消费，仅评估侧读取（§2.3）。"""

    user_goal: str | None = None
    primary_room_ids: list[str] = Field(default_factory=list)
    relevant_device_ids: list[str] = Field(default_factory=list)
    forbidden_device_ids: list[str] = Field(default_factory=list)
    required_agent_roles: list[str] = Field(default_factory=list)
    acceptable_noop: bool = False
    expected_intent: str | None = None
    safety_constraints: list[str] = Field(default_factory=list)


# ------------------------------------------------------------------- ScenarioSpec


class ScenarioSpec(_StrictModel):
    """§5.1 全字段场景契约。"""

    # §14 预埋。YAML 里请**加引号**写（1.10 不加引号会被 YAML 读成 float 1.1）。
    scenario_schema_version: str = SUPPORTED_SCENARIO_SCHEMA_VERSION

    # —— 必填（§5.1 Required fields）——
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    seed: int
    initial_state: InitialState
    timeline: list[TimelineEvent]
    expected_device_effects: list[ExpectedDeviceEffect]
    involved_agents: list[str]
    success_criteria: SuccessCriteria

    # —— 可选（§5.1 Optional fields）——
    duration_seconds: float | None = Field(default=None, gt=0)
    # 注：世界模型的 simulation_mode 只有 observe|demo；stress 是场景层语义（§5.1），
    # 由 S2-T6 的 runner 决定如何降落到运行模式，不要直接塞进 WorldState。
    mode: Literal["observe", "demo", "stress"] = "observe"
    allowed_fallback: bool = True
    noise_model: dict[str, Any] = Field(default_factory=dict)
    failure_injection: dict[str, Any] = Field(default_factory=dict)
    metrics: list[str] = Field(default_factory=list)

    # schema 1.1 增量（§13 恢复要求）。空列表 = "本场景不预期任何失败"，因此 S4 评估器
    # 可以无条件迭代，不必区分 None 与 []。
    expected_failures: list[ExpectedFailure] = Field(default_factory=list)

    # §5.3（规格称 "should include"，但存量 §5.2 示例没有，故留可选）
    ground_truth: GroundTruth | None = None

    @field_validator("scenario_schema_version", mode="before")
    @classmethod
    def _stringify_version(cls, value: Any) -> Any:
        # YAML 未加引号时 1.0 会是 float——统一成字符串，真正的兼容判定在 versioning.py
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("timeline")
    @classmethod
    def _check_timeline_order(cls, value: list[TimelineEvent]) -> list[TimelineEvent]:
        previous = None
        for index, event in enumerate(value):
            if previous is not None and event.at < previous:
                raise ValueError(
                    f"timeline 的 at 偏移必须非降序：第 {index} 条 at={event.at} 小于前一条 at={previous}"
                )
            previous = event.at
        return value

    @model_validator(mode="after")
    def _check_duration_covers_timeline(self) -> "ScenarioSpec":
        if self.duration_seconds is not None and self.timeline:
            last = self.timeline[-1].at
            if last > self.duration_seconds:
                raise ValueError(
                    f"timeline 最后一条 at={last} 超出 duration_seconds={self.duration_seconds}，"
                    "该事件永远不会被触发"
                )
        return self

    def referenced_device_ids(self) -> set[str]:
        """场景引用到的全部设备 id（供 loader 对照注册表校验、S4 评估器取搜索空间）。"""
        ids = set(self.initial_state.devices)
        ids.update(effect.device_id for effect in self.expected_device_effects)
        ids.update(event.device_id for event in self.timeline if event.device_id)
        ids.update(
            failure.device_id for failure in self.expected_failures if failure.device_id
        )
        if self.ground_truth is not None:
            ids.update(self.ground_truth.relevant_device_ids)
            ids.update(self.ground_truth.forbidden_device_ids)
        return ids

    def referenced_room_ids(self) -> set[str]:
        ids = set(self.initial_state.rooms)
        ids.update(event.room_id for event in self.timeline if event.room_id)
        if self.ground_truth is not None:
            ids.update(self.ground_truth.primary_room_ids)
        return ids

    def summary(self) -> dict[str, Any]:
        """REST 枚举用的轻量投影（GET /api/scenarios），不含 ground_truth。

        ground_truth 是 §2.3 的"不对 agent 可见"一侧，枚举接口刻意不带，避免前端/agent
        侧顺手把答案读走；需要时走 GET /api/scenarios/{id}（评估工具面向研究者）。
        """
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "seed": self.seed,
            "mode": self.mode,
            "duration_seconds": self.duration_seconds,
            "involved_agents": list(self.involved_agents),
            "timeline_event_count": len(self.timeline),
            "expected_device_effect_count": len(self.expected_device_effects),
            "root_event_types": sorted({event.type for event in self.timeline}),
            "has_ground_truth": self.ground_truth is not None,
            "scenario_schema_version": self.scenario_schema_version,
        }
