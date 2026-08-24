"""§2.3 ground truth → observable 投影。

规格把"世界真实发生了什么"和"agent 能看见什么"划成两层，并要求 **agent 默认只消费
observable**、评估侧才对照 ground truth。S2 之前这条界线在代码里根本不存在：
``_run_episode`` 把 ``world.snapshot()`` 直接递给 agent，等于宣布"感知是完美的"——
研究者因此无法评估感知受限下的策略质量，而这正是本平台的卖点之一。

实现范围刻意保持很小：``perfect`` 是身份深拷贝，``stale_offline`` 在设备离线时回放
最后一次有效报告。高斯噪声、随机丢包和通用延迟 DSL 均不在这里实现。两个条件都不消费
随机源，因此模型身份和每一帧证据可以确定性地内容寻址。

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

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from backend.engine.event_bus import SimEvent
from backend.engine.provenance import ObservationCondition
from backend.engine.state import DeviceState, DeviceStateValues, WorldState

__all__ = [
    "OBSERVATION_STALE_KEY",
    "OBSERVATION_UNAVAILABLE_KEY",
    "OBSERVATION_CONTRACT_VERSION",
    "ObservationFrame",
    "ObservableProjector",
    "build_observable_view",
    "device_reports_online",
    "observation_model_metadata",
]

# 观测侧的陈旧标记键。它只出现在 **投影** 的 device.state.extra 里，绝不写回世界——
# ground truth 没有"我的读数是旧的"这种字段，那是观测者的判断而不是设备的物理状态。
OBSERVATION_STALE_KEY = "observation_stale"
OBSERVATION_UNAVAILABLE_KEY = "observation_unavailable"
OBSERVATION_CONTRACT_VERSION = "1.0"

# online 语义位当前镜像在 state.extra.online（见 backend/execution/validation.py::
# device_is_online）。这里刻意不 import 那个函数：execution 是命令层，engine 反向依赖
# 它会让"读世界"绕道"发命令"的包。两处口径必须一致，改动时一起改。
_ONLINE_KEY = "online"

_MODEL_IDS: Mapping[ObservationCondition, str] = {
    # One implemented observation-model family with two independently sealed
    # conditions. ScenarioSpec/current artifacts already name this family.
    ObservationCondition.PERFECT: "current_projector_v1",
    ObservationCondition.STALE_OFFLINE: "current_projector_v1",
}

_MISSING = object()
_TRANSPORT_ID_KEYS = frozenset(
    {
        "event_id",
        "trigger_event_id",
        "caused_by_event_id",
        "correlation_id",
        "causal_parent",
        "run_id",
        "command_id",
        "operation_id",
    }
)


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _stable_observed_event_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_observed_event_data(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if key not in _TRANSPORT_ID_KEYS and key != "wall_time"
        }
    if isinstance(value, (list, tuple)):
        return [_stable_observed_event_data(item) for item in value]
    return value


def observation_model_metadata(
    condition: ObservationCondition | str,
) -> dict[str, Any]:
    """Return the canonical semantic identity of one implemented projector."""

    resolved = ObservationCondition(condition)
    metadata: dict[str, Any] = {
        "contract_version": OBSERVATION_CONTRACT_VERSION,
        "condition": resolved.value,
        "model_id": _MODEL_IDS[resolved],
        "event_state_projection": "device_report_values_v1",
    }
    if resolved is ObservationCondition.PERFECT:
        metadata.update(
            device_state_policy="current_ground_truth_copy",
            liveness_policy="current",
            cold_start_policy="not_applicable",
        )
    else:
        metadata.update(
            device_state_policy="last_online_report",
            liveness_policy="current",
            cold_start_policy="unavailable",
        )
    metadata["model_hash"] = hashlib.sha256(
        _canonical_json(metadata).encode("utf-8")
    ).hexdigest()
    return metadata


@dataclass(frozen=True)
class ObservationFrame:
    """One immutable, content-addressed perception boundary for an episode.

    Agents receive the copied ``observable_world`` and ``observed_root_event``.
    Evidence is captured before either object is handed to controller code, so
    an accidental mutation cannot rewrite what the run claims was perceived.
    """

    observable_world: WorldState = field(repr=False)
    observed_root_event: SimEvent = field(repr=False)
    condition: ObservationCondition
    model_id: str
    contract_version: str
    model_hash: str
    captured_at_sim_time_s: float
    stale_device_ids: tuple[str, ...] = ()
    unavailable_device_ids: tuple[str, ...] = ()
    _evidence_preimage: dict[str, Any] = field(default_factory=dict, repr=False)
    frame_hash: str = ""

    def evidence_preimage(self) -> dict[str, Any]:
        return copy.deepcopy(self._evidence_preimage)

    def observable_snapshot(self) -> dict[str, Any]:
        snapshot = self._evidence_preimage.get("observable_snapshot", {})
        return copy.deepcopy(snapshot) if isinstance(snapshot, dict) else {}


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

    def __init__(
        self,
        condition: ObservationCondition | str = ObservationCondition.STALE_OFFLINE,
    ) -> None:
        self.condition = ObservationCondition(condition)
        self._model_metadata = observation_model_metadata(self.condition)
        # device_id → 最后一次在线时的 state 深拷贝。只在设备在线时更新。
        self._last_reported: dict[str, DeviceStateValues] = {}
        self._stale_device_ids: tuple[str, ...] = ()
        self._unavailable_device_ids: tuple[str, ...] = ()

    # ------------------------------------------------------------------ 查询

    @property
    def stale_device_ids(self) -> tuple[str, ...]:
        """上一次 :meth:`observe` 中读数被判为陈旧的设备（id 升序）。"""

        return self._stale_device_ids

    @property
    def unavailable_device_ids(self) -> tuple[str, ...]:
        return self._unavailable_device_ids

    @property
    def model_metadata(self) -> dict[str, Any]:
        return copy.deepcopy(self._model_metadata)

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
        self._unavailable_device_ids = ()

    def forget(self, device_ids: Iterable[str]) -> None:
        for device_id in device_ids:
            self._last_reported.pop(device_id, None)

    def observe(self, world: WorldState) -> WorldState:
        """把 ground truth 投影成 agent 可见的世界（返回值是独立深拷贝）。"""

        observable = world.snapshot()
        if self.condition is ObservationCondition.PERFECT:
            self._stale_device_ids = ()
            self._unavailable_device_ids = ()
            return observable

        stale: list[str] = []
        unavailable: list[str] = []

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
                # 从未在线过：不能把当前物理值冒充成设备报告。WorldState 还没有
                # Optional device fields，因此使用显式 unavailable state；消费者必须看
                # marker，而不是把默认 power=False 当成一次真实读数。
                unavailable.append(device_id)
                observed.state = DeviceStateValues(
                    power=False,
                    last_changed_by="observation_unavailable",
                    extra={
                        _ONLINE_KEY: ground_truth_device.state.extra.get(
                            _ONLINE_KEY, False
                        ),
                        OBSERVATION_UNAVAILABLE_KEY: True,
                    },
                )
            observed.state.extra[OBSERVATION_STALE_KEY] = True

        self._stale_device_ids = tuple(stale)
        self._unavailable_device_ids = tuple(unavailable)
        return observable

    def observe_frame(
        self,
        world: WorldState,
        root_event: SimEvent,
        *,
        captured_at_sim_time_s: float | None = None,
    ) -> ObservationFrame:
        """Project world and root-event channels into one sealed perception."""

        observable = self.observe(world)
        observed_event = self._project_root_event(root_event, world, observable)
        captured = (
            float(captured_at_sim_time_s)
            if captured_at_sim_time_s is not None
            else (
                float(root_event.sim_time_s)
                if root_event.sim_time_s is not None
                else float(world.simulation_tick) * float(world.simulated_dt_seconds)
            )
        )
        metadata = self.model_metadata
        preimage = {
            "observation_contract_version": metadata["contract_version"],
            "observation_condition": metadata["condition"],
            "observation_model_id": metadata["model_id"],
            "observation_model_hash": metadata["model_hash"],
            "captured_at_sim_time_s": captured,
            "observable_snapshot_projection": (
                "world_state_without_agent_diagnostics.v1"
            ),
            "observable_snapshot": observable.model_dump(
                mode="json", exclude={"agents"}
            ),
            # Transport identity/wall-clock fields are deliberately outside the
            # frame commitment. The perception event already carries the causal
            # edge; hashing UUIDs would make same-seed traces irreproducible.
            "observed_root_event": {
                "event_type": observed_event.event_type,
                "source": observed_event.source,
                "timestamp": observed_event.timestamp,
                "sim_time_s": observed_event.sim_time_s,
                "priority": observed_event.priority,
                "event_generation_mode": observed_event.event_generation_mode,
                "generation_rule_id": observed_event.generation_rule_id,
                "rng_stream": observed_event.rng_stream,
                "data": _stable_observed_event_data(observed_event.data),
            },
            "stale_device_ids": list(self._stale_device_ids),
            "unavailable_device_ids": list(self._unavailable_device_ids),
        }
        frame_hash = hashlib.sha256(_canonical_json(preimage).encode("utf-8")).hexdigest()
        return ObservationFrame(
            observable_world=observable,
            observed_root_event=observed_event,
            condition=self.condition,
            model_id=str(metadata["model_id"]),
            contract_version=str(metadata["contract_version"]),
            model_hash=str(metadata["model_hash"]),
            captured_at_sim_time_s=captured,
            stale_device_ids=self._stale_device_ids,
            unavailable_device_ids=self._unavailable_device_ids,
            _evidence_preimage=copy.deepcopy(preimage),
            frame_hash=frame_hash,
        )

    def _project_root_event(
        self,
        root_event: SimEvent,
        ground_truth: WorldState,
        observable: WorldState,
    ) -> SimEvent:
        """Remove device-state values that would bypass the world projection."""

        projected = root_event.model_copy(deep=True)
        if self.condition is ObservationCondition.PERFECT:
            return projected
        # A user command carries a desired value, not a sensor report. Rewriting
        # it would change the task rather than limit observation.
        if projected.event_type == "user.command":
            return projected
        device_id = projected.data.get("device_id")
        if not isinstance(device_id, str):
            return projected
        observed_device = observable.devices.get(device_id)
        physical_device = ground_truth.devices.get(device_id)
        if observed_device is None or physical_device is None:
            for key in ("state", "device_state", "value", "new_value", "old_value"):
                projected.data.pop(key, None)
            return projected
        if device_reports_online(physical_device):
            # Online values are current reports in stale_offline as well. Keep
            # transition fields such as old_value/new_value intact instead of
            # collapsing both to the post-event snapshot.
            return projected

        data = projected.data
        if "state" in data:
            data["state"] = observed_device.state.model_dump(mode="json")
        if "device_state" in data:
            data["device_state"] = observed_device.state.model_dump(mode="json")
        if "online" in data:
            data["online"] = observed_device.state.extra.get(_ONLINE_KEY, True)
        if "power" in data:
            data["power"] = observed_device.state.power

        for key in physical_device.state.extra:
            if key not in data:
                continue
            if key in observed_device.state.extra:
                data[key] = copy.deepcopy(observed_device.state.extra[key])
            else:
                data.pop(key, None)

        state_path = data.get("path") or data.get("property")
        observed_value = ObservableProjector._read_observed_device_value(
            observed_device,
            device_id=device_id,
            state_path=state_path,
        )
        value_keys = ("value", "new_value", "old_value", "previous_value")
        if state_path is not None:
            for key in value_keys:
                if key not in data:
                    continue
                if observed_value is _MISSING:
                    data.pop(key, None)
                else:
                    data[key] = copy.deepcopy(observed_value)
        elif any(key in data for key in value_keys):
            # Device-associated raw values with no declared path cannot be
            # safely mapped to the observable state, so fail closed.
            for key in value_keys:
                data.pop(key, None)
        return projected

    @staticmethod
    def _read_observed_device_value(
        device: DeviceState,
        *,
        device_id: str,
        state_path: Any,
    ) -> Any:
        if not isinstance(state_path, str) or not state_path:
            return _MISSING
        path = state_path
        prefix = f"devices[{device_id}].state."
        if path.startswith(prefix):
            path = path[len(prefix) :]
        elif path.startswith("state."):
            path = path[len("state.") :]
        if path == "power":
            return device.state.power
        if path == "last_changed_by":
            return device.state.last_changed_by
        if path.startswith("extra."):
            return device.state.extra.get(path[len("extra.") :], _MISSING)
        return device.state.extra.get(path, _MISSING)


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
