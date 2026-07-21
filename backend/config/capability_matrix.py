"""能力矩阵（spec §3.2）：设备控制的唯一值契约来源。

Agent / UI / 场景脚本 / 规则降级四来源的命令都必须从这里读能力语义，而不是从
设备 type 猜。S1-T2 的六级校验（设备存在 / 能力存在 / 能力可写 / 值类型 / 值范围或
枚举 / 场景策略）逐条消费本模块的访问器，因此矩阵是数据而非散落在校验分支里的常量。

value_type 与 effect_class 的取值边界见 spec §3.2 表与 §3.4 效果模型声明要求
（每条能力必须声明影响物理状态 / 体感舒适 / 安防覆盖 / 仅 UI 可观测中的哪一类）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from backend.engine.state import DeviceType


# §3.2 表里写口能力只有 bool/int/float/enum 四种；只读的 camera.view 是不透明预览
# 元数据，用 metadata 表示——它永不走写入值校验，仅承载能力语义。
ValueType = Literal["bool", "int", "float", "enum", "metadata"]

# §3.4 要求每条效果声明其作用域：物理状态 / 体感舒适 / 安防覆盖 / 仅 UI 可观测。
EffectClass = Literal[
    "physical",
    "perceived_comfort",
    "security_coverage",
    "ui_observability",
]


class CapabilitySpec(BaseModel):
    """单条能力的值契约（不可变）。S1-T2 校验器只读、不改这些字段。"""

    model_config = ConfigDict(frozen=True)

    name: str
    value_type: ValueType
    writable: bool
    effect_class: EffectClass
    # 数值型能力的闭区间边界（含端点）；bool/enum/metadata 恒为 None。
    min: float | None = None
    max: float | None = None
    # enum 型能力的合法取值（有序）；非 enum 恒为 None。
    enum_values: tuple[str, ...] | None = None
    # 面向可观测性 UI 的单位标注（如 %/K/°C/min）；无量纲能力为 None。
    unit: str | None = None


# spec §3.2 Capability Matrix —— 逐行落数据，顺序与表一致。
# 键为 DeviceType；值为该类型能力 spec 的有序元组。
CAPABILITY_MATRIX: dict[DeviceType, tuple[CapabilitySpec, ...]] = {
    "light": (
        CapabilitySpec(
            name="power", value_type="bool", writable=True, effect_class="physical"
        ),
        CapabilitySpec(
            name="brightness",
            value_type="int",
            writable=True,
            effect_class="physical",
            min=0,
            max=100,
            unit="%",
        ),
        CapabilitySpec(
            name="color_temp",
            value_type="int",
            writable=True,
            effect_class="perceived_comfort",
            min=2200,
            max=6500,
            unit="K",
        ),
    ),
    "hvac": (
        CapabilitySpec(
            name="power", value_type="bool", writable=True, effect_class="physical"
        ),
        CapabilitySpec(
            name="target_temp",
            value_type="float",
            writable=True,
            effect_class="physical",
            min=16,
            max=30,
            unit="°C",
        ),
        CapabilitySpec(
            name="mode",
            value_type="enum",
            writable=True,
            effect_class="physical",
            enum_values=("cool", "heat", "fan", "dry", "auto"),
        ),
        CapabilitySpec(
            name="speed",
            value_type="enum",
            writable=True,
            effect_class="physical",
            enum_values=("low", "medium", "high", "auto"),
        ),
    ),
    "curtain": (
        CapabilitySpec(
            name="open_percent",
            value_type="int",
            writable=True,
            effect_class="physical",
            min=0,
            max=100,
            unit="%",
        ),
    ),
    "fan": (
        CapabilitySpec(
            name="power",
            value_type="bool",
            writable=True,
            effect_class="perceived_comfort",
        ),
        CapabilitySpec(
            name="speed",
            value_type="enum",
            writable=True,
            effect_class="perceived_comfort",
            enum_values=("low", "medium", "high"),  # 风扇无 auto 档
        ),
        CapabilitySpec(
            name="shake",
            value_type="bool",
            writable=True,
            effect_class="perceived_comfort",
        ),
        CapabilitySpec(
            name="timeout",
            value_type="int",
            writable=True,
            effect_class="perceived_comfort",
            min=0,
            max=240,
            unit="min",
        ),
    ),
    "camera": (
        CapabilitySpec(
            name="view",
            value_type="metadata",
            writable=False,
            effect_class="ui_observability",
        ),
        # §3.2：online 默认不可写；能力语义在此声明，设备侧仍以 extra.online 镜像
        # 防前端断链（DeviceCapability Literal + 注册表能力位补录留待后续任务）。
        CapabilitySpec(
            name="online",
            value_type="bool",
            writable=False,
            effect_class="security_coverage",
        ),
    ),
    "sensor": (
        CapabilitySpec(
            name="read",
            value_type="float",
            writable=False,
            effect_class="ui_observability",
        ),
    ),
}


def all_device_types() -> tuple[DeviceType, ...]:
    """矩阵覆盖的全部设备类型。"""

    return tuple(CAPABILITY_MATRIX.keys())


def get_capability_specs(device_type: DeviceType) -> tuple[CapabilitySpec, ...]:
    """返回某设备类型的全部能力 spec（有序）；未知类型返回空元组。"""

    return CAPABILITY_MATRIX.get(device_type, ())


def get_capability_spec(
    device_type: DeviceType, capability: str
) -> CapabilitySpec | None:
    """按 (设备类型, 能力名) 精确取 spec；不存在返回 None。"""

    for spec in CAPABILITY_MATRIX.get(device_type, ()):
        if spec.name == capability:
            return spec
    return None


def is_writable_capability(device_type: DeviceType, capability: str) -> bool:
    """该能力是否存在且可写；不存在或只读均为 False。"""

    spec = get_capability_spec(device_type, capability)
    return bool(spec and spec.writable)


def get_writable_capability_names(device_type: DeviceType) -> frozenset[str]:
    """某设备类型的可写能力名集合（供 set_state 白名单校验消费）。"""

    return frozenset(
        spec.name for spec in CAPABILITY_MATRIX.get(device_type, ()) if spec.writable
    )


def read_only_capability_names() -> frozenset[str]:
    """跨全部类型聚合的只读能力名集合（当前为 view/online/read）。"""

    return frozenset(
        spec.name
        for specs in CAPABILITY_MATRIX.values()
        for spec in specs
        if not spec.writable
    )
