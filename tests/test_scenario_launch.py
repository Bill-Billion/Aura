"""跑起来的服务端上启动一个场景 run（S2 review major-1）+ run provenance 门（major-2）。

**major-1（场景无法启动）**：S2 收工时 ``bind_generation_sources`` 只有 ScenarioRunner
一个调用方，于是活着的服务端永远装不上 §4.5 的 scripted/rule_based/stochastic 三条产线，
tick 里走的是 ``user_sim.step`` 那条旧分支——交互式事件流里**没有一条**带
``event_generation_mode``，S2 手动门 1（"前端连上后跑一个场景 run，中途点 Reset"）
根本无从演示，S5 也没有东西可驱动。本文件锁死两个入口：

  POST /api/runs {scenario_id, seed?}     REST
  WS   CMD_RUN_SCENARIO {scenario_id, seed?}

两者共用 ``backend.main.start_scenario_run`` 这一条实现——第二条实现＝第二套 run 语义。

**major-2（provenance 可以撒谎）**：``reset(scenario_id="随便写")`` 曾把任意字符串盖进
RunMetadata / run.json / 每条事件，且不应用那个场景的 initial_state。一份声称自己是某
场景的 run 工件，可以从来没跑过那个场景——对复现性工件而言，字段"在但是假的"比缺失更糟。
本文件锁死：未知 id 一律拒绝，且给了 id 就必须真的按它摆世界。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.main import app
from backend.scenarios.loader import get_scenario

# 库里真实存在的场景（backend/scenarios/library/arrive_home_evening.yaml）。
# 刻意用真 id + 真 initial_state：本文件要证明的正是"工件里的 scenario_id 名副其实"。
SCENARIO_ID = "user_arrives_home_evening"
UNKNOWN_SCENARIO_ID = "arrive_home_evening"  # 文件名，不是场景 id —— 正是 review 抓到的那个假 id


@pytest.fixture(autouse=True)
def restore_app_globals():
    """lifespan 只建不拆全局：跑完把 main 的模块级全局还原成 import 时的样子。

    不还原的话，本文件之后任何"不带 lifespan 的 TestClient"用例都会看见一台
    已经 close 掉的引擎（/api/health 的断言随即变成跨文件耦合）。
    """

    yield
    main_module.simulation_engine = None
    main_module.state_manager = None


@pytest.fixture
def live_client():
    """带 lifespan 的客户端：引擎真的起来了（不带 lifespan 时 simulation_engine 是 None）。"""

    with TestClient(app) as client:
        assert main_module.simulation_engine is not None
        # 墙钟节拍调快：本文件要看的是"tick 里真的走了三条产线"，不是 2s 的默认节拍。
        main_module.simulation_engine.timer.tick_interval = 0.1
        yield client


def _wait_for_events(client, run_id: str, *, generation_mode: str, timeout: float = 20.0):
    """轮询 run 工件直到出现该生成模式的事件；超时是**失败**，不是挂起。"""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(
            f"/api/runs/{run_id}/events", params={"generation_mode": generation_mode}
        )
        assert resp.status_code == 200, resp.text
        events = resp.json()["events"]
        if events:
            return events
        time.sleep(0.05)
    raise AssertionError(
        f"{timeout}s 内没有等到 generation_mode={generation_mode!r} 的事件"
        f"（run {run_id}）——服务端很可能仍在走未打标的旧 user_sim 分支"
    )


@pytest.mark.anyio
async def test_post_commit_launch_failure_is_finalized_and_next_launch_can_retry(monkeypatch):
    """A generation-source failure may not leave a stopped canonical lock behind."""

    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        install = engine._install_generation_sources

        def fail_install(*args, **kwargs):
            raise RuntimeError("synthetic generation install failure")

        monkeypatch.setattr(engine, "_install_generation_sources", fail_install)
        with pytest.raises(RuntimeError, match="synthetic generation"):
            await main_module.start_scenario_run(
                main_module.RunScenarioPayload(
                    scenario_id=SCENARIO_ID,
                    seed=20260820,
                )
            )

        assert engine.run_manager.current is None
        assert engine.event_bus.run_id is None
        assert engine.generation_sources is None
        assert engine.run_manager.finished[-1].end_reason == "launch_failed"

        monkeypatch.setattr(engine, "_install_generation_sources", install)
        retry = await main_module.start_scenario_run(
            main_module.RunScenarioPayload(
                scenario_id=SCENARIO_ID,
                seed=20260820,
            )
        )
        assert retry["scenario_id"] == SCENARIO_ID
        assert engine.run_manager.current is not None
        assert engine.run_manager.current.run_id == retry["run_id"]


# --------------------------------------------------------------- REST 启动路径


def test_post_api_runs_launches_scenario_with_tagged_events(live_client):
    engine = main_module.simulation_engine
    assert engine is not None
    previous_run_id = engine.run_id

    resp = live_client.post(
        "/api/runs", json={"scenario_id": SCENARIO_ID, "seed": 20260721}
    )
    assert resp.status_code == 201, resp.text
    payload = resp.json()["run"]
    run_id = payload["run_id"]

    assert run_id != previous_run_id
    assert payload["scenario_id"] == SCENARIO_ID
    assert payload["seed"] == 20260721
    assert payload["is_running"] is True

    # ① run 身份换了，且引擎确实在跑这个 run。
    assert engine.run_id == run_id
    assert engine.scenario_id == SCENARIO_ID
    assert engine.is_running is True

    # ② 三条 §4.5 产线真的装上了（major-1 的核心断言）。
    sources = engine.generation_sources
    assert sources is not None, "服务端没有装上 §4.5 生成产线"
    assert sources.scripted is not None and sources.rule_based is not None
    assert sources.context.run_id == run_id
    assert sources.context.scenario_id == SCENARIO_ID

    # ③ 场景 initial_state 真的落到了世界上（major-2：scenario_id 名副其实）。
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None
    world = engine.state_manager.world
    assert world.environment.weather == spec.initial_state.weather
    assert world.users["user_01"].location is None  # 场景声明 presence_state: away

    # ④ 事件流带生成元数据，而不是旧的未打标 user.activity_change。
    scripted = _wait_for_events(live_client, run_id, generation_mode="scripted")
    assert all(event["run_id"] == run_id for event in scripted)
    assert all(event["scenario_id"] == SCENARIO_ID for event in scripted)
    assert any(event["event_type"] == "user.arrives_home" for event in scripted)

    # ⑤ run.json 里的 scenario_id 指向一个真被加载并应用过的场景。
    detail = live_client.get(f"/api/runs/{run_id}").json()
    assert detail["run"]["scenario_id"] == SCENARIO_ID
    assert detail["run"]["seed"] == 20260721


def test_post_api_runs_rejects_unknown_scenario_id(live_client):
    engine = main_module.simulation_engine
    assert engine is not None
    run_id_before = engine.run_id

    resp = live_client.post("/api/runs", json={"scenario_id": UNKNOWN_SCENARIO_ID})
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "scenario_not_found"
    assert SCENARIO_ID in detail["details"]["known_ids"]

    # 被拒绝的启动一点副作用都不许留：run 没换，引擎没跑起来。
    assert engine.run_id == run_id_before
    assert engine.is_running is False
    assert engine.generation_sources is None


def test_post_api_runs_defaults_seed_to_scenario_seed(live_client):
    spec = get_scenario(SCENARIO_ID)
    assert spec is not None

    resp = live_client.post("/api/runs", json={"scenario_id": SCENARIO_ID})
    assert resp.status_code == 201, resp.text
    assert resp.json()["run"]["seed"] == spec.seed


def test_post_api_runs_without_engine_is_503():
    """引擎没起来时必须是一条结构化的 503，而不是 500 或者一个假的 run。"""

    client = TestClient(app)  # 不进 lifespan：simulation_engine 保持 None
    resp = client.post("/api/runs", json={"scenario_id": SCENARIO_ID})
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "engine_unavailable"


# ----------------------------------------------------------------- WS 启动路径


def _read_until(ws, message_type: str, *, limit: int = 20) -> dict:
    """读到指定类型为止；有上界，读不到就断言失败而不是无限等。"""

    seen: list[str] = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message["type"])
        if message["type"] == message_type:
            return message
    raise AssertionError(f"{limit} 条消息内没等到 {message_type}，收到的是 {seen}")


def test_ws_cmd_run_scenario_starts_the_run(live_client):
    engine = main_module.simulation_engine
    assert engine is not None

    with live_client.websocket_connect("/ws/simulation") as ws:
        assert ws.receive_json()["type"] == "STATE_FULL"
        ws.send_json(
            {"type": "CMD_RUN_SCENARIO", "payload": {"scenario_id": SCENARIO_ID, "seed": 7}}
        )
        status = _read_until(ws, "SIMULATION_STATUS")

    assert status["payload"]["scenario_id"] == SCENARIO_ID
    assert status["payload"]["run_id"] == engine.run_id
    assert status["payload"]["is_running"] is True
    assert engine.run_manager.current is not None
    assert engine.run_manager.current.seed == 7
    assert engine.generation_sources is not None


def test_ws_cmd_run_scenario_unknown_id_returns_structured_error(live_client):
    engine = main_module.simulation_engine
    assert engine is not None
    run_id_before = engine.run_id

    with live_client.websocket_connect("/ws/simulation") as ws:
        assert ws.receive_json()["type"] == "STATE_FULL"
        ws.send_json(
            {"type": "CMD_RUN_SCENARIO", "payload": {"scenario_id": UNKNOWN_SCENARIO_ID}}
        )
        error = _read_until(ws, "ERROR")

    assert error["payload"]["code"] == "scenario_not_found"
    assert engine.run_id == run_id_before
    assert engine.is_running is False


def test_ws_cmd_run_scenario_rejects_malformed_payload(live_client):
    with live_client.websocket_connect("/ws/simulation") as ws:
        assert ws.receive_json()["type"] == "STATE_FULL"
        ws.send_json({"type": "CMD_RUN_SCENARIO", "payload": {"seed": 3}})
        error = _read_until(ws, "ERROR")

    assert error["payload"]["code"] == "invalid_payload"


# ------------------------------------------------- 场景 run 中途 Reset（手动门 1）


def test_reset_mid_scenario_run_is_rejected_without_mutating_the_run(live_client):
    """研究 run 的场景/seed/world 是不变量，交互式 Reset 必须 fail closed。"""

    engine = main_module.simulation_engine
    assert engine is not None

    started = live_client.post(
        "/api/runs", json={"scenario_id": SCENARIO_ID, "seed": 20260721}
    ).json()["run"]
    scenario_run_id = started["run_id"]
    _wait_for_events(live_client, scenario_run_id, generation_mode="scripted")

    with live_client.websocket_connect("/ws/simulation") as ws:
        assert ws.receive_json()["type"] == "STATE_FULL"
        assert _read_until(ws, "SIMULATION_STATUS")["payload"]["run_id"] == scenario_run_id
        ws.send_json({"type": "CMD_SIM_RESET"})
        error = _read_until(ws, "ERROR", limit=40)

    assert error["payload"]["code"] == "research_run_locked"
    assert error["payload"]["details"]["run_id"] == scenario_run_id
    assert error["payload"]["details"]["type"] == "CMD_SIM_RESET"
    assert engine.run_id == scenario_run_id
    assert engine.scenario_id == SCENARIO_ID
    assert engine.run_manager.current is not None
    assert engine.run_manager.current.scenario_id == SCENARIO_ID
    assert engine.generation_sources is not None

    scenario_events = live_client.get(f"/api/runs/{scenario_run_id}/events").json()["events"]
    assert scenario_events
    assert all(event["run_id"] == scenario_run_id for event in scenario_events)
