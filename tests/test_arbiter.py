"""S3-T5：§9 仲裁器的单元门。

这份测试盯死审计 §六 记下的三条坑，每一条都写成**可证伪**的断言：

1. 旧 ``PRIORITY_RANK`` 只有六档，且 ``safety`` 与 ``direct_user_command`` **并列第 5**、
   ``security`` 档根本不存在。并列 = "谁先进循环谁赢"，仲裁结果不可解释。
   :func:`test_seven_tier_total_order_pairwise` 对**全部** 7×6 个有序档位对成对断言，
   并单独钉住 "safety 严格压过 explicit_user"——这在旧表里是不可能通过的。
2. 旧仲裁器只实现 §9.2 五类冲突里的**一类**（同设备同属性）。
   :func:`test_each_of_five_conflict_classes_produces_tagged_conflict_record` 逐类要求
   一条打了 ``conflict_class`` 标签的冲突记录。
3. 旧仲裁器**从不读世界**，因此"设备离线却被选中"这一类冲突在结构上无法被发现。
   :func:`test_device_unavailable_conflict_requires_world_snapshot` 把"必须读世界"
   变成机制：不传世界快照就发现不了它。

另外钉住 §9.3 的三项输出（批准 / 拒绝 / 冲突解释 + winning_priority），以及
"用户命令的占用会让并发 episode 输"——那正是审计里"用户覆盖 agent 只是名义上的"那条坑。
"""

from __future__ import annotations

import itertools

import pytest

from backend.agents.arbiter import (
    ARBITER_ID,
    COORDINATION_DECISION_EVENT_TYPE,
    UI_ACTOR_ID,
    Arbiter,
    ArbiterResult,
    ConflictClass,
    ConflictResolution,
    ExplicitUserClaim,
)
from backend.agents.contracts import (
    PRIORITY_LEVELS,
    AgentProposal,
    PriorityLevel,
    ProposalOutcome,
    priority_rank,
)
from backend.agents.energy import EnergyVeto, EnergyVetoReview
from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    EnvironmentState,
    Location3D,
    RoomState,
    WorldState,
)


# --------------------------------------------------------------------- 夹具


def _world() -> WorldState:
    """一间有人的客厅 + 一间无人的阁楼（阁楼那盏灯是离线的）。"""

    world = WorldState(environment=EnvironmentState(time_of_day="20:00"))
    world.rooms = {
        "living_room": RoomState(
            id="living_room", temperature=28.0, occupancy=True, persons=["user_01"]
        ),
        "loft": RoomState(id="loft", temperature=24.0, occupancy=False, persons=[]),
    }
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness", "color_temp"],
            state=DeviceStateValues(power=True, extra={"brightness": 60, "color_temp": 4000}),
        ),
        "curtain_living_01": DeviceState(
            id="curtain_living_01",
            type="curtain",
            location=Location3D(room="living_room"),
            capabilities=["power", "open_percent"],
            state=DeviceStateValues(power=True, extra={"open_percent": 0}),
        ),
        "hvac_living_01": DeviceState(
            id="hvac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            capabilities=["power", "target_temp", "mode", "speed"],
            state=DeviceStateValues(
                power=True, extra={"target_temp": 24.0, "mode": "cool", "speed": "low"}
            ),
        ),
        "camera_living_01": DeviceState(
            id="camera_living_01",
            type="camera",
            location=Location3D(room="living_room"),
            capabilities=["power"],
            state=DeviceStateValues(power=False, extra={"online": True}),
        ),
        "light_loft_01": DeviceState(
            id="light_loft_01",
            type="light",
            location=Location3D(room="loft"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(power=False, extra={"brightness": 0, "online": False}),
        ),
    }
    return world


def _event(event_type: str = "user.activity_change", *, wall_time: float = 100.0) -> SimEvent:
    return SimEvent(
        event_id="root-arb",
        event_type=event_type,
        source="test",
        timestamp=9.0,
        wall_time=wall_time,
        correlation_id="corr-arb",
    )


def _command(device_id: str, prop: str, value, reason: str = "") -> AgentCommandProposal:
    return AgentCommandProposal(device_id=device_id, property=prop, value=value, reason=reason)


def _proposal(
    agent_id: str,
    priority: PriorityLevel,
    commands: list[AgentCommandProposal],
    *,
    role: str = "",
    confidence: float = 0.9,
) -> AgentProposal:
    return AgentProposal(
        agent_id=agent_id,
        intent=f"{agent_id} intent",
        priority=priority,
        confidence=confidence,
        commands=commands,
        agent_role=role,
    )


# ------------------------------------------------------- §9.1 七档严格全序


def test_seven_tier_total_order_pairwise():
    """七档两两成对：高档恒胜，且**与 agent_id 字典序无关**。

    低档 agent 的 id 刻意排在前面（``a_low`` < ``z_high``），所以只要仲裁器还用
    "谁先进循环谁赢"或字典序兜底，这条断言就会红。旧六档表里 safety 与
    direct_user_command 同分，这条测试在它上面无论如何都过不了。
    """

    arbiter = Arbiter()
    root = _event()

    pairs = [
        (high, low)
        for high, low in itertools.permutations(PRIORITY_LEVELS, 2)
        if priority_rank(high) > priority_rank(low)
    ]
    assert len(pairs) == 21  # C(7,2)：七档两两一次

    for high, low in pairs:
        result = arbiter.resolve(
            [
                _proposal("a_low", low, [_command("light_living_01", "extra.brightness", 10)]),
                _proposal("z_high", high, [_command("light_living_01", "extra.brightness", 90)]),
            ],
            root_event=root,
        )
        assert [item.value for item in result.approved_commands] == [90], f"{high} vs {low}"
        assert [item.agent_id for item in result.rejected_commands] == ["a_low"]
        assert result.rejected_commands[0].conflict_class is ConflictClass.SAME_DEVICE_PROPERTY
        assert result.rejected_commands[0].winner_priority is high
        assert result.winning_priority is high


def test_safety_strictly_beats_explicit_user():
    """审计原文那条并列：旧表里 safety 与 direct_user_command 同为 5 分。"""

    assert priority_rank(PriorityLevel.SAFETY) > priority_rank(PriorityLevel.EXPLICIT_USER)

    arbiter = Arbiter()
    result = arbiter.resolve(
        [
            _proposal(
                "user_ui",
                PriorityLevel.EXPLICIT_USER,
                [_command("light_living_01", "power", False)],
            ),
            _proposal(
                "security_agent",
                PriorityLevel.SAFETY,
                [_command("light_living_01", "power", True, "疏散照明")],
            ),
        ],
        root_event=_event("safety.smoke_detected"),
    )

    assert [item.agent_id for item in result.approved_commands] == ["security_agent"]
    assert result.winning_priority is PriorityLevel.SAFETY
    assert result.rejected_commands[0].agent_id == "user_ui"


def test_same_value_on_the_same_target_is_not_a_conflict():
    """两个 agent 想把同一控制点写成**同一个值**：没有冲突，不该凭空造一条。"""

    arbiter = Arbiter()
    result = arbiter.resolve(
        [
            _proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_living_01", "power", True)]),
            _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("light_living_01", "power", True)]),
        ],
        root_event=_event(),
    )
    assert result.conflicts == []
    assert len(result.approved_commands) == 2


# --------------------------------------------------- §9.2 五类冲突各一条记录


def test_each_of_five_conflict_classes_produces_tagged_conflict_record():
    """§9.2 五类冲突**逐类**要一条打标签的记录。旧仲裁器只有第一类。"""

    arbiter = Arbiter()
    world = _world()
    root = _event()
    seen: dict[ConflictClass, ArbiterResult] = {}

    # (1) 同设备同属性不同值
    seen[ConflictClass.SAME_DEVICE_PROPERTY] = arbiter.resolve(
        [
            _proposal("hvac_agent", PriorityLevel.COMFORT, [_command("light_living_01", "extra.brightness", 80)]),
            _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("light_living_01", "extra.brightness", 20)]),
        ],
        root_event=root,
        world_snapshot=world,
    )

    # (2) 同房间互斥的环境目标：关灯（压低 light_level）vs 开窗帘（抬高 light_level）
    seen[ConflictClass.ROOM_ENVIRONMENT_GOAL] = arbiter.resolve(
        [
            _proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_living_01", "power", False)]),
            _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("curtain_living_01", "extra.open_percent", 100)]),
        ],
        root_event=root,
        world_snapshot=world,
    )

    # (3) 能耗 vs 舒适：来自 EnergyAgent 的否决（占用门已在 agent 内部判过，仲裁器不复判）
    veto = EnergyVeto(
        device_id="hvac_living_01",
        property="power",
        vetoed_agent_id="hvac_agent",
        vetoed_priority=PriorityLevel.COMFORT,
        rule="unoccupied_home_only",
        reason="全屋无人时不该维持制冷",
    )
    seen[ConflictClass.ENERGY_VS_COMFORT] = arbiter.resolve(
        [_proposal("hvac_agent", PriorityLevel.COMFORT, [_command("hvac_living_01", "power", True)])],
        root_event=root,
        world_snapshot=world,
        energy_review=EnergyVetoReview(
            home_occupied=False,
            vetoes=(veto,),
            reviewed_agent_ids=("hvac_agent",),
            explanation="规则 unoccupied_home_only：全屋无人，否决 1 条舒适/氛围档命令。",
        ),
    )

    # (4) 安防 vs 隐私：有人在家时开摄像头
    seen[ConflictClass.SECURITY_VS_PRIVACY] = arbiter.resolve(
        [_proposal("security_agent", PriorityLevel.SECURITY, [_command("camera_living_01", "power", True)])],
        root_event=root,
        world_snapshot=world,
    )

    # (5) 设备不可用却被选中
    seen[ConflictClass.DEVICE_UNAVAILABLE] = arbiter.resolve(
        [_proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_loft_01", "power", True)])],
        root_event=root,
        world_snapshot=world,
    )

    assert set(seen) == set(ConflictClass), "§9.2 五类必须全部有产生路径"
    for conflict_class, result in seen.items():
        assert result.conflicts, f"{conflict_class.value} 没有产生冲突记录"
        assert [item.conflict_class for item in result.conflicts] == [conflict_class]
        assert result.conflicts[0].explanation
        assert [item.conflict_class for item in result.rejected_commands] == [conflict_class]
        assert result.rejected_commands[0].rejection_reason


def test_device_unavailable_conflict_requires_world_snapshot():
    """不读世界就发现不了"设备离线却被选中"——把审计那句"从不读世界"变成机制。"""

    arbiter = Arbiter()
    proposals = [
        _proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_loft_01", "power", True)])
    ]

    without_world = arbiter.resolve(proposals, root_event=_event())
    assert without_world.conflicts == []
    assert len(without_world.approved_commands) == 1

    with_world = arbiter.resolve(proposals, root_event=_event(), world_snapshot=_world())
    assert with_world.approved_commands == []
    assert with_world.rejected_commands[0].conflict_class is ConflictClass.DEVICE_UNAVAILABLE
    assert with_world.winning_priority is None


def test_unknown_device_is_also_device_unavailable():
    arbiter = Arbiter()
    result = arbiter.resolve(
        [_proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("light_ghost_99", "power", True)])],
        root_event=_event(),
        world_snapshot=_world(),
    )
    assert result.rejected_commands[0].conflict_class is ConflictClass.DEVICE_UNAVAILABLE


def test_energy_veto_never_touches_the_top_three_tiers():
    """否决不越级：safety / explicit_user / security 的命令不因能耗否决被拒。

    （占用门在 EnergyAgent 里；这里断言的是仲裁器不会把越级否决也照单全收。）
    """

    arbiter = Arbiter()
    veto = EnergyVeto(
        device_id="light_living_01",
        property="power",
        vetoed_agent_id="security_agent",
        vetoed_priority=PriorityLevel.SECURITY,
        rule="unoccupied_home_only",
        reason="越级否决（不该发生）",
    )
    result = arbiter.resolve(
        [_proposal("security_agent", PriorityLevel.SECURITY, [_command("light_living_01", "power", True)])],
        root_event=_event(),
        world_snapshot=_world(),
        energy_review=EnergyVetoReview(home_occupied=False, vetoes=(veto,), reviewed_agent_ids=("security_agent",)),
    )
    assert len(result.approved_commands) == 1
    assert result.conflicts == []


def test_explicit_user_and_safety_are_exempt_from_unilateral_rejection():
    """用户直控与安全档不由仲裁器代判"设备能不能用"——那是 §10.2 校验层的词表。

    豁免的第一条腿按**行为体身份**给（``agent_id == UI_ACTOR_ID``），第二条腿按
    safety 档给：见 :func:`test_an_agent_cannot_buy_unilateral_exemption_by_declaring_a_tier`。
    """

    arbiter = Arbiter()
    world = _world()
    result = arbiter.resolve(
        [
            _proposal(UI_ACTOR_ID, PriorityLevel.EXPLICIT_USER, [_command("light_loft_01", "power", True)]),
            _proposal("security_agent", PriorityLevel.SAFETY, [_command("camera_living_01", "power", True)]),
        ],
        root_event=_event(),
        world_snapshot=world,
    )
    assert len(result.approved_commands) == 2
    assert result.conflicts == []


@pytest.mark.parametrize(
    "declared",
    [tier for tier in PRIORITY_LEVELS if tier is not PriorityLevel.SAFETY],
)
def test_an_agent_cannot_buy_unilateral_exemption_by_declaring_a_tier(declared):
    """S3 复审 blocker：豁免必须按**行为体**判，不能按提案自称的档位判。

    离线设备上的 agent 提案，无论自称哪一档（``explicit_user`` 也算），都必须触发
    §9.2 第五类。旧实现 ``exempt = entry.priority in UNILATERAL_EXEMPT_TIERS`` 让任何
    自称 ``explicit_user`` 的 agent 白拿豁免，于是 device_unavailable / 隐私 / 能耗否决
    三个检测器在那些 episode 上集体失效。
    """

    result = Arbiter().resolve(
        [_proposal("lighting_agent", declared, [_command("light_loft_01", "power", True)])],
        root_event=_event(),
        world_snapshot=_world(),
    )
    assert result.approved_commands == []
    assert result.rejected_commands[0].conflict_class is ConflictClass.DEVICE_UNAVAILABLE
    assert result.rejected_commands[0].resolution is ConflictResolution.UNILATERAL


# ------------------------------------------------------------ §9.3 三项输出


def test_result_contains_approved_rejected_explanation_winning_priority():
    arbiter = Arbiter()
    result = arbiter.resolve(
        [
            _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("light_living_01", "extra.brightness", 20)]),
            _proposal("hvac_agent", PriorityLevel.COMFORT, [_command("light_living_01", "extra.brightness", 80)]),
            _proposal("energy_agent", PriorityLevel.ENERGY, [_command("hvac_living_01", "power", False)]),
        ],
        root_event=_event(),
        world_snapshot=_world(),
    )

    assert [(item.device_id, item.value) for item in result.approved_commands] == [
        ("light_living_01", 80),
        ("hvac_living_01", False),
    ]
    assert len(result.rejected_commands) == 1
    assert result.rejected_commands[0].rejection_reason
    assert result.explanation
    assert result.winning_priority is PriorityLevel.COMFORT

    assert {item.agent_id for item in result.approved_commands} == {
        "hvac_agent",
        "energy_agent",
    }


def test_event_data_carries_the_whole_result_for_one_coordination_decision():
    """一条 episode 只发一条 ``reasoning.coordination_decision``，它必须自带全部裁决内容。"""

    assert COORDINATION_DECISION_EVENT_TYPE == "reasoning.coordination_decision"

    arbiter = Arbiter()
    result = arbiter.resolve(
        [
            _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("light_living_01", "power", False)]),
            _proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_living_01", "power", True)]),
        ],
        root_event=_event(),
        world_snapshot=_world(),
    )
    data = result.event_data()

    assert data["winning_priority"] == "comfort"
    assert data["explanation"]
    assert [item["agent_id"] for item in data["approved_commands"]] == ["lighting_agent"]
    assert [item["agent_id"] for item in data["rejected_commands"]] == ["scene_agent"]
    assert data["rejected_commands"][0]["conflict_class"] == "same_device_property"
    assert data["conflicts"][0]["conflict_class"] == "same_device_property"
    # per-agent 结论从"每 agent 一条事件"降级为本事件里的一段分解（审计：仲裁全序名存实亡）
    assert {item["agent_id"] for item in data["per_agent"]} == {"lighting_agent", "scene_agent"}
    # event schema 1.0 persisted-run compatibility.
    assert data["agent_id"] == ARBITER_ID
    assert data["outcome"] == "partial"
    assert data["priority"] == "comfort"
    assert [item["value"] for item in data["winning_commands"]] == [True]


def test_non_action_proposals_are_reported_without_inventing_commands():
    arbiter = Arbiter()
    quiet = AgentProposal(
        agent_id="energy_agent",
        intent="有人在家：能耗让位于舒适",
        priority=PriorityLevel.ENERGY,
        confidence=0.8,
        outcome=ProposalOutcome.NO_ACTION_NEEDED,
        noop_reason="有人在家，本轮不提案",
    )
    result = arbiter.resolve([quiet], root_event=_event(), world_snapshot=_world())

    assert result.approved_commands == []
    assert result.rejected_commands == []
    assert result.winning_priority is None
    per_agent = {item.agent_id: item for item in result.per_agent}
    assert per_agent["energy_agent"].outcome == "no_action_needed"


# --------------------------------- 用户命令的占用：并发 episode 必须输给它


def test_explicit_user_claim_makes_a_concurrent_agent_proposal_lose():
    """审计坑："用户覆盖 agent" 过去只是名义上的（用户命令根本不进仲裁）。"""

    arbiter = Arbiter()
    root = _event(wall_time=100.0)
    claim = ExplicitUserClaim(
        device_id="light_living_01",
        capability="brightness",
        value=0,
        agent_id="user_ui",
        command_id="cmd-user-1",
        # 用户是在这条 episode 的根事件**之后**说话的 → 用户赢
        issued_wall_time=101.0,
    )
    result = arbiter.resolve(
        [_proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_living_01", "extra.brightness", 80)])],
        root_event=root,
        world_snapshot=_world(),
        user_claims={("light_living_01", "brightness"): claim},
    )

    assert result.approved_commands == []
    rejected = result.rejected_commands[0]
    assert rejected.conflict_class is ConflictClass.SAME_DEVICE_PROPERTY
    assert rejected.winner_priority is PriorityLevel.EXPLICIT_USER
    assert rejected.winner_agent_id == "user_ui"


@pytest.mark.parametrize(
    "declared",
    [tier for tier in PRIORITY_LEVELS if tier is not PriorityLevel.SAFETY],
)
def test_a_registered_user_claim_beats_any_agent_tier_below_safety(declared):
    """S3 复审 blocker：占用检查比的是**占用的权威**，不是提案自称的档位。

    旧实现 ``if priority_rank(entry.priority) >= priority_rank(EXPLICIT_USER): return None``
    让任何自称 ``explicit_user`` 的 agent 对真人占用免疫——"用户覆盖 agent"这条审计坑
    在用户最活跃的那些 episode 上原地复活。只有 safety 压得过真人。
    """

    claim = ExplicitUserClaim(
        device_id="light_living_01",
        capability="brightness",
        value=5,
        agent_id=UI_ACTOR_ID,
        command_id="cmd-user-1",
        issued_wall_time=9.0,
    )
    result = Arbiter().resolve(
        [
            _proposal(
                "lighting_agent",
                declared,
                [_command("light_living_01", "extra.brightness", 95)],
            )
        ],
        root_event=_event(wall_time=1.0),
        world_snapshot=_world(),
        user_claims={("light_living_01", "brightness"): claim},
    )

    assert result.approved_commands == [], f"{declared.value} 档的 agent 提案压过了真人占用"
    rejected = result.rejected_commands[0]
    assert rejected.winner_agent_id == UI_ACTOR_ID
    assert rejected.winner_priority is PriorityLevel.EXPLICIT_USER


def test_a_safety_proposal_still_survives_a_user_claim():
    """唯一的例外：§9.1 里只有 safety 严格高于 explicit_user。"""

    claim = ExplicitUserClaim(
        device_id="light_living_01",
        capability="power",
        value=False,
        agent_id=UI_ACTOR_ID,
        issued_wall_time=9.0,
    )
    result = Arbiter().resolve(
        [_proposal("security_agent", PriorityLevel.SAFETY, [_command("light_living_01", "power", True)])],
        root_event=_event("safety.smoke_detected", wall_time=1.0),
        world_snapshot=_world(),
        user_claims={("light_living_01", "power"): claim},
    )
    assert len(result.approved_commands) == 1


def test_the_user_leg_does_not_lose_to_its_own_claim():
    """UI 腿在同一轮里既是提案者又是占用者：用户不与自己冲突。"""

    claim = ExplicitUserClaim(
        device_id="light_living_01",
        capability="power",
        value=False,
        agent_id=UI_ACTOR_ID,
        issued_wall_time=9.0,
    )
    result = Arbiter().resolve(
        [_proposal(UI_ACTOR_ID, PriorityLevel.EXPLICIT_USER, [_command("light_living_01", "power", False)])],
        root_event=_event("user.command", wall_time=1.0),
        world_snapshot=_world(),
        user_claims={("light_living_01", "power"): claim},
    )
    assert len(result.approved_commands) == 1
    assert result.rejected_commands == []


def test_stale_user_claim_does_not_block_a_later_episode():
    """用户是在根事件**之前**说的话：那条占用不该永久否掉后续 episode。"""

    arbiter = Arbiter()
    claim = ExplicitUserClaim(
        device_id="light_living_01",
        capability="brightness",
        value=0,
        agent_id="user_ui",
        command_id="cmd-user-0",
        issued_wall_time=10.0,
    )
    result = arbiter.resolve(
        [_proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_living_01", "extra.brightness", 80)])],
        root_event=_event(wall_time=100.0),
        world_snapshot=_world(),
        user_claims={("light_living_01", "brightness"): claim},
    )
    assert len(result.approved_commands) == 1


# ------------------------------- 全序声明 vs 实际裁决（能耗否决是全序之外的约束）


def _energy_veto_result() -> ArbiterResult:
    veto = EnergyVeto(
        device_id="hvac_living_01",
        property="power",
        vetoed_agent_id="hvac_agent",
        vetoed_priority=PriorityLevel.COMFORT,
        rule="unoccupied_home_only",
        reason="全屋无人时不该维持制冷",
    )
    return Arbiter().resolve(
        [_proposal("hvac_agent", PriorityLevel.COMFORT, [_command("hvac_living_01", "power", True)])],
        root_event=_event(),
        world_snapshot=_world(),
        energy_review=EnergyVetoReview(
            home_occupied=False,
            vetoes=(veto,),
            reviewed_agent_ids=("hvac_agent",),
            explanation="规则 unoccupied_home_only：全屋无人，否决 1 条舒适/氛围档命令。",
        ),
    )


def test_energy_veto_is_labelled_a_constraint_not_a_total_order_win():
    """S3 复审 minor：``energy`` 否决 ``comfort`` 是 §9.1 全序的**倒置**。

    行为保留（§8.2 的显式解释），但事件流不得声称"低档赢了全序"：这一条必须打上
    ``constraint_veto`` 标签，并在理由里说清它位于全序之外——否则读 trace 的研究者会
    据此推翻整张排名表。
    """

    result = _energy_veto_result()
    rejected = result.rejected_commands[0]

    assert rejected.conflict_class is ConflictClass.ENERGY_VS_COMFORT
    assert rejected.resolution is ConflictResolution.CONSTRAINT_VETO
    assert result.conflicts[0].resolution is ConflictResolution.CONSTRAINT_VETO
    # 倒置本身必须写在人话里，而不是只能从两个档位名里推断
    assert "全序" in rejected.rejection_reason
    # payload 也带这一位（S4 的评估器按它挑选"可以假设秩单调"的那些拒绝）
    data = result.event_data()
    assert data["rejected_commands"][0]["resolution"] == "constraint_veto"
    assert data["conflicts"][0]["resolution"] == "constraint_veto"


def test_no_rejection_claims_a_lower_tier_won_the_total_order():
    """全序不变式：凡标记为 ``total_order`` 的拒绝，赢家的秩不得低于输家。

    这条不变式是"严格全序"这句话在事件流里的可证伪形式。能耗否决被排除在外，
    正是因为它**不是**按秩做出的——它得自己承认这一点。
    """

    arbiter = Arbiter()
    world = _world()
    root = _event()
    results = [
        _energy_veto_result(),
        arbiter.resolve(
            [
                _proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_living_01", "power", False)]),
                _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("curtain_living_01", "extra.open_percent", 100)]),
            ],
            root_event=root,
            world_snapshot=world,
        ),
        arbiter.resolve(
            [
                _proposal("hvac_agent", PriorityLevel.COMFORT, [_command("light_living_01", "extra.brightness", 80)]),
                _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("light_living_01", "extra.brightness", 20)]),
            ],
            root_event=root,
            world_snapshot=world,
        ),
        arbiter.resolve(
            [_proposal("security_agent", PriorityLevel.SECURITY, [_command("camera_living_01", "power", True)])],
            root_event=root,
            world_snapshot=world,
        ),
    ]

    seen: set[ConflictResolution] = set()
    for result in results:
        for rejected in result.rejected_commands:
            seen.add(rejected.resolution)
            if rejected.resolution is ConflictResolution.TOTAL_ORDER:
                assert rejected.winner_priority is not None
                assert priority_rank(rejected.winner_priority) >= priority_rank(rejected.priority), (
                    f"{rejected.winner_priority.value} 被标成全序赢家，却低于输家 "
                    f"{rejected.priority.value}"
                )
            if rejected.resolution is ConflictResolution.UNILATERAL:
                assert rejected.winner_agent_id is None

    assert seen == {
        ConflictResolution.TOTAL_ORDER,
        ConflictResolution.CONSTRAINT_VETO,
        ConflictResolution.UNILATERAL,
    }


# ------------------------------------------------------------------ 确定性


def test_resolve_is_deterministic_regardless_of_input_order():
    """输入顺序不得改变裁决与输出行序（S2 字节一致性门的前提）。"""

    arbiter = Arbiter()
    world = _world()
    root = _event()
    proposals = [
        _proposal("scene_agent", PriorityLevel.AMBIENCE, [_command("light_living_01", "power", False)]),
        _proposal("lighting_agent", PriorityLevel.COMFORT, [_command("light_living_01", "power", True)]),
        _proposal("energy_agent", PriorityLevel.ENERGY, [_command("hvac_living_01", "power", False)]),
    ]

    forward = arbiter.resolve(proposals, root_event=root, world_snapshot=world).event_data()
    backward = arbiter.resolve(list(reversed(proposals)), root_event=root, world_snapshot=world).event_data()
    assert forward == backward


@pytest.mark.parametrize("tier", list(PRIORITY_LEVELS))
def test_every_tier_can_win_something(tier):
    """七档**每一档**都要有真实的胜出路径，而且这一档必须有人在现实里产出。

    两半断言缺一不可，因为它们证的是两件事：

    * 前半（合成提案）：排名表认得这一档，喂进来就能赢——这是仲裁器自己的性质；
    * 后半（生产者声明）：这一档在运行期真有产出者。**只有前半时这条测试对"死档"
      毫无鉴别力**：S3 复审实测九个库场景，``safety`` 与 ``maintenance`` 的提案数都是 0，
      而这条测试当时照绿。声明表本身由 tests/test_episode_completeness.py 的
      :func:`~tests.test_episode_completeness.test_every_priority_tier_has_a_declared_producer`
      拿整库实跑核对（LIBRARY 档必须跑得出来，RESERVED 档必须一次都不出现），
      所以这里引用它不是引用一句注释，而是引用一条被实跑钉住的声明。
    """

    from tests.test_episode_completeness import PRIORITY_TIER_PRODUCERS

    arbiter = Arbiter()
    result = arbiter.resolve(
        [_proposal("some_agent", tier, [_command("light_living_01", "power", True)])],
        root_event=_event(),
        world_snapshot=_world(),
    )
    assert result.winning_priority is tier

    # 新增档位却没登记生产者 = KeyError：死档不再能靠"合成夹具能赢"蒙混过关。
    producer = PRIORITY_TIER_PRODUCERS[tier]
    assert producer.note.strip(), f"{tier.value} 档没有说明它的运行期生产者是谁"
