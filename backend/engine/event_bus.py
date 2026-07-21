import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any, Callable, Coroutine, Literal

from pydantic import BaseModel, Field

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

    @classmethod
    def from_world_event(cls, event: WorldEvent, **overrides: Any) -> 'SimEvent':
        payload = event.model_dump()
        payload.update(overrides)
        return cls(**payload)


Handler = Callable[[SimEvent], Coroutine | None]
SimTimeSource = Callable[[], float]


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, list[Handler]] = defaultdict(list)
        self._history: list[SimEvent] = []
        self._max_history: int = 1000
        self._run_id: str | None = None
        self._scenario_id: str | None = None
        self._sim_time_source: SimTimeSource | None = None
        self._next_seq: int = 0

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
        return event

    async def publish(self, event: WorldEvent | SimEvent) -> int:
        """Publish *event* to all matching subscribers (exact + wildcard)."""
        sim_event = self._stamp(self.coerce_event(event))

        handlers: list[Handler] = []
        handlers.extend(self._subscribers.get(sim_event.event_type, []))
        if sim_event.event_type != '*':
            handlers.extend(self._subscribers.get('*', []))

        self._history.append(sim_event)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history :]

        for handler in handlers:
            result = handler(sim_event)
            if asyncio.iscoroutine(result):
                await result

        return len(handlers)

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
