from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# These bounds are part of the paid-provider boundary, not presentation hints.
# They keep a malformed/adversarial structured response from turning one accepted
# decision into an effectively unbounded bill or trace artifact.
MAX_AGENT_ID_CHARS = 128
MAX_AGENT_NAME_CHARS = 160
MAX_EVENT_TYPE_CHARS = 160
MAX_WORLD_SUMMARY_CHARS = 16_384
MAX_RECENT_EVENTS = 32
MAX_RECENT_EVENT_CHARS = 2_048
MAX_AVAILABLE_DEVICES = 128
MAX_ALLOWED_COMMANDS = 256
MAX_REQUEST_JSON_BYTES = 256 * 1_024

MAX_INTENT_CHARS = 256
MAX_EXPLANATION_CHARS = 2_048
MAX_TASK_STEPS = 8
MAX_TASK_STEP_CHARS = 512
MAX_PROPOSED_COMMANDS = 32
MAX_DEVICE_ID_CHARS = 256
MAX_PROPERTY_CHARS = 256
MAX_COMMAND_REASON_CHARS = 512
MAX_DECISION_JSON_BYTES = 32 * 1_024


PriorityLabel = Literal[
    "direct_user_command",
    "safety",
    "user_comfort",
    "convenience",
    "energy_efficiency",
    "background_optimization",
]


class AgentCommandProposal(BaseModel):
    device_id: str = Field(max_length=MAX_DEVICE_ID_CHARS)
    property: str = Field(max_length=MAX_PROPERTY_CHARS)
    value: Any
    reason: str = Field(default="", max_length=MAX_COMMAND_REASON_CHARS)


class RawCommandAssessment(BaseModel):
    command_index: int = Field(ge=0)
    admitted_by_agent: bool
    valid_at_plan_time: bool
    failure_code: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "RawCommandAssessment":
        expected_valid = self.admitted_by_agent and self.failure_code is None
        if self.valid_at_plan_time is not expected_valid:
            raise ValueError("raw command assessment is internally inconsistent")
        if not self.admitted_by_agent and self.failure_code != "agent_whitelist_rejected":
            raise ValueError("non-admitted raw command must record whitelist rejection")
        return self


class AgentLLMDecision(BaseModel):
    intent: str = Field(max_length=MAX_INTENT_CHARS)
    confidence: float = Field(ge=0.0, le=1.0)
    task_steps: list[str] = Field(default_factory=list, max_length=MAX_TASK_STEPS)
    proposed_commands: list[AgentCommandProposal] = Field(
        default_factory=list,
        max_length=MAX_PROPOSED_COMMANDS,
    )
    explanation: str = Field(max_length=MAX_EXPLANATION_CHARS)
    needs_coordination: bool = False
    # Internal research marker. Provider-facing schemas never expose this key;
    # only the strict substudy wrapper may set it after a paid invalid response.
    provider_failure_reason: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_bounded_payload(self) -> "AgentLLMDecision":
        if any(len(step) > MAX_TASK_STEP_CHARS for step in self.task_steps):
            raise ValueError(f"task_steps 每项不能超过 {MAX_TASK_STEP_CHARS} 个字符")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_DECISION_JSON_BYTES:
            raise ValueError(f"LLM 决策 JSON 不能超过 {MAX_DECISION_JSON_BYTES} 字节")
        return self


class LLMDecisionRequest(BaseModel):
    decision_role: Literal["domain_agent", "home_orchestrator"] = "domain_agent"
    agent_id: str = Field(max_length=MAX_AGENT_ID_CHARS)
    agent_name: str = Field(max_length=MAX_AGENT_NAME_CHARS)
    root_event_type: str = Field(max_length=MAX_EVENT_TYPE_CHARS)
    world_summary: str = Field(max_length=MAX_WORLD_SUMMARY_CHARS)
    recent_events: list[str] = Field(default_factory=list, max_length=MAX_RECENT_EVENTS)
    available_devices: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_AVAILABLE_DEVICES,
    )
    allowed_commands: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=MAX_ALLOWED_COMMANDS,
    )

    @model_validator(mode="after")
    def validate_bounded_payload(self) -> "LLMDecisionRequest":
        if any(len(event) > MAX_RECENT_EVENT_CHARS for event in self.recent_events):
            raise ValueError(f"recent_events 每项不能超过 {MAX_RECENT_EVENT_CHARS} 个字符")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_REQUEST_JSON_BYTES:
            raise ValueError(f"LLM 请求 JSON 不能超过 {MAX_REQUEST_JSON_BYTES} 字节")
        return self


class AgentDecisionEnvelope(BaseModel):
    agent_id: str
    agent_name: str
    mode: Literal["llm", "fallback_rule_based", "provider_failure_noop"]
    trigger_event_type: str
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str
    needs_coordination: bool = False
    priority: PriorityLabel
    task_steps: list[str] = Field(default_factory=list)
    raw_candidate_commands: list[AgentCommandProposal] = Field(default_factory=list)
    raw_command_assessments: list[RawCommandAssessment] = Field(default_factory=list)
    candidate_commands: list[AgentCommandProposal] = Field(default_factory=list)
    root_event_id: str
    root_event_timestamp: float
    provider_name: str = "fallback"
    model: str = ""
    latency_ms: int = 0
    world_summary: str = ""
    relevant_devices: list[str] = Field(default_factory=list)
    relevant_rooms: list[str] = Field(default_factory=list)
    fallback_reason: str | None = None
    provider_failure_reason: str | None = None
    failed_step: str | None = None
