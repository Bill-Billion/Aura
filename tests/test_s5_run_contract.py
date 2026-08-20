"""S5 研究运行后端契约：逐-run 策略、并发、finalize 与 trace 导出。"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import backend.main as main_module
from backend.agents.llm import LLMProvider
from backend.agents.llm_modes import (
    LLMMode,
    MockedLLMProvider,
    RuleBasedLLMProvider,
    build_provider_for_mode,
)
from backend.agents.runtime import (
    AgentRuntime,
    BaselinePolicyUnavailableError,
    DisabledLLMProvider,
)
from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus
from backend.engine.event_log import (
    EVENTS_FILENAME,
    LLM_RECORDINGS_FILENAME,
    LLM_RECORDINGS_MANIFEST_FILENAME,
    RUN_METADATA_FILENAME,
    read_run_events,
    read_run_metadata,
    run_dir,
)
from backend.engine.rng import MAX_JSON_SAFE_SEED, MAX_SEED
from backend.engine.run_manager import effective_llm_mode_for_policy
from backend.engine.simulation import SimulationEngine
from backend.main import app
from backend.models.schemas import (
    BaselinePolicy,
    RunScenarioPayload,
    ScenarioLaunchError,
    ScenarioLaunchErrorCode,
)
from backend.scenarios.loader import get_scenario
from backend.scenarios.runner import ScenarioRunner, scenario_duration_seconds
from backend.scenarios.trace import export_canonical_trace

pytestmark = pytest.mark.anyio

SCENARIO_ID = "safety_smoke_kitchen"


class _StubLiveProvider(LLMProvider):
    provider_name = "stub_live"
    model = "stub-model"
    api_key = "server-owned-test-key"
    max_tokens = 1200

    def __init__(self) -> None:
        self.calls = 0

    async def generate_decision(self, request):  # type: ignore[override]
        from backend.agents.types import AgentLLMDecision

        self.calls += 1
        return AgentLLMDecision(
            intent=f"handle {request.root_event_type}",
            confidence=0.8,
            task_steps=["inspect"],
            proposed_commands=[],
            explanation="test recording",
        )


@pytest.fixture(autouse=True)
def restore_main_globals():
    main_module._scenario_launch_lock = asyncio.Lock()
    main_module._scenario_launch_idempotency.clear()
    yield
    task = main_module._scenario_finalizer_task
    if task is not None and not task.done():
        task.cancel()
    main_module._scenario_finalizer_task = None
    main_module.simulation_engine = None
    main_module.state_manager = None
    main_module._scenario_launch_lock = asyncio.Lock()
    main_module._scenario_launch_idempotency.clear()


def test_payload_uses_distinct_policy_vocabulary_and_rejects_client_secrets():
    expected = {
        BaselinePolicy.RULE_BASED: LLMMode.RULE_BASED,
        BaselinePolicy.LLM_MOCKED: LLMMode.MOCKED,
        BaselinePolicy.LLM_RECORDED: LLMMode.RECORDED,
        BaselinePolicy.LLM_LIVE: LLMMode.LIVE,
    }
    assert {
        policy: effective_llm_mode_for_policy(policy) for policy in BaselinePolicy
    } == expected

    with pytest.raises(ValidationError):
        RunScenarioPayload.model_validate(
            {
                "scenario_id": SCENARIO_ID,
                "baseline_policy": "llm_live",
                "api_key": "must-not-cross-the-boundary",
            }
        )
    for invalid_seed in (True, 1.0, MAX_JSON_SAFE_SEED + 1, MAX_SEED + 1):
        with pytest.raises(ValidationError):
            RunScenarioPayload.model_validate(
                {"scenario_id": SCENARIO_ID, "seed": invalid_seed}
            )
    with pytest.raises(ValidationError):
        RunScenarioPayload.model_validate(
            {
                "scenario_id": SCENARIO_ID,
                "baseline_policy": "llm_recorded",
                "recordings_path": "/tmp/foreign.jsonl",
            }
        )
    with pytest.raises(ValidationError):
        RunScenarioPayload.model_validate(
            {"scenario_id": SCENARIO_ID, "idempotency_key": "retry-me"}
        )
    with pytest.raises(ValidationError):
        RunScenarioPayload.model_validate(
            {
                "scenario_id": SCENARIO_ID,
                "baseline_policy": "rule_based",
                "recording_source_run_id": "run-20260721T093012-4f3a9c21",
            }
        )


def test_rest_seed_domain_is_exactly_representable_by_browser_json_numbers():
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "scenario_id": SCENARIO_ID,
                "seed": MAX_JSON_SAFE_SEED + 1,
                "baseline_policy": "rule_based",
            },
        )
        assert response.status_code == 422

        catalog = client.get("/api/scenarios")
        assert catalog.status_code == 200
        assert all(
            0 <= scenario["seed"] <= MAX_JSON_SAFE_SEED
            for scenario in catalog.json()["scenarios"]
        )


async def test_invalid_seed_is_rejected_before_reset_mutates_the_active_run():
    engine = SimulationEngine(
        EventBus(), main_module._init_default_state(), ConnectionManager()
    )
    original_run = engine.run_manager.current
    original_state_manager = engine.state_manager
    assert original_run is not None
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None

    with pytest.raises(ValueError):
        await engine.reset(
            new_state_manager=main_module._init_default_state(),
            scenario=spec,
            seed=MAX_SEED + 1,
        )

    assert engine.run_manager.current is original_run
    assert engine.state_manager is original_state_manager
    assert original_run.ended_at is None
    await engine.close()


def test_rule_based_provider_never_constructs_live_provider():
    calls = 0

    def live_factory():
        nonlocal calls
        calls += 1
        return _StubLiveProvider()

    provider = build_provider_for_mode(
        LLMMode.RULE_BASED, live_provider_factory=live_factory
    )
    assert isinstance(provider, RuleBasedLLMProvider)
    assert calls == 0


def test_websocket_reconnect_receives_active_canonical_run_status():
    payload = {
        "scenario_id": SCENARIO_ID,
        "seed": 20260820,
        "baseline_policy": "rule_based",
    }

    with TestClient(app) as client:
        response = client.post("/api/runs", json=payload)
        assert response.status_code == 201
        expected_run_id = response.json()["run"]["run_id"]

        with client.websocket_connect("/ws/simulation") as ws:
            assert ws.receive_json()["type"] == "STATE_FULL"
            status = ws.receive_json()

        assert status["type"] == "SIMULATION_STATUS"
        assert status["payload"]["run_id"] == expected_run_id
        assert status["payload"]["scenario_id"] == SCENARIO_ID
        assert status["payload"]["seed"] == payload["seed"]
        assert status["payload"]["baseline_policy"] == "rule_based"
        assert status["payload"]["llm_mode"] == "rule_based"
        assert status["payload"]["finalized"] is False


def test_active_canonical_run_rejects_interactive_mutations():
    mutations = [
        {"type": "CMD_DEVICE_CONTROL", "payload": {"device_id": "light_living_01", "action": "turn_on"}},
        {"type": "CMD_SCENE_APPLY", "payload": {"scene_id": "away"}},
        {"type": "CMD_SIM_START", "payload": {}},
        {"type": "CMD_SIM_PAUSE", "payload": {}},
        {"type": "CMD_SIM_RESET", "payload": {}},
        {"type": "CMD_SIM_SPEED", "payload": {"speed": 4}},
        {"type": "CMD_SIM_MODE", "payload": {"mode": "demo"}},
    ]

    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            json={
                "scenario_id": SCENARIO_ID,
                "seed": 20260820,
                "baseline_policy": "rule_based",
            },
        )
        assert response.status_code == 201
        expected_run_id = response.json()["run"]["run_id"]

        with client.websocket_connect("/ws/simulation") as ws:
            assert ws.receive_json()["type"] == "STATE_FULL"
            assert ws.receive_json()["type"] == "SIMULATION_STATUS"
            for mutation in mutations:
                ws.send_json(mutation)
                for _ in range(100):
                    error = ws.receive_json()
                    if error["type"] == "ERROR":
                        break
                assert error["type"] == "ERROR"
                assert error["payload"]["code"] == "research_run_locked"
                assert error["payload"]["details"]["type"] == mutation["type"]
                assert error["payload"]["details"]["run_id"] == expected_run_id

        assert main_module.simulation_engine is not None
        current = main_module.simulation_engine.run_manager.current
        assert current is not None and current.run_id == expected_run_id
        assert main_module.simulation_engine.is_running is True
        assert main_module.simulation_engine.speed == 1.0


def test_runtime_prepares_each_policy_and_rejects_unconfigured_live():
    live = _StubLiveProvider()
    runtime = AgentRuntime(llm_provider=live)
    rule = runtime.prepare_baseline_policy(BaselinePolicy.RULE_BASED)
    mocked = runtime.prepare_baseline_policy(BaselinePolicy.LLM_MOCKED)
    selected_live = runtime.prepare_baseline_policy(BaselinePolicy.LLM_LIVE)

    assert rule.llm_mode is LLMMode.RULE_BASED
    assert isinstance(rule.provider, RuleBasedLLMProvider)
    assert mocked.llm_mode is LLMMode.MOCKED
    assert isinstance(mocked.provider, MockedLLMProvider)
    assert mocked.provider.strict is False
    assert selected_live.provider is live

    unavailable = AgentRuntime(llm_provider=DisabledLLMProvider())
    with pytest.raises(BaselinePolicyUnavailableError) as excinfo:
        unavailable.prepare_baseline_policy(BaselinePolicy.LLM_LIVE)
    assert excinfo.value.details["reason_code"] == "live_provider_not_configured"


async def test_concurrent_launch_has_one_winner_and_persists_effective_contract():
    payload = RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        seed=20260820,
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None

    async with main_module.lifespan(app):
        assert main_module.simulation_engine is not None
        assert main_module.simulation_engine.run_manager.current is not None
        assert main_module.simulation_engine.run_manager.current.scenario_id is None
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(
                client.post("/api/runs", json=payload.model_dump(mode="json")),
                client.post("/api/runs", json=payload.model_dump(mode="json")),
            )
        assert sorted(response.status_code for response in responses) == [201, 409]
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["detail"]["code"] == "run_already_active"

        run = next(response for response in responses if response.status_code == 201).json()[
            "run"
        ]
        assert run["baseline_policy"] == "rule_based"
        assert run["llm_mode"] == "rule_based"
        assert run["duration_seconds"] == scenario_duration_seconds(spec)
        assert run["scenario_schema_version"] == spec.scenario_schema_version
        assert run["recording_source_run_id"] is None
        assert run["event_schema_version"]
        assert run["command_schema_version"]
        assert run["device_registry_version"]


async def test_ambient_device_command_cannot_cross_into_concurrent_canonical_launch(
    monkeypatch,
):
    root_blocked = asyncio.Event()
    release_root = asyncio.Event()

    async def block_ambient_root(message):
        if (
            message.type == "SIM_EVENT"
            and message.payload.get("event_type") == "user.command"
            and message.payload.get("data", {}).get("message_type")
            == "CMD_DEVICE_CONTROL"
        ):
            root_blocked.set()
            await release_root.wait()

    monkeypatch.setattr(main_module.manager, "broadcast", block_ambient_root)
    payload = RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        seed=20260820,
        baseline_policy=BaselinePolicy.RULE_BASED,
    )

    async with main_module.lifespan(app):
        ambient_task = asyncio.create_task(
            main_module._handle_ws_message(
                object(),
                {
                    "type": "CMD_DEVICE_CONTROL",
                    "payload": {
                        "device_id": "light_living_01",
                        "action": "turn_on",
                    },
                },
            )
        )
        await asyncio.wait_for(root_blocked.wait(), timeout=1.0)

        launch_task = asyncio.create_task(main_module.start_scenario_run(payload))
        await asyncio.sleep(0)
        assert not launch_task.done(), "canonical launch bypassed the ambient mutation lock"

        release_root.set()
        await ambient_task
        launched = await launch_task

        events, _ = read_run_events(launched["run_id"])
        assert events
        assert {event["run_id"] for event in events} == {launched["run_id"]}
        assert not any(
            event["event_type"] == "user.command"
            and event.get("data", {}).get("message_type") == "CMD_DEVICE_CONTROL"
            for event in events
        )


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (ScenarioLaunchErrorCode.BASELINE_POLICY_UNAVAILABLE, 503),
        (ScenarioLaunchErrorCode.RECORDING_SOURCE_NOT_FOUND, 404),
        (ScenarioLaunchErrorCode.RECORDING_SOURCE_NOT_FINALIZED, 409),
        (ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH, 409),
        (ScenarioLaunchErrorCode.RECORDING_SOURCE_INVALID, 422),
        (ScenarioLaunchErrorCode.IDEMPOTENCY_CONFLICT, 409),
    ],
)
async def test_launch_policy_errors_keep_their_structured_http_status(
    monkeypatch, code, expected_status
):
    import backend.api.routes as routes_module

    async def reject_launch(_payload):
        raise ScenarioLaunchError(code, "policy rejected", details={"reason_code": "test"})

    monkeypatch.setattr(routes_module, "_scenario_launcher", reject_launch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs",
            json={"scenario_id": SCENARIO_ID, "baseline_policy": "rule_based"},
        )

    assert response.status_code == expected_status
    assert response.json()["detail"] == {
        "code": code.value,
        "message": "policy rejected",
        "details": {"reason_code": "test"},
    }


async def test_canonical_run_auto_finalizes_and_a_second_run_can_start(monkeypatch):
    payload = RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    broadcasts = []

    async def capture_broadcast(message):
        broadcasts.append(message)

    monkeypatch.setattr(main_module.manager, "broadcast", capture_broadcast)
    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        engine.timer.tick_interval = 0.005
        first = await main_module.start_scenario_run(payload)
        deadline = asyncio.get_running_loop().time() + 5.0
        while read_run_metadata(first["run_id"])["ended_at"] is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        finalized = read_run_metadata(first["run_id"])
        assert finalized["end_reason"] == "completed"
        assert engine.run_manager.current is None
        deadline = asyncio.get_running_loop().time() + 1.0
        terminal_messages = []
        while not terminal_messages:
            assert asyncio.get_running_loop().time() < deadline
            terminal_messages = [
                message
                for message in broadcasts
                if message.type == "SIMULATION_STATUS"
                and message.payload.get("finalized") is True
            ]
            await asyncio.sleep(0)
        terminal = terminal_messages[-1].payload
        assert terminal["run_id"] == first["run_id"]
        assert terminal["scenario_id"] == SCENARIO_ID
        assert terminal["baseline_policy"] == "rule_based"
        assert terminal["llm_mode"] == "rule_based"
        assert terminal["is_running"] is False
        assert terminal["end_reason"] == "completed"
        assert terminal["ended_at"] == finalized["ended_at"]

        second = await main_module.start_scenario_run(payload)
        assert second["run_id"] != first["run_id"]


async def test_launch_idempotency_replays_active_and_finalized_run_without_duplication(
    monkeypatch,
):
    payload = RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        seed=17,
        baseline_policy=BaselinePolicy.RULE_BASED,
        idempotency_key="123e4567-e89b-42d3-a456-426614174000",
    )

    async def discard_broadcast(_message):
        return None

    monkeypatch.setattr(main_module.manager, "broadcast", discard_broadcast)
    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None

        first = await main_module.start_scenario_run(payload)
        active_retry = await main_module.start_scenario_run(payload)
        assert active_retry["run_id"] == first["run_id"]
        assert active_retry["finalized"] is False
        assert engine.run_manager.current is not None

        async with main_module._scenario_launch_lock:
            await main_module._cancel_scenario_finalizer()
            await engine.pause()
            finished = engine.run_manager.end_run("completed")
        assert finished is not None

        finalized_retry = await main_module.start_scenario_run(payload)
        assert finalized_retry["run_id"] == first["run_id"]
        assert finalized_retry["finalized"] is True
        assert finalized_retry["end_reason"] == "completed"
        assert finalized_retry["is_running"] is False
        assert engine.run_manager.current is None

        conflicting = payload.model_copy(update={"seed": 18})
        with pytest.raises(ScenarioLaunchError) as excinfo:
            await main_module.start_scenario_run(conflicting)
        assert excinfo.value.code is ScenarioLaunchErrorCode.IDEMPOTENCY_CONFLICT
        assert excinfo.value.details["original_run_id"] == first["run_id"]

        second = await main_module.start_scenario_run(
            payload.model_copy(
                update={"idempotency_key": "123e4567-e89b-42d3-a456-426614174001"}
            )
        )
        assert second["run_id"] != first["run_id"]


async def test_launch_response_rechecks_finalized_state_after_slow_ws_broadcast(
    monkeypatch,
):
    entered_broadcast = asyncio.Event()
    release_broadcast = asyncio.Event()
    calls = 0

    async def block_first_broadcast(message):
        nonlocal calls
        if message.type != "STATE_FULL":
            return
        calls += 1
        if calls == 1:
            entered_broadcast.set()
            await release_broadcast.wait()

    monkeypatch.setattr(main_module.manager, "broadcast", block_first_broadcast)
    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        task = asyncio.create_task(
            main_module.start_scenario_run(
                RunScenarioPayload(
                    scenario_id=SCENARIO_ID,
                    baseline_policy=BaselinePolicy.RULE_BASED,
                )
            )
        )
        await asyncio.wait_for(entered_broadcast.wait(), timeout=1.0)
        async with main_module._scenario_launch_lock:
            await main_module._cancel_scenario_finalizer()
            await engine.pause()
            finished = engine.run_manager.end_run("completed")
        assert finished is not None
        release_broadcast.set()

        response = await asyncio.wait_for(task, timeout=1.0)
        assert response["run_id"] == finished.run_id
        assert response["finalized"] is True
        assert response["end_reason"] == "completed"
        assert response["is_running"] is False


async def test_finalizer_waits_for_the_full_duration_tick_before_closing_artifacts(
    monkeypatch,
):
    payload = RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    library_spec = get_scenario(SCENARIO_ID)
    assert library_spec is not None
    spec = library_spec.model_copy(update={"duration_seconds": 25.0})
    monkeypatch.setattr(
        main_module,
        "load_library",
        lambda dirs=None: {SCENARIO_ID: spec},
    )
    duration = scenario_duration_seconds(spec)
    flush_entered = asyncio.Event()
    release_flush = asyncio.Event()
    final_tick: int | None = None

    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        engine.timer.tick_interval = 0.005
        original_flush = engine._flush_pending_deltas

        async def block_duration_tick_flush():
            nonlocal final_tick
            if engine.sim_time_s >= duration and not flush_entered.is_set():
                final_tick = engine.timer.current_tick
                flush_entered.set()
                await release_flush.wait()
            await original_flush()

        monkeypatch.setattr(engine, "_flush_pending_deltas", block_duration_tick_flush)
        launched = await main_module.start_scenario_run(payload)
        try:
            await asyncio.wait_for(flush_entered.wait(), timeout=5.0)
            # Give the finalizer several polling turns while the exact tick
            # handler is still blocked. It must not close run.json/events.jsonl.
            await asyncio.sleep(0.05)
            active = read_run_metadata(launched["run_id"])
            assert active["ended_at"] is None
            assert engine.run_id == launched["run_id"]
            assert engine._is_processing_timer_tick is True
        finally:
            release_flush.set()

        deadline = asyncio.get_running_loop().time() + 5.0
        while read_run_metadata(launched["run_id"])["ended_at"] is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        finalized = read_run_metadata(launched["run_id"])
        events, _ = read_run_events(launched["run_id"])
        seqs = [event["seq"] for event in events]
        timer_ticks = [
            event
            for event in events
            if event["event_type"] == "system.timer_tick"
        ]
        assert finalized["end_reason"] == "completed"
        assert final_tick is not None
        assert seqs == list(range(seqs[0], seqs[-1] + 1))
        # duration=25/dt=10 ends on the first covering tick at t=30.  The timer
        # arms its stop during that tick's handler and drains the complete fan-out,
        # so the polling finalizer can never admit a fifth tick.
        assert timer_ticks[-1]["data"]["tick"] == final_tick == 4
        assert engine.timer.current_tick == 4
        assert timer_ticks[-1]["sim_time_s"] == 30.0


async def test_first_tick_engine_error_is_persisted_and_next_run_recovers(
    monkeypatch,
):
    payload = RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    injected = False

    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        engine.timer.tick_interval = 0.005
        original_flush = engine._flush_pending_deltas

        async def fail_first_tick_flush():
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("injected first canonical tick failure")
            await original_flush()

        monkeypatch.setattr(engine, "_flush_pending_deltas", fail_first_tick_flush)
        first = await main_module.start_scenario_run(payload)
        deadline = asyncio.get_running_loop().time() + 5.0
        while read_run_metadata(first["run_id"])["ended_at"] is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        first_metadata = read_run_metadata(first["run_id"])
        first_events, _ = read_run_events(first["run_id"])
        assert injected is True
        assert first_metadata["end_reason"] == "engine_error"
        assert any(
            event["event_type"] == "system.engine_error" for event in first_events
        )

        second = await main_module.start_scenario_run(payload)
        assert second["run_id"] != first["run_id"]
        deadline = asyncio.get_running_loop().time() + 5.0
        while read_run_metadata(second["run_id"])["ended_at"] is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        second_metadata = read_run_metadata(second["run_id"])
        second_events, _ = read_run_events(second["run_id"])
        assert second_metadata["end_reason"] == "completed"
        assert not any(
            event["event_type"] == "system.engine_error" for event in second_events
        )


async def test_finalizer_exception_marks_run_invalid_and_releases_next_launch(
    monkeypatch,
):
    payload = RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        baseline_policy=BaselinePolicy.RULE_BASED,
    )
    injected = False

    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        engine.timer.tick_interval = 0.005
        original_pause = engine.pause

        async def fail_finalizer_pause_once():
            nonlocal injected
            current = engine.run_manager.current
            if (
                not injected
                and current is not None
                and current.scenario_id == SCENARIO_ID
                and engine.sim_time_s >= float(current.duration_seconds or 0.0)
            ):
                injected = True
                raise RuntimeError("injected finalizer pause failure")
            await original_pause()

        monkeypatch.setattr(engine, "pause", fail_finalizer_pause_once)
        first = await main_module.start_scenario_run(payload)
        deadline = asyncio.get_running_loop().time() + 5.0
        while read_run_metadata(first["run_id"])["ended_at"] is None:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.01)

        failed = read_run_metadata(first["run_id"])
        assert injected is True
        assert failed["end_reason"] == "finalization_failed"
        assert "injected finalizer pause failure" in failed["artifact_error"]
        assert engine.run_manager.current is None

        second = await main_module.start_scenario_run(payload)
        assert second["run_id"] != first["run_id"]
        assert engine.run_manager.current is not None
        assert engine.run_manager.current.run_id == second["run_id"]


async def test_old_finalizer_cannot_end_a_new_run():
    engine = SimulationEngine(
        EventBus(), main_module._init_default_state(), ConnectionManager()
    )
    old_run_id = engine.run_id
    assert old_run_id is not None
    await engine.reset(new_state_manager=main_module._init_default_state())
    new_run_id = engine.run_id
    assert new_run_id is not None and new_run_id != old_run_id

    main_module.simulation_engine = engine
    await main_module._finalize_scenario_run(old_run_id, 0.0)
    assert engine.run_id == new_run_id
    assert engine.run_manager.current is not None
    assert engine.run_manager.current.ended_at is None
    await engine.close()


async def test_finished_run_clears_bus_and_ws_start_opens_anonymous_run(monkeypatch):
    broadcasts = []

    async def capture_broadcast(message):
        broadcasts.append(message)

    monkeypatch.setattr(main_module.manager, "broadcast", capture_broadcast)
    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        finished = engine.run_manager.end_run("completed")
        assert finished is not None
        assert engine.event_bus.run_id is None

        await main_module._handle_ws_message(object(), {"type": "CMD_SIM_START"})

        current = engine.run_manager.current
        assert current is not None
        assert current.run_id != finished.run_id
        assert current.scenario_id is None
        assert engine.event_bus.run_id == current.run_id
        assert engine.is_running is True
        assert any(
            message.type == "SIMULATION_STATUS"
            and message.payload.get("run_id") == current.run_id
            for message in broadcasts
        )


async def test_finalized_run_mutations_open_owned_anonymous_runs():
    """Device/scene/timing changes after finalize may never become run_id=None evidence."""

    mutations = [
        {
            "type": "CMD_DEVICE_CONTROL",
            "payload": {"device_id": "light_living_01", "action": "turn_off"},
        },
        {"type": "CMD_SCENE_APPLY", "payload": {"scene_id": "away"}},
        {"type": "CMD_SIM_SPEED", "payload": {"speed": 2.0}},
        {"type": "CMD_SIM_MODE", "payload": {"mode": "demo"}},
    ]

    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None

        for mutation in mutations:
            finished = engine.run_manager.end_run("completed")
            assert finished is not None
            assert engine.event_bus.run_id is None

            await main_module._handle_ws_message(object(), mutation)
            await engine.agent_runtime.wait_for_idle(timeout=5.0)

            current = engine.run_manager.current
            assert current is not None
            assert current.run_id != finished.run_id
            assert current.scenario_id is None
            assert engine.event_bus.run_id == current.run_id
            events, _ = read_run_events(current.run_id)
            assert events, f"{mutation['type']} opened an empty/unobservable ambient run"
            assert {event["run_id"] for event in events} == {current.run_id}
            assert {event["scenario_id"] for event in events} == {None}

            if mutation["type"] == "CMD_SIM_SPEED":
                assert engine.speed == 2.0
            if mutation["type"] == "CMD_SIM_MODE":
                assert engine.mode == "demo"

        assert engine.mode == "demo"


async def test_anonymous_run_restores_server_default_provider_after_canonical_policy(
    monkeypatch,
):
    monkeypatch.delenv("LLM_MODE", raising=False)
    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        launched = await main_module.start_scenario_run(
            RunScenarioPayload(
                scenario_id=SCENARIO_ID,
                seed=20260820,
                baseline_policy=BaselinePolicy.LLM_MOCKED,
            )
        )
        assert launched["llm_mode"] == "mocked"
        engine.run_manager.end_run("completed")

        await main_module._handle_ws_message(
            object(),
            {"type": "CMD_SCENE_APPLY", "payload": {"scene_id": "away"}},
        )

        current = engine.run_manager.current
        assert current is not None
        assert current.scenario_id is None
        assert current.llm_mode is LLMMode.RULE_BASED
        assert current.baseline_policy is BaselinePolicy.RULE_BASED
        assert current.recording_source_run_id is None


async def test_recorded_source_is_server_resolved_and_must_match_scenario_seed(
    monkeypatch,
):
    monkeypatch.setenv("LLM_MODE", "recorded")
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    source = await ScenarioRunner(spec, llm_provider=_StubLiveProvider()).run()
    source_metadata_path = run_dir(source.run_id) / RUN_METADATA_FILENAME
    original_source_metadata = source_metadata_path.read_text(encoding="utf-8")

    async with main_module.lifespan(app):
        matching = await main_module.start_scenario_run(
            RunScenarioPayload(
                scenario_id=SCENARIO_ID,
                seed=source.seed,
                baseline_policy=BaselinePolicy.LLM_RECORDED,
                recording_source_run_id=source.run_id,
            )
        )
        assert matching["llm_mode"] == "recorded"
        assert matching["baseline_policy"] == "llm_recorded"
        assert matching["recording_source_run_id"] == source.run_id

    async with main_module.lifespan(app):
        with pytest.raises(ScenarioLaunchError) as excinfo:
            await main_module.start_scenario_run(
                RunScenarioPayload(
                    scenario_id=SCENARIO_ID,
                    seed=source.seed + 1,
                    baseline_policy=BaselinePolicy.LLM_RECORDED,
                    recording_source_run_id=source.run_id,
                )
            )
        assert excinfo.value.code is ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH
        assert excinfo.value.details["reason_code"] == "recording_source_seed_mismatch"

    async with main_module.lifespan(app):
        with pytest.raises(ScenarioLaunchError) as excinfo:
            await main_module.start_scenario_run(
                RunScenarioPayload(
                    scenario_id="cooking_dinner",
                    seed=source.seed,
                    baseline_policy=BaselinePolicy.LLM_RECORDED,
                    recording_source_run_id=source.run_id,
                )
            )
        assert excinfo.value.code is ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH
        assert excinfo.value.details["reason_code"] == "recording_source_scenario_mismatch"

    monkeypatch.setenv("LLM_MODE", "live")
    wrong_mode = await ScenarioRunner(spec).run()
    monkeypatch.setenv("LLM_MODE", "recorded")
    async with main_module.lifespan(app):
        with pytest.raises(ScenarioLaunchError) as excinfo:
            await main_module.start_scenario_run(
                RunScenarioPayload(
                    scenario_id=SCENARIO_ID,
                    seed=wrong_mode.seed,
                    baseline_policy=BaselinePolicy.LLM_RECORDED,
                    recording_source_run_id=wrong_mode.run_id,
                )
            )
        assert excinfo.value.code is ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH
        assert excinfo.value.details["reason_code"] == "recording_source_mode_mismatch"

    source_metadata = json.loads(original_source_metadata)
    source_metadata["scenario_contract_hash"] = "0" * 64
    source_metadata_path.write_text(json.dumps(source_metadata), encoding="utf-8")
    async with main_module.lifespan(app):
        with pytest.raises(ScenarioLaunchError) as excinfo:
            await main_module.start_scenario_run(
                RunScenarioPayload(
                    scenario_id=SCENARIO_ID,
                    seed=source.seed,
                    baseline_policy=BaselinePolicy.LLM_RECORDED,
                    recording_source_run_id=source.run_id,
                )
            )
        assert excinfo.value.code is ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH
        assert excinfo.value.details["reason_code"] == "recording_source_contract_mismatch"
    source_metadata_path.write_text(original_source_metadata, encoding="utf-8")

    manifest_path = run_dir(source.run_id) / LLM_RECORDINGS_MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["complete"] = False
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    async with main_module.lifespan(app):
        with pytest.raises(ScenarioLaunchError) as excinfo:
            await main_module.start_scenario_run(
                RunScenarioPayload(
                    scenario_id=SCENARIO_ID,
                    seed=source.seed,
                    baseline_policy=BaselinePolicy.LLM_RECORDED,
                    recording_source_run_id=source.run_id,
                )
            )
        assert excinfo.value.code is ScenarioLaunchErrorCode.RECORDING_SOURCE_INVALID
        assert excinfo.value.details["reason_code"] == "recording_artifact_invalid"


async def test_recorded_source_admission_rejects_complete_but_ambiguous_recording(
    monkeypatch,
):
    """计数/hash 均完整也不能掩盖同一请求对应多个不同决策。"""

    monkeypatch.setenv("LLM_MODE", "recorded")
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    source = await ScenarioRunner(spec, llm_provider=_StubLiveProvider()).run()
    source_dir = run_dir(source.run_id)
    recordings_file = source_dir / LLM_RECORDINGS_FILENAME
    manifest_file = source_dir / LLM_RECORDINGS_MANIFEST_FILENAME

    original_raw = recordings_file.read_bytes()
    first_line = next(line for line in original_raw.splitlines() if line.strip())
    conflicting = json.loads(first_line)
    conflicting["decision"]["intent"] += " (conflicting occurrence)"
    appended = json.dumps(conflicting, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
    ambiguous_raw = original_raw + (b"" if original_raw.endswith(b"\n") else b"\n") + appended
    recordings_file.write_bytes(ambiguous_raw)

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["requested"] += 1
    manifest["recorded"] += 1
    manifest["recording_sha256"] = hashlib.sha256(ambiguous_raw).hexdigest()
    assert manifest["complete"] is True
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    async with main_module.lifespan(app):
        with pytest.raises(ScenarioLaunchError) as excinfo:
            await main_module.start_scenario_run(
                RunScenarioPayload(
                    scenario_id=SCENARIO_ID,
                    seed=source.seed,
                    baseline_policy=BaselinePolicy.LLM_RECORDED,
                    recording_source_run_id=source.run_id,
                )
            )

    assert excinfo.value.code is ScenarioLaunchErrorCode.RECORDING_SOURCE_INVALID
    assert excinfo.value.details["reason_code"] == "recording_artifact_invalid"


async def test_trace_attachments_are_finalized_complete_and_raw_is_byte_exact():
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    result = await ScenarioRunner(spec).run()
    client = TestClient(app)

    default_json = client.get(f"/api/runs/{result.run_id}/events")
    assert default_json.status_code == 200
    assert set(default_json.json()) == {"run_id", "count", "total", "offset", "events"}

    raw = client.get(f"/api/runs/{result.run_id}/events", params={"format": "raw"})
    assert raw.status_code == 200
    assert raw.content == (run_dir(result.run_id) / EVENTS_FILENAME).read_bytes()
    assert "attachment" in raw.headers["content-disposition"]

    canonical = client.get(
        f"/api/runs/{result.run_id}/events", params={"format": "canonical"}
    )
    assert canonical.status_code == 200
    assert canonical.text == export_canonical_trace(result.run_id)

    partial = client.get(
        f"/api/runs/{result.run_id}/events",
        params={"format": "raw", "limit": 1},
    )
    assert partial.status_code == 422
    assert partial.json()["detail"]["code"] == "trace_export_must_be_complete"

    filtered = client.get(
        "/api/runs",
        params={
            "scenario_id": SCENARIO_ID,
            "seed": result.seed,
            "baseline_policy": "rule_based",
            "llm_mode": "rule_based",
            "finalized": "true",
        },
    )
    assert filtered.status_code == 200
    assert result.run_id in {item["run_id"] for item in filtered.json()["runs"]}


async def test_trace_exports_and_report_reject_complete_suffix_truncation():
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    result = await ScenarioRunner(spec).run()
    events_path = run_dir(result.run_id) / EVENTS_FILENAME
    lines = events_path.read_bytes().splitlines(keepends=True)
    assert len(lines) > 1
    events_path.write_bytes(b"".join(lines[:-1]))
    client = TestClient(app)

    projected = client.get(f"/api/runs/{result.run_id}/events")
    raw = client.get(f"/api/runs/{result.run_id}/events", params={"format": "raw"})
    canonical = client.get(
        f"/api/runs/{result.run_id}/events", params={"format": "canonical"}
    )
    report = client.get(f"/api/runs/{result.run_id}/report")

    assert projected.status_code == raw.status_code == canonical.status_code == 500
    assert projected.json()["detail"]["code"] == "corrupt_event_log"
    assert raw.json()["detail"]["code"] == "corrupt_event_log"
    assert canonical.json()["detail"]["code"] == "corrupt_event_log"
    assert report.status_code == 500
    assert report.json()["detail"]["code"] == "corrupt_event_log"


async def test_legacy_trace_without_integrity_seal_is_explicitly_unsupported():
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    result = await ScenarioRunner(spec).run()
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.pop("events_integrity")
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    client = TestClient(app)

    projected = client.get(f"/api/runs/{result.run_id}/events")
    raw = client.get(f"/api/runs/{result.run_id}/events", params={"format": "raw"})
    canonical = client.get(
        f"/api/runs/{result.run_id}/events", params={"format": "canonical"}
    )
    report = client.get(f"/api/runs/{result.run_id}/report")

    assert projected.status_code == raw.status_code == canonical.status_code == 422
    assert projected.json()["detail"]["code"] == "unsupported_run_artifact"
    assert raw.json()["detail"]["code"] == "unsupported_run_artifact"
    assert canonical.json()["detail"]["code"] == "unsupported_run_artifact"
    assert report.status_code == 422
    assert report.json()["detail"]["code"] == "unsupported_run_artifact"


async def test_invalid_artifact_cannot_be_exported_or_reported_as_evidence():
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    result = await ScenarioRunner(spec).run()
    metadata_path = run_dir(result.run_id) / RUN_METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["artifact_error"] = "injected partial artifact"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    client = TestClient(app)

    raw = client.get(f"/api/runs/{result.run_id}/events", params={"format": "raw"})
    canonical = client.get(
        f"/api/runs/{result.run_id}/events", params={"format": "canonical"}
    )
    report = client.get(f"/api/runs/{result.run_id}/report")

    assert raw.status_code == canonical.status_code == report.status_code == 422
    assert raw.json()["detail"]["code"] == "run_artifact_invalid"
    assert canonical.json()["detail"]["code"] == "run_artifact_invalid"
    assert report.json()["detail"]["code"] == "evaluation_input_invalid"


async def test_active_run_rejects_report_and_trace_attachment():
    engine = SimulationEngine(
        EventBus(), main_module._init_default_state(), ConnectionManager()
    )
    run_id = engine.run_id
    assert run_id is not None
    client = TestClient(app)

    live_events = client.get(f"/api/runs/{run_id}/events")
    trace = client.get(f"/api/runs/{run_id}/events", params={"format": "raw"})
    report = client.get(f"/api/runs/{run_id}/report")
    assert live_events.status_code == 200
    assert live_events.json()["run_id"] == run_id
    assert trace.status_code == 409
    assert report.status_code == 409
    assert trace.json()["detail"]["code"] == "run_not_finalized"
    assert report.json()["detail"]["code"] == "run_not_finalized"
    await engine.close()
