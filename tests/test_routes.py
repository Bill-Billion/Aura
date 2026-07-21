"""Tests for REST API endpoints."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api import routes as routes_module
from backend.main import app
from backend.scenarios.loader import DEFAULT_LIBRARY_DIRS


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def scenario_dirs(tmp_path):
    """把 /api/scenarios 指向临时场景目录；退出时恢复默认库目录。"""
    routes_module.configure_scenario_dirs([tmp_path])
    try:
        yield tmp_path
    finally:
        routes_module.configure_scenario_dirs(None)


def test_get_scenes(client):
    resp = client.get("/api/scenes")
    assert resp.status_code == 200
    data = resp.json()
    assert "scenes" in data
    assert isinstance(data["scenes"], list)
    assert any(s["id"] == "apartment_v1" for s in data["scenes"])


def test_health_check(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["simulation"]["mode"] == "observe"
    assert payload["simulation"]["is_running"] is False
    assert "provider" in payload["llm"]


# ------------------------------------------------- S2-T4：GET /api/scenarios（§5.1）

_MINIMAL_SCENARIO = """
id: {scenario_id}
name: {name}
description: 用于 REST 枚举测试的场景
seed: 7
initial_state:
  time_of_day: "18:30"
  rooms:
    living_room:
      occupancy: false
timeline:
  - at: 0
    type: user.arrives_home
    user_id: user_01
    room_id: living_room
expected_device_effects:
  - device_id: light_living_01
    within_seconds: 5
    expected:
      power: true
involved_agents:
  - lighting_agent
success_criteria:
  require_complete_episode: true
ground_truth:
  user_goal: "arrival comfort"
  expected_intent: "arrival_comfort"
"""


def _write(directory: Path, scenario_id: str, name: str) -> None:
    (directory / f"{scenario_id}.yaml").write_text(
        _MINIMAL_SCENARIO.format(scenario_id=scenario_id, name=name), encoding="utf-8"
    )


def test_get_scenarios_enumerates_library(client, scenario_dirs):
    _write(scenario_dirs, "b_scenario", "B")
    _write(scenario_dirs, "a_scenario", "A")

    resp = client.get("/api/scenarios")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] == 2
    ids = [item["id"] for item in payload["scenarios"]]
    assert ids == ["a_scenario", "b_scenario"]  # 枚举顺序稳定，便于 diff

    first = payload["scenarios"][0]
    assert first["name"] == "A"
    assert first["seed"] == 7
    assert first["timeline_event_count"] == 1
    assert first["root_event_types"] == ["user.arrives_home"]
    assert first["has_ground_truth"] is True
    # §2.3：枚举投影不得泄露 ground truth 本体
    assert "ground_truth" not in first


def test_get_scenario_by_id_returns_full_spec(client, scenario_dirs):
    _write(scenario_dirs, "a_scenario", "A")
    resp = client.get("/api/scenarios/a_scenario")
    assert resp.status_code == 200
    spec = resp.json()["scenario"]
    assert spec["id"] == "a_scenario"
    assert spec["timeline"][0]["type"] == "user.arrives_home"
    assert spec["expected_device_effects"][0]["expected"]["power"]["equals"] is True
    # 单场景详情面向研究者，带 ground truth
    assert spec["ground_truth"]["expected_intent"] == "arrival_comfort"


def test_get_scenario_unknown_id_returns_404(client, scenario_dirs):
    resp = client.get("/api/scenarios/does_not_exist")
    assert resp.status_code == 404
    assert "does_not_exist" in str(resp.json())


def test_get_scenarios_reports_broken_yaml_as_structured_error(client, scenario_dirs):
    (scenario_dirs / "broken.yaml").write_text("id: x\n  name: [\n", encoding="utf-8")
    resp = client.get("/api/scenarios")
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_yaml"
    assert "broken.yaml" in detail["message"]


def test_get_scenarios_defaults_to_library_dir(client):
    """未配置时端点走 backend/scenarios/library/（S2-T8 填充后自动枚举 8 个）。"""
    assert routes_module.get_scenario_dirs() == list(DEFAULT_LIBRARY_DIRS)
    resp = client.get("/api/scenarios")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["count"] == len(payload["scenarios"])
