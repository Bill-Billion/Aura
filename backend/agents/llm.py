from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from backend.core.logging import log
from backend.agents.types import AgentLLMDecision, LLMDecisionRequest

DEFAULT_AGENT_SYSTEM_PROMPT = (
    "You are a smart-home orchestration planner. "
    "Return strict JSON only. "
    "Be concise. Keep intent short, keep explanation to one short sentence, "
    "and keep task_steps to at most 3 short items."
)

AGENT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "intent",
        "confidence",
        "task_steps",
        "proposed_commands",
        "explanation",
        "needs_coordination",
    ],
    "properties": {
        "intent": {"type": "string"},
        "confidence": {"type": "number"},
        "task_steps": {
            "type": "array",
            "items": {"type": "string"},
        },
        "proposed_commands": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["device_id", "property", "value", "reason"],
                "properties": {
                    "device_id": {"type": "string"},
                    "property": {"type": "string"},
                    "value": {},
                    "reason": {"type": "string"},
                },
            },
        },
        "explanation": {"type": "string"},
        "needs_coordination": {"type": "boolean"},
    },
}

JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
ANTHROPIC_OUTPUT_INSTRUCTION = (
    "Return one JSON object only. "
    "The object must use exactly these top-level keys: "
    "intent, confidence, task_steps, proposed_commands, explanation, needs_coordination. "
    "Each proposed_commands item must include device_id, property, value, reason. "
    "Keep the JSON compact and only include commands that are necessary right now. "
    "Do not wrap the JSON in markdown."
)


class LLMProviderError(RuntimeError):
    def __init__(
        self,
        reason: str,
        message: str,
        *,
        raw_output_preview: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.raw_output_preview = raw_output_preview


class LLMProvider(ABC):
    provider_name = "unknown"
    model = ""

    @abstractmethod
    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        raise NotImplementedError


class OpenAIResponsesProvider(LLMProvider):
    provider_name = "openai_responses"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "gpt-5.4",
        reasoning_effort: str = "medium",
        timeout_ms: int = 12000,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_ms = timeout_ms
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    @classmethod
    def from_env(cls) -> "OpenAIResponsesProvider":
        return cls(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
            reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "medium"),
            timeout_ms=int(os.getenv("LLM_TIMEOUT_MS", "12000")),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        if not self.api_key:
            raise LLMProviderError("provider_error", "OPENAI_API_KEY 未配置")

        payload = {
            "model": self.model,
            "reasoning": {"effort": self.reasoning_effort},
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": DEFAULT_AGENT_SYSTEM_PROMPT,
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(
                                build_compact_request_payload(request),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    ],
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "agent_decision",
                    "strict": True,
                    "schema": AGENT_DECISION_SCHEMA,
                }
            },
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout_ms / 1000,
                transport=self.transport,
            ) as client:
                response = await client.post("/responses", json=payload)
                response.raise_for_status()
        except httpx.ReadTimeout as exc:
            raise build_timeout_error(
                provider_name=self.provider_name,
                timeout_ms=self.timeout_ms,
                exc=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError("provider_error", str(exc)) from exc

        text = self._extract_text(response.json())
        if not text:
            raise LLMProviderError("invalid_output", "Responses API 返回空文本")

        try:
            return parse_agent_decision_text(text)
        except ValueError as exc:
            raise build_invalid_output_error(str(exc), text=text, provider_name=self.provider_name) from exc

    @staticmethod
    def _extract_text(payload: dict) -> str:
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
        return ""


class AnthropicCompatibleProvider(LLMProvider):
    provider_name = "anthropic_compatible"

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "MiniMax-M2.7",
        timeout_ms: int = 12000,
        max_tokens: int = 1200,
        base_url: str = "https://api.minimaxi.com/anthropic",
        anthropic_version: str = "2023-06-01",
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_ms = self._effective_timeout_ms(timeout_ms, base_url=base_url, model=model)
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self.anthropic_version = anthropic_version
        self.transport = transport

    @classmethod
    def from_env(cls) -> "AnthropicCompatibleProvider":
        api_key = (
            os.getenv("ANTHROPIC_COMPAT_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("ANTHROPIC_AUTH_TOKEN")
        )
        return cls(
            api_key=api_key,
            model=os.getenv("ANTHROPIC_MODEL", os.getenv("ANTHROPIC_COMPAT_MODEL", "MiniMax-M2.7")),
            timeout_ms=int(os.getenv("LLM_TIMEOUT_MS", "12000")),
            max_tokens=int(os.getenv("ANTHROPIC_MAX_TOKENS", "1200")),
            base_url=os.getenv("ANTHROPIC_BASE_URL", os.getenv("ANTHROPIC_COMPAT_BASE_URL", "https://api.minimaxi.com/anthropic")),
            anthropic_version=os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
        )

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        if not self.api_key:
            raise LLMProviderError("provider_error", "ANTHROPIC_COMPAT_API_KEY 未配置")

        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": f"{DEFAULT_AGENT_SYSTEM_PROMPT} {ANTHROPIC_OUTPUT_INSTRUCTION}",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Plan the smart-home response from this request payload.\n"
                                f"{json.dumps(build_compact_request_payload(request), ensure_ascii=False, separators=(',', ':'))}"
                            ),
                        }
                    ],
                }
            ],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.anthropic_version,
            "content-type": "application/json",
        }

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout_ms / 1000,
                transport=self.transport,
            ) as client:
                response = await client.post(self._messages_path(), json=payload)
                response.raise_for_status()
        except httpx.ReadTimeout as exc:
            raise build_timeout_error(
                provider_name=self.provider_name,
                timeout_ms=self.timeout_ms,
                exc=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError("provider_error", str(exc)) from exc

        text = self._extract_text(response.json())
        if not text:
            raise LLMProviderError("invalid_output", "Anthropic-compatible API 返回空文本")

        try:
            return parse_agent_decision_text(text)
        except ValueError as exc:
            raise build_invalid_output_error(str(exc), text=text, provider_name=self.provider_name) from exc

    def _messages_path(self) -> str:
        # 兼容 base_url 既可能是 .../anthropic，也可能已经带了 /v1。
        if self.base_url.endswith("/v1"):
            return "/messages"
        return "/v1/messages"

    @staticmethod
    def _effective_timeout_ms(timeout_ms: int, *, base_url: str, model: str) -> int:
        normalized_base = base_url.lower()
        normalized_model = model.lower()
        if "minimax" in normalized_base or normalized_model.startswith("minimax"):
            # 设计意图：MiniMax 在 Anthropic 兼容路径下首字节波动明显，
            # 给它一个略高于默认值的超时下限，避免实机联调时频繁假性 timeout。
            return max(timeout_ms, 15000)
        return timeout_ms

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        contents = payload.get("content")
        if not isinstance(contents, list):
            return ""

        text_chunks: list[str] = []
        for item in contents:
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_chunks.append(item["text"])
        return "\n".join(text_chunks).strip()


def parse_agent_decision_text(text: str) -> AgentLLMDecision:
    cleaned = _strip_json_fence(text)
    raw_payload: dict[str, Any]
    try:
        raw_payload = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError("模型返回的不是合法 JSON")
        raw_payload = json.loads(cleaned[start : end + 1])
    return AgentLLMDecision.model_validate(_normalize_agent_decision_payload(raw_payload))


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    match = JSON_FENCE_RE.match(cleaned)
    if match:
        return match.group(1).strip()
    return cleaned


def _normalize_agent_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    if "proposed_commands" not in normalized and isinstance(normalized.get("commands"), list):
        normalized["proposed_commands"] = normalized["commands"]

    explanation = normalized.get("explanation")
    if not isinstance(explanation, str) or not explanation.strip():
        for alias in ("reasoning", "summary", "analysis"):
            alias_value = normalized.get(alias)
            if isinstance(alias_value, str) and alias_value.strip():
                normalized["explanation"] = alias_value.strip()
                break

    if "task_steps" not in normalized or not isinstance(normalized.get("task_steps"), list):
        if isinstance(normalized.get("proposed_commands"), list) and normalized["proposed_commands"]:
            normalized["task_steps"] = [
                f"set {command.get('device_id', 'device')} {command.get('property', 'state')}"
                for command in normalized["proposed_commands"]
                if isinstance(command, dict)
            ] or ["apply proposed commands"]
        else:
            normalized["task_steps"] = ["review current context"]

    if "needs_coordination" not in normalized:
        normalized["needs_coordination"] = bool(normalized.get("proposed_commands"))

    if "confidence" not in normalized:
        normalized["confidence"] = 0.6 if normalized.get("proposed_commands") else 0.4
    else:
        confidence = normalized["confidence"]
        if isinstance(confidence, str):
            confidence = confidence.strip().removesuffix("%")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError):
            confidence_value = 0.6 if normalized.get("proposed_commands") else 0.4
        if confidence_value > 1.0 and confidence_value <= 100.0:
            confidence_value = confidence_value / 100.0
        normalized["confidence"] = max(0.0, min(confidence_value, 1.0))

    if "intent" not in normalized or not isinstance(normalized.get("intent"), str) or not normalized["intent"].strip():
        if isinstance(normalized.get("explanation"), str) and normalized["explanation"].strip():
            normalized["intent"] = normalized["explanation"].strip()
        elif normalized.get("proposed_commands"):
            normalized["intent"] = "execute proposed device commands"
        else:
            normalized["intent"] = "no device changes needed"

    return normalized


def build_compact_request_payload(request: LLMDecisionRequest) -> dict[str, Any]:
    payload = request.model_dump()
    if payload.get("recent_events"):
        payload["recent_events"] = payload["recent_events"][-4:]
    return payload


def build_invalid_output_error(
    message: str,
    *,
    text: str,
    provider_name: str,
) -> LLMProviderError:
    preview = build_output_preview(text)
    log.warning(
        "llm_invalid_output",
        provider=provider_name,
        preview=preview,
    )
    return LLMProviderError(
        "invalid_output",
        f"{message}; preview={preview}",
        raw_output_preview=preview,
    )


def build_timeout_error(
    *,
    provider_name: str,
    timeout_ms: int,
    exc: httpx.ReadTimeout,
) -> LLMProviderError:
    message = str(exc).strip() or f"{provider_name} request timed out after {timeout_ms}ms"
    return LLMProviderError("timeout", message)


def build_output_preview(text: str, limit: int = 240) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."
