"""S2-T3 run 模型：RunMetadata（§11 九字段）+ RunManager 生命周期。

§11 "Every simulation run should have a run_id"——在 S2 之前本仿真根本没有 run 概念：
事件流是一条永不结束的连续带，reset 只是把世界换掉，于是"这批事件属于哪次实验、
用的哪个 seed、哪个 provider、起始世界长什么样"全都无从回答（§18 研究者前两问）。
本模块测的就是这条身份线：run 元数据齐备、seed 可复现、起始世界可指纹、
换 run 时事件总线上下文与历史同步切换。
"""

from __future__ import annotations

import json

import pytest

from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.rng import MAX_JSON_SAFE_SEED, RngStream
from backend.engine.run_manager import (
    SPEC11_REQUIRED_FIELDS,
    LLMMode,
    RunManager,
    RunMetadata,
    compute_initial_state_hash,
    read_sim_version,
    read_source_revision,
    resolve_llm_mode,
)
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    UserState,
    WorldState,
)


def _make_world(brightness: int = 40) -> WorldState:
    world = WorldState(scene_id="apartment_v1")
    world.rooms = {"living_room": RoomState(id="living_room", temperature=24.0)}
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            capabilities=["power", "brightness"],
            state=DeviceStateValues(power=True, extra={"brightness": brightness}),
        )
    }
    world.users = {
        "user_01": UserState(id="user_01", name="User", location=Location3D(room="living_room"))
    }
    return world


class _FakeProvider:
    provider_name = "anthropic_compatible"
    model = "MiniMax-M2.7"
    api_key = "sk-test"


# ---------------------------------------------------------------------------
# §11 元数据
# ---------------------------------------------------------------------------


def test_metadata_contains_all_nine_spec11_fields():
    manager = RunManager()
    metadata = manager.start_run(
        world=_make_world(),
        scenario_id="arrive_home_evening",
        seed=1234,
        llm_provider=_FakeProvider(),
        agent_versions={"lighting_agent": "0.1.0"},
    )

    assert set(SPEC11_REQUIRED_FIELDS) <= set(RunMetadata.model_fields)
    for field in SPEC11_REQUIRED_FIELDS:
        value = getattr(metadata, field)
        assert value is not None, f"§11 必填字段 {field} 为空"
        if isinstance(value, str):
            assert value != "", f"§11 必填字段 {field} 是空串"

    assert metadata.scenario_id == "arrive_home_evening"
    assert metadata.seed == 1234
    assert metadata.llm_provider == "anthropic_compatible"
    assert metadata.llm_model == "MiniMax-M2.7"
    assert metadata.llm_mode is LLMMode.LIVE
    assert metadata.agent_versions == {"lighting_agent": "0.1.0"}
    assert metadata.sim_version == read_sim_version()
    assert metadata.source_revision == read_source_revision()
    assert metadata.ended_at is None


def test_source_revision_changes_when_runtime_source_changes(tmp_path, monkeypatch):
    import backend.engine.run_manager as run_manager_module

    source_root = tmp_path / "backend"
    source_root.mkdir()
    source = source_root / "runtime.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    version = tmp_path / "VERSION"
    version.write_text("1.0.0\n", encoding="utf-8")
    monkeypatch.delenv("AURA_SOURCE_REVISION", raising=False)
    monkeypatch.setattr(run_manager_module, "_BACKEND_SOURCE_ROOT", source_root)
    monkeypatch.setattr(run_manager_module, "_REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(run_manager_module, "_VERSION_FILE", version)

    read_source_revision.cache_clear()
    before = read_source_revision()
    source.write_text("VALUE = 2\n", encoding="utf-8")
    read_source_revision.cache_clear()
    after = read_source_revision()
    read_source_revision.cache_clear()

    assert before.startswith("sha256:")
    assert before != after


def test_metadata_round_trips_through_canonical_json():
    manager = RunManager()
    metadata = manager.start_run(world=_make_world(), seed=7)

    dumped = json.dumps(metadata.model_dump(mode="json"), sort_keys=True)
    restored = RunMetadata.model_validate(json.loads(dumped))
    assert restored == metadata


def test_missing_provider_is_recorded_as_mocked_mode():
    manager = RunManager()
    metadata = manager.start_run(world=_make_world())

    # §11.1：每份 run 工件都必须标注 LLM 模式；没有 provider 就是 rule_based（规则回退路径）。
    assert metadata.llm_mode is LLMMode.RULE_BASED
    assert metadata.llm_provider == "disabled"
    assert resolve_llm_mode(None) is LLMMode.RULE_BASED


# ---------------------------------------------------------------------------
# initial_state_hash
# ---------------------------------------------------------------------------


def test_initial_state_hash_stable_for_same_world_and_differs_on_change():
    first = compute_initial_state_hash(_make_world())
    same = compute_initial_state_hash(_make_world())
    assert first == same
    assert len(first) == 64

    changed = compute_initial_state_hash(_make_world(brightness=41))
    assert changed != first


def test_initial_state_hash_ignores_agent_runtime_diagnostics():
    """agent 的 provider/latency 是运行期诊断，不是起始世界——不得进指纹。

    否则同一个场景换个 provider 就"起始世界不同"，而 llm_provider 本来就是 §11 的独立字段。
    """

    from backend.engine.state import AgentRuntimeState

    baseline = _make_world()
    with_agents = _make_world()
    with_agents.agents = {
        "lighting_agent": AgentRuntimeState(
            id="lighting_agent", name="Lighting", provider="openai_responses", last_latency_ms=42
        )
    }

    assert compute_initial_state_hash(baseline) == compute_initial_state_hash(with_agents)


# ---------------------------------------------------------------------------
# 生命周期 + 事件总线接线
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_run_sets_bus_context_and_clears_previous_run_history():
    bus = EventBus()
    manager = RunManager(event_bus=bus)

    first = manager.start_run(world=_make_world(), scenario_id="scenario_a")
    await bus.publish(SimEvent(event_type="user.command", source="user_ui", timestamp=0.0))
    assert bus.get_history()[0].run_id == first.run_id
    assert bus.next_seq == 1

    second = manager.start_run(world=_make_world(), scenario_id="scenario_b")
    assert bus.run_id == second.run_id
    assert bus.scenario_id == "scenario_b"
    # 换 run 必须清历史与 seq：否则 get_causal_chain / correlation 查询跨 run 穿插。
    assert bus.get_history() == []
    assert bus.next_seq == 0
    assert bus.get_history(run_id=first.run_id) == []


def test_start_run_ends_previous_run_and_ids_are_unique():
    manager = RunManager()
    first = manager.start_run(world=_make_world())
    second = manager.start_run(world=_make_world())

    assert first.run_id != second.run_id
    assert first.ended_at is not None
    assert first.end_reason == "superseded"
    assert manager.current is second
    assert [run.run_id for run in manager.finished] == [first.run_id]


def test_end_run_records_ended_at_and_reason():
    manager = RunManager()
    started = manager.start_run(world=_make_world())
    ended = manager.end_run("completed")

    assert ended is started
    assert ended.ended_at is not None
    assert ended.end_reason == "completed"
    assert manager.current is None
    assert manager.run_id is None
    assert manager.end_run("completed") is None


def test_is_stale_flags_old_run_ids_only():
    manager = RunManager()
    first = manager.start_run(world=_make_world())
    assert manager.is_stale(first.run_id) is False
    # 没带 run_id 的旧调用方（S2 之前的代码路径）永远不算 stale，绝不因此静默丢事。
    assert manager.is_stale(None) is False

    second = manager.start_run(world=_make_world())
    assert manager.is_stale(first.run_id) is True
    assert manager.is_stale(second.run_id) is False

    manager.end_run("completed")
    assert manager.is_stale(second.run_id) is True


# ---------------------------------------------------------------------------
# seed / RNG
# ---------------------------------------------------------------------------


def test_seed_is_recorded_and_run_rng_is_reproducible():
    first = RunManager().start_run(world=_make_world(), seed=99)
    manager = RunManager()
    manager.start_run(world=_make_world(), seed=99)

    assert first.seed == 99
    assert manager.rng is not None
    assert manager.rng.seed == 99

    other = RunManager()
    other.start_run(world=_make_world(), seed=99)
    assert [manager.rng.stream(RngStream.ENV_NOISE).random() for _ in range(3)] == [
        other.rng.stream(RngStream.ENV_NOISE).random() for _ in range(3)
    ]


def test_seed_is_generated_and_recorded_when_absent():
    metadata = RunManager().start_run(world=_make_world())
    assert isinstance(metadata.seed, int)
    assert 0 <= metadata.seed <= MAX_JSON_SAFE_SEED


def test_invalid_seed_is_rejected_at_run_start():
    with pytest.raises((TypeError, ValueError)):
        RunManager().start_run(world=_make_world(), seed="1234")  # type: ignore[arg-type]


def test_invalid_seed_does_not_supersede_the_active_run():
    manager = RunManager()
    active = manager.start_run(world=_make_world(), seed=7)

    with pytest.raises((TypeError, ValueError)):
        manager.start_run(world=_make_world(), seed=2**64)

    assert manager.current is active
    assert active.ended_at is None
    assert manager.finished == []
