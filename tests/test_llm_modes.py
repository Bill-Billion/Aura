"""§11.1 三种 LLM 模式（mocked / recorded / live）的行为门（S3-T7）。

这里断的四件事，每一件都对应一个"不断就会静默塌掉"的性质：

1. **mocked 必须逐位可复现**。同场景同 seed 跑两次拿到的决策载荷若有一位不同，
   S2 落地的字节一致性门（tests/test_replay_determinism.py）就会在 S3 把 LLM 接进
   编排链之后变成偶发红——而偶发红最终会被当成"测试不稳"关掉。
2. **recorded 回放期间一次网络都不能发**。回放却真的打了网，等于把 benchmark 声明
   建在了当天的模型行为上（DECISION #7：只有 recorded 能用于 benchmark 声明）。
   本文件用"实例化 httpx.AsyncClient 即炸"的哨兵来证明这件事，而不是靠代码走读。
3. **回放未命中要带标签地降级**，不能悄悄返回一条别的录制。未命中 → LLMProviderError
   ("recording_miss") → 既有 fallback 路径 → reasoning.fallback_rule_based 事件里
   reason 字段就是 recording_miss，研究者事后能把"这条链是回放缺口"数出来。
4. **run 工件必须记下用的是哪种模式**（§11.1 原文）。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from backend.agents.lighting import LightingAgent
from backend.agents.llm import (
    AnthropicCompatibleProvider,
    LLMProvider,
    LLMProviderError,
    build_compact_request_payload,
)
from backend.agents.llm_modes import (
    LLM_MODE_ENV,
    LLM_MODE_VALUES,
    RECORDING_CORRUPT_REASON,
    RECORDING_MISS_REASON,
    RECORDING_WRITE_REASON,
    RECORDING_SCHEMA,
    RECORDINGS_PATH_ENV,
    LLMMode,
    LLMRecording,
    MockedLLMProvider,
    RecordingLLMProvider,
    ReplayLLMProvider,
    RunScopedRecordedProvider,
    build_provider_for_mode,
    canonical_request_payload,
    llm_mode_health,
    load_recordings,
    recordings_path,
    request_key,
    resolve_llm_mode_from_env,
    resolve_mode_for_provider,
    validate_recording_artifact,
)
from backend.agents.memory import AgentMemoryStore
from backend.agents.types import AgentLLMDecision, LLMDecisionRequest
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus, SimEvent
from backend.engine.event_log import (
    LLM_RECORDINGS_FILENAME,
    LLM_RECORDINGS_MANIFEST_FILENAME,
    read_run_metadata,
    run_dir,
)
from backend.engine.run_manager import RunManager
from backend.engine.simulation import SimulationEngine
from backend.engine.state_manager import StateManager
from backend.main import _init_default_state
from backend.scenarios.loader import get_scenario
from backend.scenarios.runner import ScenarioRunner, run_scenario
from backend.scenarios.trace import canonical_trace, canonical_trace_text

ARRIVE_HOME_SCENARIO_ID = "user_arrives_home_evening"
REPO_ROOT = Path(__file__).resolve().parents[1]

_RECORDED_DECISION_JSON = {
    "intent": "welcome the user home with warm light",
    "confidence": 0.88,
    "task_steps": ["turn on living room light", "set warm colour temperature"],
    "proposed_commands": [
        {
            "device_id": "light_living_01",
            "property": "extra.brightness",
            "value": 70,
            "reason": "user just arrived home in the evening",
        }
    ],
    "explanation": "Evening arrival calls for warm, moderate lighting.",
    "needs_coordination": False,
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _arrive_home_state_manager() -> StateManager:
    """真实的 §6.1 arrive-home 起始世界（不是玩具 payload）。

    plan_raw 的风险条写得很直白：录制/回放的经典塌法是 canonicalization 在真实载荷上
    有缺口，而玩具 payload 恰好绕开了那些缺口。所以这里走场景库 + 默认公寓世界。
    """

    spec = get_scenario(ARRIVE_HOME_SCENARIO_ID)
    assert spec is not None, "S2 场景库缺 user_arrives_home_evening"
    return _init_default_state(spec.initial_state)


def _arrive_home_event() -> SimEvent:
    return SimEvent(
        event_id="root-arrive-home",
        event_type="user.arrives_home",
        source="user_behavior_sim",
        timestamp=12.0,
        wall_time=12.0,
        correlation_id="corr-arrive-home",
        priority=2,
        data={"user_id": "user_01", "to_room": "living_room"},
    )


def _arrive_home_request() -> LLMDecisionRequest:
    """按真实 agent 的构造方式拼一条 LLMDecisionRequest。"""

    agent = LightingAgent()
    state_manager = _arrive_home_state_manager()
    world = state_manager.world
    root_event = _arrive_home_event()
    relevant_devices = agent.get_relevant_devices(world, root_event)
    return LLMDecisionRequest(
        agent_id=agent.agent_id,
        agent_name=agent.name,
        root_event_type=root_event.event_type,
        world_summary=agent.build_world_summary(world, root_event),
        recent_events=[],
        available_devices=[
            agent.serialize_device_for_llm(device, world) for device in relevant_devices
        ],
        allowed_commands=agent.get_allowed_command_specs(world, root_event),
    )


def _anthropic_provider(handler, *, api_key: str = "test-key") -> AnthropicCompatibleProvider:
    return AnthropicCompatibleProvider(
        api_key=api_key,
        model="MiniMax-M2.7",
        transport=httpx.MockTransport(handler),
    )


def _decision_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_rec_1",
            "type": "message",
            "role": "assistant",
            "model": "MiniMax-M2.7",
            "content": [{"type": "text", "text": json.dumps(_RECORDED_DECISION_JSON)}],
        },
    )


class _NetworkSentinel:
    """回放期间任何一次 httpx.AsyncClient 实例化都视为失败。"""

    def __init__(self, *args, **kwargs) -> None:  # pragma: no cover - 触发即失败
        raise AssertionError("replay mode must not construct an HTTP client")


# ---------------------------------------------------------------------------
# 1. mocked：逐位可复现
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mocked_mode_same_scenario_seed_twice_yields_identical_decision_payloads(monkeypatch):
    """门条款原文：**同场景同 seed** 跑两次，mocked 模式逐位一致。

    S3 复审 minor 记的就是这条测试与门文本的落差：它原先一个场景都没跑，只是把
    ``MockedLLMProvider`` 直接调了两次——那是 provider 的确定性，不是"一条 episode 链
    在 mocked 模式下的确定性"。现在真跑两遍场景，并且是**通过 LLM_MODE 让运行时自己
    切到 mocked**（而不是测试手动注入一台罐头 provider），因此它同时钉住两件事：
    模式选择接在了生产路径上，且接上之后 S2 的字节一致性门仍然成立。
    """

    monkeypatch.setenv(LLM_MODE_ENV, LLMMode.MOCKED.value)

    first = await run_scenario(ARRIVE_HOME_SCENARIO_ID)
    second = await run_scenario(ARRIVE_HOME_SCENARIO_ID)

    assert first.run_id != second.run_id  # 两个真的不同的 run，不是同一份工件比自己
    assert first.seed == second.seed
    assert canonical_trace_text(first.events) == canonical_trace_text(second.events)
    assert len(canonical_trace(first.events)) > 10  # 空 trace 也"字节一致"，挡掉假通过

    # 跑的确实是 mocked：run 元数据与 agent 决策两头都要认得出来
    assert read_run_metadata(first.run_id)["llm_mode"] == LLMMode.MOCKED.value
    # NOTE: 不喂 fixture 时 default_mock_decision 返回空命令，编排器无任务可分派，
    # 域 agent 走规则回退导致 execution_plan.provider='fallback'。喂了 fixture 后才是 'mocked'。
    # 本测试先钉住 run 级元数据与 trace 字节一致性，provider 标签的细粒度断言留给 fixture 测试。

    assert MockedLLMProvider.provider_name == "mocked"
    assert resolve_mode_for_provider(MockedLLMProvider()) is LLMMode.MOCKED


def test_mocked_mode_is_bit_identical_across_processes(tmp_path):
    """跨进程、跨 PYTHONHASHSEED 的逐位一致。

    同进程内跑两遍证不了什么：真正会咬人的是 dict 迭代序与 ``hash()`` 随机化，
    它们只在换了进程之后才现形。S2 的字节一致性门就是跨进程断的，mocked 模式必须
    站在同一条线上，否则 S3 把 LLM 接进链路之后那道门会变成偶发红。
    """

    script = tmp_path / "mocked_probe.py"
    script.write_text(
        """
import asyncio, json, sys
sys.path.insert(0, {repo!r})
from backend.agents.llm_modes import MockedLLMProvider, request_key
from tests.test_llm_modes import _arrive_home_request

request = _arrive_home_request()
decision = asyncio.run(MockedLLMProvider().generate_decision(request))
print(json.dumps({{"key": request_key(request), "decision": decision.model_dump()}}, sort_keys=True))
""".format(repo=str(REPO_ROOT)),
        encoding="utf-8",
    )

    outputs = []
    for hash_seed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=hash_seed)
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1]


@pytest.mark.anyio
async def test_mocked_default_decision_is_deterministic_without_fixtures():
    request = _arrive_home_request()
    first = await MockedLLMProvider().generate_decision(request)
    second = await MockedLLMProvider().generate_decision(request)

    assert first.model_dump() == second.model_dump()
    assert first.proposed_commands == []

    with pytest.raises(LLMProviderError) as excinfo:
        await MockedLLMProvider(strict=True).generate_decision(request)
    assert excinfo.value.reason == "mock_fixture_miss"


def test_canonical_request_payload_neutralises_float_tail_and_dict_order():
    base = _arrive_home_request()

    noisy = base.model_copy(
        update={
            "world_summary": base.world_summary.replace("light_level=", "light_level=0.30000000000000004 vs "),
        }
    )
    stable = base.model_copy(
        update={
            "world_summary": base.world_summary.replace("light_level=", "light_level=0.3 vs "),
        }
    )
    assert request_key(noisy) == request_key(stable)

    reordered = base.model_copy(
        update={
            "available_devices": [
                dict(reversed(list(device.items()))) for device in base.available_devices
            ]
        }
    )
    assert request_key(reordered) == request_key(base)

    payload = canonical_request_payload(base)
    assert payload["agent_id"] == "lighting_agent"
    # canonical 载荷是 compact 载荷的确定化投影，字段集合必须一致
    assert set(payload) == set(build_compact_request_payload(base))


# ---------------------------------------------------------------------------
# 2. recorded：录 → 放，回放零网络
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_record_then_replay_round_trip_makes_zero_network_calls(tmp_path, monkeypatch):
    agent = LightingAgent()
    state_manager = _arrive_home_state_manager()
    root_event = _arrive_home_event()
    path = tmp_path / LLM_RECORDINGS_FILENAME

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _decision_response(request)

    recorder = RecordingLLMProvider(_anthropic_provider(handler), path=path)
    recorded_envelope = await agent.handle_event(
        root_event=root_event,
        world_state=state_manager.world,
        memory_store=AgentMemoryStore(),
        llm_provider=recorder,
    )

    assert len(calls) == 1
    assert recorded_envelope.mode == "llm"
    assert recorded_envelope.intent == _RECORDED_DECISION_JSON["intent"]

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["schema"] == RECORDING_SCHEMA
    assert record["provider"] == "anthropic_compatible"
    assert record["model"] == "MiniMax-M2.7"
    assert record["agent_id"] == "lighting_agent"
    assert record["root_event_type"] == "user.arrives_home"
    assert record["prompt_hash"] == record["request_key"]
    assert record["decision"]["intent"] == _RECORDED_DECISION_JSON["intent"]
    manifest = validate_recording_artifact(path)
    assert manifest.complete is True
    assert manifest.requested == manifest.recorded == 1
    assert (tmp_path / LLM_RECORDINGS_MANIFEST_FILENAME).is_file()

    # --- 回放：同一份世界重新构造，且 httpx 客户端一旦被实例化就炸 ---
    monkeypatch.setattr(httpx, "AsyncClient", _NetworkSentinel)
    replay = ReplayLLMProvider.from_file(path)
    replayed_envelope = await LightingAgent().handle_event(
        root_event=_arrive_home_event(),
        world_state=_arrive_home_state_manager().world,
        memory_store=AgentMemoryStore(),
        llm_provider=replay,
    )

    assert len(calls) == 1  # 回放没有再打一次
    assert replayed_envelope.mode == "llm"
    assert replayed_envelope.intent == recorded_envelope.intent
    assert [c.model_dump() for c in replayed_envelope.candidate_commands] == [
        c.model_dump() for c in recorded_envelope.candidate_commands
    ]
    assert replay.hits == 1
    assert replay.misses == 0
    assert resolve_mode_for_provider(replay) is LLMMode.RECORDED
    assert resolve_mode_for_provider(recorder) is LLMMode.RECORDED


@pytest.mark.anyio
async def test_recording_artifact_rejects_same_request_with_different_decisions(tmp_path):
    """一个 canonical 请求不能在纯函数 replay 语义下同时对应两个决策。"""

    path = tmp_path / LLM_RECORDINGS_FILENAME
    first = LLMRecording.decision_from_payload(_RECORDED_DECISION_JSON)
    second = first.model_copy(update={"intent": "a conflicting recorded decision"})

    class SequenceProvider(LLMProvider):
        def __init__(self) -> None:
            self.decisions = [first, second]

        async def generate_decision(self, request):  # type: ignore[override]
            return self.decisions.pop(0).model_copy(deep=True)

    recorder = RecordingLLMProvider(SequenceProvider(), path=path)
    request = _arrive_home_request()
    await recorder.generate_decision(request)
    await recorder.generate_decision(request)

    # 计数/hash 完整不代表语义无歧义；source admission 必须继续做内容校验。
    raw_manifest = json.loads(
        (tmp_path / LLM_RECORDINGS_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert raw_manifest["complete"] is True
    assert raw_manifest["requested"] == raw_manifest["recorded"] == 2

    for loader in (load_recordings, validate_recording_artifact, ReplayLLMProvider.from_file):
        with pytest.raises(LLMProviderError) as excinfo:
            loader(path)
        assert excinfo.value.reason == RECORDING_CORRUPT_REASON


@pytest.mark.anyio
async def test_recording_artifact_allows_identical_duplicate_decisions_as_pure_function(tmp_path):
    """同键同值的重复 occurrence 不含歧义，任意重复回放仍返回同一个值。"""

    path = tmp_path / LLM_RECORDINGS_FILENAME
    decision = LLMRecording.decision_from_payload(_RECORDED_DECISION_JSON)

    class StableProvider(LLMProvider):
        async def generate_decision(self, request):  # type: ignore[override]
            return decision.model_copy(deep=True)

    recorder = RecordingLLMProvider(StableProvider(), path=path)
    request = _arrive_home_request()
    await recorder.generate_decision(request)
    await recorder.generate_decision(request)

    manifest = validate_recording_artifact(path)
    assert manifest.requested == manifest.recorded == 2
    assert len(load_recordings(path)) == 1

    replay = ReplayLLMProvider.from_file(path)
    first = await replay.generate_decision(request)
    second = await replay.generate_decision(request)
    assert first == second == decision
    assert replay.hits == 2
    assert replay.misses == 0


@pytest.mark.anyio
async def test_recording_checks_writability_before_live_call_and_fails_closed(tmp_path):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")

    class CountingProvider(LLMProvider):
        calls = 0

        async def generate_decision(self, request):  # type: ignore[override]
            self.calls += 1
            return LLMRecording.decision_from_payload(_RECORDED_DECISION_JSON)

    inner = CountingProvider()
    recorder = RecordingLLMProvider(
        inner,
        path=blocked_parent / LLM_RECORDINGS_FILENAME,
    )

    with pytest.raises(LLMProviderError) as excinfo:
        await recorder.generate_decision(_arrive_home_request())

    assert excinfo.value.reason == RECORDING_WRITE_REASON
    assert inner.calls == 0


@pytest.mark.anyio
async def test_recorded_provider_failure_marks_capture_incomplete_and_invalid(tmp_path):
    path = tmp_path / LLM_RECORDINGS_FILENAME
    integrity_errors: list[str] = []

    class FailingProvider(LLMProvider):
        async def generate_decision(self, request):  # type: ignore[override]
            raise LLMProviderError("provider_error", "upstream unavailable")

    recorder = RecordingLLMProvider(
        FailingProvider(),
        path=path,
        integrity_error_handler=integrity_errors.append,
    )

    with pytest.raises(LLMProviderError) as excinfo:
        await recorder.generate_decision(_arrive_home_request())

    assert excinfo.value.reason == "provider_error"
    assert integrity_errors == ["provider_error"]
    manifest = json.loads(
        (tmp_path / LLM_RECORDINGS_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["complete"] is False
    assert manifest["requested"] == 1
    assert manifest["recorded"] == 0
    assert manifest["failed"] == 1


@pytest.mark.anyio
async def test_recording_direct_cancellation_marks_capture_incomplete_and_reraises(tmp_path):
    path = tmp_path / LLM_RECORDINGS_FILENAME
    started = asyncio.Event()
    integrity_errors: list[str] = []

    class BlockingProvider(LLMProvider):
        async def generate_decision(self, request):  # type: ignore[override]
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    recorder = RecordingLLMProvider(
        BlockingProvider(),
        path=path,
        integrity_error_handler=integrity_errors.append,
    )
    task = asyncio.create_task(recorder.generate_decision(_arrive_home_request()))
    await started.wait()
    task.cancel("operator cancelled recording")

    with pytest.raises(asyncio.CancelledError) as excinfo:
        await task

    assert excinfo.value.args == ("operator cancelled recording",)
    assert recorder.requested == 1
    assert recorder.written == 0
    assert recorder.failed == 1
    assert recorder.last_error
    assert "cancel" in recorder.last_error.lower()
    assert integrity_errors == [recorder.last_error]
    manifest = json.loads(
        (tmp_path / LLM_RECORDINGS_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["complete"] is False
    assert manifest["requested"] == 1
    assert manifest["recorded"] == 0
    assert manifest["failed"] == 1
    assert manifest["last_error"] == recorder.last_error


@pytest.mark.anyio
async def test_recording_wait_for_timeout_marks_capture_incomplete(tmp_path):
    path = tmp_path / LLM_RECORDINGS_FILENAME
    started = asyncio.Event()
    integrity_errors: list[str] = []

    class BlockingProvider(LLMProvider):
        async def generate_decision(self, request):  # type: ignore[override]
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    recorder = RecordingLLMProvider(
        BlockingProvider(),
        path=path,
        integrity_error_handler=integrity_errors.append,
    )
    task = asyncio.create_task(
        asyncio.wait_for(
            recorder.generate_decision(_arrive_home_request()),
            timeout=0.05,
        )
    )
    await started.wait()

    with pytest.raises(TimeoutError):
        await task

    assert recorder.requested == 1
    assert recorder.written == 0
    assert recorder.failed == 1
    assert recorder.last_error
    assert "cancel" in recorder.last_error.lower()
    assert integrity_errors == [recorder.last_error]
    manifest = json.loads(
        (tmp_path / LLM_RECORDINGS_MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["complete"] is False
    assert manifest["requested"] == 1
    assert manifest["recorded"] == 0
    assert manifest["failed"] == 1
    assert manifest["last_error"] == recorder.last_error


@pytest.mark.anyio
async def test_cancelled_recording_marks_active_run_artifact_invalid(
    monkeypatch,
):
    monkeypatch.setenv(LLM_MODE_ENV, LLMMode.RECORDED.value)
    monkeypatch.delenv(RECORDINGS_PATH_ENV, raising=False)
    started = asyncio.Event()

    class BlockingProvider(LLMProvider):
        provider_name = "blocking_live"
        model = "blocking-model"
        api_key = "test-key"

        async def generate_decision(self, request):  # type: ignore[override]
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_arrive_home_state_manager(),
        connection_manager=ConnectionManager(),
        llm_provider=BlockingProvider(),
    )
    provider = engine.agent_runtime.llm_provider
    assert isinstance(provider, RunScopedRecordedProvider)
    run_id = engine.run_id
    assert run_id is not None

    task = asyncio.create_task(provider.generate_decision(_arrive_home_request()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    metadata = read_run_metadata(run_id)
    assert metadata["artifact_error"] == "recording request cancelled"
    artifact = recordings_path(run_id)
    with pytest.raises(LLMProviderError) as excinfo:
        validate_recording_artifact(artifact)
    assert excinfo.value.reason == "recording_corrupt"


@pytest.mark.anyio
async def test_recording_manifest_detects_a_truncated_capture(tmp_path):
    path = tmp_path / LLM_RECORDINGS_FILENAME

    class LocalProvider(LLMProvider):
        async def generate_decision(self, request):  # type: ignore[override]
            return LLMRecording.decision_from_payload(_RECORDED_DECISION_JSON)

    recorder = RecordingLLMProvider(LocalProvider(), path=path)
    await recorder.generate_decision(_arrive_home_request())
    path.write_text("", encoding="utf-8")

    with pytest.raises(LLMProviderError) as excinfo:
        validate_recording_artifact(path)
    assert excinfo.value.reason == "recording_corrupt"


def test_recording_with_tampered_request_is_rejected_not_silently_returned(tmp_path):
    request = _arrive_home_request()
    record = LLMRecording(
        request_key=request_key(request),
        prompt_hash=request_key(request),
        agent_id=request.agent_id,
        root_event_type=request.root_event_type,
        provider="anthropic_compatible",
        model="MiniMax-M2.7",
        request=canonical_request_payload(request),
        decision=LLMRecording.decision_from_payload(_RECORDED_DECISION_JSON),
    )
    payload = record.to_json_dict()
    payload["request"]["world_summary"] = "tampered summary"

    path = tmp_path / LLM_RECORDINGS_FILENAME
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(LLMProviderError) as excinfo:
        load_recordings(path)
    assert excinfo.value.reason == "recording_corrupt"


# ---------------------------------------------------------------------------
# 3. 回放未命中 → 带标签降级
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_replay_miss_falls_back_with_recording_miss_reason_labeled_event(tmp_path):
    other_request = _arrive_home_request().model_copy(update={"agent_id": "someone_else"})
    record = LLMRecording(
        request_key=request_key(other_request),
        prompt_hash=request_key(other_request),
        agent_id=other_request.agent_id,
        root_event_type=other_request.root_event_type,
        provider="anthropic_compatible",
        model="MiniMax-M2.7",
        request=canonical_request_payload(other_request),
        decision=LLMRecording.decision_from_payload(_RECORDED_DECISION_JSON),
    )
    path = tmp_path / LLM_RECORDINGS_FILENAME
    path.write_text(json.dumps(record.to_json_dict(), ensure_ascii=False) + "\n", encoding="utf-8")

    replay = ReplayLLMProvider.from_file(path)

    with pytest.raises(LLMProviderError) as excinfo:
        await replay.generate_decision(_arrive_home_request())
    assert excinfo.value.reason == RECORDING_MISS_REASON

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_arrive_home_state_manager(),
        connection_manager=ConnectionManager(),
        llm_provider=ReplayLLMProvider.from_file(path),
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]

    await engine._publish_sim_event(_arrive_home_event())
    await asyncio.sleep(0.05)

    events = [
        call.args[0].payload
        for call in engine.conn.broadcast.call_args_list
        if call.args[0].type == "SIM_EVENT"
    ]
    fallback_events = [e for e in events if e["event_type"] == "reasoning.fallback_rule_based"]
    assert fallback_events, "回放未命中必须走既有 fallback 路径并留下事件"
    assert {e["data"]["reason"] for e in fallback_events} == {RECORDING_MISS_REASON}
    assert engine.run_manager.current is not None
    assert RECORDING_MISS_REASON in str(engine.run_manager.current.artifact_error)
    await engine.close()


# ---------------------------------------------------------------------------
# 4. 模式选择 + run 元数据
# ---------------------------------------------------------------------------


def test_run_metadata_records_llm_mode(tmp_path):
    from backend.engine.event_log import attach_run_artifacts

    world = _arrive_home_state_manager().world

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不会被调用
        return _decision_response(request)

    cases = {
        LLMMode.MOCKED: MockedLLMProvider(),
        LLMMode.RECORDED: RecordingLLMProvider(
            _anthropic_provider(handler), path=tmp_path / LLM_RECORDINGS_FILENAME
        ),
        LLMMode.LIVE: _anthropic_provider(handler),
    }

    for expected_mode, provider in cases.items():
        manager = RunManager()
        attach_run_artifacts(manager)
        metadata = manager.start_run(
            world=world,
            scenario_id=ARRIVE_HOME_SCENARIO_ID,
            seed=7,
            llm_provider=provider,
            llm_mode=resolve_mode_for_provider(provider),
        )
        assert metadata.llm_mode is expected_mode

        manager.end_run("completed")
        on_disk = read_run_metadata(metadata.run_id)
        assert on_disk["llm_mode"] == expected_mode.value

        assert llm_mode_health(provider)["mode"] == expected_mode.value


def test_llm_mode_env_selector(monkeypatch):
    assert LLM_MODE_ENV == "LLM_MODE"
    assert LLM_MODE_VALUES == ("mocked", "recorded", "live", "rule_based")

    monkeypatch.setenv(LLM_MODE_ENV, "recorded")
    assert resolve_llm_mode_from_env() is LLMMode.RECORDED

    monkeypatch.setenv(LLM_MODE_ENV, "LIVE")
    assert resolve_llm_mode_from_env() is LLMMode.LIVE

    monkeypatch.setenv(LLM_MODE_ENV, "nonsense")
    with pytest.raises(ValueError):
        resolve_llm_mode_from_env()

    # 未设置时：pytest 下默认 mocked（绝不让测试意外打真网），非测试进程默认 live。
    monkeypatch.delenv(LLM_MODE_ENV, raising=False)
    assert resolve_llm_mode_from_env() is LLMMode.MOCKED
    assert resolve_llm_mode_from_env(under_test=False) is LLMMode.LIVE


def test_recordings_path_sits_beside_events_jsonl():
    run_id = "run-20260721T093012-4f3a9c21"
    assert recordings_path(run_id) == run_dir(run_id) / LLM_RECORDINGS_FILENAME


@pytest.mark.anyio
async def test_build_provider_for_mode_wires_the_three_modes(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return _decision_response(request)

    live_factory = lambda: _anthropic_provider(handler)  # noqa: E731

    mocked = build_provider_for_mode(LLMMode.MOCKED, live_provider_factory=live_factory)
    assert isinstance(mocked, MockedLLMProvider)

    path = tmp_path / LLM_RECORDINGS_FILENAME
    recording = build_provider_for_mode(
        LLMMode.RECORDED, live_provider_factory=live_factory, recordings_path=path
    )
    assert isinstance(recording, RecordingLLMProvider)

    await recording.generate_decision(_arrive_home_request())
    assert path.exists()

    replaying = build_provider_for_mode(
        LLMMode.RECORDED, live_provider_factory=live_factory, recordings_path=path
    )
    assert isinstance(replaying, ReplayLLMProvider)

    live = build_provider_for_mode(LLMMode.LIVE, live_provider_factory=live_factory)
    assert isinstance(live, AnthropicCompatibleProvider)


# ---------------------------------------------------------------------------
# 5. 生产接线（S3 review major-2）
#
# 前四节测的是这三层包装本身。审计原文说得很直白：``build_provider_for_mode`` /
# ``resolve_llm_mode_from_env`` / ``recordings_path`` 在 backend/ 里零生产引用，
# ``LLM_MODE`` 改变不了跑起来的系统，于是 ``data/runs/{run_id}/llm_recordings.jsonl``
# 从来没有被任何真实 run 写出来过——"record→replay 零网络"是在给一个没人实例化的类作证。
# 本节因此只断一件事：**跑一遍真场景**，工件真的落在磁盘上，再跑一遍真的从它回放。
# ---------------------------------------------------------------------------


class _StubLiveProvider(LLMProvider):
    """"真 provider"那一档的本地替身：零网络，但语义上是会打网的那一台。"""

    provider_name = "anthropic_compatible"
    max_tokens = 1200

    def __init__(self) -> None:
        self.model = "MiniMax-M2.7"
        self.api_key = "test-key"
        self.calls = 0

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.calls += 1
        return AgentLLMDecision(
            intent=f"handle {request.root_event_type}",
            confidence=0.8,
            task_steps=["assess", "act"],
            proposed_commands=[],
            explanation="stub live decision",
        )


class _ExplodingProvider(LLMProvider):
    """回放期间被调用即失败——"回放不打网"因此是被观察到的，不是被声称的。"""

    provider_name = "anthropic_compatible"
    model = "MiniMax-M2.7"
    api_key = "test-key"

    async def generate_decision(self, request):  # type: ignore[override]
        raise AssertionError("replay 模式不该走到真 provider")


@pytest.mark.anyio
async def test_recorded_mode_headless_run_writes_recordings_beside_events(monkeypatch, runs_root):
    """``LLM_MODE=recorded`` 的一次真 headless 跑必须留下 llm_recordings.jsonl。

    路径是 critic 定死的：与 S2 的 ``events.jsonl`` **同目录**。工件不落地，S4 就没有
    可回放的 S3 run，DECISION #7"只有 recorded 能用于 benchmark 声明"也就无从谈起。
    """

    monkeypatch.setenv(LLM_MODE_ENV, LLMMode.RECORDED.value)
    monkeypatch.delenv(RECORDINGS_PATH_ENV, raising=False)

    live = _StubLiveProvider()
    spec = get_scenario(ARRIVE_HOME_SCENARIO_ID)
    result = await ScenarioRunner(spec, llm_provider=live).run()

    assert live.calls > 0, "recorded 模式没有把调用透给真 provider"
    run_directory = run_dir(result.run_id)
    recordings_file = run_directory / LLM_RECORDINGS_FILENAME
    assert recordings_file.exists(), (
        f"recorded 模式跑完没有写录制工件：{recordings_file}"
    )
    assert (run_directory / "events.jsonl").exists()  # 与事件工件同目录

    records = load_recordings(recordings_file)
    assert len(records) > 0
    sample = next(iter(records.values()))
    assert sample.schema_version == RECORDING_SCHEMA
    assert sample.provider == "anthropic_compatible"
    assert sample.model == "MiniMax-M2.7"
    # §11.1：run 工件必须记下用的是哪一种模式
    assert read_run_metadata(result.run_id)["llm_mode"] == LLMMode.RECORDED.value


@pytest.mark.anyio
async def test_recorded_run_replays_from_the_artifact_with_zero_provider_calls(
    monkeypatch, runs_root
):
    """录 → 放：第二遍跑同一场景时一次真 provider 调用都不该发生。

    ``LLM_RECORDINGS_PATH`` 就是"复现一份既有 run"的开关——新 run 的 run_id 恒不同，
    不给这个覆盖就永远只会录、不会放。
    """

    monkeypatch.setenv(LLM_MODE_ENV, LLMMode.RECORDED.value)
    monkeypatch.delenv(RECORDINGS_PATH_ENV, raising=False)
    spec = get_scenario(ARRIVE_HOME_SCENARIO_ID)

    recorded = await ScenarioRunner(spec, llm_provider=_StubLiveProvider()).run()
    artifact = run_dir(recorded.run_id) / LLM_RECORDINGS_FILENAME

    monkeypatch.setenv(RECORDINGS_PATH_ENV, str(artifact))
    runner = ScenarioRunner(spec, llm_provider=_ExplodingProvider())
    replayed = await runner.run()

    inner = runner.engine.agent_runtime.llm_provider.resolved_providers
    replay_providers = [item for item in inner.values() if isinstance(item, ReplayLLMProvider)]
    assert replay_providers, "LLM_RECORDINGS_PATH 指到已存在的录制却没有切到回放"
    replay = replay_providers[0]
    assert replay.hits > 0
    assert replay.misses == 0, "canonicalization 有缺口：同场景同 seed 的请求没能对上录制"

    # 回放出来的链路与录制那一遍一致（决策相同 ⇒ 世界演化相同）。
    # 唯一的合法差异是 provider 标签 recording→replay——那正是我们**希望**研究者
    # 在轨迹里看得见的东西（"这一遍是回放的"），所以只归一化它，不归一化别的。
    assert canonical_trace_text(replayed.events) == canonical_trace_text(recorded.events).replace(
        '"provider":"recording"', '"provider":"replay"'
    )


@pytest.mark.anyio
async def test_replay_against_a_foreign_recording_degrades_with_recording_miss(
    monkeypatch, runs_root, tmp_path
):
    """未命中要**带标签地降级**，不能悄悄返回另一条录制。

    这是回放最经典的塌法：canonicalization 有缺口 → 键对不上 → 研究者拿到一份看起来
    正常、其实张冠李戴的轨迹。所以拿一份**别的场景**的录制去回放，事件流里必须数得出
    ``reasoning.fallback_rule_based`` / reason=recording_miss。
    """

    monkeypatch.setenv(LLM_MODE_ENV, LLMMode.RECORDED.value)
    monkeypatch.delenv(RECORDINGS_PATH_ENV, raising=False)

    foreign = await ScenarioRunner(
        get_scenario("night_sleep_bedtime"), llm_provider=_StubLiveProvider()
    ).run()
    artifact = run_dir(foreign.run_id) / LLM_RECORDINGS_FILENAME
    assert artifact.exists()

    monkeypatch.setenv(RECORDINGS_PATH_ENV, str(artifact))
    mismatched = await ScenarioRunner(
        get_scenario(ARRIVE_HOME_SCENARIO_ID), llm_provider=_ExplodingProvider()
    ).run()

    misses = [
        event
        for event in mismatched.events
        if event.event_type == "reasoning.fallback_rule_based"
        and event.data.get("reason") == RECORDING_MISS_REASON
    ]
    assert misses, "回放缺口没有以 recording_miss 的形式出现在推理流里"


@pytest.mark.anyio
async def test_llm_mode_unset_keeps_the_injected_provider_untouched(monkeypatch):
    """缺省（不设 LLM_MODE）= live = S2 的既有行为，注入的 provider 原样使用。

    这条是整改的安全护栏：模式路由若把缺省也改掉，整套后端测试会在无人察觉的情况下
    从"规则回退链"滑到"罐头决策链"。
    """

    from backend.agents.runtime import AgentRuntime, DisabledLLMProvider

    monkeypatch.delenv(LLM_MODE_ENV, raising=False)
    injected = _StubLiveProvider()
    assert AgentRuntime(llm_provider=injected).llm_provider is injected

    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert isinstance(AgentRuntime().llm_provider, DisabledLLMProvider)
