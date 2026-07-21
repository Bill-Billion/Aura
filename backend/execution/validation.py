"""设备命令校验层（spec §3.3 六级顺序 + §2.2 online 不变式 + §10.2 十类失败码）。

所有设备变更——不论来自 UI / Agent / 场景脚本 / 规则降级——都必须过同一条校验路径
（§3.3）。本模块是这条路径的唯一实现与唯一失败词表来源：``validate_command`` 按固定
顺序逐级校验，命中即返回结构化 ``CommandFailure``（§10.2 码 + 可读原因 + 定位 details），
全部通过返回 None。executor 层（S1-T3/T4）在此之上叠加执行期失败（后三类码）与生命周期。

校验顺序（前者先于后者，多问题并存时报靠前的级）:
  1. device exists            → unknown_device            §3.3
  2. device online            → device_offline            §2.2 不变式（online=false 不可执行可写命令）
  3. capability exists        → capability_not_supported  §3.3（先类型级矩阵，再设备实例能力位）
  4. capability writable      → read_only_capability      §3.3
  5. value type               → invalid_value_type        §3.3
  6. value range / enum       → invalid_value_range       §3.3
  7. scenario policy          → policy_denied             §3.3（S1 给 permissive 默认，S2 §5.1 接入）
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from backend.execution.capability_matrix import CapabilitySpec, ValueType, get_capability_spec
from backend.engine.state import DeviceState, WorldState


class CommandErrorCode(str, Enum):
    """§10.2 十类失败语义。前七类由本校验层产出，后三类由 executor 执行期产出。"""

    # —— 校验期（本模块）——
    UNKNOWN_DEVICE = "unknown_device"
    DEVICE_OFFLINE = "device_offline"
    CAPABILITY_NOT_SUPPORTED = "capability_not_supported"
    READ_ONLY_CAPABILITY = "read_only_capability"
    INVALID_VALUE_TYPE = "invalid_value_type"
    INVALID_VALUE_RANGE = "invalid_value_range"
    POLICY_DENIED = "policy_denied"
    # —— 执行期（executor 层 S1-T3/T4；列于此以保 §10.2 词表单一来源）——
    EXECUTION_TIMEOUT = "execution_timeout"
    # 【保留位，S1 无产出方】§10.2 词表完整性要求它在枚举里，但当前同步 StateManager 下
    # apply 立即返回，唯一的反馈失败路径（executor 超预算）报 execution_timeout。
    # 研究者不要等这个码：S2/S3 引入异步 episode、反馈与下发解耦后才会有真实产出方
    # （"apply 成功但没拿到任何 delta"）。改动时必须同步 docs/architecture/ws-protocol.md。
    STATE_FEEDBACK_MISSING = "state_feedback_missing"
    SUPERSEDED_BY_NEWER_COMMAND = "superseded_by_newer_command"


class CommandFailure(BaseModel):
    """一次校验/执行失败的结构化描述（不可变）。

    ``code`` 取 §10.2 枚举；``message`` 面向研究者可读；``details`` 承载 device_id/capability/
    value 等定位字段，供可观测性 UI 与失败事件 data 直接消费。
    """

    model_config = ConfigDict(frozen=True)

    code: CommandErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ScenarioPolicy(Protocol):
    """场景策略接口（§3.3 第 6 级）。

    字段口径对齐 spec §5.1 / §12.2 的 allowed / forbidden devices，以缩小 S2 ScenarioSpec
    接入时的改动面：策略只按 (device_id, capability) 判定，放行返回 None，否则返回拒绝原因。
    """

    def check(self, device_id: str, capability: str) -> str | None:
        ...


class PermissivePolicy:
    """S1 默认策略：不设限、全部放行。S2 的 ScenarioSpec §5.1 会以 forbidden_device_ids 收紧。"""

    def check(self, device_id: str, capability: str) -> str | None:  # noqa: D401
        return None


class ForbiddenDevicePolicy:
    """按 §5.1/§12.2 ``forbidden_device_ids`` 判定：命中黑名单即拒。

    ScenarioSpec 落地前的最小可用实现，也是 S1 校验测试的策略桩；S2 直接以其为接入点。
    """

    def __init__(self, forbidden_device_ids: Iterable[str]) -> None:
        self.forbidden_device_ids = frozenset(forbidden_device_ids)

    def check(self, device_id: str, capability: str) -> str | None:
        if device_id in self.forbidden_device_ids:
            return f"设备 {device_id} 在当前场景策略下被禁止操作"
        return None


def device_is_online(device: DeviceState) -> bool:
    """§2.2：online=false 的设备不能执行可写命令。

    online 语义位当前镜像在 ``state.extra.online``（camera 显式写入，其余设备缺省），
    字段缺失一律视为在线，避免多数无该位的设备被误判离线。
    """

    return bool(device.state.extra.get("online", True))


def _value_type_matches(value_type: ValueType, value: Any) -> bool:
    """值是否满足能力声明的标量类型（§3.2 值契约）。"""

    if value_type == "bool":
        # Python 中 True/False 才是 bool；int 1 不算 bool，避免 0/1 被当开关。
        return isinstance(value, bool)
    if value_type == "int":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        # 前端/JSON 常把整数发成 float；整数值的 float 视为合法 int。
        return isinstance(value, float) and value.is_integer()
    if value_type == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "enum":
        return isinstance(value, str)
    # metadata 只读，永不到达类型校验（第 4 级 writable 已拦截）。
    return False


def _value_range_error(spec: CapabilitySpec, value: Any) -> str | None:
    """值是否落在能力的闭区间或枚举内；越界/非法枚举返回可读原因，否则 None。"""

    if spec.value_type == "enum":
        if spec.enum_values is not None and value not in spec.enum_values:
            return f"{spec.name} 仅接受 {list(spec.enum_values)}，收到 {value!r}"
        return None
    if spec.min is not None and value < spec.min:
        return f"{spec.name} 低于下界 {spec.min}（收到 {value}）"
    if spec.max is not None and value > spec.max:
        return f"{spec.name} 超出上界 {spec.max}（收到 {value}）"
    return None


def validate_command(
    world: WorldState,
    device_id: str,
    capability: str,
    value: Any = None,
    *,
    policy: ScenarioPolicy | None = None,
) -> CommandFailure | None:
    """六级顺序校验一条设备命令；通过返回 None，否则返回 §10.2 结构化失败。

    ``policy`` 缺省视为 permissive（不设策略约束）；传入即在第 7 级参与判定。
    """

    # 1. device exists（§3.3）
    device = world.devices.get(device_id)
    if device is None:
        return CommandFailure(
            code=CommandErrorCode.UNKNOWN_DEVICE,
            message=f"设备 {device_id} 不存在",
            details={"device_id": device_id, "capability": capability},
        )

    # 2. device online（§2.2 不变式）
    if not device_is_online(device):
        return CommandFailure(
            code=CommandErrorCode.DEVICE_OFFLINE,
            message=f"设备 {device_id} 离线，无法执行可写命令",
            details={"device_id": device_id, "capability": capability},
        )

    # 3. capability exists（§3.3）——先看类型级矩阵（值契约的唯一来源）
    spec = get_capability_spec(device.type, capability)
    if spec is None:
        return CommandFailure(
            code=CommandErrorCode.CAPABILITY_NOT_SUPPORTED,
            message=f"{device.type} 设备不支持能力 {capability}",
            details={"device_id": device_id, "capability": capability},
        )

    # 3b. 实例级能力位（§3.2/§3.3）：类型矩阵只是上界，设备自己声明的 capabilities 才是
    # 这台设备的实际契约——一盏没登记 color_temp 的灯不该因为"light 类型有这条"而被放行。
    # S2 场景会大量生成能力位裁剪过的设备，这一级是它们唯一的兜底。
    # 能力位为空视为"未枚举"，回退到类型级声明（最小构造的设备/旧夹具不填这个字段）。
    if device.capabilities and capability not in device.capabilities:
        return CommandFailure(
            code=CommandErrorCode.CAPABILITY_NOT_SUPPORTED,
            message=f"设备 {device_id} 未声明能力 {capability}",
            details={"device_id": device_id, "capability": capability},
        )

    # 4. capability writable（§3.3）
    if not spec.writable:
        return CommandFailure(
            code=CommandErrorCode.READ_ONLY_CAPABILITY,
            message=f"能力 {capability} 只读，不能被命令写入",
            details={"device_id": device_id, "capability": capability},
        )

    # 5. value type（§3.3）
    if not _value_type_matches(spec.value_type, value):
        return CommandFailure(
            code=CommandErrorCode.INVALID_VALUE_TYPE,
            message=f"能力 {capability} 期望 {spec.value_type} 值，收到 {type(value).__name__}",
            details={"device_id": device_id, "capability": capability, "value": value},
        )

    # 6. value range / enum（§3.3）
    range_error = _value_range_error(spec, value)
    if range_error is not None:
        return CommandFailure(
            code=CommandErrorCode.INVALID_VALUE_RANGE,
            message=range_error,
            details={"device_id": device_id, "capability": capability, "value": value},
        )

    # 7. scenario policy（§3.3；S1 默认 permissive，S2 §5.1 接入）
    if policy is not None:
        denial = policy.check(device_id, capability)
        if denial is not None:
            return CommandFailure(
                code=CommandErrorCode.POLICY_DENIED,
                message=denial,
                details={"device_id": device_id, "capability": capability},
            )

    return None
