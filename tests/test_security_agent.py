"""S3-T4：SecurityAgent —— §8.2 安防域 agent + §8.4 统一提案契约。

这份测试盯死三件事：

1. **security 档真的有生产者**。审计 §六 记着一条坑：§9.1 把 safety 排在最高档，可是
   *没有任何 agent 产出过 safety/security 档提案*，仲裁器最高的两档只被合成夹具走过。
   SecurityAgent 是第一个真实生产者，所以"离家 → security 档"这条断言不是形式主义。
2. **safety 档 fail-closed**（critic 绑定修正）：``safety.smoke_detected`` 必须由
   SecurityAgent 以 :attr:`PriorityLevel.SAFETY` 兜住，S4 的安全打断场景才有真实来源。
3. **五种非动作表达里的 unsafe_rejected 有真实生产者**：§7 ``device.offline`` 行的
   default_policy 原文就是 *fail closed and explain*——覆盖不可验证时拒绝行动并解释，
   而不是静默什么都不做（那正是 §8.4 要根治的形态）。

摄像头能力约束（写给后来人）：§3.2 能力矩阵里 camera 只有 ``view``/``online`` 两条
**只读**能力，因此"布防"在设备层没有可写落点。本实现把布防表达成两件可断言的事：
提案的档位（security）+ 摄像头覆盖房间里的**门口灯**（§7 security.presence_detected 行
原文的 "entry lights"）。断言写在 :func:`test_leaves_home_proposes_camera_armed_posture_at_security_tier`。
"""

from __future__ import annotations

import pytest

from backend.agents.contracts import PriorityLevel, ProposalOutcome
from backend.agents.security import (
    EVACUATION_BRIGHTNESS,
    SECURITY_AGENT_ID,
    SecurityAgent,
)
from backend.engine.event_bus import SimEvent
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    WorldState,
)
from backend.scenarios.runner import run_scenario


# --------------------------------------------------------------------- 夹具


def _camera(device_id: str, room: str, *, online: bool = True) -> DeviceState:
    return DeviceState(
        id=device_id,
        type="camera",
        location=Location3D(room=room),
        state=DeviceStateValues(power=True, extra={"online": online}),
    )


def _light(device_id: str, room: str, *, power: bool = False, brightness: int = 0) -> DeviceState:
    return DeviceState(
        id=device_id,
        type="light",
        location=Location3D(room=room),
        state=DeviceStateValues(power=power, extra={"brightness": brightness, "color_temp": 3500}),
    )


def _world(*, occupied: bool = False, cameras: int = 2, camera_online: bool = True) -> WorldState:
    world = WorldState(environment=EnvironmentState(time_of_day="20:00"))
    world.rooms = {
        "living_room": RoomState(
            id="living_room",
            occupancy=occupied,
            persons=["user_01"] if occupied else [],
        ),
        # 没有摄像头的房间：它的灯不该被布防动作碰到（覆盖面 ≠ 全屋）。
        "bedroom": RoomState(id="bedroom", occupancy=False),
    }
    devices: dict[str, DeviceState] = {
        "light_living_01": _light("light_living_01", "living_room"),
        "light_bedroom_01": _light("light_bedroom_01", "bedroom"),
    }
    for index in range(cameras):
        devices[f"camera_living_{index:02d}"] = _camera(
            f"camera_living_{index:02d}", "living_room", online=camera_online or index > 0
        )
    world.devices = devices
    return world


def _event(event_type: str, **data) -> SimEvent:
    return SimEvent(event_type=event_type, source="test", timestamp=1.0, data=data)


# --------------------------------------------------------------- 布防 / 撤防


def test_leaves_home_proposes_camera_armed_posture_at_security_tier():
    """§6.2「离家 → 开启安防监控」：security 档 + 摄像头覆盖房间的门口灯。"""

    agent = SecurityAgent()
    world = _world(occupied=False)
    proposal = agent.propose(world_state=world, root_event=_event("user.leaves_home"))

    assert proposal.agent_id == SECURITY_AGENT_ID
    assert proposal.agent_role == "security"
    # 审计坑：security 档此前零生产者。
    assert proposal.priority is PriorityLevel.SECURITY
    assert proposal.outcome is ProposalOutcome.ACTED
    assert proposal.has_commands

    # 布防只落在**有摄像头覆盖**的房间的灯上（§7「entry lights」），卧室灯不在其中。
    touched = {command.device_id for command in proposal.commands}
    assert touched == {"light_living_01"}
    assert "camera_living_00" in " ".join(command.reason for command in proposal.commands)


def test_arrives_home_disarms_with_explicit_no_action_needed():
    """§8.4 第一种表达：无事可做也要**说出来**，不是静默不发。"""

    agent = SecurityAgent()
    world = _world(occupied=True)
    proposal = agent.propose(world_state=world, root_event=_event("user.arrives_home"))

    assert proposal.outcome is ProposalOutcome.NO_ACTION_NEEDED
    assert proposal.commands == []
    assert proposal.noop_reason


def test_smoke_detected_is_handled_fail_closed_at_safety_tier():
    """critic 绑定修正：safety 档必须有真实生产者（S4 安全打断场景的来源）。"""

    agent = SecurityAgent()
    world = _world(occupied=True)
    smoke = _event("safety.smoke_detected", room_id="living_room", severity="high")

    assert agent.is_relevant(world, smoke) is True
    proposal = agent.propose(world_state=world, root_event=smoke)

    assert proposal.priority is PriorityLevel.SAFETY
    assert proposal.outcome is ProposalOutcome.ACTED
    # fail-closed：疏散照明全开，不看时间也不看能耗。
    powered = {
        (command.device_id, command.property, command.value) for command in proposal.commands
    }
    assert ("light_living_01", "power", True) in powered
    assert ("light_living_01", "extra.brightness", 100) in powered


def test_smoke_outranks_every_other_security_posture():
    """同一 agent 的两条事件不能同档：safety 必须严格高于 security。"""

    from backend.agents.contracts import outranks

    agent = SecurityAgent()
    world = _world()
    safety = agent.proposal_priority(world, _event("safety.smoke_detected"))
    security = agent.proposal_priority(world, _event("user.leaves_home"))
    assert outranks(safety, security) is True


# ------------------------------------------------------- device.offline 兜底


def test_device_offline_fails_closed_with_unsafe_reject_outcome():
    """§7 ``device.offline`` 行 default_policy 原文：*fail closed and explain*。

    房间里最后一台摄像头掉线 → 覆盖不可验证 → 拒绝按"覆盖仍在"的假设行动，
    并把本来要发的补偿命令留在 ``withheld_commands`` 里（§9.3 rejected commands 溯源）。
    """

    agent = SecurityAgent()
    world = _world(occupied=False, cameras=1)
    world.devices["camera_living_00"].state.extra["online"] = False
    offline = _event("device.offline", device_id="camera_living_00", device_type="camera")

    assert agent.is_relevant(world, offline) is True
    proposal = agent.propose(world_state=world, root_event=offline)

    assert proposal.outcome is ProposalOutcome.UNSAFE_REJECTED
    assert proposal.commands == []
    assert proposal.risks, "unsafe_rejected 必须写明风险"
    assert any("camera_living_00" in risk for risk in proposal.risks)
    assert proposal.withheld_commands, "被扣下的命令必须留痕，否则 §9.3 只剩一句话"
    assert proposal.noop_reason


def test_device_offline_with_surviving_camera_is_no_action_needed():
    """还有备用摄像头在线 → 覆盖没丢，明确说"不用动"，而不是也走 unsafe_rejected。"""

    agent = SecurityAgent()
    world = _world(occupied=False, cameras=2)
    world.devices["camera_living_00"].state.extra["online"] = False
    offline = _event("device.offline", device_id="camera_living_00", device_type="camera")

    proposal = agent.propose(world_state=world, root_event=offline)
    assert proposal.outcome is ProposalOutcome.NO_ACTION_NEEDED
    assert "camera_living_01" in (proposal.noop_reason or "")


def test_light_offline_is_not_a_security_episode():
    """相关面收口：灯掉线不该让安防 agent 跑一轮（§7「affected device」是设备族事件）。"""

    agent = SecurityAgent()
    world = _world()
    offline = _event("device.offline", device_id="light_living_01", device_type="light")
    assert agent.is_relevant(world, offline) is False


# ----------------------------------------------------------------- 相关面收口


@pytest.mark.parametrize(
    "event_type",
    [
        "environment.state_refresh",
        "user.enters_room",
        "user.exits_room",
        "environment.temperature_threshold",
    ],
)
def test_non_security_events_do_not_open_a_security_episode(event_type: str):
    """安防 agent 只订安防/安全事件族——否则每 tick 的例行刷新都多开一轮推理。"""

    agent = SecurityAgent()
    world = _world()
    event = _event(event_type, significant_change_reasons=["temperature"])
    assert agent.is_relevant(world, event) is False


def test_presence_detected_while_empty_raises_full_lighting():
    agent = SecurityAgent()
    world = _world(occupied=False)
    event = _event("security.presence_detected", room_id="living_room")

    assert agent.is_relevant(world, event) is True
    proposal = agent.propose(world_state=world, root_event=event)
    assert proposal.priority is PriorityLevel.SECURITY
    assert proposal.outcome is ProposalOutcome.ACTED
    assert ("light_living_01", "extra.brightness", 100) in {
        (command.device_id, command.property, command.value) for command in proposal.commands
    }


# ------------------------------------------- safety 档的**场景级**生产者（S3 复审 minor）


SAFETY_SMOKE_SCENARIO_ID = "safety_smoke_kitchen"


@pytest.mark.anyio
async def test_smoke_scenario_puts_the_safety_tier_on_the_real_runtime_path(monkeypatch):
    """库场景 ``safety_smoke_kitchen`` 必须真的走出一条 safety 档决策。

    上面那些用例证明的是"SecurityAgent 收到烟雾事件会给 SAFETY 档提案"，用的是手搓
    根事件。S3 复审指出的坑正好在这条缝里：``grep -rn smoke_detected backend/scenarios/``
    一条都没有——代码里的生产者从来没有被任何 timeline 触发过，于是 §9.1 的最高档在
    实测里恒为 0。这条测试跑的是**库场景 + S2 headless runner + 编排器 + 仲裁门**的
    真实装配：安全事件必须经过编排器分派、落到 SecurityAgent、以 safety 档胜出并真的
    改动设备。
    """

    result = await run_scenario(SAFETY_SMOKE_SCENARIO_ID)

    assert result.completed is True, "timeline 必须在 duration 内全部触发"
    assert any(event.event_type == "safety.smoke_detected" for event in result.events)

    decisions = [
        event
        for event in result.events
        if event.event_type == "reasoning.coordination_decision"
    ]
    safety_decisions = [
        event for event in decisions if event.data["winning_priority"] == "safety"
    ]
    assert safety_decisions, (
        "没有一条 coordination_decision 以 safety 档胜出——最高档又变回了合成夹具专属"
    )
    assert any(
        entry["agent_id"] == SECURITY_AGENT_ID and entry["priority"] == "safety"
        for event in safety_decisions
        for entry in event.data["per_agent"]
    )

    # 真的动了设备：疏散照明落到厨房灯上（EVACUATION_BRIGHTNESS）。
    evacuation = [
        event
        for event in result.events
        if event.event_type == "action.device_control"
        and event.data.get("agent_id") == SECURITY_AGENT_ID
        and event.data.get("device_id") == "light_kitchen_01"
        and event.data.get("property") == "extra.brightness"
    ]
    assert evacuation, "safety 档赢了却没有一条真实的疏散照明命令"
    assert evacuation[-1].data["value"] == EVACUATION_BRIGHTNESS


@pytest.mark.anyio
async def test_smoke_scenario_preempts_a_comfort_tier_behaviour(monkeypatch):
    """§13「safety event interrupts comfort or energy-saving behavior」的可执行形式。

    只证明"safety 档出现过"是不够的——最高档的意义在于它**赢过别人**。这条测试要求
    同一条决策里存在一条 comfort 档的被拒命令，冲突类别是 §9.2 的
    ``same_device_property``，赢家是 safety 档的 SecurityAgent：照明域想把空厨房的亮度
    收回舒适档，烟雾未散的疏散照明把它按住了。

    场景怎么造出这个局面见 backend/scenarios/library/safety_smoke.yaml 的注释：
    收敛型 agent 只在"有差值"时才出手，所以要让 comfort 与 safety 落在同一个控制点上，
    必须让报警持续到照明域打算把疏散照明调回去的那一轮。
    """

    result = await run_scenario(SAFETY_SMOKE_SCENARIO_ID)

    preemptions = [
        (event, rejected)
        for event in result.events
        if event.event_type == "reasoning.coordination_decision"
        for rejected in event.data["rejected_commands"]
        if rejected["winner_priority"] == "safety"
    ]
    assert preemptions, (
        "safety 档从没压过任何人：这个场景要给 S4 的正是'安全打断舒适/节能行为'的素材"
    )

    event, rejected = preemptions[0]
    assert rejected["priority"] == "comfort"
    assert rejected["conflict_class"] == "same_device_property"
    assert rejected["device_id"] == "light_kitchen_01"
    assert rejected["winner_agent_id"] == SECURITY_AGENT_ID
    # 这条拒绝是按 §9.1 全序做出的，不是能耗否决那条全序之外的例外。
    assert rejected["resolution"] == "total_order"

    conflicts = event.data["conflicts"]
    assert any(
        conflict["conflict_class"] == "same_device_property"
        and conflict["winner_priority"] == "safety"
        for conflict in conflicts
    ), "被拒了却没有一条可读的冲突解释（§9.3 第三项输出）"


def test_security_agent_issues_no_camera_commands():
    """§3.2：camera 的 view/online 都是只读能力——安防 agent 不得对摄像头发写命令。

    少了这条，SecurityAgent 会一路把命令送进 CommandExecutor 再被
    ``capability_not_writable`` 打回，"布防"在演示里变成一串失败命令。
    """

    agent = SecurityAgent()
    world = _world()
    for event_type in ("user.leaves_home", "security.presence_detected", "safety.smoke_detected"):
        proposal = agent.propose(world_state=world, root_event=_event(event_type))
        for command in [*proposal.commands, *proposal.withheld_commands]:
            device = world.devices[command.device_id]
            assert device.type != "camera", f"{event_type} 对摄像头发了写命令"
