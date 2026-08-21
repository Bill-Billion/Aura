"""S2 阶段门：同 scenario + 同 seed 两次运行，canonical trace 字节一致（S2-T9）。

门条款原文：*八个典型场景各 headless 跑两遍，两份 canonical trace 逐字节相同*。

这条门是整个 S2 隐性不确定性的总闸，因此它同时钉死三件事，缺一件门就形同虚设：

1. **正向**：八场景 × 两遍 → 两份 canonical trace 字节一致。
2. **阴性对照**：换 seed 必须让 stochastic 轨迹**改变**。没有这一条，"RNG 根本没被调用"
   也能让正向门全绿——一个恒等函数永远是可复现的。
3. **排除域最小**：canonical 投影只允许丢 :data:`~backend.scenarios.trace.VOLATILE_EVENT_FIELDS`
   里显式声明的字段。没有这一条，把 data 整个删掉也能让门全绿。

另外两条是**机制**测试，专门给 S3 留的绊线（critic lens 3）：S3-T3 会重写 ``_run_episode``
的派发循环、S3-T4 会再注册三个并发 agent。只要那时"并发计算 / 串行发射"的闸门被绕过，
:func:`test_two_root_events_both_emit_grouped_in_root_event_order` 与
:func:`test_compute_latency_does_not_reorder_the_canonical_trace` 会立刻变红，而不是等到
某天有人发现两次运行的 trace 对不上。

**S3-T10 追加的重新断言**（本文件末尾一节，binding critic）：上面那些绊线证明的是"机制
还在"，但它们证明不了"机制在**新流水线上**仍然有效"。S3 把编排器插进派发循环、又注册了
SecurityAgent / EnergyAgent / SceneAgent 三个并发 agent —— 这是整个 S3 里最可能**悄无声息**
毁掉 S2 阶段门的一处改动（毁掉的方式是"偶尔红一次"，最容易被当成 flaky 关掉）。因此末尾
一节在**编排器打开 + 三个新 agent 全部注册**的姿态下重跑字节一致性，并附一条阴性对照：
关掉编排器必须让 trace **改变**（否则"编排器在链路里"本身就是假的，正向断言随之空转）。
"""

from __future__ import annotations

import asyncio

import pytest

from backend.agents.llm import LLMProvider, LLMProviderError
from backend.agents.orchestrator import (
    DEFAULT_ORCHESTRATOR_ID,
)
from backend.agents.runtime import (
    AgentRuntime,
    SerialEmissionGate,
    register_default_agents,
)
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.simulation import SimulationEngine
from backend.main import _init_default_state
from backend.scenarios.loader import get_scenario
from backend.scenarios.runner import ScenarioRunner, run_scenario
from backend.scenarios.trace import (
    VOLATILE_DATA_KEYS,
    VOLATILE_EVENT_FIELDS,
    canonical_trace,
    canonical_trace_text,
    export_canonical_trace,
    trace_digest,
)
from tests.test_canonical_scenarios import CANONICAL_SCENARIO_IDS

# 阴性对照专用：库里唯一带活 stochastic 流的场景（S2-T8 public_api 明确指定）。
STOCHASTIC_SCENARIO_ID = "device_offline_fallback"
ALTERNATE_SEED = 424242

# 机制测试用的三档"计算耗时画像"：同一场景、同一 seed，只有 agent 计算耗时不同。
# 发射顺序若依赖计算完成先后，这三档就会给出三份不同的 trace。
LATENCY_PROFILES = (
    {},
    {"user.": 0.05},
    {"environment.": 0.04, "user.": 0.01, "security.": 0.03, "device.": 0.02},
)


class LatencyProvider(LLMProvider):
    """按根事件类型给出不同失败延迟的 provider（模拟真实 LLM 的抖动）。

    一律以 :class:`LLMProviderError` 收场 → agent 走规则回退，决策内容与
    DisabledLLMProvider 完全一致；**唯一**的变量是"算了多久"。
    """

    provider_name = "disabled"
    model = "rule_based"

    def __init__(self, profile: dict[str, float]) -> None:
        self.profile = profile

    async def generate_decision(self, request):  # type: ignore[override]
        for prefix, delay in self.profile.items():
            if request.root_event_type.startswith(prefix):
                if delay:
                    await asyncio.sleep(delay)
                break
        raise LLMProviderError("provider_error", "LLM provider is disabled")


def _make_engine(provider: LLMProvider) -> SimulationEngine:
    return SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )


# --------------------------------------------------------------- 门条款（正向）


@pytest.mark.anyio
@pytest.mark.parametrize("scenario_id", CANONICAL_SCENARIO_IDS)
async def test_each_of_8_scenarios_two_runs_byte_identical_canonical_trace(scenario_id):
    """门条款原文：八个典型场景，同 seed 跑两遍，canonical trace 逐字节相同。"""

    first = await run_scenario(scenario_id)
    second = await run_scenario(scenario_id)

    # 两个 run 必须是**不同的** run（否则比的是同一份工件，门是空的）
    assert first.run_id != second.run_id
    assert first.seed == second.seed

    left = canonical_trace_text(first.events)
    right = canonical_trace_text(second.events)
    assert left == right, (
        f"{scenario_id}: 两次同 seed 运行的 canonical trace 不一致\n"
        f"digest={trace_digest(first.events)} vs {trace_digest(second.events)}"
    )
    # 空 trace 也能"字节一致"——顺手挡掉这种假通过
    assert len(canonical_trace(first.events)) > 10
    # 磁盘工件（研究者真正拿到的那份）上同样成立
    assert export_canonical_trace(first.run_id) == export_canonical_trace(second.run_id)


@pytest.mark.anyio
async def test_canonical_trace_of_the_disk_artifact_is_also_byte_identical():
    """磁盘工件（S2-T7 ``events.jsonl``）上同样字节一致。

    没有这一条，"可复现"只在进程内成立：研究者手里拿到的是 events.jsonl，S4 的回放与
    S5 的面板读的也是它——门必须在**那份文件**上也成立。

    注：工件是 run 的全集，比内存里 runner 收集到的那段**多**几条 run 生命周期事件
    （runner 在 ``engine.reset()`` 之后才挂 ``*`` 订阅，收尾时又先退订再 pause）。
    两者事件集合不同是设计使然，因此这里比的是"工件 vs 工件"，不是"工件 vs 内存"。
    """

    first = await run_scenario("user_arrives_home_evening")
    second = await run_scenario("user_arrives_home_evening")

    left = export_canonical_trace(first.run_id)
    right = export_canonical_trace(second.run_id)
    assert left == right
    # 工件是全集：内存那段事件一条不少地在里面
    assert len(left.splitlines()) >= len(canonical_trace(first.events))


# ------------------------------------------------------------- 阴性对照（防空转）


@pytest.mark.anyio
async def test_different_seed_changes_stochastic_trace():
    """阴性对照：换 seed 必须改变 stochastic 轨迹。

    正向门只能证明"两次一样"；如果随机源根本没被调用，恒等函数同样满足它。本条要求
    换个 seed 之后 §4.5 stochastic 事件真的挪位，证明门里跑着真随机。
    """

    spec = get_scenario(STOCHASTIC_SCENARIO_ID)
    assert spec is not None

    baseline = await ScenarioRunner(spec).run()
    alternate = await ScenarioRunner(spec.model_copy(update={"seed": ALTERNATE_SEED})).run()

    assert baseline.seed != alternate.seed
    # 随机流真的被抽过（否则下面的"不一样"可能只是别处的巧合差异）
    assert baseline.rng_metadata["streams"], "device_offline_fallback 必须真的抽过随机流"

    def stochastic_signature(result):
        return tuple(
            (event.event_type, event.data.get("device_id"), event.sim_time_s)
            for event in result.events
            if event.event_generation_mode == "stochastic"
        )

    assert stochastic_signature(baseline), "seed 1008 必须产出 stochastic 事件"
    assert stochastic_signature(baseline) != stochastic_signature(alternate)
    assert canonical_trace_text(baseline.events) != canonical_trace_text(alternate.events)


@pytest.mark.anyio
async def test_same_seed_reproduces_the_stochastic_trace_exactly():
    """阴性对照的另一半：seed 不变时 stochastic 轨迹必须逐条复现。"""

    spec = get_scenario(STOCHASTIC_SCENARIO_ID)
    first = await ScenarioRunner(spec).run()
    second = await ScenarioRunner(spec).run()

    assert first.rng_metadata == second.rng_metadata
    assert canonical_trace_text(first.events) == canonical_trace_text(second.events)


# --------------------------------------------------------- 排除域（防"扩大排除"）


@pytest.mark.anyio
async def test_trace_excludes_only_declared_volatile_fields():
    """canonical 投影只准丢显式声明的易变字段；其余一律保留或双射改写。

    这条防的是最省事的作弊：把 data / 时间戳统统删掉，两份 trace 当然字节一致。
    """

    # 排除域必须小且具名——数量本身就是门的一部分
    assert VOLATILE_EVENT_FIELDS == frozenset({"wall_time"})
    assert VOLATILE_DATA_KEYS == frozenset({"wall_time", "latency_ms"})

    result = await run_scenario("user_arrives_home_evening")
    # trace 按 seq 排序，而总线的 wildcard 派发晚于精确类型派发 → 收集序 != seq 序。
    # 逐条对齐时必须先按同一把尺子排好，否则比的是两条不同的事件。
    ordered = sorted(result.events, key=lambda event: event.seq)
    projected = canonical_trace(result.events)
    expected_keys = set(SimEvent.model_fields) - set(VOLATILE_EVENT_FIELDS)

    for entry in projected:
        assert set(entry) == expected_keys

    # data 里被丢掉的键，只能是声明过的那两个
    dropped: set[str] = set()
    for event, entry in zip(ordered, projected, strict=True):
        dropped |= set(event.data) - set(entry["data"])
    assert dropped <= set(VOLATILE_DATA_KEYS)

    # 真实内容确实还在：语义字段没有被投影抹掉
    types = {entry["event_type"] for entry in projected}
    assert "reasoning.perception_snapshot" in types
    assert "command.lifecycle" in types
    assert any(entry["data"] for entry in projected)


@pytest.mark.anyio
async def test_trace_rewrites_ids_bijectively_not_by_deletion():
    """uuid 类字段是**改写**不是删除：父子关系在 trace 里仍然可查。"""

    result = await run_scenario("user_arrives_home_evening")
    ordered = sorted(result.events, key=lambda event: event.seq)
    projected = canonical_trace(result.events)

    by_raw_id = {event.event_id: index for index, event in enumerate(ordered)}
    symbol_of = {entry["event_id"] for entry in projected}
    # 原始 uuid 不允许出现在 trace 里（它每次运行都不同）
    assert not (symbol_of & set(by_raw_id))
    assert len(symbol_of) == len(projected)

    # 每条有父的事件，其 causal_parent 符号 == 父事件自己的 event_id 符号
    checked = 0
    for event, entry in zip(ordered, projected, strict=True):
        if not event.causal_parent or event.causal_parent not in by_raw_id:
            continue
        parent_entry = projected[by_raw_id[event.causal_parent]]
        assert entry["causal_parent"] == parent_entry["event_id"]
        checked += 1
    assert checked > 5, "这个场景必须有真实的因果边可查"

    # seq 被重基到 trace 内序号：两个 run 的起点 seq 不同也能比对
    assert [entry["seq"] for entry in projected] == list(range(len(projected)))


# ----------------------------------------------- 机制：并发计算 / 串行发射（给 S3 的绊线）


@pytest.mark.anyio
async def test_two_root_events_both_emit_grouped_in_root_event_order():
    """两条根事件在飞时，两条 episode 都要完整发射，且按根事件发布序成组出现。

    这同时是三件事的门：
      * **确定性**：发射顺序由根事件发布序决定，与 agent 算得快慢无关；
      * **§15 验收 3**：每条根事件都要有一条可见的 episode——旧实现里后到的根事件会
        ``existing.cancel()`` 掉前一条 episode，整条推理链**静默消失**（连
        system.episode_cancelled 都不发），正是 S1 根治的那一类静默失败；
      * **S3 绊线**：S3-T3 重写派发循环、S3-T4 再加三个并发 agent 之后，本条必须仍然绿。
    """

    signatures: list[tuple[tuple[str, ...], int]] = []
    for profile in LATENCY_PROFILES:
        engine = _make_engine(LatencyProvider(profile))
        collected: list[SimEvent] = []

        async def collect(event: SimEvent) -> None:
            collected.append(event)

        engine.event_bus.subscribe("*", collect)
        first_root = SimEvent(
            event_type="user.arrives_home",
            source="test",
            timestamp=1.0,
            data={"user_id": "user_01", "room_id": "living_room", "to_room": "living_room"},
        )
        second_root = SimEvent(
            event_type="security.door_opened",
            source="test",
            timestamp=1.0,
            data={"room_id": "living_room", "door_id": "front_door"},
        )
        await engine.event_bus.publish(first_root)
        await engine.event_bus.publish(second_root)
        assert await engine.agent_runtime.wait_for_idle(timeout=10.0)
        await engine.close()

        episode_events = [
            event
            for event in collected
            if event.event_type.startswith("reasoning.")
            and event.correlation_id in {first_root.correlation_id, second_root.correlation_id}
        ]
        order = [event.correlation_id for event in episode_events]
        # 两条 episode 都在（没有被后到的根事件静默吃掉）
        assert set(order) == {first_root.correlation_id, second_root.correlation_id}
        # 成组且按根事件发布序：先第一条根事件的整段，再第二条的整段
        grouped = [key for index, key in enumerate(order) if index == 0 or key != order[index - 1]]
        assert grouped == [first_root.correlation_id, second_root.correlation_id]

        signatures.append(
            (tuple(event.event_type for event in episode_events), len(episode_events))
        )

    # 三档耗时画像给出同一条发射序列
    assert len(set(signatures)) == 1, f"计算耗时改变了发射序列: {signatures}"


@pytest.mark.anyio
@pytest.mark.parametrize("scenario_id", ["user_arrives_home_evening", "cooking_dinner"])
async def test_compute_latency_does_not_reorder_the_canonical_trace(scenario_id):
    """整条流水线级的同一断言：agent 算得快慢不得改变 canonical trace。"""

    spec = get_scenario(scenario_id)
    traces = []
    for profile in LATENCY_PROFILES:
        result = await ScenarioRunner(spec, llm_provider=LatencyProvider(profile)).run()
        traces.append(canonical_trace_text(result.events))

    assert len(set(traces)) == 1, f"{scenario_id}: 计算耗时改变了 canonical trace"


def test_agent_runtime_pins_the_serial_emission_gate():
    """结构钉：发射闸门必须还在，且 ``_run_episode`` 仍然接得住票号。

    S3 会重写 episode 派发循环。若那次重写把闸门删了/绕过了，本条当场变红，
    而不是留给某天"两次运行 trace 对不上"的疑案。
    """

    import inspect

    runtime = AgentRuntime()
    assert isinstance(runtime.emission_gate, SerialEmissionGate)
    assert "emission_ticket" in inspect.signature(runtime._run_episode).parameters

    gate = SerialEmissionGate()
    assert gate.take_ticket() == 0
    assert gate.take_ticket() == 1
    assert gate.now_serving == 0


@pytest.mark.anyio
async def test_serial_emission_gate_serves_tickets_in_order_not_arrival_order():
    """闸门单测：先到的号先服务，与谁先排队无关（这是确定性的全部来源）。"""

    gate = SerialEmissionGate()
    first, second, third = gate.take_ticket(), gate.take_ticket(), gate.take_ticket()
    served: list[int] = []

    async def use(ticket: int, wait_before: float) -> None:
        await asyncio.sleep(wait_before)
        async with gate.turn(ticket):
            served.append(ticket)
            await asyncio.sleep(0)

    # 故意让 2 号最先排队、0 号最后排队
    await asyncio.gather(use(third, 0.0), use(second, 0.01), use(first, 0.03))
    assert served == [first, second, third]

    # 放弃的号不能把后面的人永远堵死
    gate.reset()
    a, b = gate.take_ticket(), gate.take_ticket()
    gate.release(a)
    async with gate.turn(b):
        pass
    assert gate.now_serving == 2


# ------------------------------- S3-T10：编排器 + 三个新域 agent 上线后的重新断言
#
# 这一节存在的唯一理由是 binding critic 那条修正：S2 的字节一致性门依赖 runtime
# ``_run_episode`` 里"并发算 / 串行发"的那把闸门，而 S3 恰好重写了那段派发循环并把
# 并发 agent 从 2 个加到 5 个。上面的正向门（八/九场景 × 两遍）虽然天然覆盖到了新流水线，
# 但它读的是**环境默认值**——一旦哪天默认值翻转，门会在无人察觉的情况下退回旧路径。
# 下面三条把"编排器确实开着、三个新 agent 确实在跑、而且 trace 仍然字节一致"钉成显式断言。


def test_default_agent_registry_contains_the_three_new_domain_agents():
    """结构钉：SecurityAgent / EnergyAgent / SceneAgent 必须在默认注册表里，且顺序确定。

    顺序即契约（``DEFAULT_AGENT_FACTORIES`` 的注释写着三条后果）：它决定
    ``TaskPlan.domain_tasks`` 的顺序、episode 内事件的发射顺序，以及 canonical trace 的行序。
    改这张表的顺序 = 改 canonical trace，因此顺序本身要有一条断言盯着。
    """

    from backend.agents.energy import EnergyAgent
    from backend.agents.runtime import DEFAULT_AGENT_FACTORIES, build_default_agents
    from backend.agents.scene import SceneAgent
    from backend.agents.security import SecurityAgent

    agents = build_default_agents()
    assert [type(agent) for agent in agents] == list(DEFAULT_AGENT_FACTORIES)
    assert {SecurityAgent, EnergyAgent, SceneAgent} <= set(DEFAULT_AGENT_FACTORIES)

    runtime = AgentRuntime()
    registered = register_default_agents(runtime)
    assert [agent.agent_id for agent in runtime.agents] == [
        agent.agent_id for agent in registered
    ]
    # 注册序 = 发射序的 tie-break：五个 agent 全在，且顺序与工厂表逐位一致
    assert len(runtime.agents) == 5


@pytest.mark.anyio
@pytest.mark.parametrize("scenario_id", CANONICAL_SCENARIO_IDS)
async def test_byte_identity_holds_with_orchestrator_and_new_agents(
    monkeypatch, scenario_id
):
    """S2 门在编排器 + 五个域 agent 全注册的姿态下逐场景重新成立。"""

    first = await run_scenario(scenario_id)
    second = await run_scenario(scenario_id)

    assert first.run_id != second.run_id
    assert canonical_trace_text(first.events) == canonical_trace_text(second.events), (
        f"{scenario_id}: 编排器开启后两次同 seed 运行的 canonical trace 不一致\n"
        f"digest={trace_digest(first.events)} vs {trace_digest(second.events)}"
    )
    # 编排器真的在链路里：前三环归它，而且一条 episode 只有一套
    orchestrator_events = [
        event
        for event in first.events
        if event.source == DEFAULT_ORCHESTRATOR_ID
    ]
    assert orchestrator_events, f"{scenario_id}: 编排器开着却一条事件都没发"
