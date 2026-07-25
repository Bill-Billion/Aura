"""S3-T4：SceneAgent —— 场景是**数据**，展开成**显式设备提案**，不做隐藏写入。

spec 里那句话（§8 附近的实现约束）是这份测试的全部动机：

    场景不得直接改世界；它必须展开成显式的设备命令，与其他 agent 一样走仲裁与执行。

对应到当前代码，被推翻的现状是：前端 ``SceneSelector.vue`` 直接在浏览器里循环发 2×N 条
``CMD_DEVICE_CONTROL``——场景语义只存在于 .vue 的 switch 里，后端既看不见"这是一次场景
切换"，可观测性面板也拼不出因果链。S3 把它下推成 ``backend/config/scene_definitions.yaml``
+ SceneAgent。

五条断言：
1. 下推那天从 .vue 抄下来的取值**一个不落**留在 YAML 里（demo 平价，plan risk 原文点名的那条）；
2. .vue 已经换成一条 ``CMD_SCENE_APPLY``，不再循环发直控（S3 review 的「前门从未打开」）；
3. 场景展开出的是显式 ``AgentCommandProposal``；
4. 一条带 ``scene_id`` 的 ``user.command`` 根事件经 runtime 只开**一条** episode；
5. SceneAgent 不碰 StateManager（写世界只有 CommandExecutor 一条路，S1 的结构钉）。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from backend.agents import scene as scene_module
from backend.agents.contracts import PriorityLevel, ProposalOutcome
from backend.agents.orchestrator import DEFAULT_ORCHESTRATOR_ID
from backend.agents.scene import (
    SCENE_AGENT_ID,
    SCENE_APPLY_MESSAGE_TYPE,
    SCENE_DEFINITIONS_PATH,
    SceneAgent,
    load_scene_definitions,
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

SCENE_SELECTOR_VUE = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "components"
    / "dashboard"
    / "SceneSelector.vue"
)


# --------------------------------------------------------------------- 夹具


def _world() -> WorldState:
    world = WorldState(environment=EnvironmentState(time_of_day="22:00"))
    world.rooms = {"living_room": RoomState(id="living_room", occupancy=True)}
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(power=True, extra={"brightness": 80, "color_temp": 4000}),
        ),
        "curtain_living_01": DeviceState(
            id="curtain_living_01",
            type="curtain",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(power=True, extra={"open_percent": 70}),
        ),
        "hvac_living_01": DeviceState(
            id="hvac_living_01",
            type="hvac",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(
                power=False, extra={"target_temp": 26.0, "mode": "cool", "speed": "low"}
            ),
        ),
    }
    return world


def _scene_event(scene_id: str | None = "sleep", *, message_type: str = "CMD_SCENE_APPLY") -> SimEvent:
    data: dict = {"message_type": message_type}
    if scene_id is not None:
        data["scene_id"] = scene_id
    return SimEvent(event_type="user.command", source="ui", timestamp=1.0, data=data)


# ------------------------------------------------------------- 场景定义数据


def test_scene_definitions_load_and_declare_schema_version():
    library = load_scene_definitions()
    assert library.schema_version == 1
    assert SCENE_DEFINITIONS_PATH.exists()
    # .vue 现有的四个预设一个都不能少，否则演示行为直接退化。
    assert {"reading", "entertainment", "away", "sleep"} <= set(library.scenes)


def test_scene_definitions_keep_the_original_vue_values():
    """demo 平价：下推前 .vue 发的那些取值，一个不落地留在 YAML 里（plan risk 点名的那条）。

    这条钉子原先是"YAML 的取值逐个出现在 .vue 里"。前门打开（CMD_SCENE_APPLY）之后
    .vue 里已经**不该**再有任何设备取值——场景语义整体归后端——所以平价的参照物换成了
    下推那天抄下来的这张表；"浏览器里不许再出现取值"由
    frontend/tests/scenePresets.test.ts 从另一侧钉住。
    """

    library = load_scene_definitions()

    expectations = {
        "reading": [("light", "extra.brightness", 68), ("light", "extra.color_temp", 3400),
                    ("curtain", "extra.open_percent", 52)],
        "entertainment": [("light", "extra.brightness", 42), ("light", "extra.color_temp", 2850),
                          ("curtain", "extra.open_percent", 28), ("hvac", "extra.target_temp", 24)],
        "away": [("light", "power", False), ("hvac", "power", False),
                 ("curtain", "extra.open_percent", 0)],
        "sleep": [("light", "power", False), ("curtain", "extra.open_percent", 0),
                  ("hvac", "extra.target_temp", 22), ("hvac", "extra.mode", "cool")],
    }
    for scene_id, expected in expectations.items():
        steps = {
            (step.selector.device_type, step.property, step.value)
            for step in library.scenes[scene_id].steps
        }
        for item in expected:
            assert item in steps, f"{scene_id} 少了 {item}"


def test_scene_selector_vue_sends_one_scene_command_not_n_device_commands():
    """前门：面板点一下发一条 CMD_SCENE_APPLY，不再在浏览器里循环发 2×N 条直控。

    这是 S3 review 那条 minor（"scene control is backend-only; the front door was never
    opened"）的后端侧钉子：只要 .vue 里再出现 CMD_DEVICE_CONTROL，场景就又变回 N 条互不
    相干的直控，后端拼不出一条完整因果链——SceneAgent 与场景表也就再次无人可达。
    """

    vue_source = SCENE_SELECTOR_VUE.read_text(encoding="utf-8")
    assert "CMD_DEVICE_CONTROL" not in vue_source
    assert SCENE_APPLY_MESSAGE_TYPE in vue_source or "SCENE_APPLY_COMMAND" in vue_source
    assert vue_source.count("sendCommand(") == 1


def test_scene_definitions_yaml_is_pure_data():
    raw = yaml.safe_load(SCENE_DEFINITIONS_PATH.read_text(encoding="utf-8"))
    assert set(raw) == {"schema_version", "scenes"}


def test_unknown_scene_selector_key_fails_loudly(tmp_path):
    bad = tmp_path / "bad_scenes.yaml"
    bad.write_text(
        "schema_version: 1\n"
        "scenes:\n"
        "  ghost:\n"
        "    label: 幽灵\n"
        "    steps:\n"
        "      - selector: {device_type: teleporter}\n"
        "        property: power\n"
        "        value: true\n",
        encoding="utf-8",
    )
    with pytest.raises(scene_module.SceneDefinitionError):
        load_scene_definitions(bad)


def test_read_only_capability_in_scene_fails_loudly(tmp_path):
    """§3.2 只读能力（camera.online）不得出现在场景步骤里——加载期就要拦住。"""

    bad = tmp_path / "readonly_scene.yaml"
    bad.write_text(
        "schema_version: 1\n"
        "scenes:\n"
        "  arm:\n"
        "    label: 布防\n"
        "    steps:\n"
        "      - selector: {device_type: camera}\n"
        "        property: extra.online\n"
        "        value: true\n",
        encoding="utf-8",
    )
    with pytest.raises(scene_module.SceneDefinitionError):
        load_scene_definitions(bad)


# ----------------------------------------------------------------- 场景展开


def test_sleep_scene_yields_explicit_proposals_matching_scene_definitions_yaml():
    agent = SceneAgent()
    world = _world()
    proposal = agent.propose(world_state=world, root_event=_scene_event("sleep"))

    assert proposal.agent_id == SCENE_AGENT_ID
    assert proposal.agent_role == "scene"
    assert proposal.priority is PriorityLevel.AMBIENCE
    assert proposal.outcome is ProposalOutcome.ACTED

    emitted = [(c.device_id, c.property, c.value) for c in proposal.commands]
    library = load_scene_definitions()
    expected_pairs = {
        (step.selector.device_type, step.property, step.value)
        for step in library.scenes["sleep"].steps
    }
    # 展开出来的每一条都能对回 YAML 的一行（没有凭空多出来的隐藏动作）。
    for device_id, prop, value in emitted:
        assert (world.devices[device_id].type, prop, value) in expected_pairs
    assert ("light_living_01", "power", False) in emitted
    assert ("curtain_living_01", "extra.open_percent", 0) in emitted
    assert ("hvac_living_01", "extra.target_temp", 22) in emitted


def test_scene_expansion_is_deterministic():
    agent = SceneAgent()
    first = agent.propose(world_state=_world(), root_event=_scene_event("away"))
    second = agent.propose(world_state=_world(), root_event=_scene_event("away"))
    assert [c.model_dump() for c in first.commands] == [c.model_dump() for c in second.commands]


def test_unknown_scene_id_says_so_explicitly():
    agent = SceneAgent()
    proposal = agent.propose(world_state=_world(), root_event=_scene_event("teleport"))
    assert proposal.outcome is ProposalOutcome.NO_ACTION_NEEDED
    assert "teleport" in (proposal.noop_reason or "")
    assert proposal.commands == []


def test_missing_scene_id_reports_missing_observations():
    """§8.4 第三种表达：观测不足要点名缺了什么，而不是静默返回空。"""

    agent = SceneAgent()
    proposal = agent.propose(world_state=_world(), root_event=_scene_event(None))
    assert proposal.outcome is ProposalOutcome.MISSING_OBSERVATIONS
    assert "scene_id" in proposal.missing_observations
    assert proposal.noop_reason


def test_scene_agent_ignores_plain_device_control_commands():
    agent = SceneAgent()
    world = _world()
    event = SimEvent(
        event_type="user.command",
        source="ui",
        timestamp=1.0,
        data={"message_type": "CMD_DEVICE_CONTROL", "device_id": "light_living_01"},
    )
    assert agent.is_relevant(world, event) is False


def test_scene_agent_never_calls_state_manager_directly():
    """写世界只有 CommandExecutor 一条路（S1 结构钉）——场景模块不得出现旁路写入。"""

    source = inspect.getsource(scene_module)
    for forbidden in ("state_manager", "apply_action", "set_state("):
        assert forbidden not in source, f"scene.py 出现了旁路写入痕迹：{forbidden}"


# ------------------------------------------------------------- runtime 集成


@pytest.mark.anyio
async def test_cmd_scene_apply_root_event_reaches_scene_agent_and_produces_one_episode():
    """一条 CMD_SCENE_APPLY 根事件 → 恰好一条 episode，且命令来自 SceneAgent。"""

    import asyncio
    from unittest.mock import AsyncMock

    from backend.api.ws import ConnectionManager
    from backend.engine.event_bus import EventBus
    from backend.engine.simulation import SimulationEngine
    from backend.engine.state_manager import StateManager

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(_world()),
        connection_manager=ConnectionManager(),
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    engine.agent_runtime.agents.clear()
    engine.agent_runtime.register(SceneAgent())

    root = SimEvent(
        event_id="root-scene",
        event_type="user.command",
        source="ui",
        timestamp=5.0,
        wall_time=5.0,
        correlation_id="corr-scene",
        data={"message_type": "CMD_SCENE_APPLY", "scene_id": "away"},
    )
    await engine._publish_sim_event(root)
    for _ in range(300):
        if not [t for t in engine.agent_runtime._background_tasks if not t.done()]:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover
        raise AssertionError("scene episode did not finish in time")

    history = [event.model_dump() for event in engine.event_bus.get_history()]
    # 编排层的 task_decomposition（source=home_orchestrator）每 episode 恰好一条；
    # 域 agent 自己也有一条同名事件（§4.3 六族推理事件），按 source 区分——
    # S3-T3 的 public_api 明确说了"用 source / agent_id 的存在与否隔离域 agent 推理"。
    decompositions = [
        event
        for event in history
        if event["event_type"] == "reasoning.task_decomposition"
        and event["source"] == DEFAULT_ORCHESTRATOR_ID
    ]
    assert len(decompositions) == 1, "一条根事件只能开一条 episode"
    assert SCENE_AGENT_ID in decompositions[0]["data"]["agent_ids"]

    # 场景真的落到了设备上，而且是经 CommandExecutor 的生命周期（不是隐藏写入）。
    lifecycles = [event for event in history if event["event_type"] == "command.lifecycle"]
    assert lifecycles, "场景命令必须走 CommandExecutor 的十态生命周期"
    assert engine.state_manager.world.devices["light_living_01"].state.power is False
