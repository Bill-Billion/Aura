from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any

import httpx

from backend.core.logging import log
from backend.agents.llm_pricing import DEFAULT_MAX_OUTPUT_TOKENS
from backend.agents.types import (
    MAX_COMMAND_REASON_CHARS,
    MAX_DEVICE_ID_CHARS,
    MAX_EXPLANATION_CHARS,
    MAX_INTENT_CHARS,
    MAX_PROPERTY_CHARS,
    MAX_PROPOSED_COMMANDS,
    MAX_TASK_STEP_CHARS,
    MAX_TASK_STEPS,
    AgentLLMDecision,
    LLMDecisionRequest,
)

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
        "intent": {"type": "string", "maxLength": MAX_INTENT_CHARS},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "task_steps": {
            "type": "array",
            "maxItems": MAX_TASK_STEPS,
            "items": {"type": "string", "maxLength": MAX_TASK_STEP_CHARS},
        },
        "proposed_commands": {
            "type": "array",
            "maxItems": MAX_PROPOSED_COMMANDS,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["device_id", "property", "value", "reason"],
                "properties": {
                    "device_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_DEVICE_ID_CHARS,
                    },
                    "property": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_PROPERTY_CHARS,
                    },
                    "value": {},
                    "reason": {"type": "string", "maxLength": MAX_COMMAND_REASON_CHARS},
                },
            },
        },
        "explanation": {"type": "string", "maxLength": MAX_EXPLANATION_CHARS},
        "needs_coordination": {"type": "boolean"},
    },
}
AGENT_DECISION_SCHEMA_SHA256 = hashlib.sha256(
    json.dumps(
        AGENT_DECISION_SCHEMA,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
_STRICT_DECISION_KEYS = frozenset(AGENT_DECISION_SCHEMA["required"])
_STRICT_COMMAND_KEYS = frozenset({"device_id", "property", "value", "reason"})

JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
ANTHROPIC_OUTPUT_INSTRUCTION = (
    "Call the submit_agent_decision tool exactly once. "
    "Its input must use exactly these top-level keys: "
    "intent, confidence, task_steps, proposed_commands, explanation, needs_coordination. "
    "Each proposed_commands item must include device_id, property, value, reason. "
    "Keep the JSON compact and only include commands that are necessary right now. "
    "Do not answer with prose."
)
ANTHROPIC_DECISION_TOOL = "submit_agent_decision"


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
    # §11.1 三模式里的哪一种。裸 provider 一律 "live"（它真的会打网）；
    # backend/agents/llm_modes.py 的三层包装会覆盖成 mocked / recorded。
    # 声明在基类而不是在包装层单独判 isinstance：run 元数据与 /api/health 只需问
    # 一个属性，就不会出现"接了新 provider 忘了登记模式"这种静默错标。
    llm_mode = "live"

    @property
    def last_usage(self) -> Any:
        """Usage reported by the current task's most recent provider call.

        Providers are shared by the orchestrator's concurrently-running domain
        agents.  A normal instance attribute lets one task overwrite another
        task's telemetry between the provider await and budget settlement.  A
        per-instance ``ContextVar`` preserves the existing provider API while
        binding each usage report to the task that made the call.
        """

        context = self.__dict__.get("_last_usage_context")
        if context is None:
            return None
        return context.get()

    @last_usage.setter
    def last_usage(self, value: Any) -> None:
        context = self.__dict__.get("_last_usage_context")
        if context is None:
            context = ContextVar(
                f"llm_last_usage_{type(self).__name__}_{id(self)}",
                default=None,
            )
            self.__dict__["_last_usage_context"] = context
        context.set(value)

    @property
    def last_decision_transport(self) -> str | None:
        """Structured-response transport used by the current task's call."""

        context = self.__dict__.get("_last_decision_transport_context")
        if context is None:
            return None
        return context.get()

    @last_decision_transport.setter
    def last_decision_transport(self, value: str | None) -> None:
        context = self.__dict__.get("_last_decision_transport_context")
        if context is None:
            context = ContextVar(
                f"llm_last_decision_transport_{type(self).__name__}_{id(self)}",
                default=None,
            )
            self.__dict__["_last_decision_transport_context"] = context
        context.set(value)

    @property
    def last_response_model(self) -> str | None:
        """Actual model identifier reported by the current task's response."""

        context = self.__dict__.get("_last_response_model_context")
        if context is None:
            return None
        return context.get()

    @last_response_model.setter
    def last_response_model(self, value: str | None) -> None:
        context = self.__dict__.get("_last_response_model_context")
        if context is None:
            context = ContextVar(
                f"llm_last_response_model_{type(self).__name__}_{id(self)}",
                default=None,
            )
            self.__dict__["_last_response_model_context"] = context
        context.set(value)

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
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        base_url: str = "https://api.openai.com/v1",
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens 必须为正整数")
        self.api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_ms = timeout_ms
        self.max_output_tokens = max_output_tokens
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.last_usage: Any = None

    @property
    def max_tokens(self) -> int:
        """Budget wrapper compatibility; Responses names this max_output_tokens."""

        return self.max_output_tokens

    @classmethod
    def from_env(cls) -> "OpenAIResponsesProvider":
        return cls(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
            reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "medium"),
            timeout_ms=int(os.getenv("LLM_TIMEOUT_MS", "12000")),
            max_output_tokens=int(
                os.getenv("OPENAI_MAX_OUTPUT_TOKENS", str(DEFAULT_MAX_OUTPUT_TOKENS))
            ),
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        # A failed/timeout request must never inherit the preceding call's bill.
        self.last_usage = None
        if not self.api_key:
            raise LLMProviderError("provider_error", "OPENAI_API_KEY 未配置")

        payload = {
            "model": self.model,
            "max_output_tokens": self.max_output_tokens,
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

        response_payload = response.json()
        self.last_usage = response_payload.get("usage")
        text = self._extract_text(response_payload)
        if not text:
            raise LLMProviderError("invalid_output", "Responses API 返回空文本")

        try:
            return parse_agent_decision_text_strict(text)
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
        model: str = "MiniMax-M3",
        timeout_ms: int = 12000,
        max_tokens: int = 1200,
        base_url: str = "https://api.minimaxi.com/anthropic",
        anthropic_version: str = "2023-06-01",
        strict_output: bool = False,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须为正整数")
        self.api_key = api_key
        self.model = model
        self.timeout_ms = self._effective_timeout_ms(timeout_ms, base_url=base_url, model=model)
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")
        self.anthropic_version = anthropic_version
        self.strict_output = bool(strict_output)
        self.decision_schema_sha256 = AGENT_DECISION_SCHEMA_SHA256
        self.transport = transport
        self.last_usage: Any = None
        self.last_decision_transport: str | None = None
        self.last_response_model: str | None = None

    @classmethod
    def from_env(cls, *, strict_output: bool = False) -> "AnthropicCompatibleProvider":
        api_key = (
            os.getenv("ANTHROPIC_COMPAT_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("ANTHROPIC_AUTH_TOKEN")
        )
        return cls(
            api_key=api_key,
            model=os.getenv("ANTHROPIC_MODEL", os.getenv("ANTHROPIC_COMPAT_MODEL", "MiniMax-M3")),
            timeout_ms=int(os.getenv("LLM_TIMEOUT_MS", "12000")),
            max_tokens=int(os.getenv("ANTHROPIC_MAX_TOKENS", "1200")),
            base_url=os.getenv("ANTHROPIC_BASE_URL", os.getenv("ANTHROPIC_COMPAT_BASE_URL", "https://api.minimaxi.com/anthropic")),
            anthropic_version=os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
            strict_output=strict_output,
        )

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        # Clear before every attempt so timeout/HTTP errors cannot reuse old usage.
        self.last_usage = None
        self.last_decision_transport = None
        self.last_response_model = None
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
            "tools": [
                {
                    "name": ANTHROPIC_DECISION_TOOL,
                    "description": "Submit the bounded smart-home decision.",
                    "input_schema": AGENT_DECISION_SCHEMA,
                }
            ],
            # MiniMax's Anthropic-compatible API currently documents only
            # auto/none.  With one tool and the system instruction above, tool
            # input is the preferred structured path; the text parser below is
            # retained for compatible providers that still answer in JSON text.
            "tool_choice": {"type": "auto"},
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

        response_payload = response.json()
        self.last_usage = response_payload.get("usage")
        response_model = response_payload.get("model")
        self.last_response_model = (
            response_model if isinstance(response_model, str) else None
        )
        if self.strict_output and self.last_response_model != self.model:
            raise LLMProviderError(
                "provider_error",
                "strict research response model does not match the frozen model: "
                f"expected={self.model!r} actual={self.last_response_model!r}",
            )
        tool_calls = [
            item
            for item in response_payload.get("content", [])
            if isinstance(item, dict) and item.get("type") == "tool_use"
        ]
        if self.strict_output and tool_calls:
            self.last_decision_transport = "tool_use"
            if (
                len(tool_calls) != 1
                or tool_calls[0].get("name") != ANTHROPIC_DECISION_TOOL
                or not isinstance(tool_calls[0].get("input"), dict)
            ):
                raise build_invalid_output_error(
                    "strict research response must contain exactly one decision tool call",
                    text=json.dumps(tool_calls, ensure_ascii=False),
                    provider_name=self.provider_name,
                )
            tool_input = dict(tool_calls[0]["input"])
        else:
            tool_input = self._extract_tool_input(response_payload)
        if tool_input is not None:
            self.last_decision_transport = "tool_use"
            try:
                decision = (
                    parse_agent_decision_payload_strict(tool_input)
                    if self.strict_output
                    else AgentLLMDecision.model_validate(
                        _normalize_agent_decision_payload(tool_input)
                    )
                )
            except ValueError as exc:
                raise build_invalid_output_error(
                    str(exc),
                    text=json.dumps(tool_input, ensure_ascii=False),
                    provider_name=self.provider_name,
                ) from exc
            return decision
        text = self._extract_text(response_payload)
        if not text:
            self.last_decision_transport = "empty"
            raise LLMProviderError(
                "invalid_output",
                "Anthropic-compatible API 未返回决策 tool_use 或文本",
            )
        self.last_decision_transport = "text_json"

        try:
            decision = (
                parse_agent_decision_text_strict(text)
                if self.strict_output
                else parse_agent_decision_text(text)
            )
        except ValueError as exc:
            raise build_invalid_output_error(str(exc), text=text, provider_name=self.provider_name) from exc
        return decision

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
            return max(timeout_ms, 45000)
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

    @staticmethod
    def _extract_tool_input(payload: dict[str, Any]) -> dict[str, Any] | None:
        contents = payload.get("content")
        if not isinstance(contents, list):
            return None
        for item in contents:
            if (
                isinstance(item, dict)
                and item.get("type") == "tool_use"
                and item.get("name") == ANTHROPIC_DECISION_TOOL
                and isinstance(item.get("input"), dict)
            ):
                return dict(item["input"])
        return None


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


def parse_agent_decision_text_strict(text: str) -> AgentLLMDecision:
    """Parse an exact research response without compatibility repair."""

    try:
        payload = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("模型返回的不是完整合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("模型决策必须是 JSON object")
    return parse_agent_decision_payload_strict(payload)


def parse_agent_decision_payload_strict(
    payload: dict[str, Any],
) -> AgentLLMDecision:
    """Validate the frozen decision schema without dropping or inventing data."""

    if set(payload) != _STRICT_DECISION_KEYS:
        raise ValueError("模型决策字段与冻结 schema 不完全一致")
    commands = payload.get("proposed_commands")
    if not isinstance(commands, list):
        raise ValueError("proposed_commands 必须是数组")
    for command in commands:
        if not isinstance(command, dict) or set(command) != _STRICT_COMMAND_KEYS:
            raise ValueError("命令字段与冻结 schema 不完全一致")
        if not str(command.get("device_id", "")).strip() or not str(
            command.get("property", "")
        ).strip():
            raise ValueError("命令 device_id/property 不能为空")
    return AgentLLMDecision.model_validate(payload, strict=True)


def _strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    match = JSON_FENCE_RE.match(cleaned)
    if match:
        return match.group(1).strip()
    return cleaned


def _normalize_agent_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    # This field is evidence produced by Aura's strict research wrapper, never
    # something a model may assert about its own response.
    normalized.pop("provider_failure_reason", None)
    if "proposed_commands" not in normalized and isinstance(normalized.get("commands"), list):
        normalized["proposed_commands"] = normalized["commands"]

    commands = normalized.get("proposed_commands")
    if not isinstance(commands, list):
        normalized["proposed_commands"] = []
    else:
        valid_commands: list[dict[str, Any]] = []
        for command in commands:
            if not isinstance(command, dict):
                continue
            device_id = command.get("device_id")
            property_name = command.get("property")
            if (
                not isinstance(device_id, str)
                or not device_id.strip()
                or not isinstance(property_name, str)
                or not property_name.strip()
                or "value" not in command
            ):
                continue
            cleaned_command = dict(command)
            cleaned_command["device_id"] = device_id.strip()
            cleaned_command["property"] = property_name.strip()
            if not isinstance(cleaned_command.get("reason"), str):
                cleaned_command["reason"] = ""
            valid_commands.append(cleaned_command)
        normalized["proposed_commands"] = valid_commands

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
                for command in normalized["proposed_commands"][:MAX_TASK_STEPS]
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

    if not isinstance(normalized.get("explanation"), str) or not normalized["explanation"].strip():
        normalized["explanation"] = normalized["intent"]

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
        validation_error=message,
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
