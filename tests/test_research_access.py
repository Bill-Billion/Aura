"""Security boundary for browser control and server-owned paid providers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.agents.llm_modes import live_llm_allowed
from backend.api.access_control import (
    ResearchAccessError,
    authorize_run_launch,
    configured_allowed_origins,
    origin_is_trusted,
)
import backend.main as main_module
from backend.main import app
from backend.models.schemas import BaselinePolicy, RunScenarioPayload


SCENARIO_ID = "safety_smoke_kitchen"


def _payload(
    policy: BaselinePolicy,
    *,
    source: str | None = None,
) -> RunScenarioPayload:
    return RunScenarioPayload(
        scenario_id=SCENARIO_ID,
        seed=20260820,
        baseline_policy=policy,
        recording_source_run_id=source,
    )


def test_live_provider_is_disabled_without_explicit_opt_in_in_every_environment():
    assert live_llm_allowed({}) is False
    assert live_llm_allowed({"PYTEST_CURRENT_TEST": "case"}) is False
    assert live_llm_allowed({"AURA_ALLOW_LIVE_LLM": "1"}) is True
    assert live_llm_allowed({"AURA_ALLOW_LIVE_LLM": "true"}) is True


def test_run_payload_defaults_to_zero_cost_rule_based_policy():
    payload = RunScenarioPayload(scenario_id=SCENARIO_ID)
    assert payload.baseline_policy is BaselinePolicy.RULE_BASED


def test_origin_allowlist_is_local_by_default_and_rejects_wildcards():
    assert origin_is_trusted("http://localhost:5173", {})
    assert origin_is_trusted("http://127.0.0.1:4173", {})
    assert origin_is_trusted(None, {})  # curl/CLI, not a browser origin
    assert not origin_is_trusted("null", {})
    assert not origin_is_trusted("https://evil.example", {})
    assert origin_is_trusted(
        "https://research.example",
        {"AURA_ALLOWED_ORIGINS": "https://research.example"},
    )
    with pytest.raises(ValueError, match="never '\\*'"):
        configured_allowed_origins({"AURA_ALLOWED_ORIGINS": "*"})


def test_paid_launch_requires_opt_in_and_remote_bearer_authorization():
    paid = _payload(BaselinePolicy.LLM_LIVE)

    with pytest.raises(ResearchAccessError) as disabled:
        authorize_run_launch(paid, headers={}, client_host="127.0.0.1", env={})
    assert disabled.value.status_code == 503
    assert disabled.value.details["reason_code"] == "live_llm_disabled"

    authorize_run_launch(
        paid,
        headers={"origin": "http://localhost:5173", "host": "127.0.0.1:8000"},
        client_host="127.0.0.1",
        env={"AURA_ALLOW_LIVE_LLM": "1"},
    )

    remote_env = {
        "AURA_ALLOW_LIVE_LLM": "1",
        "AURA_ALLOWED_ORIGINS": "https://research.example",
        "AURA_RESEARCH_WRITE_TOKEN": "a-secure-research-token-which-is-long",
    }
    with pytest.raises(ResearchAccessError) as unauthorized:
        authorize_run_launch(
            paid,
            headers={"origin": "https://research.example"},
            client_host="203.0.113.8",
            env=remote_env,
        )
    assert unauthorized.value.code == "research_write_unauthorized"

    # A same-host reverse proxy makes the ASGI peer look loopback.  A remote
    # browser Origin must still prove the bearer capability.
    with pytest.raises(ResearchAccessError) as proxied_unauthorized:
        authorize_run_launch(
            paid,
            headers={
                "origin": "https://research.example",
                "host": "research.example",
            },
            client_host="127.0.0.1",
            env=remote_env,
        )
    assert proxied_unauthorized.value.code == "research_write_unauthorized"

    authorize_run_launch(
        paid,
        headers={
            "origin": "https://research.example",
            "authorization": "Bearer a-secure-research-token-which-is-long",
        },
        client_host="203.0.113.8",
        env=remote_env,
    )


def test_recorded_replay_is_not_treated_as_a_paid_capture():
    replay = _payload(
        BaselinePolicy.LLM_RECORDED,
        source="run-20260820T120000-aaaaaaaa",
    )
    authorize_run_launch(replay, headers={}, client_host="203.0.113.8", env={})


def test_rest_and_websocket_reject_untrusted_browser_origins_before_mutation():
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            headers={"origin": "https://evil.example"},
            json={"scenario_id": SCENARIO_ID, "baseline_policy": "rule_based"},
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "origin_not_allowed"

        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                "/ws/simulation",
                headers={"origin": "https://evil.example"},
            ):
                pass
        assert denied.value.code == 1008
        assert denied.value.reason == "origin_not_allowed"


def test_local_browser_origin_remains_usable_for_free_research_runs():
    with TestClient(app) as client:
        response = client.post(
            "/api/runs",
            headers={"origin": "http://localhost:5173"},
            json={
                "scenario_id": SCENARIO_ID,
                "seed": 20260820,
                "baseline_policy": "rule_based",
            },
        )
        assert response.status_code == 201, response.text


@pytest.mark.anyio
async def test_remote_allowlisted_ws_cannot_trigger_paid_ambient_provider_without_token(
    monkeypatch,
):
    monkeypatch.setenv("AURA_ALLOW_LIVE_LLM", "1")
    monkeypatch.setenv("AURA_ALLOWED_ORIGINS", "https://research.example")
    monkeypatch.setenv("AURA_RESEARCH_WRITE_TOKEN", "server-capability-token")
    monkeypatch.setenv("LLM_MODE", "live")
    monkeypatch.setenv("LLM_PROVIDER", "openai_responses")
    monkeypatch.setenv("OPENAI_API_KEY", "server-owned-test-key")

    class RemoteProxySocket:
        headers = {
            "origin": "https://research.example",
            "host": "research.example",
        }
        # Same-host reverse proxy: the ASGI peer alone looks local.
        client = SimpleNamespace(host="127.0.0.1")

    sent = []

    async def capture_send(_ws, message):
        sent.append(message)
        return True

    monkeypatch.setattr(main_module.manager, "send", capture_send)
    main_module._scenario_launch_lock = asyncio.Lock()
    async with main_module.lifespan(app):
        engine = main_module.simulation_engine
        assert engine is not None
        current = engine.run_manager.current
        assert current is not None
        assert current.baseline_policy is BaselinePolicy.LLM_LIVE
        assert engine.is_running is False

        await main_module._handle_ws_message(
            RemoteProxySocket(),
            {"type": "CMD_SIM_START", "payload": {}},
        )

        assert engine.is_running is False
        assert sent
        assert sent[-1].type == "ERROR"
        assert sent[-1].payload["code"] == "research_write_unauthorized"
