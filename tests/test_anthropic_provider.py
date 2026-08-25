from __future__ import annotations

import json

import httpx
import pytest

from backend.agents.llm import AnthropicCompatibleProvider, LLMProviderError
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
async def test_anthropic_provider_parses_messages_payload_and_code_fence_json():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/messages")
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["model"] == "MiniMax-M2.5"
        assert payload["max_tokens"] == 1200
        assert payload["messages"][0]["role"] == "user"
        assert payload["system"].startswith("You are a smart-home orchestration planner. Return strict JSON only.")
        assert payload["tool_choice"] == {"type": "auto"}
        assert payload["tools"][0]["name"] == "submit_agent_decision"
        return httpx.Response(
            200,
            json={
                "id": "msg_123",
                "type": "message",
                "role": "assistant",
                "model": "MiniMax-M2.5",
                "usage": {"input_tokens": 456, "output_tokens": 98},
                "content": [
                    {
                        "type": "text",
                        "text": "```json\n" + json.dumps(
                            {
                                "intent": "light occupied room",
                                "confidence": 0.93,
                                "task_steps": ["set brightness"],
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
                        ) + "\n```",
                    }
                ],
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        model="MiniMax-M2.5",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    decision = await provider.generate_decision(_request())

    assert decision.intent == "light occupied room"
    assert decision.proposed_commands[0].value == 70
    assert provider.last_usage == {"input_tokens": 456, "output_tokens": 98}


@pytest.mark.anyio
async def test_anthropic_provider_prefers_structured_tool_input():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "MiniMax-M3",
                "usage": {"input_tokens": 120, "output_tokens": 35},
                "content": [
                    {"type": "text", "text": "This text is intentionally not JSON."},
                    {
                        "type": "tool_use",
                        "name": "submit_agent_decision",
                        "id": "call_123",
                        "input": {
                            "intent": "light occupied room",
                            "confidence": 0.91,
                            "task_steps": ["set brightness"],
                            "proposed_commands": [
                                {
                                    "device_id": "light_living_01",
                                    "property": "extra.brightness",
                                    "value": 70,
                                    "reason": "occupied evening room",
                                }
                            ],
                            "explanation": "Evening occupancy needs comfortable lighting.",
                            "needs_coordination": False,
                        },
                    },
                ],
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        model="MiniMax-M3",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    decision = await provider.generate_decision(_request())

    assert decision.intent == "light occupied room"
    assert decision.proposed_commands[0].value == 70
    assert provider.last_usage == {"input_tokens": 120, "output_tokens": 35}
    assert provider.last_decision_transport == "tool_use"


@pytest.mark.anyio
async def test_anthropic_provider_drops_incomplete_tool_commands_without_guessing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "MiniMax-M3",
                "usage": {"input_tokens": 120, "output_tokens": 35},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_agent_decision",
                        "id": "call_123",
                        "input": {
                            "intent": "keep the occupied room comfortable",
                            "confidence": 0.9,
                            "task_steps": ["set brightness"],
                            "proposed_commands": [
                                {
                                    "device_id": "light_living_01",
                                    "property": "extra.brightness",
                                    "value": 70,
                                    "reason": "occupied evening room",
                                },
                                {
                                    "device_id": "light_bedroom_01",
                                    "property": "",
                                },
                            ],
                            "needs_coordination": False,
                        },
                    }
                ],
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        model="MiniMax-M3",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    decision = await provider.generate_decision(_request())

    assert [command.device_id for command in decision.proposed_commands] == [
        "light_living_01"
    ]
    assert decision.explanation == decision.intent


@pytest.mark.anyio
async def test_anthropic_provider_strict_research_mode_rejects_partial_tool_input():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "MiniMax-M3",
                "usage": {"input_tokens": 120, "output_tokens": 35},
                "content": [
                    {
                        "type": "tool_use",
                        "name": "submit_agent_decision",
                        "id": "call_123",
                        "input": {
                            "intent": "keep the room comfortable",
                            "confidence": 0.9,
                            "task_steps": ["set brightness"],
                            "proposed_commands": [
                                {
                                    "device_id": "light_living_01",
                                    "property": "extra.brightness",
                                    "value": 70,
                                }
                            ],
                            "explanation": "The room is occupied.",
                            "needs_coordination": False,
                        },
                    }
                ],
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        model="MiniMax-M3",
        base_url="https://api.minimax.io/anthropic",
        strict_output=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "invalid_output"
    assert provider.last_usage == {"input_tokens": 120, "output_tokens": 35}
    assert provider.last_decision_transport == "tool_use"


@pytest.mark.anyio
async def test_anthropic_provider_marks_billed_empty_strict_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "MiniMax-M3",
                "usage": {"input_tokens": 5, "output_tokens": 1},
                "content": [],
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        model="MiniMax-M3",
        base_url="https://api.minimax.io/anthropic",
        strict_output=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "invalid_output"
    assert provider.last_usage == {"input_tokens": 5, "output_tokens": 1}
    assert provider.last_response_model == "MiniMax-M3"
    assert provider.last_decision_transport == "empty"


@pytest.mark.anyio
async def test_anthropic_provider_clears_previous_usage_before_a_failed_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )
    provider.last_usage = {"input_tokens": 999, "output_tokens": 999}

    with pytest.raises(LLMProviderError):
        await provider.generate_decision(_request())

    assert provider.last_usage is None


def test_anthropic_provider_rejects_non_positive_output_cap():
    with pytest.raises(ValueError):
        AnthropicCompatibleProvider(api_key="test-key", max_tokens=0)


def test_minimax_provider_has_a_realistic_timeout_floor():
    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        model="MiniMax-M3",
        timeout_ms=1000,
        base_url="https://api.minimax.io/anthropic",
    )

    assert provider.timeout_ms == 45000


@pytest.mark.anyio
async def test_anthropic_provider_maps_timeout_to_timeout_reason():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "timeout"


@pytest.mark.anyio
async def test_anthropic_provider_timeout_uses_non_empty_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "timeout"
    assert str(exc_info.value)


@pytest.mark.anyio
async def test_anthropic_provider_maps_invalid_schema_to_invalid_output():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"intent": "x" * 257}),
                    }
                ]
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "invalid_output"


@pytest.mark.anyio
async def test_anthropic_provider_normalizes_common_command_aliases():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "commands": [
                                    {
                                        "device_id": "light_living_01",
                                        "property": "extra.brightness",
                                        "value": 65,
                                        "reason": "occupied evening room",
                                    }
                                ],
                                "reasoning": "The room is occupied in the evening and needs lighting.",
                            }
                        ),
                    }
                ]
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    decision = await provider.generate_decision(_request())

    assert decision.proposed_commands[0].value == 65
    assert decision.explanation == "The room is occupied in the evening and needs lighting."
    assert decision.needs_coordination is True


@pytest.mark.anyio
async def test_anthropic_provider_normalizes_percent_confidence():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "intent": "turn on living room light",
                                "confidence": 95,
                                "task_steps": ["turn on light"],
                                "proposed_commands": [
                                    {
                                        "device_id": "light_living_01",
                                        "property": "power",
                                        "value": True,
                                        "reason": "occupied room",
                                    }
                                ],
                                "explanation": "The room is occupied.",
                                "needs_coordination": False,
                            }
                        ),
                    }
                ]
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    decision = await provider.generate_decision(_request())

    assert decision.confidence == 0.95


@pytest.mark.anyio
async def test_anthropic_provider_invalid_output_exposes_preview():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": "Sorry, I cannot follow the requested JSON schema for this answer.",
                    }
                ]
            },
        )

    provider = AnthropicCompatibleProvider(
        api_key="test-key",
        base_url="https://api.minimax.io/anthropic",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMProviderError) as exc_info:
        await provider.generate_decision(_request())

    assert exc_info.value.reason == "invalid_output"
    assert "Sorry, I cannot follow" in str(exc_info.value)
