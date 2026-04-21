from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


PriorityLabel = Literal[
    "direct_user_command",
    "safety",
    "user_comfort",
    "convenience",
    "energy_efficiency",
    "background_optimization",
]


class AgentCommandProposal(BaseModel):
    device_id: str
    property: str
    value: Any
    reason: str = ""


class AgentLLMDecision(BaseModel):
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    task_steps: list[str] = Field(default_factory=list)
    proposed_commands: list[AgentCommandProposal] = Field(default_factory=list)
    explanation: str
    needs_coordination: bool = False


class LLMDecisionRequest(BaseModel):
    agent_id: str
    agent_name: str
    root_event_type: str
    world_summary: str
    recent_events: list[str] = Field(default_factory=list)
    available_devices: list[dict[str, Any]] = Field(default_factory=list)
    allowed_commands: list[dict[str, Any]] = Field(default_factory=list)


class AgentDecisionEnvelope(BaseModel):
    agent_id: str
    agent_name: str
    mode: Literal["llm", "fallback_rule_based"]
    trigger_event_type: str
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str
    needs_coordination: bool = False
    priority: PriorityLabel
    task_steps: list[str] = Field(default_factory=list)
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
    failed_step: str | None = None


class ArbiterResult(BaseModel):
    winning_commands: list[AgentCommandProposal] = Field(default_factory=list)
    winning_commands_by_agent: dict[str, list[AgentCommandProposal]] = Field(default_factory=dict)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
