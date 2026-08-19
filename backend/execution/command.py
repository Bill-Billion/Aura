"""Command 与生命周期模型（spec §10.1）。

所有设备变更走唯一 CommandExecutor 路径，每条命令必须显式经过十态生命周期，
每次迁移即发一条 `command.lifecycle` 事件，让研究者能重建"命令为何影响/未影响世界"。
本模块只负责命令数据与状态机；能力校验（§3.3）与十类失败码（§10.2）由
`backend/execution/validation.py`（S1-T2）拥有——因此 ``transition`` 的 ``failure``
参数是纯字符串失败码，本模块不定义/不导入该枚举，保持零跨任务依赖。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field

from backend.engine.event_bus import SimEvent
from backend.models.versioning import SUPPORTED_COMMAND_SCHEMA_VERSION

# 生命周期事件复用既有 SIM_EVENT 通道（schemas.py 不新增 WS 消息类型），
# 前端 useWebSocket.ts 已有 case 'SIM_EVENT'，零改动即可见。
LIFECYCLE_EVENT_TYPE = "command.lifecycle"
LIFECYCLE_EVENT_SOURCE = "command_executor"

# publish_event 与 runtime/simulator_timer 保持同一签名：接收 SimEvent，返回 SimEvent。
PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]


class CommandSource(str, Enum):
    """命令来源四类（spec §10 执行契约的唯一入口口径）。"""

    UI = "ui"
    AGENT = "agent"
    SCENARIO = "scenario"
    RULE_FALLBACK = "rule_fallback"


class CommandStatus(str, Enum):
    """命令生命周期十态（spec §10.1）。"""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    VALIDATED = "validated"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


# 终态吸收：一旦进入终态，任何再迁移都是非法。
TERMINAL_STATES: frozenset[CommandStatus] = frozenset(
    {
        CommandStatus.REJECTED,
        CommandStatus.SUCCEEDED,
        CommandStatus.FAILED,
        CommandStatus.TIMED_OUT,
        CommandStatus.CANCELLED,
        CommandStatus.SUPERSEDED,
    }
)

# 合法迁移表（spec §10.1）。approved→failed 覆盖"校验失败"；
# arbiter winning→approved / conflict loser→rejected 给 S3 留稳定接缝。
LEGAL_TRANSITIONS: dict[CommandStatus, frozenset[CommandStatus]] = {
    CommandStatus.PROPOSED: frozenset(
        {
            CommandStatus.APPROVED,
            CommandStatus.REJECTED,
            CommandStatus.CANCELLED,
            CommandStatus.SUPERSEDED,
        }
    ),
    CommandStatus.APPROVED: frozenset(
        {
            CommandStatus.VALIDATED,
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.SUPERSEDED,
        }
    ),
    # validated 也必须能被取代/取消：共用一台 executor 之后，记录会真的停在 validated 上
    # （下一次迁移的事件外发是 await 点），此时来一条同控制点的新命令或一次 reset，
    # 若这里没有出边，取代/取消就只能把记录从注册表里静默丢掉——零可观测性（review2 finding-1）。
    CommandStatus.VALIDATED: frozenset(
        {
            CommandStatus.EXECUTING,
            CommandStatus.CANCELLED,
            CommandStatus.SUPERSEDED,
        }
    ),
    CommandStatus.EXECUTING: frozenset(
        {
            CommandStatus.SUCCEEDED,
            CommandStatus.FAILED,
            CommandStatus.TIMED_OUT,
            CommandStatus.CANCELLED,
            CommandStatus.SUPERSEDED,
        }
    ),
    # 终态：出边为空
    CommandStatus.REJECTED: frozenset(),
    CommandStatus.SUCCEEDED: frozenset(),
    CommandStatus.FAILED: frozenset(),
    CommandStatus.TIMED_OUT: frozenset(),
    CommandStatus.CANCELLED: frozenset(),
    CommandStatus.SUPERSEDED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    """状态机拒绝的非法迁移（跳级或从终态再迁移）。"""

    def __init__(self, from_status: CommandStatus, to_status: CommandStatus) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"illegal command transition {from_status.value} -> {to_status.value}"
        )


class DeviceCommand(BaseModel):
    """一条设备变更请求（不可变意图，状态存于 CommandRecord）。

    correlation_id / causal_parent 继承命令所属根事件（§4.4），生命周期事件由此继承。
    ``capability`` 即被设置的设备属性（property），与 spec §3 能力矩阵同名。

    ``source`` 是四值审计口径（谁这一类），``actor`` 是执行者身份（具体哪一个）——两者都必须
    保留：可观测性产品要回答的是"哪个 agent 干的"，只留 source 就退化成 'agent' 一个值。
    agent/rule_fallback 路径把 actor 置为 agent_id、actor_name 置为展示名；ui/scenario 留 None，
    归因回落到 ``source.value``（绝不伪造 agent 身份字段）。
    """

    command_schema_version: str = SUPPORTED_COMMAND_SCHEMA_VERSION
    command_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    source: CommandSource
    actor: str | None = None
    actor_name: str | None = None
    device_id: str
    capability: str
    value: Any = None
    reason: str | None = None
    correlation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    causal_parent: str | None = None
    issued_tick: int = 0
    priority: int = Field(default=1, ge=0, le=3)


@dataclass
class LifecycleTransition:
    """一次迁移的时间戳留痕，用于重建因果链。"""

    from_status: CommandStatus | None
    to_status: CommandStatus
    failure_code: str | None
    detail: str | None
    tick: int
    wall_time: float


class CommandRecord:
    """包裹一条 DeviceCommand 的可变生命周期状态机。

    ``propose`` 出生即发 ``None -> proposed`` 事件；``transition`` 每次迁移都校验
    合法性后再发事件——非法迁移抛 IllegalTransitionError 且不发事件、不改状态。
    """

    def __init__(
        self,
        command: DeviceCommand,
        *,
        publish_event: PublishEvent | None = None,
        status: CommandStatus = CommandStatus.PROPOSED,
    ) -> None:
        self.command = command
        self.publish_event = publish_event
        self.status = status
        self.failure_code: str | None = None
        self.detail: str | None = None
        now = time.time()
        self.created_at: float = now
        self.updated_at: float = now
        self.history: list[LifecycleTransition] = []

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @classmethod
    async def propose(
        cls,
        command: DeviceCommand,
        publish_event: PublishEvent | None = None,
        *,
        detail: str | None = None,
        tick: int | None = None,
    ) -> "CommandRecord":
        """创建记录并发出生事件（from_status=None -> proposed）。"""
        record = cls(command, publish_event=publish_event, status=CommandStatus.PROPOSED)
        await record._emit(
            from_status=None,
            to_status=CommandStatus.PROPOSED,
            failure=None,
            detail=detail,
            tick=tick,
        )
        return record

    async def transition(
        self,
        to: CommandStatus | str,
        failure: str | None = None,
        *,
        detail: str | None = None,
        tick: int | None = None,
    ) -> SimEvent | None:
        """迁移到 *to*：先校验合法性、再落状态、最后发事件。

        非法迁移抛 IllegalTransitionError（不发事件、不改状态）。
        *failure* 为 §10.2 失败码字符串（枚举归 validation.py 所有）。

        **先落状态再发事件**是并发正确性要求（review2 finding-1）：事件外发是 await 点，
        期间同一控制点的另一条命令可能对本记录发起合法迁移（取代）或 reset 取消它。
        若等 emit 回来再赋值，那次迁移会被这里覆盖掉——一条已经 superseded 的记录会"复活"
        成 executing 继续下发。校验与赋值之间没有 await，因此这一步对事件循环是原子的。
        """
        to_status = CommandStatus(to)
        if to_status not in LEGAL_TRANSITIONS[self.status]:
            raise IllegalTransitionError(self.status, to_status)

        from_status = self.status
        self.status = to_status
        if failure is not None:
            self.failure_code = failure
        if detail is not None:
            self.detail = detail

        return await self._emit(
            from_status=from_status,
            to_status=to_status,
            failure=failure,
            detail=detail,
            tick=tick,
        )

    def build_lifecycle_event(
        self,
        *,
        from_status: CommandStatus | None,
        to_status: CommandStatus,
        failure: str | None = None,
        detail: str | None = None,
        tick: int | None = None,
    ) -> SimEvent:
        """把一次迁移物化为 SimEvent（不发布，供纯数据/测试使用）。

        correlation_id / causal_parent 继承命令所属根事件（§4.4）。
        """
        data: dict[str, Any] = {
            "command_schema_version": self.command.command_schema_version,
            "command_id": self.command.command_id,
            "device_id": self.command.device_id,
            "capability": self.command.capability,
            "from_status": from_status.value if from_status is not None else None,
            "to_status": to_status.value,
            "source": self.command.source.value,
            "detail": detail,
        }
        # 执行者身份只在存在时出现：ui/scenario 命令不该带 agent_* 字段（前端按有无分组）。
        if self.command.actor is not None:
            data["agent_id"] = self.command.actor
            data["agent_name"] = self.command.actor_name or self.command.actor
        if failure is not None:
            data["failure_code"] = failure

        return SimEvent(
            event_type=LIFECYCLE_EVENT_TYPE,
            source=LIFECYCLE_EVENT_SOURCE,
            timestamp=float(tick if tick is not None else self.command.issued_tick),
            wall_time=time.time(),
            correlation_id=self.command.correlation_id,
            causal_parent=self.command.causal_parent,
            priority=self.command.priority,
            data=data,
        )

    async def _emit(
        self,
        *,
        from_status: CommandStatus | None,
        to_status: CommandStatus,
        failure: str | None,
        detail: str | None,
        tick: int | None,
    ) -> SimEvent:
        event = self.build_lifecycle_event(
            from_status=from_status,
            to_status=to_status,
            failure=failure,
            detail=detail,
            tick=tick,
        )
        self.updated_at = event.wall_time
        self.history.append(
            LifecycleTransition(
                from_status=from_status,
                to_status=to_status,
                failure_code=failure,
                detail=detail,
                tick=int(event.timestamp),
                wall_time=event.wall_time,
            )
        )
        if self.publish_event is not None:
            await self.publish_event(event)
        return event
