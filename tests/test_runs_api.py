"""GET /api/runs*（S2-T7）——事件历史第一次有了 REST 出口。

审计发现：事件历史除了 WS 实时推送之外没有任何查询端点，研究者跑完一个场景之后
无法回答 §18 的第一个问题——"这批事件是哪个场景、哪个 seed 跑出来的，因果链长什么样"。
这三个端点是 S4（报告）与 S5（可观测性面板）的唯一读侧契约：

  GET /api/runs                              §11 元数据列表（新的在前）
  GET /api/runs/{run_id}                     单 run 元数据 + 事件条数 + 同目录工件清单
  GET /api/runs/{run_id}/events?correlation_id=&event_type=&generation_mode=&limit=&offset=

S4 之后会在同一前缀下加 GET /api/runs/{run_id}/report；本文件锁死上面三个的形状。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.api.ws import ConnectionManager
from backend.engine.event_bus import EventBus
from backend.engine.event_types import TIMER_TICK_EVENT_TYPE
from backend.engine.run_manager import SPEC11_REQUIRED_FIELDS
from backend.engine.simulation import SimulationEngine
from backend.engine.state import (
    DeviceState,
    DeviceStateValues,
    Location3D,
    RoomState,
    WorldState,
)
from backend.engine.state_manager import StateManager
from backend.main import _init_default_state, app


@pytest.fixture
def client():
    return TestClient(app)


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


def _produce_run(*, scenario_id: str | None = None, seed: int | None = None, ticks: int = 2) -> str:
    """跑一个真 run 并落工件，返回 run_id（同步封装，便于配合 TestClient）。

    带 scenario_id 时用**默认公寓世界**：``reset(scenario=...)`` 会把该场景的
    initial_state 真的摆上去（S2 review major-2），而上面那个两台设备的迷你世界
    摆不下一份真实场景的起始状态。
    """

    async def _run() -> str:
        state = _init_default_state() if scenario_id is not None else StateManager(_make_world())
        engine = SimulationEngine(EventBus(), state, ConnectionManager())
        if scenario_id is not None or seed is not None:
            await engine.reset(scenario=scenario_id, seed=seed)
        run_id = engine.run_id
        assert run_id is not None
        await engine.start(drive_timer=False)
        for _ in range(ticks):
            await engine.timer.tick_once()
            await engine.agent_runtime.wait_for_idle(timeout=5.0)
        await engine.pause()
        await engine.close()
        return run_id

    return asyncio.run(_run())


# ------------------------------------------------------------------ 列表 / 详情


def test_runs_list_returns_spec11_metadata(client):
    run_id = _produce_run(scenario_id="user_arrives_home_evening", seed=1001)

    resp = client.get("/api/runs")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] >= 1
    entry = next(item for item in payload["runs"] if item["run_id"] == run_id)
    missing = [field for field in SPEC11_REQUIRED_FIELDS if field not in entry]
    assert missing == [], f"/api/runs 少了 §11 字段: {missing}"
    assert entry["event_count"] > 0
    assert entry["ended_at"] is not None


def test_run_metadata_answers_spec18_q1_scenario_and_seed(client):
    """§18 Q1：拿到一份事件流，必须能立刻回答它属于哪个场景、哪个 seed。"""

    run_id = _produce_run(scenario_id="user_arrives_home_evening", seed=4242)

    resp = client.get(f"/api/runs/{run_id}")
    assert resp.status_code == 200
    payload = resp.json()
    run = payload["run"]
    assert run["run_id"] == run_id
    assert run["scenario_id"] == "user_arrives_home_evening"
    assert run["seed"] == 4242
    assert run["sim_version"]
    assert run["llm_mode"] in {"mocked", "recorded", "live"}
    assert len(run["initial_state_hash"]) == 64
    assert payload["event_count"] > 0
    # S3 会把 llm_recordings.jsonl 放进同一个目录，S4 两份一起读——清单必须自描述。
    assert "events.jsonl" in payload["artifacts"]
    assert "run.json" in payload["artifacts"]


def test_unknown_run_returns_404_with_known_ids(client):
    _produce_run()
    resp = client.get("/api/runs/run-nope")
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["code"] == "run_not_found"
    assert isinstance(detail["known_ids"], list)


# --------------------------------------------------------------------- 事件端点


def test_events_endpoint_correlation_filter_returns_full_chain(client):
    run_id = _produce_run(ticks=2)

    everything = client.get(f"/api/runs/{run_id}/events")
    assert everything.status_code == 200
    all_events = everything.json()["events"]
    assert all_events

    tick = next(
        event for event in all_events if event["event_type"] == TIMER_TICK_EVENT_TYPE
    )
    correlation_id = tick["correlation_id"]
    expected = [
        event for event in all_events if event["correlation_id"] == correlation_id
    ]
    assert len(expected) >= 2, "一拍应当派生出至少一条子事件，否则这个断言测不到链"

    resp = client.get(
        f"/api/runs/{run_id}/events", params={"correlation_id": correlation_id}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["run_id"] == run_id
    assert payload["count"] == payload["total"] == len(expected)
    assert [event["event_id"] for event in payload["events"]] == [
        event["event_id"] for event in expected
    ]
    # 因果链完整＝根事件本身也在结果里，而不是只剩下派生事件。
    assert tick["event_id"] in {event["event_id"] for event in payload["events"]}
    assert [event["seq"] for event in payload["events"]] == sorted(
        event["seq"] for event in payload["events"]
    )


def test_events_endpoint_supports_type_mode_and_paging(client):
    run_id = _produce_run(ticks=2)

    ticks = client.get(
        f"/api/runs/{run_id}/events", params={"event_type": TIMER_TICK_EVENT_TYPE}
    ).json()
    assert ticks["count"] == 2
    assert all(e["event_type"] == TIMER_TICK_EVENT_TYPE for e in ticks["events"])

    system = client.get(
        f"/api/runs/{run_id}/events", params={"generation_mode": "system"}
    ).json()
    assert system["count"] > 0
    assert all(e["event_generation_mode"] == "system" for e in system["events"])

    everything = client.get(f"/api/runs/{run_id}/events").json()
    page = client.get(
        f"/api/runs/{run_id}/events", params={"limit": 2, "offset": 1}
    ).json()
    assert page["total"] == everything["total"]
    assert page["offset"] == 1
    assert [e["event_id"] for e in page["events"]] == [
        e["event_id"] for e in everything["events"][1:3]
    ]


def test_events_endpoint_unknown_run_returns_404(client):
    resp = client.get("/api/runs/run-nope/events")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "run_not_found"
