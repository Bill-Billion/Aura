from __future__ import annotations

import json

import httpx
import pytest

from backend.agents.llm import LLMProviderError, OpenAIResponsesProvider
from backend.agents.types import LLMDecisionRequest


def _request() -> LLMDecisionRequest:
    return LLMDecisionRequest(
        agent_id="lighting_agent",
        agent_name="Lighting Agent",
        root_event_type="user.activity_change",
        world_summary="living room became occupied at 19:00",
        recent_events=["user moved from entry to living_room"],
        available_devices=[
            {
                "device_id": "light_living_01",
                "type": "light",
                "room": "living_room",
                "state": {"brightness": 0, "color_temp": 4000},
            }
        ],
        allowed_commands=[
            {
                "device_id": "light_living_01",
                "property": "extra.brightness",
            }
        ],
    )


@pytest.mark.anyio
async def test_openai_provider_parses_responses_api_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "gpt-5.4"
        assert payload["reasoning"]["effort"] == "medium"
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "intent": "light occupied room",
                                        "confidence": 0.92,
                                        "task_steps": ["set brightness", "warm color"],
                                        "proposed_commands": [
                                            {
                                                "device_id": "light_living_01",
                                                "property": "extra.brightness",
                                                "value": 70,
                                                "reason": "occupied evening room",
                                            }
                                        ],
                                        "explanation": "Evening occupancy needs comfortable lighting",
                                        "needs_coordination": False,
                                    }
                                ),
                            }
                        ]
                    }
                ]
            },
        )

    provider = OpenAIResponsesProvider(
        api_key="test-key",
        model="gpt-5.4",
        reasoning_effort="medium",
        transport=httpx.MockTransport(handler),
    )

    decision = await provider.generate_decision(_request())

    assert decision.intent == "light occupied room"
    assert decision.proposed_commands[0].value == 70


@pytest.mark.anyio
async def test_openai_provider_maps_timeout_to_timeout_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = OpenAIResponsesProvider(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "timeout"


@pytest.mark.anyio
async def test_openai_provider_timeout_uses_non_empty_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    provider = OpenAIResponsesProvider(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "timeout"
    assert str(exc_info.value)


@pytest.mark.anyio
async def test_openai_provider_maps_invalid_schema_to_invalid_output():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"intent": "missing fields"}),
                            }
                        ]
                    }
                ]
            },
        )

    provider = OpenAIResponsesProvider(
        api_key="test-key",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "invalid_output"
