import asyncio
import os
import time
import uuid
from collections import defaultdict
from typing import Any, Callable, Coroutine, Literal

from pydantic import BaseModel, Field

from backend.core.logging import log

# §4.5 事件生成模式：**根事件是怎么进这个世界的**。规格定义三种"被生成"的模式
# （scripted/rule_based/stochastic）；system 是本实现补的一种——引擎自己发的
# timer/reset 这类生命周期根事件。
#
# 派生事件（reasoning.* / command.lifecycle / action.* / feedback.*）与用户直发的
# user.command **不带**生成模式：前者的来源由 causal_parent + source 表达，后者根本
# 不是平台生成的。曾经这里还有一个 'agent' 成员，backend/ 里没有任何生产方写过它
# （S2 review：97/138 条事件 mode=None），留着只会让 /api/runs/{id}/events?generation_mode=
# 看起来能筛出"agent 事件"——所以删掉，别在枚举里立一个 grep 就能证伪的承诺。
EventGenerationMode = Literal['scripted', 'rule_based', 'stochastic', 'system']

# —— S3-T6：因果深度上限（反事件风暴）——————————————————————————————
#
# 旧的 tick 循环自带一层限速：一拍最多推进一次世界，派生事件再多也被拍频压着。
# 事件驱动之后这层保护没有了——一条 feedback → 触发新根事件 → 再派生 feedback 的回环
# 可以在毫秒级把总线、WS 和 events.jsonl 一起打满，而且现场只留下"事件特别多"这一个
# 症状，谁派生谁根本读不出来。
#
# 因此总线按 ``causal_parent`` 给每条事件盖一个 ``depth``，超过上限就**拒发**。
# 关键设计：拒发不等于静默丢弃——每条 correlation 发一条 ``system.event_storm_suppressed``
# 把"这里有一条链被刹住了、刹在多深、是什么事件"写进事件流本身。静默丢弃会把风暴
# 变成一个更难查的故障（链在中途断掉，看起来像 agent 死了），正是 S1 全程根治的那类
# 静默失败。抑制事件本身每条 correlation 只发一条，否则防风暴自己就是新的风暴。
MAX_CAUSAL_DEPTH_ENV = 'AGENT_MAX_CAUSAL_DEPTH'
DEFAULT_MAX_CAUSAL_DEPTH = 16
EVENT_STORM_SUPPRESSED_EVENT_TYPE = 'system.event_storm_suppressed'
EVENT_STORM_SUPPRESSED_REASON = 'causal_depth_exceeded'


class WorldEvent(BaseModel):
    event_type: str
    source: str
    timestamp: float
    data: dict[str, Any] = Field(default_factory=dict)


class SimEvent(WorldEvent):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    wall_time: float = Field(default_factory=time.time)
    correlation_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    causal_parent: str | None = None
    priority: int = Field(default=1, ge=0, le=3)

    # --- S2 可复现元数据（§4.5 五项 + §11 run 模型锚点）---------------------
    # 全部可选且默认 None：既有事件构造式（17 个测试文件 + 仿真/agent/executor 三条产线）
    # 一行不改仍然合法，S2 之后的生产方逐步补齐。
    run_id: str | None = None
    scenario_id: str | None = None
    event_generation_mode: EventGenerationMode | None = None
    generation_rule_id: str | None = None  # rule_based 事件必填：命中的是哪条规则
    rng_stream: str | None = None  # stochastic 事件必填：抽样用的是哪条命名随机子流
    # seq：run 内单调发布序号，由 EventBus 盖章（publish，或需要先外发时用 stamp）；
    # 生产方不要自己填。同一条事件只分配一次号，盖过章再 publish 不会改号。
    # 存在的理由：timestamp 是 tick 计数，同一 tick 内的事件全部并列，无法稳定排序；
    # seq 才是 canonical trace（S2-T9 字节一致性门）唯一可靠的排序锚。
    seq: int | None = None
    # sim_time_s：run 起点起算的模拟秒（float）。与既有三个时间域的关系见
    # docs/architecture/sim-event-schema.md §11——本字段只做"新增统一"，不改 timestamp 语义。
    sim_time_s: float | None = None
    # depth：因果树深度（根事件 = 0，其余 = 父事件 depth + 1），由 EventBus 盖章。
    # 生产方不要自己填。它同时是两件事的载体：反事件风暴的闸门读数（见
    # DEFAULT_MAX_CAUSAL_DEPTH），以及 S5 因果树不必回溯全链就能画层级的现成层号。
    # **已知局限**：父事件被总线的 1000 条环形历史挤掉之后，子事件的 depth 从 0 重新起算
    # （查不到父就当自己是根）。对一条正常 episode（实测树深 ≤ 7）不可能发生；对一条
    # 真的跑了上千条事件的失控链，重新起算意味着闸门晚一点才刹住，不会失效。
    depth: int = Field(default=0, ge=0)

    @classmethod
    def from_world_event(cls, event: WorldEvent, **overrides: Any) -> 'SimEvent':
        payload = event.model_dump()
        payload.update(overrides)
        return cls(**payload)


Handler = Callable[[SimEvent], Coroutine | None]
SimTimeSource = Callable[[], float]


def _resolve_max_causal_depth(value: int | None = None) -> int:
    """因果深度上限：显式入参 > 环境变量 > 默认 16。``0`` = 关闭闸门。"""

    if value is not None:
        return max(0, int(value))
    raw = os.getenv(MAX_CAUSAL_DEPTH_ENV, '').strip()
    if not raw:
        return DEFAULT_MAX_CAUSAL_DEPTH
    try:
        return max(0, int(raw))
    except ValueError:
        log.warning('event_bus_bad_max_causal_depth', value=raw)
        return DEFAULT_MAX_CAUSAL_DEPTH


class EventBus:
    def __init__(self, *, max_causal_depth: int | None = None):
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._history: list[SimEvent] = []
        self._max_history: int = 1000
        self._run_id: str | None = None
        self._scenario_id: str | None = None
        self._sim_time_source: SimTimeSource | None = None
        self._next_seq: int = 0
        # 因果深度索引（event_id → depth），与 _history 同寿命：环形历史挤掉谁，
        # 这里也跟着删，否则一次长 run 会在这张表上无声地漏内存。
        self._depth_by_event_id: dict[str, int] = {}
        self.max_causal_depth = _resolve_max_causal_depth(max_causal_depth)
        # 已经报过风暴抑制的 correlation：每条只报一条（防风暴自己不能变成风暴）。
        self._storm_suppressed: dict[str, int] = {}

    # --- run 上下文 --------------------------------------------------------

    def set_run_context(self, run_id: str | None, scenario_id: str | None = None) -> None:
        """设置当前 run 上下文；此后 publish 会为缺失该字段的事件盖章。

        注意本方法不清历史也不重置 seq——换 run 时由调用方（S2-T3 RunManager）
        显式配一次 clear()，两个动作分开是为了让"保留历史换 run"这种调试用法仍然可能。
        """
        self._run_id = run_id
        self._scenario_id = scenario_id

    def set_sim_time_source(self, source: SimTimeSource | None) -> None:
        """注入模拟时钟读数源（返回 run 起点起算的模拟秒）。

        未注入时 sim_time_s 保持生产方自己填的值（通常是 None），不会凭空造时间。
        """
        self._sim_time_source = source

    @property
    def run_id(self) -> str | None:
        return self._run_id

    @property
    def scenario_id(self) -> str | None:
        return self._scenario_id

    @property
    def next_seq(self) -> int:
        """下一条发布事件将拿到的 seq（测试与 run 工件写入方用来对账）。"""
        return self._next_seq

    def clear(self) -> None:
        """清空事件历史并把 seq 归零；订阅者与 run 上下文保持不变。

        修审计发现：reset 不清历史导致 get_causal_chain / correlation 查询跨 run 穿插，
        旧 run 的事件会以"同一条链"的形态混进新世界。
        """
        self._history = []
        self._next_seq = 0
        self._depth_by_event_id.clear()
        # 深度额度跟着历史一起归零：新 run 不该继承上一个 run 的"已经报过风暴"。
        self._storm_suppressed.clear()

    # --- 订阅 --------------------------------------------------------------

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register *handler* for events matching *event_type*."""
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """Remove *handler* from *event_type* subscribers."""
        subs = self._subscribers.get(event_type, [])
        try:
            subs.remove(handler)
        except ValueError:
            pass

    def coerce_event(self, event: WorldEvent | SimEvent) -> SimEvent:
        if isinstance(event, SimEvent):
            return event
        return SimEvent.from_world_event(event)

    def stamp(self, event: WorldEvent | SimEvent) -> SimEvent:
        """在 publish 之前先盖章，给"必须先拿到盖章副本再外发"的调用方用。

        唯一的生产用例是 main.py 的 UI 根事件：它必须是这条命令外发的第一条 WS 消息
        （否则前端先看到状态变更、后看到因果头），同时又必须带 seq——S5 的因果树按
        seq 排序，无号的根节点排不进自己的子节点之前。盖过章的事件随后照常 publish，
        不会被重新编号（见 _stamp）。
        """
        return self._stamp(self.coerce_event(event))

    def _stamp(self, event: SimEvent) -> SimEvent:
        """原地盖章 run 元数据与 seq。

        run_id/scenario_id/sim_time_s 采用"缺失才填"：生产方显式给的值优先，
        否则旧 run 的在飞事件会被改写成当前 run，§2.2 的 stale_run 判定就废了。
        seq 是总线的唯一权威，但只分配一次：生产方不填（约定），第一次进总线
        （stamp() 或 publish()，谁先算谁）拿号，此后号码终身不变——否则先广播后
        publish 的那条对象会带着两个不同的 seq 出现在 WS 与 events.jsonl 上。
        """
        if event.run_id is None:
            event.run_id = self._run_id
        if event.scenario_id is None:
            event.scenario_id = self._scenario_id
        if event.sim_time_s is None and self._sim_time_source is not None:
            event.sim_time_s = float(self._sim_time_source())
        if event.seq is None:
            event.seq = self._next_seq
            self._next_seq += 1
        # depth 每次都现算：它是父事件的函数，重算是幂等的（同一个父给出同一个深度），
        # 而"只在 depth==0 时才算"会让先 stamp 后 publish 的那条路径永远停在 0。
        event.depth = self._resolve_depth(event)
        return event

    def _resolve_depth(self, event: SimEvent) -> int:
        """按 ``causal_parent`` 算深度；父不在索引里（根事件 / 已被历史挤掉）时归 0。"""

        if not event.causal_parent:
            return 0
        parent_depth = self._depth_by_event_id.get(event.causal_parent)
        if parent_depth is None:
            return 0
        return parent_depth + 1

    def _exceeds_depth_cap(self, event: SimEvent) -> bool:
        return self.max_causal_depth > 0 and event.depth > self.max_causal_depth

    def _remember(self, event: SimEvent) -> None:
        """入历史 + 入深度索引（两者必须同进同出，否则索引会漏内存）。"""

        self._history.append(event)
        self._depth_by_event_id[event.event_id] = event.depth
        if len(self._history) > self._max_history:
            evicted = self._history[: len(self._history) - self._max_history]
            self._history = self._history[-self._max_history :]
            for old in evicted:
                self._depth_by_event_id.pop(old.event_id, None)

    async def _fan_out(self, sim_event: SimEvent) -> int:
        handlers: list[Handler] = []
        handlers.extend(self._subscribers.get(sim_event.event_type, []))
        if sim_event.event_type != '*':
            handlers.extend(self._subscribers.get('*', []))

        self._remember(sim_event)

        for handler in handlers:
            result = handler(sim_event)
            if asyncio.iscoroutine(result):
                await result

        return len(handlers)

    async def publish(self, event: WorldEvent | SimEvent) -> int:
        """Publish *event* to all matching subscribers (exact + wildcard).

        超过 :attr:`max_causal_depth` 的事件**不发**：既不进历史，也不派发给订阅者
        （这就是刹车本体——继续派发等于继续给风暴供燃料）。取而代之的是每条 correlation
        一条 ``system.event_storm_suppressed``，见模块顶部 DEFAULT_MAX_CAUSAL_DEPTH 的说明。
        """

        sim_event = self._stamp(self.coerce_event(event))
        if self._exceeds_depth_cap(sim_event):
            return await self._suppress_event_storm(sim_event)
        return await self._fan_out(sim_event)

    async def _suppress_event_storm(self, refused: SimEvent) -> int:
        """拒发一条超深事件，并（每条 correlation 一次）把这件事发成事件。"""

        correlation_id = refused.correlation_id
        already = self._storm_suppressed.get(correlation_id)
        self._storm_suppressed[correlation_id] = (already or 0) + 1
        log.warning(
            'event_storm_suppressed',
            correlation_id=correlation_id,
            event_type=refused.event_type,
            source=refused.source,
            depth=refused.depth,
            max_depth=self.max_causal_depth,
            first_report=already is None,
        )
        if already is not None:
            # 同一条链后续的超深事件继续拒发，但不再重复报告。
            return 0

        notice = SimEvent(
            event_type=EVENT_STORM_SUPPRESSED_EVENT_TYPE,
            source='event_bus',
            timestamp=refused.timestamp,
            correlation_id=correlation_id,
            # 挂在**被拒事件的父**上：被拒的那条不存在，指向它会留一个悬挂指针。
            causal_parent=refused.causal_parent,
            priority=2,
            run_id=refused.run_id,
            scenario_id=refused.scenario_id,
            sim_time_s=refused.sim_time_s,
            event_generation_mode='system',
            data={
                'reason': EVENT_STORM_SUPPRESSED_REASON,
                'correlation_id': correlation_id,
                'max_depth': self.max_causal_depth,
                'depth': refused.depth,
                'suppressed_event_type': refused.event_type,
                'suppressed_event_source': refused.source,
                'suppressed_count': 1,
            },
        )
        # 走 _fan_out 而不是 publish：这条通知自己就在上限之外（它和被拒事件同父），
        # 再过一次闸门只会被自己刹掉，于是抑制变成静默丢弃——正是要避免的那件事。
        self._stamp(notice)
        return await self._fan_out(notice)

    def get_history(
        self,
        event_type: str | None = None,
        since: float | None = None,
        correlation_id: str | None = None,
        source: str | None = None,
        min_priority: int | None = None,
        causal_parent: str | None = None,
        run_id: str | None = None,
        event_generation_mode: str | None = None,
    ) -> list[SimEvent]:
        """Return historical events filtered by common query dimensions."""
        results = self._history
        if event_type is not None:
            results = [event for event in results if event.event_type == event_type]
        if since is not None:
            results = [event for event in results if event.timestamp >= since]
        if correlation_id is not None:
            results = [event for event in results if event.correlation_id == correlation_id]
        if source is not None:
            results = [event for event in results if event.source == source]
        if min_priority is not None:
            results = [event for event in results if event.priority >= min_priority]
        if causal_parent is not None:
            results = [event for event in results if event.causal_parent == causal_parent]
        if run_id is not None:
            results = [event for event in results if event.run_id == run_id]
        if event_generation_mode is not None:
            results = [
                event
                for event in results
                if event.event_generation_mode == event_generation_mode
            ]
        return results

    def storm_suppressed_count(self, correlation_id: str) -> int:
        """这条 correlation 上被深度闸门拒发了多少条事件（0 = 没触发过）。

        事件流里只有一条通知（刻意的），真实拒发条数由本读数补齐——S5 面板要能说
        "这里被刹住了 N 条"，而不是"这里刹过一次"。
        """

        return self._storm_suppressed.get(correlation_id, 0)

    def get_correlation_history(self, correlation_id: str) -> list[SimEvent]:
        return self.get_history(correlation_id=correlation_id)

    @staticmethod
    def _order_key(event: SimEvent) -> tuple[float, float, int]:
        # timestamp（tick 计数）同刻并列时用 seq 兜底；未发布事件 seq=None 排在同刻最前。
        return (event.timestamp, event.wall_time, event.seq if event.seq is not None else -1)

    def get_causal_chain(self, root_event_id: str) -> list[SimEvent]:
        indexed = {event.event_id: event for event in self._history}
        root = indexed.get(root_event_id)
        if root is None:
            return []

        # 因果链绝不跨 run：reset 后的新 run 可能复用 correlation_id/causal_parent
        # （测试构造与录制回放都会），只按 event_id 认父会把两个 run 的链焊在一起。
        scoped = [event for event in self._history if event.run_id == root.run_id]

        grouped_children: dict[str, list[SimEvent]] = defaultdict(list)
        for event in scoped:
            if event.causal_parent:
                grouped_children[event.causal_parent].append(event)

        for children in grouped_children.values():
            children.sort(key=self._order_key)

        chain: list[SimEvent] = []
        stack = [root]
        while stack:
            current = stack.pop()
            chain.append(current)
            children = grouped_children.get(current.event_id, [])
            stack.extend(reversed(children))

        chain.sort(key=self._order_key)
        return chain
