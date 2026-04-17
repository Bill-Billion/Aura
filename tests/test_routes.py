"""Tests for REST API endpoints."""

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


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
    assert resp.json() == {"status": "ok"}
