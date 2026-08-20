"""S3 阶段门（二）：§6.10 多用户冲突 —— 仲裁必须真的选边，并且说得出为什么。

这是整个 S3 最难造假的一条门。审计 §六记下的原话是：

    *仲裁全序名存实亡：safety 与用户命令同分、security 档根本不存在；五类冲突只处理了
    「同设备同属性」一类；safety / energy_efficiency / background_optimization 是死档，
    没有任何 agent 产出过——真正发生过的仲裁只在 user_comfort 与 convenience 之间。*

要证明这条坑被填上，需要一次**真实的 headless run** 里同时出现：

1. 一条 episode **恰好一条** ``reasoning.coordination_decision``（§9.3；旧实现是每个
   agent 各发一条，于是"全序"在事件流里根本没有落点）；
2. 那条事件里同时有**批准**与**被拒**的命令，被拒的每一条都带 §9.2 五类之一的
   ``conflict_class`` 与人话 ``rejection_reason``；
3. ``winning_priority`` 是 §9.1 全序里的一档，而且冲突记录能指名道姓地说出
   **谁赢了、谁输了**；
4. 被拒的命令不消失：它在命令生命周期上走 ``proposed → rejected``（S1 根治的静默吞
   失败在协作层的对应物）。

场景本体是 ``backend/scenarios/library/multi_user_conflict.yaml``（S2-T8 刻意留给 S3-T10）：
两位用户在同一间客厅里先后表达互不相容的偏好，照明域要把灯拉回睡前亮度，场景域要把它
关掉 —— 同一房间、同一条环境量、方向相反，正是 §9.2 第二类冲突。

跑法::

    ./backend/.venv/bin/python -m pytest tests/test_multi_user_conflict.py -q --timeout=60
"""

from __future__ import annotations

import pytest

from backend.agents.arbiter import (
    ARBITER_ID,
    COORDINATION_DECISION_EVENT_TYPE,
    ConflictClass,
)
from backend.agents.contracts import PriorityLevel
from backend.agents.orchestrator import ORCHESTRATOR_ENABLED_ENV
from backend.engine.event_bus import SimEvent
from backend.scenarios.loader import get_scenario
from backend.scenarios.runner import ScenarioRunResult, run_scenario

pytestmark = pytest.mark.anyio

SCENARIO_ID = "multi_user_conflict"
SHARED_ROOM_ID = "living_room"

# §9.2 五类冲突的字符串词表（唯一来源是 arbiter.ConflictClass，这里只做投影）。
CONFLICT_CLASS_VALUES = frozenset(item.value for item in ConflictClass)
PRIORITY_VALUES = frozenset(item.value for item in PriorityLevel)


@pytest.fixture
def orchestrated(monkeypatch):
    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "1")


@pytest.fixture
async def conflict_run(orchestrated) -> ScenarioRunResult:
    """跑一遍 §6.10 场景（headless、固定 seed、mocked LLM）。"""

    return await run_scenario(SCENARIO_ID)


def _episode_correlation_ids(result: ScenarioRunResult) -> list[str]:
    """有推理发生的那些 correlation（按首次出现顺序）。"""

    seen: list[str] = []
    for event in sorted(result.events, key=lambda item: item.seq or -1):
        if event.event_type != "reasoning.perception_snapshot":
            continue
        if event.correlation_id not in seen:
            seen.append(event.correlation_id)
    return seen


def _coordination_events(result: ScenarioRunResult) -> list[SimEvent]:
    return [
        event
        for event in sorted(result.events, key=lambda item: item.seq or -1)
        if event.event_type == COORDINATION_DECISION_EVENT_TYPE
    ]


def _conflicted_decision(result: ScenarioRunResult) -> SimEvent:
    """本 run 里那条**真的发生了冲突**的协调决策（没有就当场失败）。"""

    for event in _coordination_events(result):
        if event.data["rejected_commands"] and event.data["approved_commands"]:
            return event
    pytest.fail(
        "§6.10 场景没有产出任何「既有批准又有拒绝」的协调决策——多用户冲突门退化成了摆拍"
    )


# ------------------------------------------------------------ 0. 场景本身可用


def test_scenario_loads_through_the_s2_loader_with_ground_truth():
    """场景必须经 S2 的 loader 加载（不是测试里现搓的字典），且带齐 §5.3 标签。"""

    spec = get_scenario(SCENARIO_ID)
    assert spec is not None, f"{SCENARIO_ID} 不在场景库里"
    assert spec.seed == 1010
    # 两位用户、同一个共享房间：这是 §6.10 的立身之本
    assert set(spec.initial_state.users) == {"user_01", "user_02"}
    assert all(
        user.location == SHARED_ROOM_ID for user in spec.initial_state.users.values()
    )
    assert spec.ground_truth is not None
    assert spec.ground_truth.primary_room_ids == [SHARED_ROOM_ID]
    assert spec.ground_truth.expected_intent
    assert spec.success_criteria.max_command_failures is not None

    # 跨阶段约定：本场景不得带脚本直控项（否则会抢走 §15-2 的验收锚点，
    # 见 tests/test_canonical_scenarios.py 与 YAML 顶部的注释）。
    from backend.scenarios.generator import scenario_timeline_device_entries

    assert not scenario_timeline_device_entries(spec)


async def test_scenario_runs_headless_to_completion(conflict_run):
    result = conflict_run
    assert result.completed is True, "timeline 必须在 duration 内全部触发"
    assert result.scenario_id == SCENARIO_ID
    emitted = {event.event_type for event in result.events}
    assert "user.starts_activity" in emitted
    assert "user.command" in emitted


# ------------------------------------------- 1. 一条 episode 恰好一条协调决策


async def test_exactly_one_coordination_decision_per_episode(conflict_run):
    """§9.3：一条 episode 只发一条协调决策——多一条就说明"全序"又散回了 per-agent。"""

    result = conflict_run
    correlations = _episode_correlation_ids(result)
    assert correlations, "这个 run 里一条 episode 都没有"

    for correlation_id in correlations:
        decisions = [
            event
            for event in _coordination_events(result)
            if event.correlation_id == correlation_id
        ]
        assert len(decisions) == 1, (
            f"correlation {correlation_id} 下有 {len(decisions)} 条协调决策，应当恰好 1 条"
        )
        assert decisions[0].source == ARBITER_ID

    # 反向也要成立：没有哪条协调决策游离在 episode 之外
    assert len(_coordination_events(result)) == len(correlations)


# --------------------------------------------- 2. 协调决策带齐 §9.3 三项输出


async def test_coordination_decision_carries_winner_losers_and_conflict_class(
    conflict_run,
):
    """门条款：批准 + 被拒 + §9.2 分类 + winning_priority + 人话解释，一个不缺。"""

    decision = _conflicted_decision(conflict_run)
    data = decision.data

    approved = data["approved_commands"]
    rejected = data["rejected_commands"]
    conflicts = data["conflicts"]

    assert approved, "赢家必须有名有姓"
    assert rejected, "输家必须留在事件流里，不能凭空消失"
    assert conflicts, "§9.3 第三项输出（冲突解释）不能为空"

    # winning_priority 是 §9.1 全序里的一档，不是自由文本
    assert data["winning_priority"] in PRIORITY_VALUES
    assert isinstance(data["explanation"], str) and data["explanation"].strip()

    winner_ids = {item["agent_id"] for item in approved}
    loser_ids = {item["agent_id"] for item in rejected}
    assert winner_ids, "批准侧必须能指出是谁赢了"
    assert loser_ids, "拒绝侧必须能指出是谁输了"

    for item in rejected:
        assert item["conflict_class"] in CONFLICT_CLASS_VALUES
        assert item["rejection_reason"].strip(), "被拒必须给出人话理由"
        assert item["priority"] in PRIORITY_VALUES
        assert item["device_id"] and item["property"]

    for conflict in conflicts:
        assert conflict["conflict_class"] in CONFLICT_CLASS_VALUES
        assert conflict["loser_agent_id"]
        assert conflict["explanation"].strip()
        # 冲突必须能指名赢家：只说"你输了"而不说"输给谁"，研究者读不出仲裁在做什么
        assert conflict["winner_agent_id"]
        assert conflict["winner_agent_id"] != conflict["loser_agent_id"]

    # per_agent 分解仍在（旧的 per-agent 结论降级成本事件里的一段，信息不丢）
    assert data["per_agent"]
    assert {item["agent_id"] for item in data["per_agent"]} >= winner_ids | loser_ids


async def test_the_conflict_happens_in_the_shared_room(conflict_run):
    """冲突必须落在两位用户共处的那个房间——否则测的不是"多用户冲突"。"""

    decision = _conflicted_decision(conflict_run)
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    shared_devices = set(spec.ground_truth.relevant_device_ids)

    in_shared_room = [
        conflict
        for conflict in decision.data["conflicts"]
        if conflict.get("room_id") == SHARED_ROOM_ID
        or conflict["device_id"] in shared_devices
    ]
    assert in_shared_room, (
        f"没有一条冲突发生在共享房间 {SHARED_ROOM_ID}："
        f"{[c['device_id'] for c in decision.data['conflicts']]}"
    )
    # 两位用户的偏好由两个不同的 agent 代表：冲突必须是**跨提案者**的
    assert len({conflict["loser_agent_id"] for conflict in in_shared_room}) >= 1
    for conflict in in_shared_room:
        assert conflict["winner_agent_id"] != conflict["loser_agent_id"]


async def test_rejected_commands_reach_a_rejected_lifecycle_state(conflict_run):
    """被仲裁否掉的命令不静默消失：它在命令生命周期上走到 ``rejected``。

    这是 S1 那条"绝不静默吞失败"在协作层的对应物。少了它，"被拒"只存在于一条 reasoning
    事件的 data 里，命令本身从未出生过——研究者按命令流复盘时会看到一条凭空缺失的动作。
    """

    result = conflict_run
    decision = _conflicted_decision(result)
    rejected_targets = {
        (item["device_id"], item["property"].removeprefix("extra."))
        for item in decision.data["rejected_commands"]
    }

    lifecycle_rejected = {
        (event.data["device_id"], event.data["capability"])
        for event in result.events
        if event.event_type == "command.lifecycle"
        and event.correlation_id == decision.correlation_id
        and event.data.get("to_status") == "rejected"
    }
    assert rejected_targets <= lifecycle_rejected, (
        f"这些被仲裁拒绝的命令没有对应的 rejected 生命周期事件："
        f"{sorted(rejected_targets - lifecycle_rejected)}"
    )
    # 拒绝的理由带着 §9.2 分类进了生命周期 detail（面板与工件都读它）
    details = [
        event.data.get("detail") or ""
        for event in result.events
        if event.event_type == "command.lifecycle"
        and event.correlation_id == decision.correlation_id
        and event.data.get("to_status") == "rejected"
    ]
    assert any(
        any(value in detail for value in CONFLICT_CLASS_VALUES) for detail in details
    ), f"rejected 生命周期事件的 detail 里没有 §9.2 冲突分类：{details}"


# ------------------------------------------------- 3. 仲裁结论真的落到了世界上


async def test_the_arbitration_winner_is_what_the_world_ends_up_with(orchestrated):
    """选边不能只写在事件里：共享房间的灯最终必须是**赢家**的值。

    这条与上面几条是两件事。仲裁可以把结论写得很漂亮，而落地那一步照样把输家的命令
    也执行了（旧实现里 approved/rejected 只是事件 payload，executor 并不消费它）——
    那样"仲裁"就退化成一层解说词。场景的 ``expected_device_effects`` 区间刻意卡在两个
    候选之间（照明域的睡前档在区间内、场景域的阅读档在区间外），因此这条断言问的正是
    "谁的值留在了世界上"。
    """

    from backend.scenarios.runner import ScenarioRunner

    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    runner = ScenarioRunner(spec)
    await runner.run()

    world = runner.state_manager.world
    checked = 0
    for effect in spec.expected_device_effects:
        device = world.devices[effect.device_id]
        for path, expected in effect.expected.items():
            actual = (
                device.state.power
                if path == "power"
                else device.state.extra.get(path.removeprefix("extra."))
            )
            assert expected.matches(actual), (
                f"{effect.device_id}.{path} 落地值 {actual!r} 不满足场景声明 {expected!r}"
            )
            checked += 1
    assert checked >= 4, "场景必须对冲突落点做出可证伪的声明"


# ------------------------------------------------------------------ 4. 可复现


async def test_conflict_outcome_is_reproducible_across_runs(orchestrated):
    """同 seed 两次运行，冲突结论逐字段相同——仲裁不能随调度抖动改口。"""

    def signature(result: ScenarioRunResult):
        return [
            (
                event.data["winning_priority"],
                tuple(
                    (item["agent_id"], item["device_id"], item["property"])
                    for item in event.data["approved_commands"]
                ),
                tuple(
                    (item["agent_id"], item["device_id"], item["property"], item["conflict_class"])
                    for item in event.data["rejected_commands"]
                ),
            )
            for event in _coordination_events(result)
        ]

    first = await run_scenario(SCENARIO_ID)
    second = await run_scenario(SCENARIO_ID)

    assert first.run_id != second.run_id
    assert signature(first) == signature(second)
