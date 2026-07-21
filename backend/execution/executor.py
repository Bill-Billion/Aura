"""CommandExecutor：四来源（UI / Agent / 场景脚本 / 规则降级）设备变更的唯一入口。

spec §10 执行序列 5–8 步在此落地为一条流水线，且是全系统唯一调用
``state_manager.apply_action`` 的地方——根治审计必修缺陷①（agent 路径绕过校验、
``except KeyError: continue`` 静默吞失败）与 UI 直控绕过 executor 的旧结构：

    propose(None→proposed) → approved（S1 自动放行，仲裁接缝留给 S3）
      → validate_command（S1-T2 六级校验；失败→approved→failed + device.command_failed，世界零变更）
      → validated → pre_submit 接缝（S1 no-op；S3 装仲裁/取代）
      → executing + 发 action.device_control（步骤6）
      → state_manager.apply_action（步骤7，唯一变更方）
      → 房间光照统一 effect 钩子（收拢 main.py/runtime.py 两处重复逻辑）
      → 逐 delta 发 feedback.state_delta（步骤8）
      → succeeded

生命周期十态与迁移合法性由 S1-T3（backend/execution/command.py）拥有；十类失败码词表
由 S1-T2（backend/config/validation.py）拥有。本模块只编排，不新增枚举。

**接缝（现在定义、后续扩展而非返工）**：
  - ``submit(..., pre_submit=hook)``：hook 在校验之后、执行之前被调用，返回
    ``PreSubmitDecision | None``（None=直通放行）。S1 出厂即 no-op；S3-T5 在此装
    仲裁/取代逻辑。hook 只对当前命令放行 + 取代其他在飞命令（当前命令的拒绝属仲裁，
    位于 proposed→approved，由 S3 另行接入），因 VALIDATED 只合法迁向 EXECUTING。
  - ``submit(..., source=...)``：覆盖命令来源并贯穿全部事件，供 S2
    ``executor.submit(source="scenario")`` 直接调用。
  - ``cancel_pending(reason)``：把在飞（未终态）命令迁到 cancelled，供 S2 reset 取消在飞。

**同步语义边界**：同步 StateManager 下 apply 立即返回，无真实在飞窗口——
``superseded`` 收敛为『批内排队未执行 + 显式 pending 队列』，``timed_out`` 由可注入
时钟诚实触发（测量下发到反馈的耗时，超预算即报，不造假异步）。S2/S3 引入异步
episode 后，pending 注册表会在 proposed 起就承载在飞命令，取代窗口随之自然变宽。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Sequence

from backend.config.validation import CommandErrorCode, ScenarioPolicy, validate_command
from backend.engine.event_bus import SimEvent
from backend.engine.state_manager import (
    DeltaChange,
    InvariantViolation,
    StateManager,
    verify_world_invariants,
)
from backend.execution.command import (
    LEGAL_TRANSITIONS,
    LIFECYCLE_EVENT_SOURCE,
    CommandRecord,
    CommandSource,
    CommandStatus,
    DeviceCommand,
    PublishEvent,
)
from backend.simulators.effects import (
    calculate_perceived_temperature,
    calculate_security_coverage,
)
from backend.simulators.environment import calculate_room_light_level

# executor 派生事件类型（均复用既有 SIM_EVENT 通道，前端零改动可见）。
ACTION_EVENT_TYPE = "action.device_control"
FEEDBACK_EVENT_TYPE = "feedback.state_delta"
COMMAND_FAILED_EVENT_TYPE = "device.command_failed"
# §2.2 不变式违规的结构化失败事件与失败码。故意不进 §10.2 十类命令失败词表：
# 那十类描述「命令为什么没被执行」，不变式违规描述「世界差点被改坏」，是系统级故障。
INVARIANT_VIOLATION_EVENT_TYPE = "system.invariant_violation"
INVARIANT_VIOLATION_ERROR_CODE = "invariant_violation"

# 命令目标键：唯一 (device_id, capability) 决定「同一控制点」，取代/在飞判定按此聚合。
CommandTarget = tuple[str, str]

# effect 钩子：apply 之后的派生变更（如房间光照重算），返回追加的 delta 列表。
EffectHook = Callable[[StateManager, DeviceCommand, "list[DeltaChange]"], "list[DeltaChange]"]


@dataclass(frozen=True)
class PreSubmitDecision:
    """pre_submit 接缝的返回契约（S1 仅用默认值；S3-T5 填充仲裁语义）。

    ``superseded_targets``：需要被当前命令取代的其他在飞命令目标 (device_id, capability)。
    当前命令本身在此接缝恒放行（VALIDATED 只合法迁向 EXECUTING），其拒绝属 proposed→approved
    的仲裁职责，由 S3 另接。
    """

    superseded_targets: tuple[CommandTarget, ...] = ()


# pre_submit hook：给定当前命令，返回取代决策；返回 None 即 no-op 直通。
PreSubmitHook = Callable[[DeviceCommand], "PreSubmitDecision | None"]

# 可注入时钟：默认单调钟，测试注入假钟以诚实触发反馈超时。
Clock = Callable[[], float]


def _property_path(capability: str) -> str:
    """能力名 → StateManager 点路径：power 是顶层字段，其余能力镜像在 extra。"""

    return "power" if capability == "power" else f"extra.{capability}"


def _command_affects_room_light(device_type: str, capability: str) -> bool:
    """该命令是否改变房间光照（收拢 main.py 与 runtime.py 两处等价判定）。"""

    if device_type == "light":
        return capability in {"power", "brightness"}
    if device_type == "curtain":
        return capability == "open_percent"
    return False


def room_light_effect(
    state_manager: StateManager,
    command: DeviceCommand,
    deltas: list[DeltaChange],
) -> list[DeltaChange]:
    """统一房间光照 effect：灯/窗帘落地后按 ground truth 重算一次房间 light_level。"""

    device = state_manager.world.devices.get(command.device_id)
    if device is None:
        return []
    if not _command_affects_room_light(device.type, command.capability):
        return []
    room_id = device.location.room
    if room_id not in state_manager.world.rooms:
        return []
    return state_manager.apply_path_update(
        caused_by=command.source.value,
        path=f"rooms[{room_id}].light_level",
        new_value=calculate_room_light_level(state_manager.world, room_id),
        reason="apply device light feedback",
    )


def fan_comfort_effect(
    state_manager: StateManager,
    command: DeviceCommand,
    deltas: list[DeltaChange],
) -> list[DeltaChange]:
    """§3.4 fan 效果：风扇命令落地后重算房间体感温度（绝不动物理 temperature）。"""

    device = state_manager.world.devices.get(command.device_id)
    if device is None or device.type != "fan":
        return []
    if command.capability not in {"power", "speed", "shake"}:
        return []
    room = state_manager.world.rooms.get(device.location.room)
    if room is None:
        return []
    return state_manager.apply_path_update(
        caused_by=command.source.value,
        path=f"rooms[{room.id}].perceived_temperature",
        new_value=calculate_perceived_temperature(room, state_manager.world),
        reason="apply fan comfort feedback",
    )


def camera_security_effect(
    state_manager: StateManager,
    command: DeviceCommand,
    deltas: list[DeltaChange],
) -> list[DeltaChange]:
    """§3.4 camera 效果：摄像头命令落地后重算房间安防覆盖。

    当前能力矩阵里 camera 无可写能力，命令路径走不到这里；保留是为了 §13 故障注入
    （摄像头掉线）与 S2 场景脚本改 online 时，覆盖率同样有唯一重算入口。
    """

    device = state_manager.world.devices.get(command.device_id)
    if device is None or device.type != "camera":
        return []
    room_id = device.location.room
    if room_id not in state_manager.world.rooms:
        return []
    return state_manager.apply_path_update(
        caused_by=command.source.value,
        path=f"rooms[{room_id}].security_coverage",
        new_value=calculate_security_coverage(state_manager.world)[room_id],
        reason="apply camera security coverage feedback",
    )


# executor 出厂 effect 组合：按设备类型自选生效，互不重叠（各自 return [] 早退）。
DEFAULT_EFFECTS: tuple[EffectHook, ...] = (
    room_light_effect,
    fan_comfort_effect,
    camera_security_effect,
)


def default_device_effects(
    state_manager: StateManager,
    command: DeviceCommand,
    deltas: list[DeltaChange],
) -> list[DeltaChange]:
    """把 §3.4 各设备类型的最小效果串成一个钩子（S2/S3 可整体替换）。"""

    derived: list[DeltaChange] = []
    for effect in DEFAULT_EFFECTS:
        derived.extend(effect(state_manager, command, deltas))
    return derived


class CommandExecutor:
    """所有设备变更的唯一入口与唯一 apply_action 调用方。

    经 SimulationEngine 构造注入（非全局单例），reset 时随 state_manager 换新，避免重演
    main.py 全局可变状态问题。``policy`` 缺省 permissive；``effects`` 缺省房间光照重算；
    ``feedback_timeout`` 缺省 None（不设超时），设值后由 ``clock`` 测量下发到反馈的耗时判定。
    """

    def __init__(
        self,
        state_manager: StateManager,
        publish_event: PublishEvent,
        *,
        policy: ScenarioPolicy | None = None,
        effects: EffectHook | None = None,
        clock: Clock = time.monotonic,
        feedback_timeout: float | None = None,
    ) -> None:
        self.state_manager = state_manager
        self.publish_event = publish_event
        self.policy = policy
        self.effects = effects if effects is not None else default_device_effects
        self.clock = clock
        self.feedback_timeout = feedback_timeout
        # 在飞命令注册表：键=目标控制点，值=尚未终态的记录（S1 同步下多为瞬态；S2/S3 承载异步在飞）。
        self._pending: dict[CommandTarget, CommandRecord] = {}

    @property
    def pending(self) -> dict[CommandTarget, CommandRecord]:
        """在飞命令的只读快照。"""

        return dict(self._pending)

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    async def submit(
        self,
        command: DeviceCommand,
        *,
        source: CommandSource | str | None = None,
        pre_submit: PreSubmitHook | None = None,
        tick: int | None = None,
    ) -> CommandRecord:
        """提交单条命令，跑完整生命周期流水线，返回其 CommandRecord。"""

        command = self._with_source(command, source)
        return await self._run(command, pre_submit=pre_submit, tick=tick)

    async def submit_batch(
        self,
        commands: Sequence[DeviceCommand],
        *,
        source: CommandSource | str | None = None,
        pre_submit: PreSubmitHook | None = None,
        tick: int | None = None,
    ) -> list[CommandRecord]:
        """按顺序提交一批命令；同批内同一控制点的旧命令被最新命令取代（不执行）。"""

        prepared = [self._with_source(command, source) for command in commands]
        winner_by_target: dict[CommandTarget, str] = {}
        for command in prepared:
            winner_by_target[(command.device_id, command.capability)] = command.command_id

        records: list[CommandRecord] = []
        for command in prepared:
            target = (command.device_id, command.capability)
            if winner_by_target[target] != command.command_id:
                # 被同批更新命令取代：出生后直接 superseded，绝不落地。
                record = await self._propose(command, tick)
                await self._supersede(record, tick)
                records.append(record)
            else:
                records.append(await self._run(command, pre_submit=pre_submit, tick=tick))
        return records

    async def cancel_pending(
        self, reason: str, *, tick: int | None = None
    ) -> list[CommandRecord]:
        """把所有在飞（未终态且可合法取消）命令迁到 cancelled，供 S2 reset 取消在飞。"""

        cancelled: list[CommandRecord] = []
        for record in list(self._pending.values()):
            if record.is_terminal:
                self._deregister(record)
                continue
            if CommandStatus.CANCELLED in LEGAL_TRANSITIONS[record.status]:
                await record.transition(
                    CommandStatus.CANCELLED, detail=reason, tick=tick
                )
                cancelled.append(record)
            self._deregister(record)
        return cancelled

    # ------------------------------------------------------------------
    # 流水线
    # ------------------------------------------------------------------

    async def _run(
        self,
        command: DeviceCommand,
        *,
        pre_submit: PreSubmitHook | None,
        tick: int | None,
    ) -> CommandRecord:
        target = (command.device_id, command.capability)
        # 同一控制点已有在飞命令 → 被本命令取代（跨调用/异步在飞的取代接缝）。
        existing = self._pending.get(target)
        if existing is not None and not existing.is_terminal:
            await self._supersede(existing, tick)

        record = await self._propose(command, tick)
        # S1 自动放行：仲裁（proposed→approved 的拒绝分支）留给 S3，此处不做全序仲裁。
        await record.transition(CommandStatus.APPROVED, tick=tick)

        # §3.3 六级校验（S1-T2 唯一实现）：失败即 approved→failed，绝不下发、绝不动世界。
        failure = validate_command(
            self.state_manager.world,
            command.device_id,
            command.capability,
            command.value,
            policy=self.policy,
        )
        if failure is not None:
            await record.transition(
                CommandStatus.FAILED,
                failure=failure.code.value,
                detail=failure.message,
                tick=tick,
            )
            await self._emit_command_failed(
                command,
                error_code=failure.code.value,
                reason=failure.message,
                causal_parent=command.causal_parent,
                tick=tick,
            )
            self._deregister(record)
            return record

        await record.transition(CommandStatus.VALIDATED, tick=tick)

        # 接缝：pre_submit 在校验之后、执行之前。S1 no-op；S3 在此装仲裁/取代。
        decision = pre_submit(command) if pre_submit is not None else None
        if decision is not None:
            for victim_target in decision.superseded_targets:
                victim = self._pending.get(victim_target)
                if victim is not None and victim is not record and not victim.is_terminal:
                    await self._supersede(victim, tick)

        await record.transition(CommandStatus.EXECUTING, tick=tick)
        action_event = await self._emit_action(command, tick)

        # 步骤7 前置守卫：§2.2 命令级不变式（离线设备写 / 只读能力被非仿真源写）。
        # 与六级校验重叠是有意的——那道守入口，这道守状态层，先抛先拦，世界零变更。
        device = self.state_manager.world.devices.get(command.device_id)
        if device is not None:
            try:
                self.state_manager.check_command_invariants(
                    device, command.capability, caused_by=command.source.value
                )
            except InvariantViolation as exc:
                await self._fail_on_invariant(
                    record, command, exc, action_event.event_id, (), tick
                )
                return record

        # 步骤7：唯一的世界变更入口。校验通过后设备仍可能并发消失（reset）——
        # 转结构化 failed，绝不静默吞（根治缺陷①）。
        dispatch_at = self.clock()
        try:
            deltas = self.state_manager.apply_action(
                agent_id=command.source.value,
                device_id=command.device_id,
                property_path=_property_path(command.capability),
                new_value=command.value,
                reason=command.reason or "",
                caused_by_event_id=action_event.event_id,
            )
        except KeyError as exc:
            await record.transition(
                CommandStatus.FAILED,
                failure=CommandErrorCode.UNKNOWN_DEVICE.value,
                detail=str(exc),
                tick=tick,
            )
            await self._emit_command_failed(
                command,
                error_code=CommandErrorCode.UNKNOWN_DEVICE.value,
                reason=str(exc),
                causal_parent=action_event.event_id,
                tick=tick,
            )
            self._deregister(record)
            return record

        # 反馈超时：动作已下发但确认超出预算窗口 → 诚实标 timed_out（注入时钟触发）。
        feedback_at = self.clock()
        if (
            self.feedback_timeout is not None
            and (feedback_at - dispatch_at) > self.feedback_timeout
        ):
            await record.transition(
                CommandStatus.TIMED_OUT,
                failure=CommandErrorCode.EXECUTION_TIMEOUT.value,
                detail="state feedback exceeded budget",
                tick=tick,
            )
            await self._emit_command_failed(
                command,
                error_code=CommandErrorCode.EXECUTION_TIMEOUT.value,
                reason="state feedback exceeded budget",
                causal_parent=action_event.event_id,
                tick=tick,
            )
            self._deregister(record)
            return record

        # 统一 effect 钩子（房间光照重算）追加派生 delta。
        deltas = list(deltas) + list(self.effects(self.state_manager, command, deltas))
        # §2.2 第7条：effect 钩子按既有签名产出的 delta 也必须归因到同一条 action 事件。
        for delta in deltas:
            if delta.caused_by_event_id is None:
                delta.caused_by_event_id = action_event.event_id

        # 步骤7.5：§2.2 世界级不变式后置校验。违规 → 逆序回滚本命令落地的全部 delta
        # → executing→failed + system.invariant_violation（禁止静默纠正）；不发反馈。
        try:
            verify_world_invariants(self.state_manager.world)
        except InvariantViolation as exc:
            reverted = self.state_manager.revert(deltas)
            await self._fail_on_invariant(
                record, command, exc, action_event.event_id, reverted, tick
            )
            return record

        # 步骤8：逐 delta 发 feedback.state_delta，因果挂在 action 之下。
        for delta in deltas:
            await self._emit_feedback(command, delta, action_event.event_id, tick)

        await record.transition(CommandStatus.SUCCEEDED, tick=tick)
        self._deregister(record)
        return record

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _with_source(
        self, command: DeviceCommand, source: CommandSource | str | None
    ) -> DeviceCommand:
        if source is None:
            return command
        coerced = CommandSource(source)
        if command.source == coerced:
            return command
        return command.model_copy(update={"source": coerced})

    async def _propose(
        self, command: DeviceCommand, tick: int | None
    ) -> CommandRecord:
        record = await CommandRecord.propose(command, self.publish_event, tick=tick)
        self._pending[(command.device_id, command.capability)] = record
        return record

    async def _supersede(self, record: CommandRecord, tick: int | None) -> None:
        if CommandStatus.SUPERSEDED in LEGAL_TRANSITIONS[record.status]:
            await record.transition(
                CommandStatus.SUPERSEDED,
                failure=CommandErrorCode.SUPERSEDED_BY_NEWER_COMMAND.value,
                tick=tick,
            )
        self._deregister(record)

    def _deregister(self, record: CommandRecord) -> None:
        key = (record.command.device_id, record.command.capability)
        if self._pending.get(key) is record:
            del self._pending[key]

    async def _emit_action(
        self, command: DeviceCommand, tick: int | None
    ) -> SimEvent:
        event = SimEvent(
            event_type=ACTION_EVENT_TYPE,
            source=LIFECYCLE_EVENT_SOURCE,
            timestamp=float(tick if tick is not None else command.issued_tick),
            correlation_id=command.correlation_id,
            causal_parent=command.causal_parent,
            priority=command.priority,
            data={
                "command_id": command.command_id,
                "device_id": command.device_id,
                "capability": command.capability,
                "property": _property_path(command.capability),
                "value": command.value,
                "reason": command.reason,
                "source": command.source.value,
            },
        )
        return await self.publish_event(event)

    async def _emit_feedback(
        self,
        command: DeviceCommand,
        delta: DeltaChange,
        action_event_id: str,
        tick: int | None,
    ) -> SimEvent:
        data = delta.model_dump()
        data["device_id"] = command.device_id
        data["source"] = command.source.value
        event = SimEvent(
            event_type=FEEDBACK_EVENT_TYPE,
            source="state_manager",
            timestamp=float(tick if tick is not None else command.issued_tick),
            correlation_id=command.correlation_id,
            causal_parent=action_event_id,
            priority=command.priority,
            data=data,
        )
        return await self.publish_event(event)

    async def _fail_on_invariant(
        self,
        record: CommandRecord,
        command: DeviceCommand,
        violation: InvariantViolation,
        causal_parent: str,
        reverted_paths: Sequence[str],
        tick: int | None,
    ) -> None:
        """不变式违规的统一收口：命令 failed + 结构化违规事件 + 同码 device.command_failed。"""

        await record.transition(
            CommandStatus.FAILED,
            failure=INVARIANT_VIOLATION_ERROR_CODE,
            detail=violation.message,
            tick=tick,
        )
        await self._emit_invariant_violation(
            command, violation, causal_parent, reverted_paths, tick
        )
        await self._emit_command_failed(
            command,
            error_code=INVARIANT_VIOLATION_ERROR_CODE,
            reason=violation.message,
            causal_parent=causal_parent,
            tick=tick,
        )
        self._deregister(record)

    async def _emit_invariant_violation(
        self,
        command: DeviceCommand,
        violation: InvariantViolation,
        causal_parent: str,
        reverted_paths: Sequence[str],
        tick: int | None,
    ) -> SimEvent:
        event = SimEvent(
            event_type=INVARIANT_VIOLATION_EVENT_TYPE,
            source="state_manager",
            timestamp=float(tick if tick is not None else command.issued_tick),
            correlation_id=command.correlation_id,
            causal_parent=causal_parent,
            # 系统级故障恒取最高优先级，保证在可观测性面板里不被普通事件淹没。
            priority=3,
            data={
                "invariant": violation.invariant,
                "message": violation.message,
                "details": violation.details,
                "command_id": command.command_id,
                "device_id": command.device_id,
                "capability": command.capability,
                "source": command.source.value,
                # 回滚留痕：静默纠正被 spec §2.2 明令禁止。
                "reverted_paths": list(reverted_paths),
            },
        )
        return await self.publish_event(event)

    async def _emit_command_failed(
        self,
        command: DeviceCommand,
        *,
        error_code: str,
        reason: str,
        causal_parent: str | None,
        tick: int | None,
    ) -> SimEvent:
        event = SimEvent(
            event_type=COMMAND_FAILED_EVENT_TYPE,
            source=LIFECYCLE_EVENT_SOURCE,
            timestamp=float(tick if tick is not None else command.issued_tick),
            correlation_id=command.correlation_id,
            causal_parent=causal_parent,
            priority=command.priority,
            data={
                "command_id": command.command_id,
                "device_id": command.device_id,
                "capability": command.capability,
                "value": command.value,
                "source": command.source.value,
                "error_code": error_code,
                "reason": reason,
            },
        )
        return await self.publish_event(event)
