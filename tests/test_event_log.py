"""每 run 一份 JSONL 事件工件（S2-T7，§11 run 模型 / §18 Q1）。

S2 之前事件只活在 EventBus 的 1000 条环形历史里：进程一停就没了，超过 1000 条开头就
被挤掉，而且没有任何 REST/WS 端点能把它取出来。研究者拿到的"实验记录"实际上是一段
无法引用、无法回放、无法归属的内存缓冲。本模块把它落成磁盘工件，因此这里钉死四件事：

  1. **恰好一次、按 seq 顺序**——事件总线的 wildcard 派发在精确类型派发之后，所以
     "tick 事件的孩子先落盘、tick 自己后落盘" 是默认行为；工件必须重排回 seq 序，
     否则 S4 的回放与 §18 的因果链查询读到的是一条乱序的历史；
  2. **两个 run 的工件互不污染**——审计发现③（reset 从不清历史）的磁盘版；
  3. **run.json 带齐 §11 九字段 + §11.1 llm_mode**，且 run 结束后补上 ended_at；
  4. 关工件（AURA_RUN_ARTIFACTS=0）时一个字节都不写——headless 批跑不该被磁盘绑架。
"""

from __future__ import annotations

import hashlib
import json

import pytest

import backend.engine.event_log as event_log_module
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.event_log import (
    EVENTS_FILENAME,
    RUN_METADATA_FILENAME,
    RunArtifactError,
    RunArtifactErrorCode,
    list_run_ids,
    read_run_events,
    read_run_metadata,
    run_dir,
    verify_finalized_event_log,
)
from backend.engine.event_types import TIMER_TICK_EVENT_TYPE
from backend.engine.run_manager import SPEC11_REQUIRED_FIELDS, new_run_id
from backend.engine.simulation import SimulationEngine
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.scenarios.spec import ScenarioSpec
from backend.scenarios.trace import export_canonical_trace

pytestmark = pytest.mark.anyio


def _make_world() -> WorldState:
    world = WorldState(scene_id="test")
    world.rooms = {
        "living_room": RoomState(
            id="living_room", temperature=24.0, occupancy=True, persons=["user_01"]
        ),
    }
    world.devices = {
        "light_living_01": DeviceState(
            id="light_living_01",
            type="light",
            location=Location3D(room="living_room"),
            state=DeviceStateValues(power=False, extra={"brightness": 0}),
        ),
    }
    return world


def _make_engine(bus: EventBus | None = None) -> SimulationEngine:
    return SimulationEngine(bus or EventBus(), StateManager(_make_world()), ConnectionManager())


def _make_scenario(scenario_id: str) -> ScenarioSpec:
    """一份最小但**真实**的 ScenarioSpec，其 initial_state 摆得进上面那个迷你世界。

    S2 review major-2 之后 ``reset`` 不再接受裸字符串 scenario_id：工件上的场景标签
    只能来自一个真被校验过、且其 initial_state 真被应用的场景。
    """

    return ScenarioSpec.model_validate(
        {
            "id": scenario_id,
            "name": scenario_id,
            "description": "run 工件测试用的最小场景",
            "seed": 1234,
            "initial_state": {
                "time_of_day": "18:30",
                "rooms": {"living_room": {"temperature": 22.0}},
            },
            "timeline": [],
            "expected_device_effects": [],
            "involved_agents": [],
            "success_criteria": {},
        }
    )


def _read_lines(run_id: str) -> list[dict]:
    path = run_dir(run_id) / EVENTS_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --------------------------------------------------------------- 1. 恰好一次 + seq 序


async def test_every_published_event_appears_exactly_once_in_seq_order():
    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None

    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.timer.tick_once()
    await engine.agent_runtime.wait_for_idle(timeout=5.0)
    await engine.pause()
    await engine.close()

    lines = _read_lines(run_id)
    assert lines, "run 跑了两拍却没有落下任何事件"

    seqs = [line["seq"] for line in lines]
    assert seqs == sorted(seqs), "工件不是 seq 序——wildcard 派发顺序泄漏进了磁盘"
    assert len(set(seqs)) == len(seqs), "同一条 seq 被写了两次"

    event_ids = [line["event_id"] for line in lines]
    assert len(set(event_ids)) == len(event_ids)

    # 总线历史是同一条流的内存镜像：两者的事件集合必须完全一致（不多不少）。
    history_ids = [event.event_id for event in engine.event_bus.get_history()]
    assert event_ids == history_ids

    assert all(line["run_id"] == run_id for line in lines)


async def test_root_event_is_written_before_the_children_it_caused():
    """tick 的孩子在 wildcard 派发到 writer 之前就发布了——工件仍须父在子前。"""

    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None

    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.agent_runtime.wait_for_idle(timeout=5.0)
    await engine.close()

    lines = _read_lines(run_id)
    index_by_id = {line["event_id"]: position for position, line in enumerate(lines)}
    ticks = [line for line in lines if line["event_type"] == TIMER_TICK_EVENT_TYPE]
    assert ticks, "没有 tick 事件落盘"

    children = [
        line
        for line in lines
        if line.get("causal_parent") in index_by_id and line["causal_parent"] != line["event_id"]
    ]
    assert children, "这一拍没有产生任何带 causal_parent 的派生事件"
    for child in children:
        assert index_by_id[child["causal_parent"]] < index_by_id[child["event_id"]]


# ------------------------------------------------------------------- 2. run 间隔离


async def test_two_runs_produce_isolated_artifacts_no_cross_contamination():
    engine = _make_engine()
    first_run = engine.run_id
    assert first_run is not None

    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.agent_runtime.wait_for_idle(timeout=5.0)
    await engine.pause()

    await engine.reset(scenario=_make_scenario("scenario_a"), seed=1234)
    second_run = engine.run_id
    assert second_run is not None and second_run != first_run

    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.agent_runtime.wait_for_idle(timeout=5.0)
    await engine.close()

    first_lines = _read_lines(first_run)
    second_lines = _read_lines(second_run)
    assert first_lines and second_lines

    first_ids = {line["event_id"] for line in first_lines}
    second_ids = {line["event_id"] for line in second_lines}
    assert first_ids.isdisjoint(second_ids), "两个 run 的事件混进了同一份工件"
    assert all(line["run_id"] == first_run for line in first_lines)
    assert all(line["run_id"] == second_run for line in second_lines)
    assert all(line["scenario_id"] == "scenario_a" for line in second_lines)

    # seq 是 run 内序号：换 run 之后必须重新从 0 开始，否则两份工件无法各自对账。
    assert second_lines[0]["seq"] == 0

    first_meta = read_run_metadata(first_run)
    second_meta = read_run_metadata(second_run)
    assert first_meta["scenario_id"] is None
    assert second_meta["scenario_id"] == "scenario_a"
    assert second_meta["seed"] == 1234
    # 上一 run 被 start_run 顶掉时必须已经收尾——否则工件永远停在"还在跑"。
    assert first_meta["ended_at"] is not None
    assert first_meta["end_reason"] == "superseded"
    assert second_meta["end_reason"] == "closed"


async def test_recorder_rejects_cross_run_event_and_marks_artifact_invalid():
    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None
    foreign_run_id = new_run_id()

    engine.run_artifacts.record(
        SimEvent(
            event_type="test.cross_run",
            source="test",
            timestamp=0.0,
            run_id=foreign_run_id,
            seq=0,
        )
    )
    await engine.close()

    metadata = read_run_metadata(run_id)
    assert foreign_run_id in metadata["artifact_error"]
    assert not any(
        event.get("run_id") == foreign_run_id for event in _read_lines(run_id)
    )


async def test_midstream_event_write_failure_survives_recovery_and_finalize():
    """A transient disk failure must not later finalize as a valid short prefix."""

    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None
    writer = engine.run_artifacts.writer
    assert writer is not None
    real_handle = writer._handle

    class FailOnceHandle:
        failed = False

        def write(self, value):
            if not self.failed:
                self.failed = True
                raise OSError("injected event write failure")
            return real_handle.write(value)

        def flush(self):
            return real_handle.flush()

        def fileno(self):
            return real_handle.fileno()

        def close(self):
            return real_handle.close()

    writer._handle = FailOnceHandle()  # type: ignore[assignment]
    engine.run_artifacts.record(
        SimEvent(
            event_type="test.write_failure",
            source="test",
            timestamp=0.0,
            run_id=run_id,
            seq=0,
        )
    )
    await engine.close()

    metadata = read_run_metadata(run_id)
    assert metadata["ended_at"] is not None
    assert metadata["artifact_error"] == "events: injected event write failure"
    assert _read_lines(run_id) == []


# --------------------------------------------------------------- 3. run.json §11 字段


async def test_run_json_carries_all_spec11_fields():
    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None
    await engine.close()

    metadata = read_run_metadata(run_id)
    missing = [field for field in SPEC11_REQUIRED_FIELDS if field not in metadata]
    assert missing == [], f"run.json 缺 §11 字段: {missing}"
    # §11.1：每份 run 工件都必须标注用的是哪种 LLM 决定性模式。
    assert metadata["llm_mode"] in {"mocked", "recorded", "live", "rule_based"}
    assert len(metadata["initial_state_hash"]) == 64
    assert metadata["events_integrity"] == {
        "event_count": 0,
        "final_seq": -1,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    assert (run_dir(run_id) / RUN_METADATA_FILENAME).exists()


async def test_finalize_closes_events_before_persisting_integrity_metadata(monkeypatch):
    """The seal must describe durable bytes, not a still-buffered file prefix."""

    bus = EventBus()
    engine = _make_engine(bus)
    run_id = engine.run_id
    writer = engine.run_artifacts.writer
    assert run_id is not None and writer is not None

    operations: list[str] = []
    original_write_metadata = writer.write_metadata
    original_fsync = event_log_module.os.fsync
    events_fd = writer._handle.fileno()

    def track_fsync(fd: int) -> None:
        if not writer._handle.closed and fd == events_fd:
            operations.append("events_fsync")
        return original_fsync(fd)

    def write_metadata_after_close() -> None:
        if writer.metadata.ended_at is not None:
            operations.append(
                "metadata_after_close"
                if writer._handle.closed
                else "metadata_before_close"
            )
        original_write_metadata()

    monkeypatch.setattr(event_log_module.os, "fsync", track_fsync)
    writer.write_metadata = write_metadata_after_close  # type: ignore[method-assign]
    await bus.publish(
        SimEvent(event_type="test.integrity", source="test", timestamp=0.0)
    )
    await engine.close()

    events_path = run_dir(run_id) / EVENTS_FILENAME
    events = _read_lines(run_id)
    metadata = read_run_metadata(run_id)
    integrity = metadata["events_integrity"]

    assert operations[:2] == ["events_fsync", "metadata_after_close"]
    assert integrity == {
        "event_count": len(events),
        "final_seq": events[-1]["seq"],
        "sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
    }
    assert verify_finalized_event_log(run_id) == integrity


async def test_complete_event_suffix_truncation_is_detected():
    """Removing whole JSONL records must not look like a valid shorter trace."""

    bus = EventBus()
    engine = _make_engine(bus)
    run_id = engine.run_id
    assert run_id is not None
    await bus.publish(SimEvent(event_type="test.first", source="test", timestamp=0.0))
    await bus.publish(SimEvent(event_type="test.last", source="test", timestamp=1.0))
    await engine.close()

    events_path = run_dir(run_id) / EVENTS_FILENAME
    lines = events_path.read_bytes().splitlines(keepends=True)
    assert len(lines) >= 2
    events_path.write_bytes(b"".join(lines[:-1]))

    with pytest.raises(RunArtifactError) as excinfo:
        verify_finalized_event_log(run_id)
    assert excinfo.value.code is RunArtifactErrorCode.corrupt_event_log
    assert "event_count" in excinfo.value.details["mismatches"]

    with pytest.raises(RunArtifactError) as export_excinfo:
        export_canonical_trace(run_id)
    assert export_excinfo.value.code is RunArtifactErrorCode.corrupt_event_log


@pytest.mark.parametrize("missing_field", [None, "event_count", "final_seq", "sha256"])
async def test_finalized_artifact_with_missing_event_integrity_is_unsupported(
    missing_field: str | None,
):
    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None
    await engine.close()

    metadata_path = run_dir(run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if missing_field is None:
        metadata.pop("events_integrity")
    else:
        metadata["events_integrity"].pop(missing_field)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(RunArtifactError) as excinfo:
        verify_finalized_event_log(run_id)
    assert excinfo.value.code is RunArtifactErrorCode.unsupported_run_artifact
    assert excinfo.value.details["reason"] in {
        "events_integrity_missing",
        "events_integrity_incomplete",
    }
    if missing_field is not None:
        assert missing_field in excinfo.value.details["missing"]


@pytest.mark.parametrize("field_name", ["event_count", "final_seq", "sha256"])
async def test_event_integrity_mismatch_is_rejected(field_name: str):
    bus = EventBus()
    engine = _make_engine(bus)
    run_id = engine.run_id
    assert run_id is not None
    await bus.publish(
        SimEvent(event_type="test.integrity", source="test", timestamp=0.0)
    )
    await engine.close()

    metadata_path = run_dir(run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if field_name == "sha256":
        metadata["events_integrity"][field_name] = "0" * 64
    else:
        metadata["events_integrity"][field_name] += 1
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(RunArtifactError) as excinfo:
        verify_finalized_event_log(run_id)
    assert excinfo.value.code is RunArtifactErrorCode.corrupt_event_log
    assert field_name in excinfo.value.details["mismatches"]


# ------------------------------------------------------------------ 4. 读侧过滤


async def test_read_run_events_filters_by_correlation_and_type():
    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None

    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.agent_runtime.wait_for_idle(timeout=5.0)
    await engine.close()

    all_events, total = read_run_events(run_id)
    assert total == len(all_events) == len(_read_lines(run_id))

    correlation_id = next(
        line["correlation_id"]
        for line in all_events
        if line["event_type"] == TIMER_TICK_EVENT_TYPE
    )
    chain, chain_total = read_run_events(run_id, correlation_id=correlation_id)
    assert chain_total == len(chain) >= 2
    assert {line["correlation_id"] for line in chain} == {correlation_id}

    ticks, _ = read_run_events(run_id, event_type=TIMER_TICK_EVENT_TYPE)
    assert ticks and all(line["event_type"] == TIMER_TICK_EVENT_TYPE for line in ticks)

    system_events, _ = read_run_events(run_id, generation_mode="system")
    assert system_events and all(
        line["event_generation_mode"] == "system" for line in system_events
    )

    page, page_total = read_run_events(run_id, limit=2, offset=1)
    assert page_total == total
    assert page == all_events[1:3]


async def test_unknown_run_id_raises_structured_error():
    with pytest.raises(RunArtifactError) as excinfo:
        read_run_metadata("run-does-not-exist")
    assert excinfo.value.code is RunArtifactErrorCode.run_not_found
    assert excinfo.value.to_dict()["code"] == "run_not_found"


async def test_corrupt_event_log_is_reported_not_silently_skipped():
    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None
    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.close()

    events_path = run_dir(run_id) / EVENTS_FILENAME
    events_path.write_text(events_path.read_text(encoding="utf-8") + "{not json\n", encoding="utf-8")

    with pytest.raises(RunArtifactError) as excinfo:
        read_run_events(run_id)
    assert excinfo.value.code is RunArtifactErrorCode.corrupt_event_log
    assert excinfo.value.to_dict()["details"]["line"] > 0


# --------------------------------------------------------------------- 5. 可关闭


async def test_artifacts_can_be_disabled_by_env(monkeypatch, runs_root):
    monkeypatch.setenv("AURA_RUN_ARTIFACTS", "0")
    engine = _make_engine()
    run_id = engine.run_id
    assert run_id is not None
    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.close()

    assert not (runs_root / run_id).exists(), "关掉工件后仍然写盘了"


async def test_run_ended_directly_on_the_run_manager_still_finalizes_the_artifact():
    """ScenarioRunner 的收尾姿势：它不调 engine.close()，直接 run_manager.end_run()。

    工件收尾若挂在引擎方法上，这条路径下 run.json 会永远停在 ended_at=None，
    S4 拿到的每份场景工件都像"还在跑"。
    """

    engine = _make_engine()
    await engine.reset(scenario=_make_scenario("scenario_b"), seed=99)
    run_id = engine.run_id
    assert run_id is not None
    await engine.start(drive_timer=False)
    await engine.timer.tick_once()
    await engine.agent_runtime.wait_for_idle(timeout=5.0)
    await engine.pause()

    engine.run_manager.end_run("completed")

    metadata = read_run_metadata(run_id)
    assert metadata["end_reason"] == "completed"
    assert metadata["ended_at"] is not None
    assert metadata["scenario_id"] == "scenario_b"
    assert _read_lines(run_id), "场景 run 的事件没有落盘"


# ------------------------------------------------------------ 6. run_id 形状校验（边界）


def test_run_dir_accepts_a_run_manager_shaped_id(runs_root):
    """RunManager 造出来的 id 必须原样通过——校验不能挡住正常写盘路径。"""

    run_id = new_run_id()
    assert run_dir(run_id) == runs_root / run_id


@pytest.mark.parametrize(
    "bad_run_id",
    [
        "../../../../tmp",  # 目录穿越：runs 根之外
        "run-20260721T073632-0d8961ba/../../etc",  # 合法前缀 + 穿越尾巴
        "/etc/passwd",  # 绝对路径（Path 拼接会直接丢掉左操作数）
        "run-does-not-exist",  # 形状不对（历史测试口径：仍是 run_not_found）
        "run-20260721T073632-0D8961BA",  # 大写 hex：不是 RunManager 的产物
        "",
    ],
)
def test_run_dir_rejects_ids_that_are_not_run_manager_shaped(bad_run_id):
    """工件根是被写、被 S4 套件跑批读的目录：形状在边界处挡住，而不是靠路由碰巧兜住。"""

    with pytest.raises(RunArtifactError) as excinfo:
        run_dir(bad_run_id)
    assert excinfo.value.code is RunArtifactErrorCode.run_not_found
    assert excinfo.value.to_dict()["run_id"] == bad_run_id


def test_traversal_id_never_reaches_the_filesystem(tmp_path):
    """穿越串不能因为换了 root 就绕过校验。"""

    with pytest.raises(RunArtifactError):
        run_dir("../../../../tmp", root=tmp_path)
    with pytest.raises(RunArtifactError):
        read_run_metadata("../../../../tmp", root=tmp_path)


def test_list_run_ids_ignores_foreign_directories(runs_root):
    """外来目录（手工拷贝、旧格式）不冒充 run，否则 /api/runs 会整体报错。"""

    foreign = runs_root / "not-a-run"
    foreign.mkdir(parents=True, exist_ok=True)
    (foreign / RUN_METADATA_FILENAME).write_text("{}", encoding="utf-8")

    real = new_run_id()
    real_dir = runs_root / real
    real_dir.mkdir(parents=True, exist_ok=True)
    (real_dir / RUN_METADATA_FILENAME).write_text("{}", encoding="utf-8")

    ids = list_run_ids()
    assert real in ids
    assert "not-a-run" not in ids


async def test_events_published_straight_to_the_bus_are_also_recorded():
    """不经引擎 _publish_sim_event 的事件（main.py 的 UI 根事件）同样要落盘。"""

    bus = EventBus()
    engine = _make_engine(bus)
    run_id = engine.run_id
    assert run_id is not None

    await bus.publish(
        SimEvent(event_type="user.command", source="user_ui", timestamp=0.0, priority=2)
    )
    await engine.close()

    lines = _read_lines(run_id)
    assert [line["event_type"] for line in lines] == ["user.command"]
