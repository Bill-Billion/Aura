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
      → 不变式后置校验（步骤7.5；只对本命令引入的违规问责，见下）
      → 逐 delta 发 feedback.state_delta（步骤8）
      → succeeded

**不变式归因（§2.2）**：apply 前后各探一次世界不变式。apply 前就已违规 → 责任在仿真等
非命令写入方，发一条 ``pre_existing`` 的 ``system.invariant_violation`` 并放行本命令；
apply 前干净、apply 后违规 → 本命令改坏了世界，逆序回滚全部 delta + failed。仿真写入侧
的同名探测在 ``SimulationEngine._report_world_invariants``。

生命周期十态与迁移合法性由 S1-T3（backend/execution/command.py）拥有；十类失败码词表
由 S1-T2（backend/execution/validation.py）拥有。本模块只编排，不新增枚举。

**唯一实例（S1 review finding-8）**：executor 由 ``SimulationEngine`` 持有一台，UI 腿
（main.py）与 agent 腿（runtime.py）共用它、reset 时随世界换绑；因此 ``publish_event``
是**按次传入**的（``submit(..., publish=wrapper)``），而不是烧进构造函数——每条调用有
自己的 delta 聚合/记忆归属包装，但 ``_pending`` 注册表只有一份、有真实生产寿命。
每次用完即弃的 executor 会让 ``cancel_pending`` 永远无事可取消、取代只能发生在单次
``submit_batch`` 内部，S2「reset 取消在飞」与 S3「用户命令取代 agent 命令」全部落空。

**接缝（现在定义、后续扩展而非返工）**：
  - ``submit(..., pre_submit=hook)``：hook 在校验之后、执行之前被调用，返回
    ``PreSubmitDecision | None``（None=直通放行）。S1 出厂即 no-op；S3-T5 在此装
    仲裁/取代逻辑。hook 只对当前命令放行 + 取代其他在飞命令（当前命令的拒绝属仲裁，
    位于 proposed→approved，由 S3 另行接入），因 VALIDATED 只合法迁向 EXECUTING。
  - ``submit(..., source=...)``：覆盖命令来源并贯穿全部事件，供 S2
    ``executor.submit(source="scenario")`` 直接调用。
  - ``cancel_pending(reason)``：把在飞（未终态）命令迁到 cancelled，供 S2 reset 取消在飞。

**同步语义边界**：同步 StateManager 下 apply 立即返回，``timed_out`` 由可注入时钟诚实触发
（测量下发到反馈的耗时，超预算即报，不造假异步）。但**取代窗口是真实存在的**：流水线里每
条事件外发都是 await（逐 socket 广播），agent episode 又是后台任务，所以一条命令可以在
任意 await 期间被同控制点的另一条命令取代（review2 finding-1）。因此每个 await 之后、
下一次迁移或世界变更之前都要问一次 ``record.is_terminal``——终态吸收，再迁移会抛
IllegalTransitionError 冲出 ``submit()``；被取代是普通结局，安静收工即可。

**中途被取代的世界语义**：apply 之前被取代 → 世界零变更；apply 之后被取代 → 保留已落地的
变更并照常发 feedback，**绝不回滚**。取代者写的是同一个控制点且通常已经落地，回滚等于用
受害者的 old_value 覆盖一条更新的合法写入（把世界改坏）；相邻的 timed_out 分支能回滚，是
因为它的 apply 与回滚之间没有任何 await，没有第二个写入方插得进来。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence

from backend.core.logging import log
from backend.devices.latency import DeviceRuntimeProfile, legacy_runtime_profile
from backend.devices.operation import DeviceOperation
from backend.devices.runtime import DeviceRuntime
from backend.execution.validation import CommandErrorCode, validate_command
from backend.engine.event_bus import SimEvent
from backend.engine.state_manager import (
    INVARIANT_VIOLATION_EVENT_TYPE,
    DeltaChange,
    InvariantViolation,
    StateManager,
    find_world_invariant_violation,
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
DEVICE_EFFECT_APPLIED_EVENT_TYPE = "device.effect_applied"
FEEDBACK_EVENT_TYPE = "feedback.state_delta"
COMMAND_FAILED_EVENT_TYPE = "device.command_failed"
# §2.2 不变式违规的失败码（事件类型定义在状态层，此处 re-export 保持既有导入路径）。
# 故意不进 §10.2 十类命令失败词表：那十类描述「命令为什么没被执行」，
# 不变式违规描述「世界差点被改坏」，是系统级故障。
INVARIANT_VIOLATION_ERROR_CODE = "invariant_violation"
# 不是本命令造成的违规（apply 前世界就已不一致）统一归因到仿真，而不是当前命令来源。
INVARIANT_SIMULATION_ATTRIBUTION = "simulation"

# 命令目标键：唯一 (device_id, capability) 决定「同一控制点」，取代/在飞判定按此聚合。
CommandTarget = tuple[str, str]

# effect 钩子：apply 之后的派生变更（如房间光照重算），返回追加的 delta 列表。
EffectHook = Callable[[StateManager, DeviceCommand, "list[DeltaChange]"], "list[DeltaChange]"]


class InvariantReportDebounce:
    """不变式违规上报去抖：同一条违规（``invariant|message`` 签名）只上报一次。

    命令路径与仿真写入路径必须共用同一份签名（引擎构造时把同一个实例注入 executor）——
    否则世界一旦被仿真写坏，仿真侧报过一次之后，此后**每条**命令还会各补一条 pre_existing
    事件，一次损坏在演示里变成一路刷屏的事件洪水（review2 finding-3）。

    世界恢复一致（或换成另一条违规）后签名自动复位：去抖不是永久静音。
    """

    def __init__(self) -> None:
        self._signature: str | None = None

    @property
    def signature(self) -> str | None:
        return self._signature

    def should_report(self, violation: "InvariantViolation | None") -> bool:
        """记账并回答"这条违规现在要发事件吗"。世界一致时传 None 即可复位签名。"""

        signature = (
            None if violation is None else f"{violation.invariant}|{violation.message}"
        )
        if signature == self._signature:
            return False
        self._signature = signature
        return violation is not None

    def reset(self) -> None:
        """换世界 / reset：旧违规不再成立，下一条违规必须重新上报。"""

        self._signature = None


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
RuntimeProfileResolver = Callable[[DeviceCommand], DeviceRuntimeProfile]
DeviceFailureHandler = Callable[[str, float, int | None], Awaitable[None]]


def _property_path(capability: str) -> str:
    """能力名 → StateManager 点路径：power 是顶层字段，其余能力镜像在 extra。"""

    return "power" if capability == "power" else f"extra.{capability}"


def command_actor(command: DeviceCommand) -> str:
    """归因主体：优先具体执行者（agent_id），无则回落四值来源。

    DeltaChange.caused_by / device.last_changed_by / action 事件 source 三处必须同口径，
    否则可观测性面板（按 agent 分组着色）与世界的"谁改的"会对不上。
    """

    return command.actor or command.source.value


def _actor_fields(command: DeviceCommand) -> dict[str, str]:
    """事件 data 里的执行者身份字段；无 actor 时返回空（不伪造 agent 身份）。"""

    if command.actor is None:
        return {}
    return {
        "agent_id": command.actor,
        "agent_name": command.actor_name or command.actor,
    }


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
        caused_by=command_actor(command),
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
        caused_by=command_actor(command),
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
        caused_by=command_actor(command),
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

    由 SimulationEngine 持有一台（非全局单例），reset 时经 ``bind_state_manager`` 换绑新世界；
    UI 腿与 agent 腿共用它，各自把 publish 包装按次传给 ``submit``/``submit_batch``。
    ``publish_event`` 是缺省包装（不传按次 publish 时用它），可为 None——此时每次调用必须
    显式传 ``publish=``。``effects`` 缺省 §3.4 各设备效果；
    ``feedback_timeout`` 缺省 None（不设超时），设值后由 ``clock`` 测量下发到反馈的耗时判定。
    """

    def __init__(
        self,
        state_manager: StateManager,
        publish_event: PublishEvent | None = None,
        *,
        effects: EffectHook | None = None,
        clock: Clock = time.monotonic,
        feedback_timeout: float | None = None,
        invariant_debounce: InvariantReportDebounce | None = None,
        device_runtime: DeviceRuntime | None = None,
        runtime_profile: RuntimeProfileResolver = legacy_runtime_profile,
        sim_time_source: Callable[[], float] = lambda: 0.0,
        run_id_source: Callable[[], str | None] = lambda: None,
        device_failure_handler: DeviceFailureHandler | None = None,
    ) -> None:
        self.state_manager = state_manager
        self.publish_event = publish_event
        self.effects = effects if effects is not None else default_device_effects
        self.clock = clock
        self.feedback_timeout = feedback_timeout
        self.runtime_profile = runtime_profile
        self.sim_time_source = sim_time_source
        self.run_id_source = run_id_source
        self.device_failure_handler = device_failure_handler
        self.device_runtime = device_runtime or DeviceRuntime()
        self.device_runtime.bind_driver(self)
        # 与仿真写入路径共用的违规上报去抖（引擎注入同一实例）；独立构造时自带一份。
        self.invariant_debounce = (
            invariant_debounce if invariant_debounce is not None else InvariantReportDebounce()
        )
        # 在飞命令注册表：键=目标控制点，值=尚未终态的记录（S1 同步下多为瞬态；S2/S3 承载异步在飞）。
        self._pending: dict[CommandTarget, CommandRecord] = {}

    @property
    def pending(self) -> dict[CommandTarget, CommandRecord]:
        """在飞命令的只读快照。"""

        return dict(self._pending)

    async def bind_state_manager(
        self,
        state_manager: StateManager,
        *,
        reason: str = "state_manager_rebound",
        tick: int | None = None,
    ) -> list[CommandRecord]:
        """换绑世界（reset / run 切换）：先把在飞命令**带事件地**取消，再换世界。

        S1 时这里是一句 ``self._pending.clear()``——一次无事件的静默丢弃。当时那条路
        走不到（注册表只在同步调用内瞬态存在），但 S2 的 run 模型让它可达：reset 换世界、
        runtime 发现世界被换过都会调它。静默丢弃正是 S1 到处根治的缺陷类：命令消失、
        生命周期停在非终态、可观测性面板永远等一个不会来的收尾。

        取消发生在换世界**之前**（cancel-before-swap）：这些记录属于旧世界，
        它们的终态迁移不该记在新世界头上。
        """

        cancelled = await self.cancel_pending(reason, tick=tick)
        self.device_runtime.reset()
        self.state_manager = state_manager
        # cancel_pending 已逐条注销；这里兜底清掉不可合法取消的残留（终态记录）。
        self._pending.clear()
        # 换了世界，旧世界的违规不再成立：去抖签名必须跟着复位，否则新世界的同名违规会被吞掉。
        self.invariant_debounce.reset()
        return cancelled

    async def advance_device_runtime(
        self, sim_time_s: float, *, tick: int | None = None
    ) -> None:
        """Advance device work to *sim_time_s* without consulting wall time."""

        await self.device_runtime.advance(
            sim_time_s, tick=tick, active_run_id=self.run_id_source()
        )

    async def interrupt_device_operations(
        self, *, reason: str = "safety_interrupt", tick: int | None = None
    ) -> list[DeviceOperation]:
        return await self.device_runtime.interrupt(reason=reason, tick=tick)

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
        publish: PublishEvent | None = None,
    ) -> CommandRecord:
        """提交单条命令，跑完整生命周期流水线，返回其 CommandRecord。

        ``publish``：本次调用的事件外发包装（UI 腿聚合 delta、agent 腿另记 agent 记忆），
        不传则用构造时的缺省包装。按次传入是共用一台 executor 的前提。
        """

        command = self._with_source(command, source)
        return await self._run(
            command, pre_submit=pre_submit, tick=tick, publish=self._resolve_publish(publish)
        )

    async def submit_batch(
        self,
        commands: Sequence[DeviceCommand],
        *,
        source: CommandSource | str | None = None,
        pre_submit: PreSubmitHook | None = None,
        tick: int | None = None,
        publish: PublishEvent | None = None,
    ) -> list[CommandRecord]:
        """按顺序提交一批命令；同批内同一控制点的旧命令被最新命令取代（不执行）。"""

        resolved_publish = self._resolve_publish(publish)
        prepared = [self._with_source(command, source) for command in commands]
        winner_by_target: dict[CommandTarget, str] = {}
        for command in prepared:
            winner_by_target[(command.device_id, command.capability)] = command.command_id

        records: list[CommandRecord] = []
        for command in prepared:
            target = (command.device_id, command.capability)
            if winner_by_target[target] != command.command_id:
                # 被同批更新命令取代：出生后直接 superseded，绝不落地。
                record = await self._propose(command, tick, publish=resolved_publish)
                await self._supersede(record, tick)
                records.append(record)
            else:
                records.append(
                    await self._run(
                        command,
                        pre_submit=pre_submit,
                        tick=tick,
                        publish=resolved_publish,
                    )
                )
        return records

    async def propose(
        self,
        command: DeviceCommand,
        *,
        tick: int | None = None,
        publish: PublishEvent | None = None,
    ) -> CommandRecord:
        """S3-T5 仲裁门入口：只让命令**出生**（``proposed``）并登记在飞，不跑流水线。

        S1 的 ``submit()`` 把 propose 与执行焊在一起，因为那时 ``proposed → approved``
        是自动放行的。S3 把仲裁装进这条岔口之后，"已提出、尚未批准"成了一个**真实存在
        且可被观测的窗口**：仲裁门在这里开窗，:meth:`execute_approved` 关窗。

        开窗的副作用正是它存在的理由——记录进了 ``_pending``，于是一条用户命令能在
        agent 命令还在等仲裁时把它取代掉（S1 文档把取代范围写成"同批/待发队列内"，
        这里把它扩宽到仲裁窗口）。
        """

        return await self._propose(command, tick, publish=self._resolve_publish(publish))

    async def execute_approved(
        self,
        record: CommandRecord,
        *,
        pre_submit: PreSubmitHook | None = None,
        tick: int | None = None,
        publish: PublishEvent | None = None,
    ) -> CommandRecord:
        """S3-T5 仲裁门入口：接手一条**已由仲裁批准**的 ``proposed`` 记录跑完流水线。

        与 :meth:`submit` 的唯一差别是不再自己 propose——否则同一条命令会发出两条
        ``None → proposed`` 生命周期事件，因果树上凭空多一个节点。

        终态早退是必须的：开窗期间它可能已经被用户命令取代，此时再迁移会抛
        IllegalTransitionError 冲出调用方（正是 review2 finding-1 的现场）。
        """

        if record.is_terminal:
            return record
        resolved_publish = self._resolve_publish(publish)
        try:
            return await self._pipeline(
                record,
                record.command,
                pre_submit=pre_submit,
                tick=tick,
                publish=resolved_publish,
            )
        except asyncio.CancelledError:
            # 与 _run 同一条收尾纪律：绝不留非终态的幽灵记录。
            try:
                if (
                    not record.is_terminal
                    and CommandStatus.CANCELLED in LEGAL_TRANSITIONS[record.status]
                ):
                    await record.transition(
                        CommandStatus.CANCELLED, detail="command task cancelled", tick=tick
                    )
            finally:
                self._deregister(record)
            raise

    async def report_command_failed(
        self,
        command: DeviceCommand,
        *,
        error_code: str,
        reason: str,
        causal_parent: str | None,
        tick: int | None = None,
        publish: PublishEvent | None = None,
    ) -> SimEvent:
        """S3-T5 仲裁门入口：一条**没能走到执行**的命令仍按 §10.2 词表如实上报失败。

        为什么需要它：仲裁在 ``proposed`` 阶段就能否掉一条命令（例如设备离线，§9.2 第五类），
        于是它永远到不了 :func:`validate_command`。但 S0-4 那条护栏要求「同一条坏命令在
        agent 腿与 UI 腿拿到**完全相同**的 §10.2 错误码」——UI 腿不受仲裁单边拒绝约束，
        照常走校验拿到 ``unknown_device``；agent 腿若只留一条"仲裁落败"，两条腿就在
        错误词表上分叉了，而那正是 S0-4 要钉死的东西。

        本方法不新增词表、不新增事件类型，只是把既有的 ``device.command_failed`` 发射口
        开放给仲裁门——失败码仍由 ``validation.py`` 计算，事件仍由 executor 发。
        """

        return await self._emit_command_failed(
            command,
            error_code=error_code,
            reason=reason,
            causal_parent=causal_parent,
            tick=tick,
            publish=publish,
        )

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
        publish: PublishEvent,
    ) -> CommandRecord:
        target = (command.device_id, command.capability)
        # 同一控制点已有在飞命令 → 被本命令取代（跨调用/异步在飞的取代接缝）。
        # 注册表由引擎持有的那台 executor 承载，所以"先前那条命令"可以来自另一次调用。
        existing = self._pending.get(target)
        if existing is not None and not existing.is_terminal:
            await self._supersede(existing, tick)

        record = await self._propose(command, tick, publish=publish)
        try:
            return await self._pipeline(
                record, command, pre_submit=pre_submit, tick=tick, publish=publish
            )
        except asyncio.CancelledError:
            # 提交任务被取消（runtime 砍上一轮 episode / reset）时绝不能留幽灵记录：
            # 注册表现在有真实生产寿命，残留的非终态记录会让下一条同控制点的合法命令
            # 发出一条它其实没经历过的 superseded 生命周期（review2 finding-1 子缺陷）。
            try:
                if (
                    not record.is_terminal
                    and CommandStatus.CANCELLED in LEGAL_TRANSITIONS[record.status]
                ):
                    await record.transition(
                        CommandStatus.CANCELLED, detail="command task cancelled", tick=tick
                    )
            finally:
                self._deregister(record)
            raise

    async def _pipeline(
        self,
        record: CommandRecord,
        command: DeviceCommand,
        *,
        pre_submit: PreSubmitHook | None,
        tick: int | None,
        publish: PublishEvent,
    ) -> CommandRecord:
        """propose 之后的完整流水线。

        每个 ``record.is_terminal`` 早退点都紧跟一个 await：期间同控制点的另一条命令可能
        把本记录取代掉（或 reset 取消掉）。终态是吸收态，此时再迁移会抛
        IllegalTransitionError 冲出 ``submit()``——那正是 review2 finding-1 的现场：UI 腿
        把一条 §10.2 失败降级成 internal_error 并跳过 STATE_DELTA，agent 腿则整条 episode
        带着未取回的异常中断。被取代是普通结局，终态迁移与生命周期事件已由 ``_supersede``
        发过，这里安静收工即可。
        """

        if record.is_terminal:
            return record
        # S1 自动放行：仲裁（proposed→approved 的拒绝分支）留给 S3，此处不做全序仲裁。
        await record.transition(CommandStatus.APPROVED, tick=tick)

        # §3.3 六级校验（S1-T2 唯一实现）：失败即 approved→failed，绝不下发、绝不动世界。
        failure = validate_command(
            self.state_manager.world,
            command.device_id,
            command.capability,
            command.value,
        )
        if failure is not None:
            if record.is_terminal:
                return record
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
                publish=publish,
            )
            self._deregister(record)
            return record

        if record.is_terminal:
            return record
        await record.transition(CommandStatus.VALIDATED, tick=tick)

        # 接缝：pre_submit 在校验之后、执行之前。S1 no-op；S3 在此装仲裁/取代。
        decision = pre_submit(command) if pre_submit is not None else None
        if decision is not None:
            for victim_target in decision.superseded_targets:
                victim = self._pending.get(victim_target)
                if victim is not None and victim is not record and not victim.is_terminal:
                    await self._supersede(victim, tick)

        if record.is_terminal:
            return record
        await record.transition(CommandStatus.EXECUTING, tick=tick)
        action_event = await self._emit_action(command, tick, publish=publish)

        # 动作已下发但世界尚未变更：此刻被取代 → 世界零变更收工（apply 绝不能再跑）。
        if record.is_terminal:
            return record
        now = float(self.sim_time_source())
        self.device_runtime.schedule(
            record,
            publish=publish,
            action_event_id=action_event.event_id,
            profile=self.runtime_profile(command),
            sim_time_s=now,
            run_id=self.run_id_source(),
        )
        # The default profile drains at the current simulated instant, preserving
        # the v1 API contract that submit() returns a terminal record.
        await self.advance_device_runtime(now, tick=tick)
        return record

    async def apply_device_operation(
        self, operation: DeviceOperation, *, sim_time_s: float, tick: int | None
    ) -> bool:
        """Apply ground truth for a due operation; runtime itself cannot mutate it."""

        record = operation.record
        command = operation.command
        publish = operation.publish
        # Revalidate only if simulated time could have advanced since admission.
        # The zero-delay v1 path keeps its historical single validation call.
        failure = None
        if (
            operation.start_at_s > operation.issued_at_s
            or operation.finish_at_s > operation.issued_at_s
        ):
            failure = validate_command(
                self.state_manager.world,
                command.device_id,
                command.capability,
                command.value,
            )
        if failure is not None:
            await self.fail_device_operation(
                operation,
                status=CommandStatus.FAILED,
                failure_code=failure.code.value,
                detail=failure.message,
                tick=tick,
            )
            return False

        device = self.state_manager.world.devices.get(command.device_id)
        if device is not None:
            try:
                self.state_manager.check_command_invariants(
                    device, command.capability, caused_by=command.source.value
                )
            except InvariantViolation as exc:
                await self._fail_on_invariant(
                    record,
                    command,
                    exc,
                    operation.action_event_id,
                    (),
                    tick,
                    publish=publish,
                )
                return False

        pre_existing = find_world_invariant_violation(self.state_manager.world)
        if self.invariant_debounce.should_report(pre_existing):
            await self._emit_invariant_violation(
                command,
                pre_existing,
                operation.action_event_id,
                (),
                tick,
                publish=publish,
                pre_existing=True,
            )
            if record.is_terminal:
                return False

        dispatch_at = (
            self.clock()
            if operation.legacy_wall_clock_timeout
            and self.feedback_timeout is not None
            else None
        )
        try:
            deltas = self.state_manager.apply_action(
                agent_id=command_actor(command),
                device_id=command.device_id,
                property_path=_property_path(command.capability),
                new_value=command.value,
                reason=command.reason or "",
                caused_by_event_id=operation.action_event_id,
            )
        except KeyError as exc:
            await self.fail_device_operation(
                operation,
                status=CommandStatus.FAILED,
                failure_code=CommandErrorCode.UNKNOWN_DEVICE.value,
                detail=str(exc),
                tick=tick,
            )
            return False

        if (
            dispatch_at is not None
            and self.feedback_timeout is not None
            and self.clock() - dispatch_at > self.feedback_timeout
        ):
            reverted = self.state_manager.revert(deltas)
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
                causal_parent=operation.action_event_id,
                reverted_paths=reverted,
                tick=tick,
                publish=publish,
            )
            self._deregister(record)
            return False

        deltas = list(deltas) + list(self.effects(self.state_manager, command, deltas))
        for delta in deltas:
            if delta.caused_by_event_id is None:
                delta.caused_by_event_id = operation.action_event_id
        violation = find_world_invariant_violation(self.state_manager.world)
        if violation is not None and pre_existing is None:
            reverted = self.state_manager.revert(deltas)
            await self._fail_on_invariant(
                record,
                command,
                violation,
                operation.action_event_id,
                reverted,
                tick,
                publish=publish,
            )
            return False

        operation.deltas = deltas
        operation.effect_applied_at_s = sim_time_s
        effect = await self._emit_device_effect(operation, sim_time_s, tick)
        operation.effect_event_id = effect.event_id
        return True

    async def deliver_device_feedback(
        self, operation: DeviceOperation, *, sim_time_s: float, tick: int | None
    ) -> None:
        record = operation.record
        for delta in operation.deltas:
            await self._emit_feedback(
                operation.command,
                delta,
                (
                    operation.effect_event_id
                    if operation.feedback_causal_parent_effect
                    else operation.action_event_id
                )
                or operation.action_event_id,
                tick,
                publish=operation.publish,
                sim_time_s=sim_time_s,
            )
        if not record.is_terminal:
            await record.transition(
                CommandStatus.SUCCEEDED, tick=tick, sim_time_s=sim_time_s
            )
        self._deregister(record)

    async def activate_device_failure(
        self, device_id: str, *, sim_time_s: float, tick: int | None
    ) -> None:
        """Delegate availability ground truth to the engine's simulator path."""

        if self.device_failure_handler is not None:
            await self.device_failure_handler(device_id, sim_time_s, tick)

    async def fail_device_operation(
        self,
        operation: DeviceOperation,
        *,
        status: CommandStatus,
        failure_code: str,
        detail: str,
        tick: int | None,
        sim_time_s: float | None = None,
    ) -> None:
        record = operation.record
        if not record.is_terminal:
            await record.transition(
                status,
                failure=failure_code,
                detail=detail,
                tick=tick,
                sim_time_s=sim_time_s,
            )
            await self._emit_command_failed(
                operation.command,
                error_code=failure_code,
                reason=detail,
                causal_parent=operation.effect_event_id or operation.action_event_id,
                tick=tick,
                publish=operation.publish,
                sim_time_s=sim_time_s,
            )
        self._deregister(record)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _resolve_publish(self, publish: PublishEvent | None) -> PublishEvent:
        """本次调用的 publish 包装：显式传入优先，否则回落构造时的缺省包装。"""

        resolved = publish if publish is not None else self.publish_event
        if resolved is None:
            raise RuntimeError(
                "CommandExecutor 未绑定 publish_event：构造时注入或按次传 publish="
            )
        return resolved

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
        self,
        command: DeviceCommand,
        tick: int | None,
        *,
        publish: PublishEvent | None = None,
    ) -> CommandRecord:
        # 记录随身带走本次调用的 publish 包装：此后它的每次迁移（含被别的调用取代/被 reset
        # 取消）都仍从提交它的那条腿外发，因果链不会因为共用 executor 而串台。
        record = await CommandRecord.propose(
            command, publish if publish is not None else self.publish_event, tick=tick
        )
        target = (command.device_id, command.capability)
        # 出生事件外发也是 await 点：期间可能另一条同控制点命令刚完成注册，而它在 _run 入口
        # 查注册表时本记录还不存在（两条命令几乎同时提交）。注册的这一刻再判一次取代，
        # 保证「同一控制点最多一条在飞」不被并发提交绕过。
        previous = self._pending.get(target)
        if previous is not None and previous is not record and not previous.is_terminal:
            await self._supersede(previous, tick)
        self._pending[target] = record
        return record

    async def _supersede(self, record: CommandRecord, tick: int | None) -> None:
        """把一条在飞命令落账成 superseded（取代的唯一收口）。

        终态记录只注销不迁移；非终态记录必须发出 superseded 生命周期事件后再注销——
        静默从注册表里删掉一条活命令等于零可观测性（review2 finding-1 子缺陷）。
        """

        if not record.is_terminal:
            if CommandStatus.SUPERSEDED in LEGAL_TRANSITIONS[record.status]:
                await record.transition(
                    CommandStatus.SUPERSEDED,
                    failure=CommandErrorCode.SUPERSEDED_BY_NEWER_COMMAND.value,
                    tick=tick,
                )
            else:
                # 当前迁移表里所有非终态都能迁向 superseded；真走到这里说明迁移表被改坏了，
                # 记一条 warning 而不是无声丢弃。
                log.warning(
                    "command_supersede_illegal_transition",
                    command_id=record.command.command_id,
                    status=record.status.value,
                )
        self._deregister(record)

    def forget(self, record: CommandRecord) -> None:
        """把一条**已终态**的记录移出在飞注册表（S3-T5 仲裁门收尾用）。

        非终态记录一律不动：静默删掉一条活命令等于零可观测性，那是 review2 finding-1
        的原症状。要终结它请走 ``cancel_pending`` / ``_supersede``，它们都会先落账再注销。
        """

        if record.is_terminal:
            self._deregister(record)

    def _deregister(self, record: CommandRecord) -> None:
        key = (record.command.device_id, record.command.capability)
        if self._pending.get(key) is record:
            del self._pending[key]

    async def _emit_action(
        self, command: DeviceCommand, tick: int | None, *, publish: PublishEvent | None = None
    ) -> SimEvent:
        event = SimEvent(
            event_type=ACTION_EVENT_TYPE,
            # 事件 source 取执行者（agent_id），前端 extractEventAgentId 与 episode 图按此分组；
            # 无 actor（ui/scenario）时回落到 executor 自身。
            source=command.actor or LIFECYCLE_EVENT_SOURCE,
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
                **_actor_fields(command),
            },
        )
        return await self._resolve_publish(publish)(event)

    async def _emit_feedback(
        self,
        command: DeviceCommand,
        delta: DeltaChange,
        action_event_id: str,
        tick: int | None,
        *,
        publish: PublishEvent | None = None,
        sim_time_s: float | None = None,
    ) -> SimEvent:
        data = delta.model_dump()
        data["device_id"] = command.device_id
        data["source"] = command.source.value
        data.update(_actor_fields(command))
        event = SimEvent(
            event_type=FEEDBACK_EVENT_TYPE,
            source="state_manager",
            timestamp=float(tick if tick is not None else command.issued_tick),
            correlation_id=command.correlation_id,
            causal_parent=action_event_id,
            priority=command.priority,
            sim_time_s=sim_time_s,
            data=data,
        )
        return await self._resolve_publish(publish)(event)

    async def _emit_device_effect(
        self, operation: DeviceOperation, sim_time_s: float, tick: int | None
    ) -> SimEvent:
        """Publish ground truth separately from the fallible feedback channel."""

        command = operation.command
        event = SimEvent(
            event_type=DEVICE_EFFECT_APPLIED_EVENT_TYPE,
            source="device_runtime",
            timestamp=float(tick if tick is not None else command.issued_tick),
            correlation_id=command.correlation_id,
            causal_parent=operation.action_event_id,
            priority=command.priority,
            sim_time_s=sim_time_s,
            data={
                "operation_id": operation.operation_id,
                "operation_kind": operation.kind.value,
                "command_id": command.command_id,
                "device_id": command.device_id,
                "capability": command.capability,
                "source": command.source.value,
                "effect_applied_at_sim_time_s": sim_time_s,
                "issued_at_sim_time_s": operation.issued_at_s,
                "scheduled_start_at_sim_time_s": operation.start_at_s,
                "scheduled_finish_at_sim_time_s": operation.finish_at_s,
                "feedback_deadline_at_sim_time_s": (
                    sim_time_s + operation.feedback_timeout_s
                ),
                "feedback_delay_s": operation.feedback_delay_s,
                "feedback_timeout_s": operation.feedback_timeout_s,
                "deltas": [delta.model_dump(mode="json") for delta in operation.deltas],
                **_actor_fields(command),
            },
        )
        return await self._resolve_publish(operation.publish)(event)

    async def _fail_on_invariant(
        self,
        record: CommandRecord,
        command: DeviceCommand,
        violation: InvariantViolation,
        causal_parent: str,
        reverted_paths: Sequence[str],
        tick: int | None,
        *,
        publish: PublishEvent | None = None,
    ) -> None:
        """不变式违规的统一收口：命令 failed + 结构化违规事件 + 同码 device.command_failed。"""

        await record.transition(
            CommandStatus.FAILED,
            failure=INVARIANT_VIOLATION_ERROR_CODE,
            detail=violation.message,
            tick=tick,
        )
        await self._emit_invariant_violation(
            command, violation, causal_parent, reverted_paths, tick, publish=publish
        )
        await self._emit_command_failed(
            command,
            error_code=INVARIANT_VIOLATION_ERROR_CODE,
            reason=violation.message,
            causal_parent=causal_parent,
            tick=tick,
            publish=publish,
        )
        self._deregister(record)

    async def _emit_invariant_violation(
        self,
        command: DeviceCommand,
        violation: InvariantViolation,
        causal_parent: str,
        reverted_paths: Sequence[str],
        tick: int | None,
        *,
        publish: PublishEvent | None = None,
        pre_existing: bool = False,
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
                # 归因三件套：违规在本命令之前就成立吗？该记在谁头上？本命令因此失败了吗？
                # 前端/评估器据此区分「命令把世界改坏」与「命令只是撞上了已坏的世界」。
                "pre_existing": pre_existing,
                "attributed_to": (
                    INVARIANT_SIMULATION_ATTRIBUTION
                    if pre_existing
                    else command.source.value
                ),
                "command_failed": not pre_existing,
                # 回滚留痕：静默纠正被 spec §2.2 明令禁止。
                "reverted_paths": list(reverted_paths),
                **_actor_fields(command),
            },
        )
        return await self._resolve_publish(publish)(event)

    async def _emit_command_failed(
        self,
        command: DeviceCommand,
        *,
        error_code: str,
        reason: str,
        causal_parent: str | None,
        reverted_paths: Sequence[str] = (),
        tick: int | None,
        publish: PublishEvent | None = None,
        sim_time_s: float | None = None,
    ) -> SimEvent:
        event = SimEvent(
            event_type=COMMAND_FAILED_EVENT_TYPE,
            source=LIFECYCLE_EVENT_SOURCE,
            timestamp=float(tick if tick is not None else command.issued_tick),
            correlation_id=command.correlation_id,
            causal_parent=causal_parent,
            priority=command.priority,
            sim_time_s=sim_time_s,
            data={
                "command_id": command.command_id,
                "device_id": command.device_id,
                "capability": command.capability,
                "value": command.value,
                "source": command.source.value,
                "error_code": error_code,
                "reason": reason,
                # 回滚留痕：绝大多数失败分支世界零变更（空列表），超时分支带回滚路径。
                "reverted_paths": list(reverted_paths),
                **_actor_fields(command),
            },
        )
        return await self._resolve_publish(publish)(event)
