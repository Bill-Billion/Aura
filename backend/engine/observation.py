"""§2.3 ground truth → observable 投影。

规格把"世界真实发生了什么"和"agent 能看见什么"划成两层，并要求 **agent 默认只消费
observable**、评估侧才对照 ground truth。S2 之前这条界线在代码里根本不存在：
``_run_episode`` 把 ``world.snapshot()`` 直接递给 agent，等于宣布"感知是完美的"——
研究者因此无法评估感知受限下的策略质量，而这正是本平台的卖点之一。

**本期范围（S2 评审裁定的缩减）**：只实现 *身份投影 + 离线设备陈旧快照*，即把分离这
件事和它的管线做出来。高斯噪声/取整/延迟/丢失全部推迟到 S4-T3 的漂移注入器——那一期
需要的前提只是"分离已经存在"，不需要 S2 先造一套噪声模型（造了也会在 S4 被重写）。
因此本模块**不消费任何随机源**：``build_observable_view`` 是纯函数，
:class:`ObservableProjector` 的唯一状态是"每台设备最后一次在线时的读数"。

投影规则（当前两条）：

1. **默认恒等**。observable 与 ground truth 逐字段相等，只是换成一份深拷贝——
   agent 拿到的绝不能是能改世界的引用。恒等是有意的：它把 S1 既有测试的回归面压到零。
2. **离线设备只报最后一次在线读数**（§2.3 "Device-reported state, which may be stale
   or inconsistent if a device is offline"）。可达性本身仍然可观测：``extra.online``
   保持 ``False`` 并额外打上 :data:`OBSERVATION_STALE_KEY` 标——agent 必须能分辨
   "读数是旧的"，否则陈旧就变成了另一种静默失败。

刻意的形状决定：投影结果仍是 :class:`~backend.engine.state.WorldState`，不是新类型。
agent 的 ``serialize_device_for_llm`` / ``build_world_summary`` / ``is_relevant`` 全部
按 WorldState 写成，换类型会把 S3 的改动面放大一个数量级，收益却只有"类型上禁止误用"
——而误用的真正防线是 ``AgentRuntime`` 那条唯一入口（见 ``observable_world()``）。
"""

from __future__ import annotations

from typing import Iterable

from backend.engine.state import DeviceState, DeviceStateValues, WorldState

__all__ = [
    "OBSERVATION_STALE_KEY",
    "ObservableProjector",
    "build_observable_view",
    "device_reports_online",
]

# 观测侧的陈旧标记键。它只出现在 **投影** 的 device.state.extra 里，绝不写回世界——
# ground truth 没有"我的读数是旧的"这种字段，那是观测者的判断而不是设备的物理状态。
OBSERVATION_STALE_KEY = "observation_stale"

# online 语义位当前镜像在 state.extra.online（见 backend/execution/validation.py::
# device_is_online）。这里刻意不 import 那个函数：execution 是命令层，engine 反向依赖
# 它会让"读世界"绕道"发命令"的包。两处口径必须一致，改动时一起改。
_ONLINE_KEY = "online"


def device_reports_online(device: DeviceState) -> bool:
    """设备是否仍在汇报（缺失 ``extra.online`` 一律视为在线）。

    与 ``backend.execution.validation.device_is_online`` 同口径：多数设备没有这个位，
    默认离线会让整个世界一夜之间不可观测。
    """

    return bool(device.state.extra.get(_ONLINE_KEY, True))


class ObservableProjector:
    """有记忆的观测者：记住每台设备最后一次在线时的读数，离线期间反复回放它。

    为什么必须有状态：陈旧语义天生需要历史。无状态的一次性投影只能说"这台设备离线"，
    说不出"它离线前最后一次报的是 brightness=80"——而后者才是 agent 真正会据以决策的
    东西，也是研究者评估感知受限时要看的东西。

    生命周期：一个 run 一台。世界被换掉（reset / 场景连跑）时必须 :meth:`reset`，
    否则上一个 run 的读数会被当成本 run 的"最后一次汇报"回放出来——这与 S1 根治的
    reset 污染是同一类缺陷。``AgentRuntime.update_state_manager`` 已经替调用方做了这件事。
    """

    def __init__(self) -> None:
        # device_id → 最后一次在线时的 state 深拷贝。只在设备在线时更新。
        self._last_reported: dict[str, DeviceStateValues] = {}
        self._stale_device_ids: tuple[str, ...] = ()

    # ------------------------------------------------------------------ 查询

    @property
    def stale_device_ids(self) -> tuple[str, ...]:
        """上一次 :meth:`observe` 中读数被判为陈旧的设备（id 升序）。"""

        return self._stale_device_ids

    def last_reported_state(self, device_id: str) -> DeviceStateValues | None:
        """某设备最后一次在线时的读数副本；从未在线过则为 None。"""

        cached = self._last_reported.get(device_id)
        return cached.model_copy(deep=True) if cached is not None else None

    def known_device_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._last_reported))

    # ------------------------------------------------------------------ 变更

    def reset(self) -> None:
        """丢弃全部观测历史（换世界/换 run 时必须调用）。"""

        self._last_reported.clear()
        self._stale_device_ids = ()

    def forget(self, device_ids: Iterable[str]) -> None:
        for device_id in device_ids:
            self._last_reported.pop(device_id, None)

    def observe(self, world: WorldState) -> WorldState:
        """把 ground truth 投影成 agent 可见的世界（返回值是独立深拷贝）。"""

        observable = world.snapshot()
        stale: list[str] = []

        # 按 id 升序遍历：投影本身不依赖顺序，但让 stale_device_ids 与日志稳定可比。
        for device_id in sorted(observable.devices):
            ground_truth_device = world.devices[device_id]
            if device_reports_online(ground_truth_device):
                # 设备在线 = 本次汇报有效，刷新"最后一次读数"。
                self._last_reported[device_id] = ground_truth_device.state.model_copy(deep=True)
                continue

            stale.append(device_id)
            cached = self._last_reported.get(device_id)
            observed = observable.devices[device_id]
            if cached is not None:
                # 回放最后一次在线读数，但 online 位取当前值：可达性是观测得到的，
                # 读数不是。二者一起冻结会让 agent 永远以为设备还在线。
                replay = cached.model_copy(deep=True)
                replay.extra[_ONLINE_KEY] = ground_truth_device.state.extra.get(
                    _ONLINE_KEY, False
                )
                observed.state = replay
            else:
                # 从未在线过：没有历史可回放。此时只能标陈旧，绝不编造读数
                # （编一份"合理的"初值＝把 ground truth 伪装成观测结果）。
                observed.state = ground_truth_device.state.model_copy(deep=True)
            observed.state.extra[OBSERVATION_STALE_KEY] = True

        self._stale_device_ids = tuple(stale)
        return observable


def build_observable_view(
    world: WorldState,
    *,
    projector: ObservableProjector | None = None,
) -> WorldState:
    """一次性投影入口（§2.3）。

    ``projector=None`` 时用一台临时观测者：没有历史，因此离线设备只会被标陈旧而不会
    回放旧读数。长期运行的调用方（AgentRuntime / S2-T6 场景 runner / S3 编排器）应当
    持有一台 :class:`ObservableProjector` 并调用 :meth:`ObservableProjector.observe`。

    ``noise_model`` / ``rng`` 参数**有意缺席**：S2 评审把噪声推迟到 S4-T3 的漂移注入器。
    届时的接法是给 :class:`ObservableProjector` 注入一条 ``observation_noise``
    SimStream（见 backend/engine/rng.py），而不是给本函数加可选参数——噪声同样需要
    "上一次读数"这份历史，无状态函数装不下它。
    """

    return (projector or ObservableProjector()).observe(world)
