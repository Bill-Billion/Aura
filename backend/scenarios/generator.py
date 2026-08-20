"""§4.5 三种事件生成模式：scripted / rule_based / stochastic（S2-T6）。

在 S2 之前，"事件从哪来"这个问题在代码里没有答案：UserBehaviorSimulator 按半小时槽位
硬编码日程、EnvironmentSimulator 每 tick 无条件刷一条 ``environment.state_refresh``，
两者都不标注自己的出处。研究者看着一条事件流无法回答 §4.5 末段那个问题——**这次行为
是脚本安排的、规则推出来的，还是噪声抽出来的**。本模块把三条产线显式化：

  - :class:`ScriptedEventSource`  —— 按 ``ScenarioSpec.timeline`` 的 ``at``（模拟秒）到点发根事件；
  - :class:`RuleBasedEventSource` —— 收编硬编码日程 + 温度/光照阈值规则，每条事件盖 ``generation_rule_id``；
  - :class:`StochasticEventSource`—— 由 T1 的命名随机子流驱动，每条事件盖 ``rng_stream``。

四条不可退让的设计约束：

1. **零执行权**。三个源都是**纯生产者**：``emit()`` 只返回 :class:`GeneratedEvent`，
   既不 publish 也不写世界。发布、下命令、写世界一律由 SimulationEngine 统一编排
   （``_dispatch_generated``）——源自己动手就等于又开了一条绕过 executor 的旁路。
2. **timeline 的设备变更必须走 CommandExecutor**（critic 修正①）。脚本直控产出的是
   ``DeviceCommand(source=scenario)``，与 UI 直控同一条六级校验 + 十态生命周期；
   **没有** state_manager 兜底路径，也没有 TODO 分支。
3. **根事件与物理触发分栏**（critic 修正②）。§4.1 富事件会开启新的 agent episode，
   所以 ``causal_parent`` 必须为空；真正把读数推过阈值的 tick / 用户事件记在
   ``data.trigger_event_id``。这样既不把根伪装成别人的子节点，也不丢物理归因。
4. **stochastic 只做 device.offline/recovered**（critic 修正④的 MVP 收缩）。传感器漂移
   归 S4-T3 的观测噪声，用户改主意归 S3。

``device.offline`` 为什么**不**走 executor：§3.2 把 ``online`` 声明为不可写能力
（``capability_matrix.py``），它是设备可用性这一"世界事实"，不是可下发的控制点。让它
走命令路径只会拿到一条 ``read_only_capability`` 失败。因此它以
:class:`DeviceAvailabilityWrite` 的形式交给引擎，按仿真写入落到世界上（§13 故障注入口径）。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from backend.engine.event_bus import EventGenerationMode, SimEvent
from backend.engine.event_types import (
    DEVICE_OFFLINE,
    DEVICE_RECOVERED,
    ENVIRONMENT_LIGHT_LEVEL_THRESHOLD,
    ENVIRONMENT_TEMPERATURE_THRESHOLD,
    OUTSIDE_ROOM_ID,
    USER_ACTIVITY_CHANGE,
    USER_ARRIVES_HOME,
    USER_ENDS_ACTIVITY,
    USER_ENTERS_ROOM,
    USER_EXITS_ROOM,
    USER_LEAVES_HOME,
    USER_STARTS_ACTIVITY,
)
from backend.engine.rng import RngStream, SimRandom, SimStream
from backend.engine.state import WorldState
from backend.execution.command import CommandSource, DeviceCommand
from backend.scenarios.spec import ScenarioSpec, TimelineEvent
from backend.simulators.user_behavior import UserBehaviorSimulator

__all__ = [
    "SCRIPTED_EVENT_SOURCE",
    "RULE_EVENT_SOURCE",
    "STOCHASTIC_EVENT_SOURCE",
    "FAILURE_INJECTION_CAUSED_BY",
    "DEFAULT_TEMPERATURE_COMFORT_RANGE",
    "DEFAULT_LIGHT_LEVEL_THRESHOLD",
    "DEFAULT_OFFLINE_PROBABILITY",
    "DEFAULT_RECOVERY_PROBABILITY",
    "GenerationContext",
    "DeviceAvailabilityWrite",
    "GeneratedEvent",
    "EventSource",
    "ScriptedEventSource",
    "RuleBasedEventSource",
    "StochasticEventSource",
    "GenerationSources",
    "build_generation_sources",
    "scenario_timeline_device_entries",
    "timeline_device_command",
]


# 事件 ``source`` 字段（"谁发的"），与 ``event_generation_mode``（"怎么生成的"）正交。
SCRIPTED_EVENT_SOURCE = "scenario_script"
RULE_EVENT_SOURCE = "rule_engine"
STOCHASTIC_EVENT_SOURCE = "stochastic_source"
# 设备可用性写入的归因标签（§13 故障注入）。刻意与 SIMULATION_WRITE_SOURCES 里的
# environment_sim 区分：掉线不是物理演化，是注入的故障，工件里必须看得出差别。
FAILURE_INJECTION_CAUSED_BY = "failure_injector"

# 规则阈值默认值。它们是"可调常量"而不是物理常数，故显式命名并允许场景覆盖。
DEFAULT_TEMPERATURE_COMFORT_RANGE: tuple[float, float] = (18.0, 28.0)
DEFAULT_LIGHT_LEVEL_THRESHOLD = 120.0
# 默认不注入故障：噪声必须是场景**显式声明**的实验条件，不是所有 run 的隐形背景。
DEFAULT_OFFLINE_PROBABILITY = 0.0
DEFAULT_RECOVERY_PROBABILITY = 0.25


@dataclass(frozen=True)
class GenerationContext:
    """一次 run 的生成上下文：所有生成事件都盖这两个章（§4.5 前两项元数据）。

    每开一个 run 换一份（run_id 变了就是另一次实验）；源自己不去问"当前 run 是哪个"，
    否则一条在飞事件会在换 run 后被改写成新 run 的东西——正是 §2.2 stale 判定要防的。
    """

    run_id: str | None = None
    scenario_id: str | None = None


@dataclass(frozen=True)
class DeviceAvailabilityWrite:
    """设备可用性（``state.extra.online``）的仿真侧写入意图。

    不是命令：§3.2 声明 ``online`` 不可写，命令路径会（正确地）拒绝它。由引擎按
    ``caused_by=failure_injector`` 落到世界上，delta 照常可归因到本事件。
    """

    device_id: str
    online: bool
    reason: str = ""


@dataclass(frozen=True)
class GeneratedEvent:
    """一条生成事件及其"世界后果"。三个字段的关系是刻意的：

    - ``event`` 恒有：先有根事件，后有后果（§4.4 根事件必须先于其派生事件可见）；
    - ``device_command`` 走 CommandExecutor（唯一 apply_action 落点）；
    - ``availability_write`` 走仿真写入（不可写能力，见模块 docstring）。
    """

    event: SimEvent
    device_command: DeviceCommand | None = None
    availability_write: DeviceAvailabilityWrite | None = None


# --------------------------------------------------------------------- 基类


class EventSource(ABC):
    """事件生成源的公共契约（纯生产者）。"""

    mode: EventGenerationMode
    source_name: str

    def __init__(self, *, context: GenerationContext | None = None) -> None:
        self.context = context or GenerationContext()

    @abstractmethod
    def emit(
        self,
        world: WorldState,
        *,
        trigger: SimEvent | None,
        sim_time_s: float,
    ) -> list[GeneratedEvent]:
        """产出本步的事件；无事发生返回空列表。绝不 publish、绝不写世界。"""

    def reset(self) -> None:
        """换 run / reset：清掉迟滞等跨步状态。默认无状态源无需覆写。"""

    # -- 内部工具 ------------------------------------------------------

    def _build_event(
        self,
        *,
        event_type: str,
        data: dict[str, Any],
        sim_time_s: float,
        tick: float,
        causal_parent: str | None = None,
        priority: int = 2,
        generation_rule_id: str | None = None,
        rng_stream: str | None = None,
    ) -> SimEvent:
        return SimEvent(
            event_type=event_type,
            source=self.source_name,
            timestamp=float(tick),
            wall_time=time.time(),
            causal_parent=causal_parent,
            priority=priority,
            run_id=self.context.run_id,
            scenario_id=self.context.scenario_id,
            event_generation_mode=self.mode,
            generation_rule_id=generation_rule_id,
            rng_stream=rng_stream,
            sim_time_s=sim_time_s,
            data=data,
        )

    @staticmethod
    def _tick_of(trigger: SimEvent | None) -> float:
        return float(trigger.timestamp) if trigger is not None else 0.0


# ------------------------------------------------------------------ scripted


def timeline_device_command(entry: TimelineEvent) -> DeviceCommand | None:
    """timeline 项 → 场景来源的设备命令（不是设备变更项则返回 None）。

    识别口径（唯一判定入口，S2-T8 的 YAML 作者按此写）：
      - ``device_id`` 必填；
      - ``payload.capability`` + ``payload.value`` 给出控制点与目标值；
      - ``payload.reason`` 可选，进 delta/事件的可读原因。

    ``correlation_id`` / ``causal_parent`` 由调用方在根事件发布后补齐（§4.4：命令挂在
    它自己的根事件之下）。
    """

    if not entry.device_id:
        return None
    payload = entry.payload or {}
    capability = payload.get("capability")
    if not capability:
        return None
    if "value" not in payload:
        return None
    return DeviceCommand(
        source=CommandSource.SCENARIO,
        device_id=entry.device_id,
        capability=str(capability).removeprefix("extra."),
        value=payload["value"],
        reason=str(payload.get("reason") or f"scenario timeline @{entry.at}s"),
        priority=int(payload.get("priority", 2)),
    )


def scenario_timeline_device_entries(spec: ScenarioSpec) -> list[TimelineEvent]:
    """场景里所有"会下发设备命令"的 timeline 项。

    §15-2 的验收测试用它在库场景里定位一条真实的 scenario 来源命令，S2-T8 的 YAML
    作者也用它自查"我写的直控项真的被识别成命令了吗"。
    """

    return [entry for entry in spec.timeline if timeline_device_command(entry) is not None]


class ScriptedEventSource(EventSource):
    """§4.5 scripted：按 ``timeline`` 的 ``at``（相对 run 起点的模拟秒）到点发根事件。

    同刻多条按 timeline 索引排序（YAML 里的书写顺序即执行顺序）——这是"同场景同 seed
    产出同样根事件顺序"这条 §4.5 要求的全部实现，此外不引入任何时间源。
    """

    mode: EventGenerationMode = "scripted"
    source_name = SCRIPTED_EVENT_SOURCE

    def __init__(self, spec: ScenarioSpec, *, context: GenerationContext | None = None) -> None:
        super().__init__(context=context)
        self.spec = spec
        self._fired: set[int] = set()

    @property
    def fired_count(self) -> int:
        return len(self._fired)

    @property
    def exhausted(self) -> bool:
        return len(self._fired) == len(self.spec.timeline)

    def reset(self) -> None:
        self._fired.clear()

    def due(self, sim_time_s: float) -> list[tuple[int, TimelineEvent]]:
        """本刻应当触发、且尚未触发过的 timeline 项（按索引升序）。"""

        return [
            (index, entry)
            for index, entry in enumerate(self.spec.timeline)
            if index not in self._fired and entry.at <= sim_time_s
        ]

    def emit(
        self,
        world: WorldState,
        *,
        trigger: SimEvent | None,
        sim_time_s: float,
    ) -> list[GeneratedEvent]:
        generated: list[GeneratedEvent] = []
        for index, entry in self.due(sim_time_s):
            self._fired.add(index)
            event = self._build_event(
                event_type=entry.type,
                data=self._entry_data(entry, world),
                sim_time_s=sim_time_s,
                tick=self._tick_of(trigger),
                # 根事件不认父：它是一条新因果链的起点（§4.4）。
                causal_parent=None,
            )
            command = timeline_device_command(entry)
            if command is not None:
                command = command.model_copy(
                    update={
                        "correlation_id": event.correlation_id,
                        "causal_parent": event.event_id,
                        "issued_tick": int(event.timestamp),
                    }
                )
            generated.append(GeneratedEvent(event=event, device_command=command))
        return generated

    def _entry_data(self, entry: TimelineEvent, world: WorldState) -> dict[str, Any]:
        """timeline 项 → 事件 data。

        用户类事件补齐 ``from_room``（读世界当前位置）与 ``to_room``，与
        ``user.activity_change`` 的键名保持一致——引擎那台"用户位置写回"处理器认这三个键，
        富事件因此不需要第二套写回逻辑。
        """

        data: dict[str, Any] = {
            "scenario_id": self.spec.id,
            "timeline_at": entry.at,
        }
        if entry.user_id:
            data["user_id"] = entry.user_id
        if entry.device_id:
            data["device_id"] = entry.device_id
        if entry.room_id:
            data["room_id"] = entry.room_id
        if entry.activity:
            data["activity"] = entry.activity
        data.update(entry.payload or {})

        if entry.user_id and entry.type in _USER_LOCATION_EVENT_TYPES:
            user = world.users.get(entry.user_id)
            current_room = user.location.room if (user is not None and user.location) else ""
            from_room, to_room = _user_rooms(entry, current_room)
            data.setdefault("from_room", from_room)
            data.setdefault("to_room", to_room)
            data.setdefault("activity", entry.activity or _default_activity(entry.type))
        return data


# 会被引擎翻译成"用户位置/活动写回"的 timeline 事件类型。
# user.command / environment.* 不在其中：它们不描述人在哪。
_USER_LOCATION_EVENT_TYPES: frozenset[str] = frozenset(
    {
        USER_ARRIVES_HOME,
        USER_LEAVES_HOME,
        USER_ENTERS_ROOM,
        USER_EXITS_ROOM,
        USER_STARTS_ACTIVITY,
        USER_ENDS_ACTIVITY,
        USER_ACTIVITY_CHANGE,
    }
)


def _user_rooms(entry: TimelineEvent, current_room: str) -> tuple[str, str]:
    """(from_room, to_room)：``room_id`` 在不同富事件里指的**不是同一个房间**。

    ``user.enters_room`` 的 room_id 是目的地，``user.exits_room`` 的 room_id 是被离开的
    房间——用一条通用规则处理会让"离开客厅"变成"进入客厅"。``exits_room`` 的目的地是未知
    的（世界模型没有"在途"状态），故 to_room 留空：位置由随后那条 enters_room 事件给出，
    引擎在 to_room 为空时只更新活动、不动 persons（否则会写出一个不在任何房间的人）。
    """

    if entry.type == USER_LEAVES_HOME:
        return (entry.room_id or current_room, OUTSIDE_ROOM_ID)
    if entry.type == USER_EXITS_ROOM:
        return (entry.room_id or current_room, "")
    if entry.type in {USER_ARRIVES_HOME, USER_ENTERS_ROOM}:
        return (current_room, entry.room_id or "")
    # starts_activity / ends_activity / 兼容期 activity_change：房间可选，不给就留在原地。
    return (current_room, entry.room_id or current_room)


def _default_activity(event_type: str) -> str:
    """富用户事件在场景没写 activity 时的默认活动标签（只影响可读性，不影响判定）。"""

    return {
        USER_ARRIVES_HOME: "arrive_home",
        USER_LEAVES_HOME: "away",
        USER_ENTERS_ROOM: "moving",
        USER_EXITS_ROOM: "moving",
        USER_STARTS_ACTIVITY: "active",
        USER_ENDS_ACTIVITY: "idle",
    }.get(event_type, "idle")


# ---------------------------------------------------------------- rule_based


@dataclass
class _ThresholdState:
    """单条阈值规则的迟滞记忆：只在"跨越那一刻"发事件，不是每 tick 复读。"""

    active: bool = False


class RuleBasedEventSource(EventSource):
    """§4.5 rule_based：状态阈值 + 用户日程推出来的事件，每条盖 ``generation_rule_id``。

    两个规则族：

    ``user_schedule``
        收编 :class:`~backend.simulators.user_behavior.UserBehaviorSimulator` 的硬编码
        SCHEDULE，把它产出的 ``user.activity_change`` 翻译成 §4.1 富根事件
        （到家 / 离家 / 换房间 / 开始活动）。规则 id 形如 ``user_schedule.arrive_home``。

    ``temperature_threshold`` / ``light_level_threshold``
        房间温度越出舒适区、有人房间光照低于目标。规则 id 形如
        ``temperature_threshold.high`` / ``light_level_threshold.low``。

    **因果**：``emit_threshold_events(trigger=...)`` 的 trigger 就是把读数推过阈值的那条
    事件——引擎在每条用户事件写回世界之后立刻评一次（此时 trigger 是那条用户事件），
    tick 收尾再评一次（trigger 是 tick 事件）。富事件本身仍是 causal-none episode 根，
    物理触发身份保存在 ``data.trigger_event_id``，不存在"最近一条用户事件"这种猜测。
    """

    mode: EventGenerationMode = "rule_based"
    source_name = RULE_EVENT_SOURCE

    def __init__(
        self,
        *,
        context: GenerationContext | None = None,
        user_sim: UserBehaviorSimulator | None = None,
        emit_user_events: bool = True,
        temperature_comfort: tuple[float, float] = DEFAULT_TEMPERATURE_COMFORT_RANGE,
        light_level_threshold: float = DEFAULT_LIGHT_LEVEL_THRESHOLD,
    ) -> None:
        super().__init__(context=context)
        self.user_sim = user_sim or UserBehaviorSimulator()
        self.emit_user_events = emit_user_events
        self.temperature_comfort = temperature_comfort
        self.light_level_threshold = light_level_threshold
        self._temperature_state: dict[str, _ThresholdState] = {}
        self._light_state: dict[str, _ThresholdState] = {}

    def reset(self) -> None:
        self.user_sim = UserBehaviorSimulator()
        self._temperature_state.clear()
        self._light_state.clear()

    def emit(
        self,
        world: WorldState,
        *,
        trigger: SimEvent | None,
        sim_time_s: float,
    ) -> list[GeneratedEvent]:
        generated = self.emit_schedule_events(world, trigger=trigger, sim_time_s=sim_time_s)
        generated.extend(self.emit_threshold_events(world, trigger=trigger, sim_time_s=sim_time_s))
        return generated

    # -- 用户日程 -------------------------------------------------------

    def emit_schedule_events(
        self,
        world: WorldState,
        *,
        trigger: SimEvent | None,
        sim_time_s: float,
    ) -> list[GeneratedEvent]:
        if not self.emit_user_events:
            return []

        generated: list[GeneratedEvent] = []
        for world_event in self.user_sim.step(world):
            data = dict(world_event.data)
            if trigger is not None:
                data["trigger_event_id"] = trigger.event_id
            from_room = str(data.get("from_room") or "")
            to_room = str(data.get("to_room") or "")
            activity = str(data.get("activity") or "")
            event_type = _rich_user_event_type(from_room, to_room)
            generated.append(
                GeneratedEvent(
                    event=self._build_event(
                        event_type=event_type,
                        data=data,
                        sim_time_s=sim_time_s,
                        tick=self._tick_of(trigger),
                        # §4.1 rich root starts a new episode; the clock trigger is
                        # retained separately in data.trigger_event_id.
                        causal_parent=None,
                        generation_rule_id=f"user_schedule.{activity or 'advance'}",
                    ),
                    device_command=None,
                )
            )
        return generated

    # -- 阈值规则 -------------------------------------------------------

    def emit_threshold_events(
        self,
        world: WorldState,
        *,
        trigger: SimEvent | None,
        sim_time_s: float,
    ) -> list[GeneratedEvent]:
        generated: list[GeneratedEvent] = []
        low, high = self.temperature_comfort
        trigger_event_id = trigger.event_id if trigger is not None else None
        tick = self._tick_of(trigger)

        for room_id in sorted(world.rooms):
            room = world.rooms[room_id]

            # 温度：越出舒适区即跨越；回到区间内复位迟滞。
            state = self._temperature_state.setdefault(room_id, _ThresholdState())
            direction = "high" if room.temperature > high else ("low" if room.temperature < low else None)
            if direction is not None and not state.active:
                state.active = True
                generated.append(
                    GeneratedEvent(
                        event=self._build_event(
                            event_type=ENVIRONMENT_TEMPERATURE_THRESHOLD,
                            data={
                                "room_id": room_id,
                                "value": round(float(room.temperature), 2),
                                "direction": direction,
                                "comfort_min": low,
                                "comfort_max": high,
                                # significant_change_reasons 是既有 agent 相关性判定读的键，
                                # 富事件带上它，旧订阅者不用改就能认这条事件"值得跑一轮"。
                                "significant_change_reasons": ["room_temperature_threshold"],
                                "trigger_event_id": trigger_event_id,
                            },
                            sim_time_s=sim_time_s,
                            tick=tick,
                            causal_parent=None,
                            generation_rule_id=f"temperature_threshold.{direction}",
                        )
                    )
                )
            elif direction is None:
                state.active = False

            # 光照：有人的房间低于目标照度才算问题（空房间黑着不是异常）。
            light_state = self._light_state.setdefault(room_id, _ThresholdState())
            too_dark = bool(room.persons) and room.light_level < self.light_level_threshold
            if too_dark and not light_state.active:
                light_state.active = True
                generated.append(
                    GeneratedEvent(
                        event=self._build_event(
                            event_type=ENVIRONMENT_LIGHT_LEVEL_THRESHOLD,
                            data={
                                "room_id": room_id,
                                "value": round(float(room.light_level), 2),
                                "direction": "low",
                                "threshold": self.light_level_threshold,
                                "persons": list(room.persons),
                                "significant_change_reasons": ["room_light_level_threshold"],
                                "trigger_event_id": trigger_event_id,
                            },
                            sim_time_s=sim_time_s,
                            tick=tick,
                            causal_parent=None,
                            generation_rule_id="light_level_threshold.low",
                        )
                    )
                )
            elif not too_dark:
                light_state.active = False

        return generated


def _rich_user_event_type(from_room: str, to_room: str) -> str:
    """旧 ``user.activity_change`` 的 (from, to) → §4.1 富事件类型。"""

    if to_room == OUTSIDE_ROOM_ID:
        return USER_LEAVES_HOME
    if from_room in {"", OUTSIDE_ROOM_ID}:
        return USER_ARRIVES_HOME
    return USER_ENTERS_ROOM


# ---------------------------------------------------------------- stochastic


class StochasticEventSource(EventSource):
    """§4.5 stochastic：由命名随机子流驱动的设备掉线/恢复（MVP 只做这一种）。

    每条事件盖 ``rng_stream``（子流名），**不**逐事件复制 seed——seed 属于 run 元数据
    （§11），复制到每条事件上只会制造"同一个 seed 出现在一千条事件里"的冗余，还让
    canonical trace 多一个必须排除的字段。

    抽样次序（先掉线判定后恢复判定、设备 id 升序）是确定性契约的一部分：换个遍历序
    等于换了随机流的消费顺序，同 seed 也不再复现。
    """

    mode: EventGenerationMode = "stochastic"
    source_name = STOCHASTIC_EVENT_SOURCE

    def __init__(
        self,
        *,
        stream: SimStream,
        context: GenerationContext | None = None,
        device_ids: Sequence[str] = (),
        offline_probability: float = DEFAULT_OFFLINE_PROBABILITY,
        recovery_probability: float = DEFAULT_RECOVERY_PROBABILITY,
    ) -> None:
        super().__init__(context=context)
        self.stream = stream
        self.device_ids = tuple(sorted(device_ids))
        self.offline_probability = float(offline_probability)
        self.recovery_probability = float(recovery_probability)

    @property
    def enabled(self) -> bool:
        return bool(self.device_ids) and (
            self.offline_probability > 0.0 or self.recovery_probability > 0.0
        )

    def emit(
        self,
        world: WorldState,
        *,
        trigger: SimEvent | None,
        sim_time_s: float,
    ) -> list[GeneratedEvent]:
        if not self.enabled:
            return []

        generated: list[GeneratedEvent] = []
        trigger_event_id = trigger.event_id if trigger is not None else None
        tick = self._tick_of(trigger)

        for device_id in self.device_ids:
            device = world.devices.get(device_id)
            if device is None:
                continue
            online = bool(device.state.extra.get("online", True))
            if online:
                if self.offline_probability <= 0.0:
                    continue
                draw = self.stream.random()
                if draw >= self.offline_probability:
                    continue
                event_type, next_online = DEVICE_OFFLINE, False
            else:
                if self.recovery_probability <= 0.0:
                    continue
                draw = self.stream.random()
                if draw >= self.recovery_probability:
                    continue
                event_type, next_online = DEVICE_RECOVERED, True

            reason = "stochastic failure injection" if not next_online else "stochastic recovery"
            generated.append(
                GeneratedEvent(
                    event=self._build_event(
                        event_type=event_type,
                        data={
                            "device_id": device_id,
                            "device_type": device.type,
                            "room_id": device.location.room,
                            "online": next_online,
                            "significant_change_reasons": ["device_availability"],
                            "trigger_event_id": trigger_event_id,
                        },
                        sim_time_s=sim_time_s,
                        tick=tick,
                        causal_parent=None,
                        priority=3 if not next_online else 2,
                        rng_stream=self.stream.name,
                    ),
                    availability_write=DeviceAvailabilityWrite(
                        device_id=device_id, online=next_online, reason=reason
                    ),
                )
            )
        return generated


# ------------------------------------------------------------------- 装配


@dataclass(frozen=True)
class GenerationSources:
    """一次 run 的三条产线。任一为 None 表示该模式在本 run 里不启用。"""

    scripted: ScriptedEventSource | None = None
    rule_based: RuleBasedEventSource | None = None
    stochastic: StochasticEventSource | None = None
    context: GenerationContext = field(default_factory=GenerationContext)

    def all(self) -> tuple[EventSource, ...]:
        return tuple(
            source
            for source in (self.scripted, self.rule_based, self.stochastic)
            if source is not None
        )

    def reset(self) -> None:
        for source in self.all():
            source.reset()


def build_generation_sources(
    spec: ScenarioSpec,
    *,
    context: GenerationContext,
    rng: SimRandom | None = None,
    user_sim: UserBehaviorSimulator | None = None,
    stochastic_overrides: Mapping[str, Any] | None = None,
) -> GenerationSources:
    """按场景声明装配三条产线（ScenarioRunner 的唯一装配入口）。

    stochastic 的配置来源优先级：``stochastic_overrides``（测试/CLI）→
    ``spec.failure_injection["device_offline"]`` → ``spec.noise_model["device_offline"]``。
    三处都没有就用默认概率 0——噪声必须是场景显式声明的实验条件。
    """

    scripted = ScriptedEventSource(spec, context=context)
    rule_based = RuleBasedEventSource(
        context=context,
        user_sim=user_sim,
        temperature_comfort=_comfort_range(spec),
        light_level_threshold=_light_threshold(spec),
    )

    config: dict[str, Any] = {}
    for holder in (spec.noise_model, spec.failure_injection):
        section = (holder or {}).get("device_offline")
        if isinstance(section, Mapping):
            config.update(section)
    if stochastic_overrides:
        config.update(stochastic_overrides)

    stochastic: StochasticEventSource | None = None
    if rng is not None:
        stochastic = StochasticEventSource(
            context=context,
            stream=rng.stream(RngStream.STOCHASTIC_EVENTS),
            device_ids=_stochastic_device_ids(spec, config),
            offline_probability=float(config.get("probability", DEFAULT_OFFLINE_PROBABILITY)),
            recovery_probability=float(
                config.get("recovery_probability", DEFAULT_RECOVERY_PROBABILITY)
            ),
        )

    return GenerationSources(
        scripted=scripted, rule_based=rule_based, stochastic=stochastic, context=context
    )


def _comfort_range(spec: ScenarioSpec) -> tuple[float, float]:
    section = (spec.noise_model or {}).get("temperature_comfort")
    if isinstance(section, (list, tuple)) and len(section) == 2:
        return (float(section[0]), float(section[1]))
    return DEFAULT_TEMPERATURE_COMFORT_RANGE


def _light_threshold(spec: ScenarioSpec) -> float:
    value = (spec.noise_model or {}).get("light_level_threshold")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return DEFAULT_LIGHT_LEVEL_THRESHOLD


def _stochastic_device_ids(spec: ScenarioSpec, config: Mapping[str, Any]) -> tuple[str, ...]:
    declared: Iterable[str] | None = config.get("device_ids")
    if declared:
        return tuple(str(device_id) for device_id in declared)
    # 未点名时以场景引用到的设备为候选：噪声只打在这个实验关心的设备上，
    # 不去骚扰一屋子与本场景无关的设备。
    return tuple(sorted(spec.referenced_device_ids()))
