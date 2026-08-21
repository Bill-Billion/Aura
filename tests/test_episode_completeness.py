"""S3 阶段门（一）：一条根事件 → 一条**完整且可见**的 episode。

这份文件是 S3 阶段门的可执行形式，钉两件事：

1. **六环完整**（§4.3 + §15 验收 4）。一次脚本化的 ``user.arrives_home`` run（固定 seed、
   mocked LLM、走 S2 的真实装配：SimulationEngine + AgentRuntime + CommandExecutor）必须在
   **同一个 correlation_id** 下按 §4.4 的先后顺序产出
   root → ``reasoning.perception_snapshot`` → ``reasoning.intent_recognized`` →
   ``reasoning.task_decomposition`` → ``reasoning.coordination_decision`` →
   ``reasoning.execution_plan`` → ``action.device_control`` → ``feedback.state_delta``，
   且每条事件都能顺着 ``causal_parent`` 走回根事件。
   判定被抽成 :func:`assert_six_ring_complete`——它同时是 S4 §12.1 ``episode_complete``
   指标的种子实现，S4-T2 应当复用它而不是另写一份"六环齐了吗"。

2. **每个根事件类型都必须有可见 episode**（§15 验收 3，critic lens 4 的补充门）。
   §4.1 的十七个根事件类型逐个参数化：每一条都必须在自己的 correlation 下留下
   **感知 + 意图**，或者一条**显式的 no-op** ``reasoning.task_decomposition``——
   绝不允许"零子事件"。这条门防的是最隐蔽的坏法：分类学新增了一个事件类型，但
   ``TriggerClassifier`` / ``is_relevant`` / §7 映射表里漏登记它，于是它一路穿过运行期
   **什么都不发生**，看起来和"agent 判断不需要动作"一模一样。有了这条门，漏登记会
   立刻变红，而不是变成一条谁也不会发现的静默失明。

3. **§9.1 每一档都要有真实的运行期生产者**（S3 复审 minor：死档）。
   ``tests/test_arbiter.py::test_every_tier_can_win_something`` 用合成提案证明的是
   "排名表认得这一档"，对"这一档现实中没人产出"零鉴别力——复审实测九个 §6 场景的提案
   档位分布是 comfort 52 / security 10 / energy 7 / ambience 7 / explicit_user 4，
   而 ``safety`` 与 ``maintenance`` 都是 **0**。§9.1 把 safety 排在最高档，最高档从不出场
   等于全序只在下半截被走过。:data:`PRIORITY_TIER_PRODUCERS` 因此逐档声明生产者
   （库场景 / UI 直控腿 / 显式保留），并由 :func:`test_every_priority_tier_has_a_declared_producer`
   拿**整库实跑**的观测去核对：声明说"库里有"就必须真跑得出来，说"保留档"就必须真的一次
   都没出现——两个方向都可证伪。

跑法（WS 测试会阻塞，绝不无界跑）::

    ./backend/.venv/bin/python -m pytest tests/test_episode_completeness.py -q --timeout=60
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence
from unittest.mock import AsyncMock

import pytest

from backend.agents.arbiter import COORDINATION_DECISION_EVENT_TYPE
from backend.agents.contracts import PRIORITY_LEVELS, PriorityLevel
from backend.agents.runtime import DisabledLLMProvider
from backend.api.ws import ConnectionManager
from backend.engine import event_types as event_types_module
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.event_types import ALL_ROOT_EVENT_TYPES
from backend.engine.simulation import SimulationEngine
from backend.main import _init_default_state
from backend.scenarios.loader import load_library
from backend.scenarios.runner import ScenarioRunResult, run_scenario

pytestmark = pytest.mark.anyio


# §4.3 六环 + 动作 + 反馈，**按 §4.4 要求的先后**。顺序即断言：改这张表等于改门。
SIX_RING_SEQUENCE: tuple[str, ...] = (
    "reasoning.perception_snapshot",
    "reasoning.intent_recognized",
    "reasoning.task_decomposition",
    "reasoning.coordination_decision",
    "reasoning.execution_plan",
    "action.device_control",
    "feedback.state_delta",
)

# 阶段门指定的脚本场景（S2-T8 §6.1，seed 固定写在 YAML 里）。
ARRIVE_HOME_SCENARIO_ID = "user_arrives_home_evening"


# --------------------------------------------------------------------- 判定器


def episode_events(
    events: Iterable[SimEvent], correlation_id: str
) -> list[SimEvent]:
    """一条 episode 的全部事件，按 ``seq`` 排序。

    必须按 seq 排而不是按收集序：总线的 wildcard 订阅晚于精确类型订阅派发，收集顺序
    和事件流真正的先后并不是同一把尺子（见 tests/test_replay_determinism.py 的同款注释）。
    """

    scoped = [event for event in events if event.correlation_id == correlation_id]
    return sorted(scoped, key=lambda event: event.seq if event.seq is not None else -1)


def assert_six_ring_complete(
    events: Sequence[SimEvent], correlation_id: str
) -> dict[str, int]:
    """§15 验收 4 的判定：这条 correlation 是不是一条完整的六环链。

    返回 ``{事件类型: 首次出现的 seq}``，供调用方做更细的断言；S4-T2 的
    ``episode_complete`` 指标可以直接复用这个返回值，不必再解析一遍事件流。

    三层断言，缺一层门就有洞：

    * **在场**：七种事件类型一个不缺（少一环 = 链断了）；
    * **有序**：首次出现的 seq 严格递增，且根事件排在所有环之前（§4.4「因先于果」）；
    * **连通**：反馈事件顺着 ``causal_parent`` 能走回根事件，且路过每一环——
      只断"都在场"是不够的，N 棵互不相连的小树也能让在场断言全绿（审计 §六
      "episode 被稀释"那条坑正是这么过关的）。
    """

    scoped = episode_events(events, correlation_id)
    assert scoped, f"correlation {correlation_id} 下一条事件都没有"

    by_id = {event.event_id: event for event in scoped}
    roots = [event for event in scoped if not event.causal_parent]
    assert len(roots) == 1, f"一条 episode 只能有一个根事件，实际 {len(roots)} 个"
    root = roots[0]

    first_seq: dict[str, int] = {}
    for event in scoped:
        first_seq.setdefault(event.event_type, event.seq)

    missing = [ring for ring in SIX_RING_SEQUENCE if ring not in first_seq]
    assert not missing, f"{root.event_type} 的 episode 缺环：{missing}"

    ordered_seqs = [first_seq[ring] for ring in SIX_RING_SEQUENCE]
    assert root.seq < ordered_seqs[0], "根事件必须排在推理链之前"
    assert ordered_seqs == sorted(ordered_seqs) and len(set(ordered_seqs)) == len(
        ordered_seqs
    ), f"六环顺序不符合 §4.4：{list(zip(SIX_RING_SEQUENCE, ordered_seqs))}"

    # 连通性：每条非根事件的父都在本 episode 内，且父先于子。
    for event in scoped:
        if event.event_id == root.event_id:
            continue
        parent = by_id.get(event.causal_parent or "")
        assert parent is not None, f"{event.event_type} 的父事件不在本 episode 内"
        assert parent.seq < event.seq, f"{event.event_type} 排在了它的父事件之前"

    # 反馈能一路追回根事件，且路过六环的每一环。
    feedback = next(
        event for event in scoped if event.event_type == "feedback.state_delta"
    )
    lineage: list[str] = []
    cursor = by_id.get(feedback.causal_parent or "")
    while cursor is not None:
        lineage.append(cursor.event_type)
        if cursor.event_id == root.event_id:
            break
        cursor = by_id.get(cursor.causal_parent or "")
    assert cursor is not None and cursor.event_id == root.event_id, (
        f"反馈事件追不回根事件，链路={lineage}"
    )
    for ring in SIX_RING_SEQUENCE[:-1]:
        assert ring in lineage, f"反馈事件的祖先链里缺 {ring}；链路={lineage}"

    return first_seq


def root_correlation_id(result: ScenarioRunResult, event_type: str) -> str:
    """run 里那条指定类型的脚本根事件的 correlation_id。"""

    roots = [
        event
        for event in result.events
        if event.event_type == event_type and not event.causal_parent
    ]
    assert roots, f"这个 run 里没有 {event_type} 根事件"
    return roots[0].correlation_id


# --------------------------------------------------- 1. 脚本化 arrive-home 六环


async def test_scripted_arrive_home_run_completes_all_six_rings():
    """门条款：脚本化 ``user.arrives_home``（固定 seed + mocked LLM）→ 六环齐全且有序。

    走的是 S2 的 headless runner，也就是**真实装配**：SimulationEngine + AgentRuntime +
    CommandExecutor + 仲裁门，provider 是 headless 默认的 mocked（一律失败 → 规则路径，
    绝不打网）。不用手搓 engine 是有意的——门要证明的是"研究者跑一个库场景就能拿到
    完整推理链"，不是"在实验室条件下拼得出来"。
    """

    result = await run_scenario(ARRIVE_HOME_SCENARIO_ID)

    assert result.scenario_id == ARRIVE_HOME_SCENARIO_ID
    assert result.seed == 1001, "seed 必须来自 YAML，不能是随机的"
    assert result.completed is True

    correlation_id = root_correlation_id(result, "user.arrives_home")
    first_seq = assert_six_ring_complete(result.events, correlation_id)

    # 六环真的是"这一条根事件"的六环，不是别处飘过来的同名事件
    scoped = episode_events(result.events, correlation_id)
    assert all(event.correlation_id == correlation_id for event in scoped)
    assert first_seq["reasoning.coordination_decision"] < first_seq["action.device_control"]


async def test_arrive_home_episode_has_exactly_one_orchestrated_prefix():
    """一条根事件 = 一棵树：前三环各恰好一条，协调决策也恰好一条（§9.3）。

    这条与"六环在场"是两件事：在场断言对"每个 agent 各发一套"同样成立，而那正是
    审计 §六记下的坏形状。数量断言才是"树只有一棵"的判据。
    """

    result = await run_scenario(ARRIVE_HOME_SCENARIO_ID)
    correlation_id = root_correlation_id(result, "user.arrives_home")
    scoped = episode_events(result.events, correlation_id)

    counts: dict[str, int] = {}
    for event in scoped:
        counts[event.event_type] = counts.get(event.event_type, 0) + 1

    assert counts["reasoning.perception_snapshot"] == 1
    assert counts["reasoning.intent_recognized"] == 1
    assert counts["reasoning.task_decomposition"] == 1
    assert counts["reasoning.coordination_decision"] == 1
    # 执行环仍是 per-agent：域 agent 的贡献落在这一环，而不是各自另起一棵树。
    assert counts["reasoning.execution_plan"] >= 1

    decomposition = next(
        event for event in scoped if event.event_type == "reasoning.task_decomposition"
    )
    # 审计坑：拆分环里装 LLM 散文。事件流上必须是结构化编排契约。
    assert isinstance(decomposition.data["domain_tasks"], list)
    assert decomposition.data["domain_tasks"], "被派到任务的 agent 一个都没有"
    for task in decomposition.data["domain_tasks"]:
        assert {"agent_role", "task", "relevant_device_ids", "priority"} <= set(task)


async def test_same_seed_arrive_home_run_repeats_the_same_episode_shape():
    """同 seed 两次运行，六环链的形状（类型序列）逐条相同。

    S2-T9 的字节一致性门盯的是整条 trace；这里盯的是**推理链本身**——两者都绿，
    "可复现"才既覆盖世界演化也覆盖 agent 决策。
    """

    def shape(result: ScenarioRunResult) -> list[str]:
        correlation_id = root_correlation_id(result, "user.arrives_home")
        return [event.event_type for event in episode_events(result.events, correlation_id)]

    first = await run_scenario(ARRIVE_HOME_SCENARIO_ID)
    second = await run_scenario(ARRIVE_HOME_SCENARIO_ID)

    assert first.run_id != second.run_id
    assert shape(first) == shape(second)


# ------------------------------------- 2. 每个 §4.1 根事件类型都必须有可见 episode


# 十七个根事件类型各一份**最小合法 payload**。键必须与 ``ALL_ROOT_EVENT_TYPES`` 完全一致
# （下面有一条测试在盯）：分类学里新增一个类型却忘了在这里登记，会当场变红，而不是让
# 那个新类型悄悄绕过"每条根事件都要有可见 episode"这条门。
ROOT_EVENT_SAMPLE_DATA: dict[str, dict] = {
    event_types_module.USER_ARRIVES_HOME: {
        "user_id": "user_01",
        "room_id": "living_room",
        "to_room": "living_room",
        "activity": "arrive_home",
    },
    event_types_module.USER_LEAVES_HOME: {
        "user_id": "user_01",
        "room_id": "living_room",
        "from_room": "living_room",
        "to_room": "outside",
        "activity": "leave_home",
    },
    event_types_module.USER_ENTERS_ROOM: {
        "user_id": "user_01",
        "room_id": "living_room",
        "from_room": "bedroom",
        "to_room": "living_room",
        "activity": "moving",
    },
    event_types_module.USER_EXITS_ROOM: {
        "user_id": "user_01",
        "room_id": "living_room",
        "from_room": "living_room",
        "to_room": "",
        "activity": "moving",
    },
    event_types_module.USER_STARTS_ACTIVITY: {
        "user_id": "user_01",
        "room_id": "living_room",
        "to_room": "living_room",
        "activity": "reading",
    },
    event_types_module.USER_ENDS_ACTIVITY: {
        "user_id": "user_01",
        "room_id": "living_room",
        "to_room": "living_room",
        "activity": "reading",
    },
    # 环境类根事件必须带 significant_change_reasons：没有"显著变化理由"的例行刷新
    # 本来就不该开 episode（TriggerClassifier 的唯一"看情况"分支）。
    event_types_module.ENVIRONMENT_WEATHER_CHANGE: {
        "weather": "rainy",
        "significant_change_reasons": ["weather:clear->rainy"],
    },
    event_types_module.ENVIRONMENT_TEMPERATURE_THRESHOLD: {
        "room_id": "living_room",
        "temperature": 29.0,
        "significant_change_reasons": ["temperature>28"],
    },
    event_types_module.ENVIRONMENT_LIGHT_LEVEL_THRESHOLD: {
        "room_id": "living_room",
        "light_level": 5.0,
        "significant_change_reasons": ["light_level<10"],
    },
    event_types_module.SECURITY_PRESENCE_DETECTED: {
        "room_id": "living_room",
        "sensor_id": "sensor_living_temp_01",
    },
    event_types_module.SECURITY_DOOR_OPENED: {
        "room_id": "living_room",
        "door_id": "front_door",
    },
    event_types_module.SAFETY_SMOKE_DETECTED: {"room_id": "kitchen"},
    event_types_module.DEVICE_OFFLINE: {"device_id": "ac_living_01"},
    event_types_module.DEVICE_RECOVERED: {"device_id": "ac_living_01"},
    # —— §4.1 迁移期兼容命名空间：它们同样受这条门约束 ——
    event_types_module.USER_ACTIVITY_CHANGE: {
        "user_id": "user_01",
        "room_id": "living_room",
        "to_room": "living_room",
        "activity": "reading",
    },
    # 刻意不带 message_type=CMD_DEVICE_CONTROL：那一条是"已经是最终设备命令"的旧协议腿，
    # 运行期按设计跳过它（不重复开一轮推理）。这里测的是会开 episode 的那一支。
    event_types_module.USER_COMMAND: {
        "user_id": "user_01",
        "room_id": "living_room",
        "scene_id": "reading",
    },
    # simulated_dt 是引擎那台环境刷新处理器的必需键（缺了会 KeyError，而不是静默）。
    event_types_module.ENVIRONMENT_STATE_REFRESH: {
        "simulated_dt": 2.0,
        "significant_change_reasons": ["temperature:+1.2"],
    },
}


def test_root_event_sample_table_covers_the_whole_taxonomy():
    """样本表必须与 §4.1 分类学**逐字对齐**。

    没有这一条，下面那条参数化门就会随分类学扩张而悄悄漏测：新增的事件类型不在表里，
    参数化根本不会为它生成用例，"每个根事件都有可见 episode"就变成"我们记得测的那些有"。
    """

    assert set(ROOT_EVENT_SAMPLE_DATA) == set(ALL_ROOT_EVENT_TYPES)


def _fresh_engine() -> SimulationEngine:
    """每条根事件一台全新引擎：环境类事件有 5s 去抖，共用引擎会让后来的用例被抖掉。"""

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=DisabledLLMProvider(),  # §11.1 mocked：绝不打网
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


@pytest.mark.parametrize("event_type", sorted(ALL_ROOT_EVENT_TYPES))
async def test_every_root_event_type_yields_visible_episode(event_type):
    """§15 验收 3：**每一条**根事件都要产生一条可见的 episode，零子事件即失败。

    "可见"的最低标准刻意给了两条通路，因为它们表达的是两件不同的事：

    * 感知 + 意图俱在 —— 这一轮真的推理了；
    * 一条带 ``noop_reason`` 的 ``reasoning.task_decomposition`` —— 这一轮**判断**不需要
      动手，而且把理由写在了事件流里。

    两条都没有 = 事件掉进了黑洞。审计 §六那条"silently emitting nothing"就是这个形状：
    研究者看到一条根事件后面什么都没有，分不清是 agent 想过了不动手，还是链路断了。
    """

    engine = _fresh_engine()
    root = SimEvent(
        event_type=event_type,
        source="test",
        timestamp=1.0,
        data=dict(ROOT_EVENT_SAMPLE_DATA[event_type]),
    )
    try:
        await engine.event_bus.publish(root)
        assert await engine.agent_runtime.wait_for_idle(timeout=20.0), (
            f"{event_type}: episode 没有在超时内收工"
        )
        history = engine.event_bus.get_history(correlation_id=root.correlation_id)
    finally:
        await engine.close()

    children = [event for event in history if event.event_id != root.event_id]
    assert children, f"{event_type}: 根事件下零子事件——这条事件类型对运行期是隐形的"

    types = {event.event_type for event in children}
    perception_and_intent = {
        "reasoning.perception_snapshot",
        "reasoning.intent_recognized",
    } <= types
    visible_noop = any(
        event.event_type == "reasoning.task_decomposition"
        and event.data.get("noop_reason")
        for event in children
    )
    assert perception_and_intent or visible_noop, (
        f"{event_type}: 既没有感知/意图，也没有一条写明理由的 no-op 任务拆分；"
        f"实际事件类型={sorted(types)}"
    )

    # 可见 ≠ 挂在别处：所有子事件都属于这条 correlation，且能追回根事件。
    assert all(event.correlation_id == root.correlation_id for event in children)
    by_id = {event.event_id: event for event in history}
    for event in children:
        assert event.causal_parent in by_id, (
            f"{event_type}: {event.event_type} 的父事件不在本 episode 内"
        )


# ------------------------------------- 3. §9.1 七档：每一档都要有真实的运行期生产者


class ProducerKind(str, Enum):
    """一档提案**是谁在现实中产出的**。三选一，没有第四种"大概有吧"。"""

    #: 跑某个库场景就能观测到。由 :func:`test_every_priority_tier_has_a_declared_producer`
    #: 拿整库实跑核对——声明不成立会当场变红。
    LIBRARY = "library"
    #: 只有真人下的命令（UI 直控腿）能产出，脚本化场景里不该出现。
    UI_LEG = "ui_leg"
    #: MVP 里**没有**生产者，显式保留。声明一旦过时（这一档真出现在库里）同样变红。
    RESERVED = "reserved"


class TierProducer(NamedTuple):
    kind: ProducerKind
    #: LIBRARY：产出这一档的库场景 id；UI_LEG：钉住这条腿的测试函数名；RESERVED：空。
    witness: str
    note: str


# §9.1 七档 × 各自的生产者。这张表是这条门的**被测对象**，不是注释：
# 新增档位而不在这里登记，:func:`test_every_priority_tier_has_a_declared_producer` 与
# ``tests/test_arbiter.py::test_every_tier_can_win_something`` 会同时变红。
PRIORITY_TIER_PRODUCERS: dict[PriorityLevel, TierProducer] = {
    PriorityLevel.SAFETY: TierProducer(
        ProducerKind.LIBRARY,
        "safety_smoke_kitchen",
        "SecurityAgent 对 safety.smoke_detected 的疏散照明提案（backend/agents/security.py）。"
        "S3 复审时这一档在库里是 0——代码里有生产者，却没有任何 timeline 发得出这条根事件。",
    ),
    PriorityLevel.EXPLICIT_USER: TierProducer(
        ProducerKind.UI_LEG,
        "test_user_ws_command_enters_arbitration_at_explicit_user_tier",
        "这一档的含义是「命令的行为体是真人」（arbiter.is_user_actor），因此只有 WS 直控腿"
        "产得出来；脚本化场景里没有真人，出现即说明某个 agent 又在自称用户档。"
        "钉在 tests/test_arbitration_integration.py。",
    ),
    PriorityLevel.SECURITY: TierProducer(
        ProducerKind.LIBRARY,
        "security_presence_night",
        "SecurityAgent 的布防/告警姿态（§6.8）。",
    ),
    PriorityLevel.COMFORT: TierProducer(
        ProducerKind.LIBRARY,
        "morning_wake_up",
        "LightingAgent / HVACAgent 的舒适档提案，库里最常见的一档。",
    ),
    PriorityLevel.ENERGY: TierProducer(
        ProducerKind.LIBRARY,
        "hot_weather_afternoon",
        "EnergyAgent 的节能提案（§8.2）。",
    ),
    PriorityLevel.AMBIENCE: TierProducer(
        ProducerKind.LIBRARY,
        "user_leaves_home_morning",
        "LightingAgent 的氛围/回落档提案。",
    ),
    PriorityLevel.MAINTENANCE: TierProducer(
        ProducerKind.RESERVED,
        "",
        "MVP 无生产者，显式保留：它只是 Orchestrator 对**未分类**事件的兜底档"
        "（orchestrator.py 的 DOMAIN_PRIORITY.get(..., MAINTENANCE) 与未映射事件行），"
        "而 §4.1 分类学里每一个事件类型现在都有归属，兜底档因此打不着。"
        "S4 的覆盖率评估器应当把它记成「保留」，不要报成覆盖缺口；"
        "哪天真有 agent 产出这一档（例如设备自检/固件维护域），把它改成 LIBRARY 并给出场景。",
    ),
}


def _tiers_by_scenario(results: dict[str, ScenarioRunResult]) -> dict[str, set[str]]:
    """每个场景的 coordination_decision 里出现过的提案档位。

    读 ``per_agent`` 而不是 ``approved_commands``：一档提案"被产出过"与"赢了"是两件事，
    这条门问的是前者（审计原文是"没有 agent 产出过这两档"）。
    """

    observed: dict[str, set[str]] = {}
    for scenario_id, result in results.items():
        tiers: set[str] = set()
        for event in result.events:
            if event.event_type != COORDINATION_DECISION_EVENT_TYPE:
                continue
            tiers.update(str(entry["priority"]) for entry in event.data.get("per_agent", []))
        observed[scenario_id] = tiers
    return observed


def test_priority_tier_producer_table_covers_every_tier():
    """声明表必须与 §9.1 七档逐字对齐，且每一条都写清楚生产者是谁。

    没有这一条，新增一档只要没人登记就会静默跳过下面那条实跑门——"死档"这个审计发现
    就会以另一种形式回来。
    """

    assert set(PRIORITY_TIER_PRODUCERS) == set(PRIORITY_LEVELS)
    for tier, producer in PRIORITY_TIER_PRODUCERS.items():
        assert producer.note.strip(), f"{tier.value} 的生产者声明没有写理由"
        if producer.kind is ProducerKind.RESERVED:
            assert producer.witness == "", "保留档不该有 witness"
        else:
            assert producer.witness, f"{tier.value} 声明了生产者却没给出 witness"

    library = load_library()
    for tier, producer in PRIORITY_TIER_PRODUCERS.items():
        if producer.kind is ProducerKind.LIBRARY:
            assert producer.witness in library, (
                f"{tier.value} 的 witness 场景 {producer.witness} 不在场景库里"
            )

    # UI 腿的 witness 是一条真实存在的测试函数，不是一句安慰话。
    ui_leg = [p for p in PRIORITY_TIER_PRODUCERS.values() if p.kind is ProducerKind.UI_LEG]
    integration = Path(__file__).with_name("test_arbitration_integration.py").read_text(
        encoding="utf-8"
    )
    for producer in ui_leg:
        assert f"def {producer.witness}(" in integration, (
            f"UI 腿的 witness {producer.witness} 已经不在 test_arbitration_integration.py 里了"
        )


async def test_every_priority_tier_has_a_declared_producer():
    """整库实跑 → 逐档核对声明（S3 复审 minor：两档没有运行期生产者）。

    这条门补的正是 ``test_every_tier_can_win_something`` 证不了的那一半：那条测试给
    仲裁器喂合成提案，因此对"这一档现实中没人产出"完全免疫——复审实测 safety 与
    maintenance 在九个库场景里都是 0，而那条测试照绿。这里改用**真跑**的观测：

    * ``LIBRARY`` 档：必须在它自己声明的 witness 场景里被观测到；
    * ``RESERVED`` 档：必须整库一次都没出现（出现了说明声明过时，得改成 LIBRARY）；
    * ``UI_LEG`` 档：脚本化场景里不该出现（出现了说明有 agent 自称用户档，见 §9.1）。

    整库十个场景 headless 跑完在本机约 2 秒（每个场景 ~0.1s），因此这条门是逐场景真跑，
    而不是抽样。
    """

    results = {
        scenario_id: await run_scenario(scenario_id) for scenario_id in load_library()
    }
    per_scenario = _tiers_by_scenario(results)
    everywhere = set().union(*per_scenario.values())
    census = {scenario: sorted(tiers) for scenario, tiers in per_scenario.items()}

    for tier, producer in PRIORITY_TIER_PRODUCERS.items():
        if producer.kind is ProducerKind.LIBRARY:
            # .get 而不是 []：witness 场景整个消失时也要给出这条门自己的失败信息，
            # 而不是抛一个 KeyError 让人以为是测试写坏了。
            assert tier.value in per_scenario.get(producer.witness, set()), (
                f"{tier.value} 档在它声明的 witness 场景 {producer.witness} 里没有生产者；"
                f"整库观测={census}"
            )
        elif producer.kind is ProducerKind.RESERVED:
            assert tier.value not in everywhere, (
                f"{tier.value} 被声明为保留档，但库里真产出了它——请把 PRIORITY_TIER_PRODUCERS "
                f"改成 LIBRARY 并写上 witness 场景；整库观测={census}"
            )
        else:  # UI_LEG
            assert tier.value not in everywhere, (
                f"{tier.value} 只属于真人（UI 直控腿），却出现在脚本化场景里——"
                f"多半是某个 agent 又在 determine_priority 里自称用户档；整库观测={census}"
            )

    # 反向：每一档观测到的档位都得在声明表里（词表漂移当场变红）。
    assert everywhere <= {tier.value for tier in PRIORITY_LEVELS}
