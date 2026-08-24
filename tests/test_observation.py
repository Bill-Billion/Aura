"""S2-T5：§2.3 ground truth 与 observable 的分离。

范围（S2 评审裁定的缩减）：本期只做 **身份投影 + 离线设备陈旧快照** 这一条分离与它的
管线。高斯传感器噪声/延迟/丢失推迟到 S4-T3 的漂移注入器——那一期只需要"分离已经存在"
这个前提，不需要 S2 先造一套噪声模型。因此这里没有 noise 测试，只有：

  1. 无噪声模型时 observable ≡ ground truth（回归面压到零的前提）；
  2. observable 是拷贝，agent 拿不到能改世界的引用；
  3. 离线设备只报最后一次在线时的读数（§2.3 "may be stale ... if a device is offline"），
     并且**一直陈旧到设备恢复**；
  4. §5.3 ground truth 标签绝不出现在 agent 的 LLM 载荷里；
  5. 同一起始世界的两次投影完全一致（本期无随机源，等价于"seed 化"的下界）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from unittest.mock import AsyncMock

import pytest

from backend.agents.lighting import LightingAgent
from backend.agents.llm import LLMProvider
from backend.agents.types import AgentLLMDecision, LLMDecisionRequest
from backend.api.ws import ConnectionManager
from backend.config.device_registry import build_default_devices, build_default_rooms
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.observation import (
    OBSERVATION_STALE_KEY,
    OBSERVATION_UNAVAILABLE_KEY,
    ObservableProjector,
    build_observable_view,
    observation_model_metadata,
)
from backend.engine.provenance import ObservationCondition
from backend.engine.run_manager import canonical_json
from backend.engine.simulation import SimulationEngine
from backend.engine.state import Location3D, UserState, WorldState
from backend.engine.state_manager import StateManager
from backend.scenarios.spec import ScenarioSpec


def _make_world() -> WorldState:
    world = WorldState(scene_id="apartment_v1")
    world.environment.time_of_day = "19:00"
    world.rooms = build_default_rooms()
    world.devices = build_default_devices()
    world.users = {
        "user_01": UserState(
            id="user_01",
            name="User",
            location=Location3D(room="living_room"),
            activity="idle",
        )
    }
    world.rooms["living_room"].occupancy = True
    world.rooms["living_room"].persons = ["user_01"]
    return world


# ------------------------------------------------------------------ 身份投影


def test_no_noise_observable_equals_ground_truth():
    world = _make_world()
    # 注册表默认里 camera_loft_01 就是 online=false（§6 的"有一台掉线摄像头"世界）。
    # 恒等断言要的是"没有噪声模型时投影不改任何东西"，所以先把世界拉成全在线，
    # 免得把陈旧规则误判成恒等性被破坏。
    for device in world.devices.values():
        if "online" in device.state.extra:
            device.state.extra["online"] = True

    observable = build_observable_view(world)

    assert observable.model_dump() == world.model_dump()


def test_perfect_condition_is_identity_even_for_an_offline_device():
    world = _make_world()
    camera = world.devices["camera_entry_01"]
    camera.state.extra.update(online=False, secret_physical_value="oracle-only")

    observable = ObservableProjector(ObservationCondition.PERFECT).observe(world)

    assert observable.model_dump() == world.model_dump()
    assert OBSERVATION_STALE_KEY not in observable.devices[camera.id].state.extra


def test_observable_view_is_a_copy_not_the_world():
    world = _make_world()

    observable = build_observable_view(world)
    observable.devices["light_living_01"].state.extra["brightness"] = 1
    observable.rooms["living_room"].temperature = -100.0

    assert world.devices["light_living_01"].state.extra["brightness"] != 1
    assert world.rooms["living_room"].temperature != -100.0


def test_projection_identical_across_two_same_seed_runs():
    """两次独立投影逐字节一致——可复现性的下界断言。

    本期投影不消费任何随机源（噪声推迟到 S4-T3），因此"同 seed 同结果"退化为
    "同世界同结果"；S4 接上 SimStream 之后这条断言应升级为跨 seed 对照。
    """

    world_a = _make_world()
    world_b = _make_world()

    view_a = ObservableProjector().observe(world_a)
    view_b = ObservableProjector().observe(world_b)

    assert json.dumps(view_a.model_dump(mode="json"), sort_keys=True) == json.dumps(
        view_b.model_dump(mode="json"), sort_keys=True
    )


# ---------------------------------------------------------------- 离线 → 陈旧


def test_offline_device_reports_stale_state():
    world = _make_world()
    projector = ObservableProjector()

    # 设备在线时报了一次 brightness=80
    world.devices["light_living_01"].state.extra["brightness"] = 80
    projector.observe(world)

    # ground truth 继续演化，但设备已经掉线：观测侧不该看到掉线之后的变化
    world.devices["light_living_01"].state.extra["online"] = False
    world.devices["light_living_01"].state.extra["brightness"] = 5
    world.devices["light_living_01"].state.power = False

    observable = projector.observe(world)
    reported = observable.devices["light_living_01"].state

    assert reported.extra["brightness"] == 80, "离线设备不该继续汇报新读数"
    assert reported.power is True
    # 可达性本身是可观测的：agent 必须能分辨"读数旧"与"读数新"
    assert reported.extra["online"] is False
    assert reported.extra[OBSERVATION_STALE_KEY] is True
    # camera_loft_01 在注册表默认里就是离线的，故只断言本例的设备在陈旧名单里。
    assert "light_living_01" in projector.stale_device_ids

    # ground truth 未被投影污染
    assert world.devices["light_living_01"].state.extra["brightness"] == 5
    assert OBSERVATION_STALE_KEY not in world.devices["light_living_01"].state.extra


def test_stale_observation_stays_stale_until_the_device_recovers():
    world = _make_world()
    projector = ObservableProjector()
    world.devices["light_living_01"].state.extra["brightness"] = 80
    projector.observe(world)

    world.devices["light_living_01"].state.extra["online"] = False
    for brightness in (10, 20, 30):
        world.devices["light_living_01"].state.extra["brightness"] = brightness
        observable = projector.observe(world)
        assert observable.devices["light_living_01"].state.extra["brightness"] == 80

    # 恢复：立刻回到 ground truth，并且陈旧标记消失
    world.devices["light_living_01"].state.extra["online"] = True
    observable = projector.observe(world)
    assert observable.devices["light_living_01"].state.extra["brightness"] == 30
    assert OBSERVATION_STALE_KEY not in observable.devices["light_living_01"].state.extra
    assert "light_living_01" not in projector.stale_device_ids


def test_device_offline_before_any_report_is_unavailable_without_ground_truth_leak():
    """一上来就离线的设备不能把物理初值冒充成历史报告。"""

    world = _make_world()
    world.devices["camera_entry_01"].state.extra["online"] = False
    world.devices["camera_entry_01"].state.extra["secret_physical_value"] = (
        "oracle-only"
    )

    observable = ObservableProjector().observe(world)
    reported = observable.devices["camera_entry_01"].state

    assert reported.extra[OBSERVATION_STALE_KEY] is True
    assert reported.extra[OBSERVATION_UNAVAILABLE_KEY] is True
    assert "secret_physical_value" not in reported.extra
    assert reported.last_changed_by == "observation_unavailable"


def test_projector_reset_drops_the_previous_world_cache():
    """reset 换世界后不得把上一个 run 的读数继续报出来（跨 run 陈旧泄漏）。"""

    world = _make_world()
    projector = ObservableProjector()
    world.devices["light_living_01"].state.extra["brightness"] = 80
    projector.observe(world)

    projector.reset()

    fresh = _make_world()
    fresh.devices["light_living_01"].state.extra["online"] = False
    fresh.devices["light_living_01"].state.extra["brightness"] = 7
    observable = projector.observe(fresh)

    reported = observable.devices["light_living_01"].state
    assert OBSERVATION_UNAVAILABLE_KEY in reported.extra
    assert "brightness" not in reported.extra


def test_frame_rebuilds_device_state_event_and_commits_exact_observation():
    world = _make_world()
    projector = ObservableProjector()
    light = world.devices["light_living_01"]
    light.state.extra["brightness"] = 80
    projector.observe(world)
    light.state.extra.update(online=False, brightness=5)
    root = SimEvent(
        event_type="environment.state_refresh",
        source="device_report",
        timestamp=2.0,
        sim_time_s=20.0,
        data={
            "device_id": light.id,
            "property": "extra.brightness",
            "value": 5,
            "significant_change_reasons": ["device_report"],
        },
    )

    frame = projector.observe_frame(world, root)

    assert frame.observed_root_event.data["value"] == 80
    assert frame.evidence_preimage()["observed_root_event"]["data"]["value"] == 80
    assert light.id in frame.evidence_preimage()["stale_device_ids"]
    assert frame.frame_hash == hashlib.sha256(
        canonical_json(frame.evidence_preimage()).encode("utf-8")
    ).hexdigest()


def test_perfect_and_online_events_preserve_transition_values():
    world = _make_world()
    light = world.devices["light_living_01"]
    light.state.extra["brightness"] = 80
    root = SimEvent(
        event_type="device.state_changed",
        source="device_report",
        timestamp=2.0,
        data={
            "device_id": light.id,
            "property": "extra.brightness",
            "old_value": 20,
            "new_value": 80,
        },
    )

    perfect = ObservableProjector("perfect").observe_frame(world, root)
    stale_online = ObservableProjector("stale_offline").observe_frame(world, root)

    assert perfect.observed_root_event.data == root.data
    assert stale_online.observed_root_event.data == root.data
    assert perfect.observed_root_event is not root
    assert stale_online.observed_root_event is not root


def test_unavailable_device_value_is_removed_from_root_event_channel():
    world = _make_world()
    camera = world.devices["camera_entry_01"]
    camera.state.extra.update(online=False, secret_physical_value="oracle-only")
    frame = ObservableProjector().observe_frame(
        world,
        SimEvent(
            event_type="device.offline",
            source="device_report",
            timestamp=0,
            data={
                "device_id": camera.id,
                "property": "extra.secret_physical_value",
                "value": "oracle-only",
                "online": False,
            },
        ),
    )

    assert "value" not in frame.observed_root_event.data
    assert "oracle-only" not in canonical_json(frame.evidence_preimage())
    assert camera.id in frame.unavailable_device_ids


def test_observation_model_hash_binds_condition_and_contract():
    perfect = observation_model_metadata("perfect")
    stale = observation_model_metadata("stale_offline")

    assert perfect["model_id"] == "current_projector_v1"
    assert stale["model_id"] == "current_projector_v1"
    assert perfect["model_hash"] != stale["model_hash"]


# --------------------------------------------------- ground truth 标签不可见


def _scenario_with_ground_truth() -> ScenarioSpec:
    return ScenarioSpec.model_validate(
        {
            "id": "gt_leak_probe",
            "name": "ground truth leak probe",
            "description": "§2.3 标签不得进入 agent 载荷",
            "seed": 11,
            "initial_state": {},
            "timeline": [],
            "expected_device_effects": [
                {"device_id": "light_living_01", "expected": {"power": True}}
            ],
            "involved_agents": ["lighting_agent"],
            "success_criteria": {},
            "ground_truth": {
                "user_goal": "SECRET_GOAL_ARRIVAL_COMFORT",
                "expected_intent": "SECRET_INTENT_LIGHT_ON",
                "forbidden_device_ids": ["camera_bedroom_02"],
                "primary_room_ids": ["living_room"],
                "safety_constraints": ["SECRET_CONSTRAINT_NO_BEDROOM_CAMERA"],
            },
        }
    )


def test_ground_truth_labels_absent_from_agent_llm_payload():
    spec = _scenario_with_ground_truth()
    world = _make_world()
    observable = build_observable_view(world)

    agent = LightingAgent()
    root_event = SimEvent(
        event_type="user.activity_change",
        source="user_behavior_sim",
        timestamp=0.0,
        wall_time=time.time(),
        data={"user_id": "user_01", "activity": "reading"},
    )
    payload = json.dumps(
        {
            "world": observable.model_dump(mode="json"),
            "devices": [
                agent.serialize_device_for_llm(device, observable)
                for device in agent.get_relevant_devices(observable, root_event)
            ],
            "summary": agent.build_world_summary(observable, root_event),
            "allowed": agent.get_allowed_command_specs(observable, root_event),
        },
        ensure_ascii=False,
    )

    assert spec.ground_truth is not None
    for secret in (
        spec.ground_truth.user_goal,
        spec.ground_truth.expected_intent,
        *spec.ground_truth.safety_constraints,
    ):
        assert secret not in payload

    # forbidden 设备本身仍然可见（它在世界里存在），但"它被禁止"这条标签不可见
    assert "forbidden_device_ids" not in payload
    assert "ground_truth" not in payload
    # WorldState 结构上就没有 ground truth 的落点——这是分离的最强形式
    assert "ground_truth" not in WorldState.model_fields


# ------------------------------------------------------------- runtime 管线


class _CapturingProvider(LLMProvider):
    provider_name = "capturing"
    model = "stub"

    def __init__(self) -> None:
        self.requests: list[LLMDecisionRequest] = []

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.requests.append(request)
        return AgentLLMDecision(
            intent="noop",
            confidence=0.5,
            task_steps=["observe"],
            proposed_commands=[],
            explanation="observation probe",
            needs_coordination=False,
        )


def _make_engine(provider: LLMProvider) -> SimulationEngine:
    conn = ConnectionManager()
    conn.broadcast = AsyncMock()
    conn.send = AsyncMock()
    return SimulationEngine(
        event_bus=EventBus(),
        state_manager=StateManager(_make_world()),
        connection_manager=conn,
        llm_provider=provider,
    )


async def _drain_episodes(engine: SimulationEngine) -> None:
    for _ in range(300):
        pending = [t for t in engine.agent_runtime._background_tasks if not t.done()]
        if not pending:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("agent episode did not finish in time")


@pytest.mark.anyio
async def test_agent_episode_reads_the_observable_view_not_ground_truth():
    provider = _CapturingProvider()
    engine = _make_engine(provider)
    runtime = engine.agent_runtime
    world = engine.state_manager.world

    # 先让投影记下一次在线读数
    world.devices["light_living_01"].state.extra["brightness"] = 80
    runtime.observable_world()

    # 设备掉线后 ground truth 继续变化
    world.devices["light_living_01"].state.extra["online"] = False
    world.devices["light_living_01"].state.extra["brightness"] = 5

    await engine._publish_sim_event(
        SimEvent(
            event_type="user.activity_change",
            source="user_behavior_sim",
            timestamp=0.0,
            wall_time=time.time(),
            priority=2,
            data={
                "user_id": "user_01",
                "from_room": "bedroom",
                "to_room": "living_room",
                "activity": "reading",
            },
        )
    )
    await _drain_episodes(engine)

    lighting_requests = [r for r in provider.requests if r.agent_id == "lighting_agent"]
    assert lighting_requests, "LightingAgent 应当被触发"
    devices = {
        entry["device_id"]: entry for entry in lighting_requests[0].available_devices
    }
    reported = devices["light_living_01"]["state"]["extra"]
    assert reported["brightness"] == 80, "agent 读到的必须是投影，不是 ground truth"
    assert reported["online"] is False
    assert reported[OBSERVATION_STALE_KEY] is True
    await engine.close()


@pytest.mark.anyio
async def test_runtime_projects_root_event_and_persists_frame_before_planning():
    provider = _CapturingProvider()
    engine = _make_engine(provider)
    runtime = engine.agent_runtime
    original_compute = runtime._compute_episode
    event_types_at_compute: list[list[str]] = []

    async def tracked_compute(*args: object, **kwargs: object):
        event_types_at_compute.append(
            [event.event_type for event in engine.event_bus.get_history()]
        )
        return await original_compute(*args, **kwargs)  # type: ignore[arg-type]

    runtime._compute_episode = tracked_compute  # type: ignore[method-assign]
    light = engine.state_manager.world.devices["light_living_01"]
    light.state.extra["brightness"] = 80
    runtime.observable_world()
    light.state.extra.update(online=False, brightness=5)
    root = SimEvent(
        event_type="device.offline",
        source="device_report",
        timestamp=1.0,
        sim_time_s=10.0,
        data={
            "device_id": light.id,
            "property": "extra.brightness",
            "value": 5,
            "online": False,
        },
    )

    await engine._publish_sim_event(root)
    await _drain_episodes(engine)

    remembered = runtime.memory_store.get_correlation_history(root.correlation_id)
    observed_root = next(item for item in remembered if item.event_id == root.event_id)
    assert observed_root.data["value"] == 80
    assert root.data["value"] == 5
    history = [
        item
        for item in engine.event_bus.get_history()
        if item.correlation_id == root.correlation_id
    ]
    capture = next(
        item for item in history if item.event_type == "observation.frame_captured"
    )
    perception = next(
        item for item in history if item.event_type == "reasoning.perception_snapshot"
    )
    assert root.seq is not None and capture.seq is not None
    assert perception.seq is not None
    assert root.seq < capture.seq < perception.seq
    assert event_types_at_compute
    assert all(
        "observation.frame_captured" in event_types
        for event_types in event_types_at_compute
    )
    assert (
        capture.data["observation_frame_hash"]
        == perception.data["observation_frame_hash"]
    )
    await engine.close()


@pytest.mark.anyio
async def test_runtime_observation_cache_is_dropped_when_the_world_is_swapped():
    provider = _CapturingProvider()
    engine = _make_engine(provider)
    runtime = engine.agent_runtime
    engine.state_manager.world.devices["light_living_01"].state.extra["brightness"] = 80
    runtime.observable_world()

    new_manager = StateManager(_make_world())
    new_manager.world.devices["light_living_01"].state.extra["online"] = False
    new_manager.world.devices["light_living_01"].state.extra["brightness"] = 7
    runtime.update_state_manager(new_manager)

    observable = runtime.observable_world()
    reported = observable.devices["light_living_01"].state
    assert "brightness" not in reported.extra
    assert reported.extra[OBSERVATION_UNAVAILABLE_KEY] is True
    await engine.close()


@pytest.mark.anyio
async def test_engine_reset_drops_observation_history():
    """CMD_SIM_RESET 那条路径：换世界 = 换 run，观测历史不得跨 run 存活。"""

    engine = _make_engine(_CapturingProvider())
    engine.state_manager.world.devices["light_living_01"].state.extra["brightness"] = 80
    engine.agent_runtime.observable_world()
    assert engine.agent_runtime.observation_projector.known_device_ids()

    new_manager = StateManager(_make_world())
    new_manager.world.devices["light_living_01"].state.extra["online"] = False
    new_manager.world.devices["light_living_01"].state.extra["brightness"] = 3
    await engine.reset(new_state_manager=new_manager)

    observable = engine.agent_runtime.observable_world()
    reported = observable.devices["light_living_01"].state
    assert "brightness" not in reported.extra
    assert reported.extra[OBSERVATION_UNAVAILABLE_KEY] is True
    await engine.close()
