"""Sealed MiniMax-M3 live/capture/replay substudy for AuraBench."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from backend.agents.llm import (
    AnthropicCompatibleProvider,
    HOME_ORCHESTRATOR_AGENT_ID,
    LLMProvider,
    LLMProviderError,
    STRICT_DECISION_SCHEMA_SET_SHA256,
)
from backend.agents.llm_modes import (
    ALLOW_LIVE_LLM_ENV,
    LLMRecordingManifest,
    load_recordings,
    live_llm_allowed,
    validate_recording_artifact,
)
from backend.agents.llm_pricing import LLM_COST_FILENAME, parse_usage
from backend.agents.types import (
    MAX_PROPOSED_COMMANDS,
    AgentLLMDecision,
    LLMDecisionRequest,
)
from backend.core.local_env import load_local_env
from backend.core.safe_io import read_bounded_regular_file
from backend.engine.event_log import (
    LLM_RECORDINGS_FILENAME,
    read_run_events,
    read_run_metadata,
    run_dir,
    verify_finalized_event_log,
)
from backend.engine.provenance import (
    ExperimentProvenance,
    ExperimentRuntimeSelection,
    ObservationCondition,
    ResearchRuntimeProfile,
)
from backend.engine.run_manager import read_source_revision
from backend.evaluation.evaluator import EvalOutcome, evaluate_run
from backend.models.schemas import BaselinePolicy
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import load_scenario_file
from backend.scenarios.runner import (
    ScenarioRunError,
    ScenarioRunErrorCode,
    ScenarioRunner,
)
from backend.scenarios.trace import canonical_trace_text, trace_digest

from .artifacts import ArtifactSeal, atomic_create_json
from .resolve import FileOrLibraryScenarioResolver
from .spec import sha256_json


SUBSTUDY_SCHEMA_VERSION = "1.1"
RESOLVED_SUBSTUDY_SCHEMA_VERSION = "1.1"
SLOT_RESULT_SCHEMA_VERSION = "1.2"
RESULTS_MANIFEST_SCHEMA_VERSION = "1.2"
RESOLVED_SUBSTUDY_FILENAME = "resolved-substudy.json"
RESULTS_MANIFEST_FILENAME = "results-manifest.json"
PREFLIGHT_FILENAME = "preflight.json"
SLOT_RESULT_FILENAME = "result.json"
MAX_SUBSTUDY_BYTES = 16 * 1024 * 1024
EXPECTED_INSTANCE_COUNT = 24
EXPECTED_SLOT_COUNT = 168
MINIMAX_M3_ENDPOINT = "https://api.minimaxi.com/anthropic"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_SLOT_ID_RE = re.compile(r"^slot-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PricingSnapshot(_StrictModel):
    input_usd_per_mtok: float = Field(ge=0)
    output_usd_per_mtok: float = Field(ge=0)
    source: str = Field(min_length=1)
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class LLMSubstudySpec(_StrictModel):
    substudy_schema_version: Literal["1.1"] = SUBSTUDY_SCHEMA_VERSION
    study_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,95}$")
    scenarios: list[str] = Field(min_length=8, max_length=8)
    seeds: list[StrictInt] = Field(min_length=3, max_length=3)
    provider: Literal["anthropic_compatible"]
    model: Literal["MiniMax-M3"]
    endpoint: Literal["https://api.minimaxi.com/anthropic"]
    anthropic_version: Literal["2023-06-01"]
    max_tokens: Literal[1200]
    timeout_ms: Literal[45000]
    strict_output: Literal[True]
    decision_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    topology: Literal["domain_multi"] = "domain_multi"
    governance: Literal["aura"] = "aura"
    observation: Literal["stale_offline"] = "stale_offline"
    live_repetitions: Literal[3] = 3
    capture_repetitions: Literal[1] = 1
    replay_repetitions: Literal[3] = 3
    billing_mode: Literal["token_plan"] = "token_plan"
    pricing: PricingSnapshot

    @model_validator(mode="after")
    def _fixed_design(self) -> "LLMSubstudySpec":
        if len(set(self.scenarios)) != 8:
            raise ValueError("substudy requires eight unique scenarios")
        if len(set(self.seeds)) != 3 or any(seed < 0 for seed in self.seeds):
            raise ValueError("substudy requires three unique non-negative seeds")
        self.scenarios = sorted(value.strip() for value in self.scenarios)
        self.seeds = sorted(self.seeds)
        if self.decision_schema_sha256 != STRICT_DECISION_SCHEMA_SET_SHA256:
            raise ValueError("decision schema hash does not match this Aura revision")
        return self

    def contract_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class ResolvedScenario(_StrictModel):
    reference: str
    scenario_id: str
    scenario_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SubstudyInstance(_StrictModel):
    instance_id: str = Field(pattern=r"^instance-[0-9a-f]{32}$")
    scenario_reference: str
    scenario_id: str
    scenario_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: StrictInt = Field(ge=0)


class SubstudySlot(_StrictModel):
    slot_id: str = Field(pattern=r"^slot-[0-9a-f]{32}$")
    instance_id: str = Field(pattern=r"^instance-[0-9a-f]{32}$")
    kind: Literal["live", "capture", "replay"]
    repetition: StrictInt = Field(ge=0)
    capture_slot_id: str | None = Field(
        default=None,
        pattern=r"^slot-[0-9a-f]{32}$",
    )

    @model_validator(mode="after")
    def _capture_dependency(self) -> "SubstudySlot":
        if (self.kind == "replay") != (self.capture_slot_id is not None):
            raise ValueError("only replay slots require capture_slot_id")
        return self


class ResolvedLLMSubstudy(_StrictModel):
    resolved_substudy_schema_version: Literal["1.1"] = (
        RESOLVED_SUBSTUDY_SCHEMA_VERSION
    )
    study_id: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    study_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str
    provider: Literal["anthropic_compatible"]
    model: Literal["MiniMax-M3"]
    endpoint: Literal["https://api.minimaxi.com/anthropic"]
    anthropic_version: Literal["2023-06-01"]
    max_tokens: Literal[1200]
    timeout_ms: Literal[45000]
    strict_output: Literal[True]
    decision_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    topology: Literal["domain_multi"]
    governance: Literal["aura"]
    observation: Literal["stale_offline"]
    billing_mode: Literal["token_plan"]
    pricing: PricingSnapshot
    scenarios: list[ResolvedScenario]
    seeds: list[StrictInt]
    instances: list[SubstudyInstance]
    slots: list[SubstudySlot]

    def contract_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"study_hash"})

    @model_validator(mode="after")
    def _validate_contract(self) -> "ResolvedLLMSubstudy":
        if self.study_hash != sha256_json(self.contract_payload()):
            raise ValueError("resolved substudy hash does not match its contents")
        if self.decision_schema_sha256 != STRICT_DECISION_SCHEMA_SET_SHA256:
            raise ValueError("resolved decision schema hash does not match this Aura revision")
        if len(self.scenarios) != 8 or len(self.instances) != EXPECTED_INSTANCE_COUNT:
            raise ValueError("resolved substudy must contain 8 scenarios and 24 instances")
        if (
            self.seeds != sorted(self.seeds)
            or len(self.seeds) != 3
            or len(set(self.seeds)) != 3
            or any(seed < 0 for seed in self.seeds)
        ):
            raise ValueError("resolved substudy must contain three sorted unique seeds")
        if len(self.slots) != EXPECTED_SLOT_COUNT:
            raise ValueError("resolved substudy must contain exactly 168 slots")
        references = [scenario.reference for scenario in self.scenarios]
        scenario_ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(set(references)) != 8 or len(set(scenario_ids)) != 8:
            raise ValueError("resolved scenarios must have unique references and ids")
        expected_instances: list[SubstudyInstance] = []
        for scenario in sorted(self.scenarios, key=lambda item: item.reference):
            for seed in self.seeds:
                identity = {
                    "scenario_contract_hash": scenario.scenario_contract_hash,
                    "seed": seed,
                }
                expected_instances.append(
                    SubstudyInstance(
                        instance_id=f"instance-{sha256_json(identity)[:32]}",
                        scenario_reference=scenario.reference,
                        scenario_id=scenario.scenario_id,
                        scenario_contract_hash=scenario.scenario_contract_hash,
                        seed=seed,
                    )
                )
        if self.instances != expected_instances:
            raise ValueError("resolved instances are not the frozen scenario × seed product")
        expected_slots: list[SubstudySlot] = []
        for instance in expected_instances:
            for repetition in range(3):
                identity = {
                    "instance_id": instance.instance_id,
                    "kind": "live",
                    "repetition": repetition,
                }
                expected_slots.append(
                    SubstudySlot(
                        slot_id=f"slot-{sha256_json(identity)[:32]}",
                        **identity,
                    )
                )
            capture_identity = {
                "instance_id": instance.instance_id,
                "kind": "capture",
                "repetition": 0,
            }
            capture = SubstudySlot(
                slot_id=f"slot-{sha256_json(capture_identity)[:32]}",
                **capture_identity,
            )
            expected_slots.append(capture)
            for repetition in range(3):
                identity = {
                    "instance_id": instance.instance_id,
                    "kind": "replay",
                    "repetition": repetition,
                }
                expected_slots.append(
                    SubstudySlot(
                        slot_id=f"slot-{sha256_json(identity)[:32]}",
                        capture_slot_id=capture.slot_id,
                        **identity,
                    )
                )
        if self.slots != expected_slots:
            raise ValueError("resolved slots or repetitions differ from the frozen 3+1+3 design")
        instance_ids = {item.instance_id for item in self.instances}
        if len(instance_ids) != len(self.instances):
            raise ValueError("instance ids must be unique")
        slot_ids = {item.slot_id for item in self.slots}
        if len(slot_ids) != len(self.slots):
            raise ValueError("slot ids must be unique")
        if any(slot.instance_id not in instance_ids for slot in self.slots):
            raise ValueError("slot references an unknown instance")
        capture_ids = {slot.slot_id for slot in self.slots if slot.kind == "capture"}
        if any(
            slot.capture_slot_id not in capture_ids
            for slot in self.slots
            if slot.kind == "replay"
        ):
            raise ValueError("replay slot references an unknown capture")
        for instance_id in instance_ids:
            grouped = [slot for slot in self.slots if slot.instance_id == instance_id]
            counts = {kind: sum(slot.kind == kind for slot in grouped) for kind in ("live", "capture", "replay")}
            if counts != {"live": 3, "capture": 1, "replay": 3}:
                raise ValueError(f"instance {instance_id} does not have a 3+1+3 design")
            capture = next(slot for slot in grouped if slot.kind == "capture")
            if any(
                slot.capture_slot_id != capture.slot_id
                for slot in grouped
                if slot.kind == "replay"
            ):
                raise ValueError("replays must depend on their own instance capture")
        return self


class PreflightRoleCheck(_StrictModel):
    agent_id: str
    response_model: str
    decision_transport: Literal["tool_use", "text_json"]
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    usage_source: str


class PreflightReceipt(_StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    study_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str
    provider: str
    model: str
    endpoint: str
    anthropic_version: str
    max_tokens: int
    timeout_ms: int
    strict_output: bool
    decision_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    role_checks: dict[
        Literal["home_orchestrator", "domain_agent"],
        PreflightRoleCheck,
    ]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def _complete_role_checks(self) -> "PreflightReceipt":
        if set(self.role_checks) != {"home_orchestrator", "domain_agent"}:
            raise ValueError("preflight must seal orchestrator and domain role checks")
        if self.input_tokens != sum(
            check.input_tokens for check in self.role_checks.values()
        ) or self.output_tokens != sum(
            check.output_tokens for check in self.role_checks.values()
        ):
            raise ValueError("preflight token totals do not match role checks")
        return self


class SealedPreflightReceipt(_StrictModel):
    receipt: PreflightReceipt
    seal: ArtifactSeal

    @model_validator(mode="after")
    def _seal(self) -> "SealedPreflightReceipt":
        expected = sha256_json(self.receipt.model_dump(mode="json"))
        if self.seal.sha256 != expected:
            raise ValueError("preflight seal does not match receipt")
        return self


class SlotError(_StrictModel):
    type: str
    message: str


class AgentSchemaCompliance(_StrictModel):
    responses: int = Field(ge=0)
    compliant: int = Field(ge=0)
    invalid_output: int = Field(ge=0)

    @model_validator(mode="after")
    def _partition(self) -> "AgentSchemaCompliance":
        if self.compliant + self.invalid_output != self.responses:
            raise ValueError("schema response counts do not partition responses")
        return self


class SchemaComplianceEvidence(_StrictModel):
    responses: int = Field(ge=0)
    compliant: int = Field(ge=0)
    invalid_output: int = Field(ge=0)
    by_agent: dict[str, AgentSchemaCompliance]

    @model_validator(mode="after")
    def _totals(self) -> "SchemaComplianceEvidence":
        if (
            self.responses != sum(item.responses for item in self.by_agent.values())
            or self.compliant != sum(item.compliant for item in self.by_agent.values())
            or self.invalid_output
            != sum(item.invalid_output for item in self.by_agent.values())
        ):
            raise ValueError("schema evidence totals do not match per-agent counts")
        return self


class RawPlanEvidence(_StrictModel):
    proposed_commands: int = Field(ge=0)
    admitted_commands: int = Field(ge=0)
    whitelist_rejected_commands: int = Field(ge=0)
    validation_failed_commands: int = Field(ge=0)
    valid_commands: int = Field(ge=0)
    invalid_reasons: dict[str, int]
    frozen_target_commands: int = Field(ge=0)
    frozen_target_matches: int = Field(ge=0)

    @model_validator(mode="after")
    def _bounded_counts(self) -> "RawPlanEvidence":
        if self.admitted_commands > self.proposed_commands:
            raise ValueError("admitted command count exceeds raw proposals")
        if self.whitelist_rejected_commands != (
            self.proposed_commands - self.admitted_commands
        ):
            raise ValueError("whitelist rejection count does not partition proposals")
        if self.valid_commands + self.validation_failed_commands != self.admitted_commands:
            raise ValueError("valid command count does not partition admitted commands")
        if sum(self.invalid_reasons.values()) != (
            self.whitelist_rejected_commands + self.validation_failed_commands
        ) or any(count <= 0 for count in self.invalid_reasons.values()):
            raise ValueError("invalid reason counts do not partition invalid commands")
        if self.frozen_target_commands > self.proposed_commands:
            raise ValueError("target command count exceeds raw proposals")
        if self.frozen_target_matches > self.frozen_target_commands:
            raise ValueError("target matches exceed target commands")
        return self


class SlotResult(_StrictModel):
    slot_result_schema_version: Literal["1.2"] = SLOT_RESULT_SCHEMA_VERSION
    study_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    slot_id: str = Field(pattern=r"^slot-[0-9a-f]{32}$")
    status: Literal["admitted", "invalid_evidence", "failed"]
    run_id: str | None = None
    capture_source_run_id: str | None = None
    trace_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluation: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    recording: dict[str, Any] | None = None
    replay_equivalent: bool | None = None
    model_failure_count: int = Field(default=0, ge=0)
    model_failure_reasons: dict[str, int] = Field(default_factory=dict)
    schema_compliance: SchemaComplianceEvidence | None = None
    raw_plan: RawPlanEvidence | None = None
    error: SlotError | None = None

    @model_validator(mode="after")
    def _status_shape(self) -> "SlotResult":
        if self.status == "admitted":
            if (
                self.run_id is None
                or self.evaluation is None
                or self.schema_compliance is None
                or self.raw_plan is None
                or self.error is not None
            ):
                raise ValueError(
                    "admitted slot requires evaluation/plan evidence and forbids error"
                )
        elif self.error is None:
            raise ValueError("non-admitted slot requires an error")
        return self


class SlotResultArtifact(_StrictModel):
    result: SlotResult
    seal: ArtifactSeal

    @model_validator(mode="after")
    def _seal(self) -> "SlotResultArtifact":
        expected = sha256_json(self.result.model_dump(mode="json"))
        if self.seal.sha256 != expected:
            raise ValueError("slot result seal does not match its contents")
        return self

    @classmethod
    def build(cls, result: SlotResult) -> "SlotResultArtifact":
        return cls(
            result=result,
            seal=ArtifactSeal(sha256=sha256_json(result.model_dump(mode="json"))),
        )


class RatioMetric(_StrictModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    rate: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _exact_ratio(self) -> "RatioMetric":
        expected = (
            None
            if self.denominator == 0
            else round(self.numerator / self.denominator, 8)
        )
        if self.numerator > self.denominator or self.rate != expected:
            raise ValueError("ratio metric is internally inconsistent")
        return self


class CapabilityMetrics(_StrictModel):
    schema_compliance: RatioMetric
    schema_compliance_by_agent: dict[str, RatioMetric]
    conditional_task_success: RatioMetric
    live_only_success: RatioMetric
    raw_command_validity: RatioMetric
    frozen_target_command_match: RatioMetric


class ReplayDiagnostics(_StrictModel):
    evaluation_outcomes: dict[str, int]
    usage_totals: dict[str, float | int]
    model_failure_reasons: dict[str, int]
    response_models: dict[str, int]


class SubstudyResultsManifest(_StrictModel):
    results_manifest_schema_version: Literal["1.2"] = RESULTS_MANIFEST_SCHEMA_VERSION
    study_id: str
    study_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str
    provider: str
    model: str
    endpoint: str
    anthropic_version: str
    max_tokens: int
    timeout_ms: int
    strict_output: bool
    decision_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    preflight_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    billing_mode: Literal["token_plan"]
    planned: int = Field(ge=0)
    admitted: int = Field(ge=0)
    invalid_evidence: int = Field(ge=0)
    failed: int = Field(ge=0)
    by_kind: dict[str, dict[str, int]]
    evaluation_outcomes: dict[str, int]
    usage_totals: dict[str, float | int]
    replay_equivalent: int = Field(ge=0)
    model_failures_by_kind: dict[str, int]
    model_failure_reasons: dict[str, int]
    response_models: dict[str, int]
    replay_diagnostics: ReplayDiagnostics
    capability_metrics: CapabilityMetrics
    scientific_gate: Literal["passed", "failed"]
    slot_result_sha256: dict[str, str]

    @model_validator(mode="after")
    def _consistent_summary(self) -> "SubstudyResultsManifest":
        if self.admitted + self.invalid_evidence + self.failed != self.planned:
            raise ValueError("result statuses do not partition planned slots")
        if set(self.by_kind) != {"live", "capture", "replay"}:
            raise ValueError("by_kind must contain the three frozen slot kinds")
        if set(self.model_failures_by_kind) != {"live", "capture", "replay"}:
            raise ValueError("model failure totals must contain all slot kinds")
        counted_maps = (
            self.evaluation_outcomes,
            self.model_failures_by_kind,
            self.model_failure_reasons,
            self.response_models,
            self.replay_diagnostics.evaluation_outcomes,
            self.replay_diagnostics.model_failure_reasons,
            self.replay_diagnostics.response_models,
        )
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for counts in counted_maps
            for count in counts.values()
        ):
            raise ValueError("summary count maps must contain non-negative integers")
        for kind, counts in self.by_kind.items():
            if set(counts) != {"planned", "admitted", "invalid_evidence", "failed"}:
                raise ValueError(f"by_kind counts are incomplete for {kind}")
            if any(
                not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
                for count in counts.values()
            ):
                raise ValueError(f"by_kind counts are invalid for {kind}")
            if (
                counts["admitted"]
                + counts["invalid_evidence"]
                + counts["failed"]
                != counts["planned"]
            ):
                raise ValueError(f"by_kind counts do not partition {kind}")
        if sum(counts["planned"] for counts in self.by_kind.values()) != self.planned:
            raise ValueError("by_kind planned counts differ from the result total")
        for status in ("admitted", "invalid_evidence", "failed"):
            if sum(counts[status] for counts in self.by_kind.values()) != getattr(
                self,
                status,
            ):
                raise ValueError(f"by_kind {status} differs from the result total")
        source_admitted = (
            self.by_kind["live"]["admitted"]
            + self.by_kind["capture"]["admitted"]
        )
        if sum(self.evaluation_outcomes.values()) != source_admitted:
            raise ValueError("source evaluation outcomes differ from admitted slots")
        if (
            sum(self.replay_diagnostics.evaluation_outcomes.values())
            != self.by_kind["replay"]["admitted"]
        ):
            raise ValueError("replay outcomes differ from admitted replay slots")
        if sum(self.model_failure_reasons.values()) != (
            self.model_failures_by_kind["live"]
            + self.model_failures_by_kind["capture"]
        ):
            raise ValueError("source model failure reasons do not match by-kind totals")
        if sum(self.replay_diagnostics.model_failure_reasons.values()) != (
            self.model_failures_by_kind["replay"]
        ):
            raise ValueError("replay model failure reasons do not match by-kind totals")
        if sum(self.response_models.values()) != int(
            self.usage_totals.get("billable_calls", -1)
        ):
            raise ValueError("source response models differ from billable calls")
        if self.replay_diagnostics.usage_totals.get("billable_calls") != 0:
            raise ValueError("replay diagnostics contain billable calls")
        if (
            len(self.slot_result_sha256) != self.planned
            or any(_SLOT_ID_RE.fullmatch(slot_id) is None for slot_id in self.slot_result_sha256)
            or any(
                not isinstance(digest, str)
                or _SHA256_RE.fullmatch(digest) is None
                for digest in self.slot_result_sha256.values()
            )
        ):
            raise ValueError("slot result hashes do not cover the planned slots")
        expected_pass = (
            self.planned == EXPECTED_SLOT_COUNT
            and self.admitted == EXPECTED_SLOT_COUNT
            and self.invalid_evidence == 0
            and self.failed == 0
            and self.by_kind["live"]["planned"] == 72
            and self.by_kind["capture"]["planned"] == 24
            and self.by_kind["replay"]["planned"] == 72
            and self.by_kind["live"]["admitted"] == 72
            and self.by_kind["capture"]["admitted"] == 24
            and self.by_kind["replay"]["admitted"] == 72
            and self.replay_equivalent == 72
        )
        if (self.scientific_gate == "passed") is not expected_pass:
            raise ValueError("scientific gate disagrees with frozen admission rules")
        return self


class SealedSubstudyResults(_StrictModel):
    results: SubstudyResultsManifest
    seal: ArtifactSeal

    @model_validator(mode="after")
    def _seal(self) -> "SealedSubstudyResults":
        expected = sha256_json(self.results.model_dump(mode="json"))
        if self.seal.sha256 != expected:
            raise ValueError("results seal does not match manifest")
        return self


def _id(prefix: str, payload: Mapping[str, Any]) -> str:
    return f"{prefix}-{sha256_json(dict(payload))[:32]}"


def load_llm_substudy(path: Path | str) -> LLMSubstudySpec:
    path = Path(path)
    try:
        encoded = read_bounded_regular_file(path, max_bytes=MAX_SUBSTUDY_BYTES)
        raw = yaml.safe_load(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid substudy YAML: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("substudy YAML must contain a mapping")
    return LLMSubstudySpec.model_validate(raw)


def resolve_llm_substudy(
    path: Path | str,
    *,
    source_revision: str | None = None,
) -> ResolvedLLMSubstudy:
    manifest_path = Path(path).resolve()
    spec = load_llm_substudy(manifest_path)
    resolver = FileOrLibraryScenarioResolver(
        base_dir=manifest_path.parent,
        scenario_dirs=[_REPOSITORY_ROOT],
    )
    scenarios = [
        ResolvedScenario(
            reference=(contract := resolver.resolve(reference)).reference,
            scenario_id=contract.scenario_id,
            scenario_contract_hash=contract.scenario_contract_hash,
        )
        for reference in spec.scenarios
    ]
    instances: list[SubstudyInstance] = []
    slots: list[SubstudySlot] = []
    for scenario in sorted(scenarios, key=lambda item: item.reference):
        for seed in spec.seeds:
            instance_payload = {
                "scenario_contract_hash": scenario.scenario_contract_hash,
                "seed": seed,
            }
            instance = SubstudyInstance(
                instance_id=_id("instance", instance_payload),
                scenario_reference=scenario.reference,
                scenario_id=scenario.scenario_id,
                scenario_contract_hash=scenario.scenario_contract_hash,
                seed=seed,
            )
            instances.append(instance)
            for repetition in range(spec.live_repetitions):
                payload = {"instance_id": instance.instance_id, "kind": "live", "repetition": repetition}
                slots.append(SubstudySlot(slot_id=_id("slot", payload), **payload))
            capture_payload = {"instance_id": instance.instance_id, "kind": "capture", "repetition": 0}
            capture = SubstudySlot(slot_id=_id("slot", capture_payload), **capture_payload)
            slots.append(capture)
            for repetition in range(spec.replay_repetitions):
                payload = {"instance_id": instance.instance_id, "kind": "replay", "repetition": repetition}
                slots.append(
                    SubstudySlot(
                        slot_id=_id("slot", payload),
                        capture_slot_id=capture.slot_id,
                        **payload,
                    )
                )
    payload = {
        "resolved_substudy_schema_version": RESOLVED_SUBSTUDY_SCHEMA_VERSION,
        "study_id": spec.study_id,
        "manifest_hash": spec.contract_hash(),
        "source_revision": source_revision or read_source_revision(),
        "provider": spec.provider,
        "model": spec.model,
        "endpoint": spec.endpoint,
        "anthropic_version": spec.anthropic_version,
        "max_tokens": spec.max_tokens,
        "timeout_ms": spec.timeout_ms,
        "strict_output": spec.strict_output,
        "decision_schema_sha256": spec.decision_schema_sha256,
        "topology": spec.topology,
        "governance": spec.governance,
        "observation": spec.observation,
        "billing_mode": spec.billing_mode,
        "pricing": spec.pricing.model_dump(mode="json"),
        "scenarios": [item.model_dump(mode="json") for item in scenarios],
        "seeds": spec.seeds,
        "instances": [item.model_dump(mode="json") for item in instances],
        "slots": [item.model_dump(mode="json") for item in slots],
    }
    return ResolvedLLMSubstudy.model_validate(
        {**payload, "study_hash": sha256_json(payload)}
    )


def write_resolved_llm_substudy(
    output_dir: Path | str,
    study: ResolvedLLMSubstudy,
) -> Path:
    return atomic_create_json(
        Path(output_dir) / RESOLVED_SUBSTUDY_FILENAME,
        study.model_dump(mode="json"),
        max_bytes=MAX_SUBSTUDY_BYTES,
    )


def read_resolved_llm_substudy(path: Path | str) -> ResolvedLLMSubstudy:
    return _read_model(path, ResolvedLLMSubstudy)


def _read_model(path: Path | str, model_type: type[BaseModel]):
    path = Path(path)
    try:
        encoded = read_bounded_regular_file(path, max_bytes=MAX_SUBSTUDY_BYTES)
        raw = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read artifact {path}: {exc}") from exc
    return model_type.model_validate(raw)


def _slot_result_path(root: Path | str, slot_id: str) -> Path:
    if _SLOT_ID_RE.fullmatch(slot_id) is None:
        raise ValueError(f"invalid slot id: {slot_id!r}")
    return Path(root) / "slots" / slot_id / SLOT_RESULT_FILENAME


def read_slot_result(root: Path | str, slot_id: str) -> SlotResultArtifact:
    return _read_model(_slot_result_path(root, slot_id), SlotResultArtifact)


def write_slot_result(root: Path | str, artifact: SlotResultArtifact) -> Path:
    return atomic_create_json(
        _slot_result_path(root, artifact.result.slot_id),
        artifact.model_dump(mode="json"),
        max_bytes=MAX_SUBSTUDY_BYTES,
    )


def build_live_provider(study: ResolvedLLMSubstudy) -> LLMProvider:
    load_local_env()
    if not live_llm_allowed():
        raise ValueError(f"{ALLOW_LIVE_LLM_ENV}=1 is required for the live substudy")
    configured_provider = str(os.environ.get("LLM_PROVIDER", "")).strip()
    if configured_provider != study.provider:
        raise ValueError(
            f"configured provider {configured_provider!r} does not match {study.provider!r}"
        )
    provider = AnthropicCompatibleProvider.from_env(strict_output=True)
    _validate_live_provider(study, provider)
    return provider


def _validate_live_provider(
    study: ResolvedLLMSubstudy,
    provider: LLMProvider,
) -> None:
    if getattr(provider, "provider_name", None) != study.provider:
        raise ValueError("live provider does not match the frozen study")
    if getattr(provider, "model", None) != study.model:
        raise ValueError(
            f"configured model {getattr(provider, 'model', None)!r} does not "
            f"match {study.model!r}"
        )
    if str(getattr(provider, "base_url", "")) != study.endpoint:
        raise ValueError(
            "configured Anthropic-compatible endpoint does not match the frozen "
            f"MiniMax endpoint {study.endpoint!r}"
        )
    if not bool(getattr(provider, "api_key", None)):
        raise ValueError("server-side Anthropic-compatible API key is missing")
    request_contract = {
        "anthropic_version": getattr(provider, "anthropic_version", None),
        "max_tokens": getattr(provider, "max_tokens", None),
        "timeout_ms": getattr(provider, "timeout_ms", None),
        "strict_output": getattr(provider, "strict_output", None),
        "decision_schema_sha256": getattr(
            provider,
            "decision_schema_sha256",
            None,
        ),
    }
    expected_contract = {
        "anthropic_version": study.anthropic_version,
        "max_tokens": study.max_tokens,
        "timeout_ms": study.timeout_ms,
        "strict_output": study.strict_output,
        "decision_schema_sha256": study.decision_schema_sha256,
    }
    if request_contract != expected_contract:
        raise ValueError(
            f"configured request contract does not match the frozen study: {request_contract}"
        )


async def preflight_llm_substudy(
    study: ResolvedLLMSubstudy,
    *,
    output_dir: Path | str,
    provider_factory: Callable[[ResolvedLLMSubstudy], LLMProvider] = build_live_provider,
) -> Path:
    if read_source_revision() != study.source_revision:
        raise ValueError("source revision drift: resolve the substudy again")
    provider = provider_factory(study)
    _validate_live_provider(study, provider)
    role_requests = {
        "home_orchestrator": LLMDecisionRequest(
            decision_role="home_orchestrator",
            agent_id=HOME_ORCHESTRATOR_AGENT_ID,
            agent_name="Home Orchestrator Preflight",
            root_event_type="system.llm_preflight",
            world_summary=(
                "Classify a routine occupied-room comfort event. "
                "No device command is permitted at the orchestration layer."
            ),
        ),
        "domain_agent": LLMDecisionRequest(
            agent_id="lighting_agent_preflight",
            agent_name="Lighting Agent Preflight",
            root_event_type="system.llm_preflight",
            world_summary=(
                "Validate a domain-agent planning response for an occupied room."
            ),
            available_devices=[
                {
                    "device_id": "light_preflight_01",
                    "room": "preflight_room",
                    "type": "light",
                    "state": {"power": False},
                }
            ],
            allowed_commands=[
                {"device_id": "light_preflight_01", "property": "power"}
            ],
        ),
    }
    role_checks: dict[str, PreflightRoleCheck] = {}
    for role, request in role_requests.items():
        decision = await provider.generate_decision(request)
        usage = _usage_payload(provider)
        if usage["input_tokens"] + usage["output_tokens"] <= 0:
            raise ValueError(
                f"preflight {role} response returned no usable token telemetry"
            )
        role_checks[role] = PreflightRoleCheck(
            agent_id=request.agent_id,
            response_model=str(getattr(provider, "last_response_model", "") or ""),
            decision_transport=str(
                getattr(provider, "last_decision_transport", "")
            ),
            response_sha256=sha256_json(decision.model_dump(mode="json")),
            **usage,
        )
    receipt = PreflightReceipt(
        study_hash=study.study_hash,
        source_revision=study.source_revision,
        provider=str(getattr(provider, "provider_name", "")),
        model=str(getattr(provider, "model", "")),
        endpoint=str(getattr(provider, "base_url", "")),
        anthropic_version=str(getattr(provider, "anthropic_version", "")),
        max_tokens=int(getattr(provider, "max_tokens", 0)),
        timeout_ms=int(getattr(provider, "timeout_ms", 0)),
        strict_output=bool(getattr(provider, "strict_output", False)),
        decision_schema_sha256=str(
            getattr(provider, "decision_schema_sha256", "")
        ),
        role_checks=role_checks,
        input_tokens=sum(check.input_tokens for check in role_checks.values()),
        output_tokens=sum(check.output_tokens for check in role_checks.values()),
    )
    sealed = SealedPreflightReceipt(
        receipt=receipt,
        seal=ArtifactSeal(sha256=sha256_json(receipt.model_dump(mode="json"))),
    )
    return atomic_create_json(
        Path(output_dir) / PREFLIGHT_FILENAME,
        sealed.model_dump(mode="json"),
        max_bytes=MAX_SUBSTUDY_BYTES,
    )


def validate_preflight_receipt(
    study: ResolvedLLMSubstudy,
    *,
    output_dir: Path | str,
) -> SealedPreflightReceipt:
    sealed = _read_model(
        Path(output_dir) / PREFLIGHT_FILENAME,
        SealedPreflightReceipt,
    )
    receipt = sealed.receipt
    if (
        receipt.study_hash != study.study_hash
        or receipt.source_revision != study.source_revision
        or receipt.provider != study.provider
        or receipt.model != study.model
        or receipt.endpoint != study.endpoint
        or receipt.anthropic_version != study.anthropic_version
        or receipt.max_tokens != study.max_tokens
        or receipt.timeout_ms != study.timeout_ms
        or receipt.strict_output is not study.strict_output
        or receipt.decision_schema_sha256 != study.decision_schema_sha256
        or receipt.input_tokens + receipt.output_tokens <= 0
    ):
        raise ValueError("preflight receipt does not admit this frozen study")
    for role, expected_agent_id in {
        "home_orchestrator": HOME_ORCHESTRATOR_AGENT_ID,
        "domain_agent": "lighting_agent_preflight",
    }.items():
        check = receipt.role_checks[role]
        if (
            check.agent_id != expected_agent_id
            or check.response_model != study.model
            or check.input_tokens + check.output_tokens <= 0
            or check.usage_source == "missing"
        ):
            raise ValueError("preflight role check does not admit this frozen study")
    return sealed


def _usage_payload(provider: LLMProvider) -> dict[str, Any]:
    usage = parse_usage(getattr(provider, "last_usage", None))
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "usage_source": "missing"}
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "usage_source": usage.source.value,
    }


def _baseline_for(slot: SubstudySlot) -> BaselinePolicy:
    return (
        BaselinePolicy.LLM_LIVE
        if slot.kind == "live"
        else BaselinePolicy.LLM_RECORDED
    )


class _ReplayNetworkSentinel(LLMProvider):
    """Injected into replay runs so an accidental live call fails immediately."""

    provider_name = "anthropic_compatible"
    model = "MiniMax-M3"
    api_key = "offline-replay-sentinel"

    async def generate_decision(self, request: LLMDecisionRequest):  # type: ignore[override]
        raise AssertionError("replay attempted to call the live provider")


class _InvalidOutputAsNoOpProvider(LLMProvider):
    """Turn a malformed paid response into an explicit research no-op."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        return str(getattr(self.inner, "provider_name", "unknown") or "unknown")

    @property
    def model(self) -> str:  # type: ignore[override]
        return str(getattr(self.inner, "model", "") or "")

    @property
    def llm_mode(self) -> Any:  # type: ignore[override]
        return getattr(self.inner, "llm_mode", "live")

    @property
    def api_key(self) -> Any:
        return getattr(self.inner, "api_key", None)

    @property
    def timeout_ms(self) -> Any:
        return getattr(self.inner, "timeout_ms", None)

    @property
    def base_url(self) -> Any:
        return getattr(self.inner, "base_url", None)

    @property
    def anthropic_version(self) -> Any:
        return getattr(self.inner, "anthropic_version", None)

    @property
    def max_tokens(self) -> Any:
        return getattr(self.inner, "max_tokens", None)

    @property
    def strict_output(self) -> Any:
        return getattr(self.inner, "strict_output", None)

    @property
    def decision_schema_sha256(self) -> Any:
        return getattr(self.inner, "decision_schema_sha256", None)

    @property
    def last_usage(self) -> Any:
        return getattr(self.inner, "last_usage", None)

    @property
    def last_decision_transport(self) -> Any:
        return getattr(self.inner, "last_decision_transport", None)

    @property
    def last_response_model(self) -> Any:
        return getattr(self.inner, "last_response_model", None)

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        try:
            return await self.inner.generate_decision(request)
        except LLMProviderError as exc:
            if exc.reason != "invalid_output":
                raise
            if (
                parse_usage(getattr(self.inner, "last_usage", None)) is None
                or getattr(self.inner, "last_decision_transport", None)
                not in {"tool_use", "text_json", "empty"}
                or getattr(self.inner, "last_response_model", None) != self.model
            ):
                raise
            return AgentLLMDecision(
                intent="provider invalid output; strict research no-op",
                confidence=0.0,
                task_steps=[],
                proposed_commands=[],
                explanation=(
                    "The model returned invalid structured output; strict research mode "
                    "performed no action and did not invoke a rule fallback."
                ),
                needs_coordination=False,
                provider_failure_reason=exc.reason,
            )


def _experiment_for(
    study: ResolvedLLMSubstudy,
    slot: SubstudySlot,
) -> tuple[ExperimentProvenance, ExperimentRuntimeSelection]:
    experiment = ExperimentProvenance(
        experiment_id=study.study_id,
        matrix_spec_hash=study.manifest_hash,
        matrix_hash=study.study_hash,
        cell_id=f"cell-{hashlib.sha256(slot.slot_id.encode()).hexdigest()[:32]}",
        runtime_profile=ResearchRuntimeProfile.AURA,
        model="recorded" if slot.kind == "capture" else slot.kind,
        topology=study.topology,
        governance=study.governance,
        observation=ObservationCondition(study.observation),
        repetition=slot.repetition,
    )
    runtime = ExperimentRuntimeSelection.for_profile(
        ResearchRuntimeProfile.AURA,
        model=experiment.model,
        baseline_policy=_baseline_for(slot),
        observation=ObservationCondition(study.observation),
    )
    return experiment, runtime


class LLMSubstudyRunner:
    def __init__(
        self,
        study: ResolvedLLMSubstudy,
        *,
        output_dir: Path | str,
        provider_factory: Callable[[ResolvedLLMSubstudy], LLMProvider] = build_live_provider,
    ) -> None:
        self.study = study
        self.output_dir = Path(output_dir)
        self.runs_root = self.output_dir / "runs"
        self.provider_factory = provider_factory
        self.instances = {item.instance_id: item for item in study.instances}
        self.slots = {item.slot_id: item for item in study.slots}

    async def run(
        self,
        *,
        resume: bool = True,
        continue_on_error: bool = False,
    ) -> dict[str, Any]:
        if read_source_revision() != self.study.source_revision:
            raise ValueError("source revision drift: resolve the substudy again")
        validate_preflight_receipt(self.study, output_dir=self.output_dir)
        counts = {"planned": len(self.study.slots), "admitted": 0, "invalid_evidence": 0, "failed": 0, "skipped": 0}
        for slot in self.study.slots:
            path = _slot_result_path(self.output_dir, slot.slot_id)
            if path.exists() and not resume:
                raise ValueError(
                    f"slot result already exists; use resume or a new output root: {slot.slot_id}"
                )
            if path.exists() and resume:
                try:
                    existing = read_slot_result(self.output_dir, slot.slot_id)
                    valid = self.validate_result(slot, existing.result)
                except Exception:
                    existing = None
                    valid = False
                if existing is not None and existing.result.status == "admitted" and valid:
                    counts["admitted"] += 1
                    counts["skipped"] += 1
                    continue
                status = existing.result.status if existing is not None else "failed"
                counts[status] += 1
                counts["skipped"] += 1
                if not continue_on_error:
                    break
                continue
            try:
                result = await self._execute(slot)
            except Exception as exc:  # evidence boundary: persist typed failure
                result = SlotResult(
                    study_hash=self.study.study_hash,
                    slot_id=slot.slot_id,
                    status="failed",
                    error=SlotError(type=type(exc).__name__, message=str(exc)),
                )
            write_slot_result(self.output_dir, SlotResultArtifact.build(result))
            counts[result.status] += 1
            if result.status != "admitted" and not continue_on_error:
                break
        return counts

    async def _execute(self, slot: SubstudySlot) -> SlotResult:
        instance = self.instances[slot.instance_id]
        scenario_path = _REPOSITORY_ROOT / instance.scenario_reference
        spec = load_scenario_file(scenario_path)
        if (
            spec.id != instance.scenario_id
            or scenario_contract_fingerprint(spec) != instance.scenario_contract_hash
        ):
            raise ValueError("scenario contract drift")
        source_run_id: str | None = None
        if slot.capture_slot_id is not None:
            source_result = read_slot_result(self.output_dir, slot.capture_slot_id).result
            if source_result.status != "admitted" or source_result.run_id is None:
                raise ValueError("replay source capture is not admitted")
            source_run_id = source_result.run_id
        experiment, runtime = _experiment_for(self.study, slot)
        if slot.kind == "replay":
            provider: LLMProvider = _ReplayNetworkSentinel()
        else:
            live_provider = self.provider_factory(self.study)
            _validate_live_provider(self.study, live_provider)
            provider = _InvalidOutputAsNoOpProvider(live_provider)
        provider_timeout_ms = getattr(provider, "timeout_ms", 0)
        settle_timeout_s = max(
            60.0,
            (
                float(provider_timeout_ms) / 1000.0 + 15.0
                if isinstance(provider_timeout_ms, int)
                else 60.0
            ),
        )
        runner = ScenarioRunner(
            spec,
            seed=instance.seed,
            llm_provider=provider,
            baseline_policy=_baseline_for(slot),
            recording_source_run_id=source_run_id,
            experiment=experiment,
            experiment_runtime=runtime,
            episode_settle_timeout_s=settle_timeout_s,
            allow_unobserved_perturbation_anchor=True,
            run_artifacts_root=self.runs_root,
            enforce_llm_budget=False,
        )
        try:
            result = await runner.run()
        except ScenarioRunError as exc:
            finished = runner.engine.run_manager.finished
            run_id = finished[-1].run_id if finished else None
            status = (
                "invalid_evidence"
                if exc.code is ScenarioRunErrorCode.PERTURBATION_PHASE_INVALID
                else "failed"
            )
            return SlotResult(
                study_hash=self.study.study_hash,
                slot_id=slot.slot_id,
                status=status,
                run_id=run_id,
                capture_source_run_id=source_run_id,
                error=SlotError(type=exc.code.value, message=exc.message),
            )
        finally:
            await runner.engine.close()
        try:
            return self._admit(slot, result.run_id, source_run_id=source_run_id)
        except Exception as exc:
            return SlotResult(
                study_hash=self.study.study_hash,
                slot_id=slot.slot_id,
                status="invalid_evidence",
                run_id=result.run_id,
                capture_source_run_id=source_run_id,
                error=SlotError(type=type(exc).__name__, message=str(exc)),
            )

    def _admit(
        self,
        slot: SubstudySlot,
        run_id: str,
        *,
        source_run_id: str | None,
    ) -> SlotResult:
        instance = self.instances[slot.instance_id]
        scenario = load_scenario_file(
            _REPOSITORY_ROOT / instance.scenario_reference
        )
        metadata = read_run_metadata(run_id, root=self.runs_root)
        verify_finalized_event_log(run_id, metadata=metadata, root=self.runs_root)
        events, _ = read_run_events(run_id, root=self.runs_root, verify_integrity=True)
        experiment, _ = _experiment_for(self.study, slot)
        expected_provider = {"live": self.study.provider, "capture": "recording", "replay": "replay"}[slot.kind]
        expected_mode = "live" if slot.kind == "live" else "recorded"
        expected = {
            "scenario_id": instance.scenario_id,
            "scenario_contract_hash": instance.scenario_contract_hash,
            "seed": instance.seed,
            "source_revision": self.study.source_revision,
            "llm_provider": expected_provider,
            "llm_model": self.study.model,
            "llm_endpoint": (
                self.study.endpoint if slot.kind in {"live", "capture"} else None
            ),
            "llm_protocol_version": (
                self.study.anthropic_version
                if slot.kind in {"live", "capture"}
                else None
            ),
            "llm_max_tokens": (
                self.study.max_tokens if slot.kind in {"live", "capture"} else None
            ),
            "llm_timeout_ms": (
                self.study.timeout_ms if slot.kind in {"live", "capture"} else None
            ),
            "llm_strict_output": (
                self.study.strict_output if slot.kind in {"live", "capture"} else None
            ),
            "llm_decision_schema_sha256": (
                self.study.decision_schema_sha256
                if slot.kind in {"live", "capture"}
                else None
            ),
            "llm_cost_policy": "telemetry_only",
            "llm_mode": expected_mode,
            "baseline_policy": _baseline_for(slot).value,
            "recording_source_run_id": source_run_id,
            "experiment": experiment.model_dump(mode="json"),
            "end_reason": "completed",
            "artifact_error": None,
        }
        mismatches = {
            key: {"expected": value, "actual": metadata.get(key)}
            for key, value in expected.items()
            if metadata.get(key) != value
        }
        if mismatches or metadata.get("ended_at") is None:
            raise ValueError(f"run metadata mismatch: {mismatches}")
        fallbacks = [event for event in events if event.get("event_type") == "reasoning.fallback_rule_based"]
        if fallbacks:
            reasons = sorted({str(event.get("data", {}).get("reason")) for event in fallbacks})
            raise ValueError(f"LLM fallback disqualifies evidence: {reasons}")
        model_failure_reasons = _validate_model_failure_noops(events)
        schema_compliance = _schema_compliance_evidence(events)
        if schema_compliance.invalid_output != sum(model_failure_reasons.values()):
            raise ValueError("schema compliance evidence differs from failure no-ops")
        decisions = [event for event in events if event.get("event_type") == "reasoning.coordination_decision"]
        if not decisions or any(
            event.get("data", {}).get("runtime_profile") != "aura"
            or event.get("data", {}).get("governance") != "aura"
            or event.get("data", {}).get("observation_condition") != "stale_offline"
            for event in decisions
        ):
            raise ValueError("runtime treatment evidence is missing or mismatched")
        cost_path = run_dir(run_id, root=self.runs_root) / LLM_COST_FILENAME
        try:
            cost_payload = json.loads(
                read_bounded_regular_file(
                    cost_path,
                    max_bytes=MAX_SUBSTUDY_BYTES,
                ).decode("utf-8")
            )
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"LLM cost artifact is not safely readable: {exc}") from exc
        usage = cost_payload["totals"]
        if cost_payload.get("budget_policy") != "telemetry_only":
            raise ValueError("token-plan run did not use telemetry-only cost accounting")
        if usage.get("blocked_calls") != 0 or usage.get("calls", 0) <= 0:
            raise ValueError("LLM cost telemetry is missing or contains blocked calls")
        if slot.kind in {"live", "capture"}:
            if usage.get("billable_calls", 0) <= 0 or usage.get("input_tokens", 0) + usage.get("output_tokens", 0) <= 0:
                raise ValueError("paid run has no billable token evidence")
            if usage.get("response_models") != {
                self.study.model: usage.get("billable_calls")
            }:
                raise ValueError("paid run response model evidence is missing or mismatched")
            if schema_compliance.responses != usage.get("billable_calls"):
                raise ValueError("schema response evidence differs from paid call count")
            used_prices = cost_payload.get("pricing", {}).get("used", [])
            matching_prices = [
                item.get("price", {})
                for item in used_prices
                if item.get("model") == self.study.model
            ]
            expected_price = self.study.pricing.model_dump(mode="json")
            if not matching_prices or any(
                {
                    key: price.get(key)
                    for key in (
                        "input_usd_per_mtok",
                        "output_usd_per_mtok",
                        "source",
                        "as_of",
                    )
                }
                != expected_price
                for price in matching_prices
            ):
                raise ValueError("run pricing evidence differs from the frozen snapshot")
        else:
            if usage.get("billable_calls") != 0:
                raise ValueError("replay made a billable call")
            if schema_compliance.responses != usage.get("calls"):
                raise ValueError("replay schema evidence differs from replay call count")
        recording: dict[str, Any] | None = None
        equivalent: bool | None = None
        if slot.kind == "capture":
            recording_path = run_dir(run_id, root=self.runs_root) / LLM_RECORDINGS_FILENAME
            manifest: LLMRecordingManifest = validate_recording_artifact(
                recording_path,
                require_replay_schedule=True,
            )
            records = load_recordings(recording_path)
            if any(
                item.provider != self.study.provider
                or item.model != self.study.model
                or item.response_model != self.study.model
                for item in records.values()
            ):
                raise ValueError("recording provider/model does not match the study")
            recording = manifest.model_dump(mode="json", by_alias=True)
        if slot.kind == "replay":
            assert source_run_id is not None
            source_metadata = read_run_metadata(source_run_id, root=self.runs_root)
            source_request_contract = {
                "llm_endpoint": self.study.endpoint,
                "llm_protocol_version": self.study.anthropic_version,
                "llm_max_tokens": self.study.max_tokens,
                "llm_timeout_ms": self.study.timeout_ms,
                "llm_strict_output": self.study.strict_output,
                "llm_decision_schema_sha256": self.study.decision_schema_sha256,
            }
            if any(
                source_metadata.get(key) != value
                for key, value in source_request_contract.items()
            ):
                raise ValueError("replay source request contract does not match the study")
            for key in ("scenario_id", "scenario_contract_hash", "seed", "source_revision", "llm_model"):
                if source_metadata.get(key) != metadata.get(key):
                    raise ValueError(f"replay source mismatch: {key}")
            source_events, _ = read_run_events(source_run_id, root=self.runs_root, verify_integrity=True)
            equivalent = _replay_equivalence_trace(events) == (
                _replay_equivalence_trace(source_events)
            )
            if not equivalent:
                raise ValueError("replay canonical trace differs from capture")
        report = evaluate_run(
            run_id,
            data_root=self.runs_root,
            scenario_dirs=[(_REPOSITORY_ROOT / instance.scenario_reference).parent],
        )
        if report.outcome is EvalOutcome.ERROR:
            raise ValueError(f"evaluation error: {report.failure_reasons}")
        evaluation = report.to_dict()
        raw_plan = _raw_plan_evidence(
            events,
            scenario=scenario,
        )
        if slot.kind == "replay":
            source_report = evaluate_run(
                source_run_id,
                data_root=self.runs_root,
                scenario_dirs=[(_REPOSITORY_ROOT / instance.scenario_reference).parent],
            ).to_dict()
            if _evaluation_semantics(evaluation) != _evaluation_semantics(source_report):
                raise ValueError("replay evaluation differs from capture")
        return SlotResult(
            study_hash=self.study.study_hash,
            slot_id=slot.slot_id,
            status="admitted",
            run_id=run_id,
            capture_source_run_id=source_run_id,
            trace_sha256=trace_digest(events),
            evaluation=evaluation,
            usage=usage,
            recording=recording,
            replay_equivalent=equivalent,
            model_failure_count=sum(model_failure_reasons.values()),
            model_failure_reasons=model_failure_reasons,
            schema_compliance=schema_compliance,
            raw_plan=raw_plan,
        )

    def validate_result(self, slot: SubstudySlot, result: SlotResult) -> bool:
        if result.study_hash != self.study.study_hash or result.slot_id != slot.slot_id:
            return False
        if result.status != "admitted" or result.run_id is None:
            return True
        try:
            rebuilt = self._admit(
                slot,
                result.run_id,
                source_run_id=result.capture_source_run_id,
            )
        except Exception:
            return False
        return rebuilt.model_dump(mode="json") == result.model_dump(mode="json")


def _evaluation_semantics(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project behavior/effect metrics, excluding replay's zero-network latency."""

    semantics = {
        key: payload.get(key)
        for key in (
            "outcome",
            "criteria_checks",
            "failed_metrics",
            "failure_reasons",
            "final_state_success",
            "trajectory_properties_satisfied",
            "trajectory_safe_success",
        )
    }
    metrics = payload.get("metrics")
    semantics["metrics"] = {
        str(name): {
            "value": datum.get("value"),
            "unit": datum.get("unit"),
        }
        for name, datum in (metrics.items() if isinstance(metrics, Mapping) else ())
        if isinstance(datum, Mapping) and name != "first_action_latency_ms"
    }
    return semantics


def _validate_model_failure_noops(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Admit declared invalid model output only when it caused no rule action."""

    def event_seq(event: Mapping[str, Any]) -> int:
        seq = event.get("seq")
        if not isinstance(seq, int):
            raise ValueError("provider failure no-op evidence has no integer sequence")
        return seq

    failures = [
        event
        for event in events
        if event.get("event_type") == "reasoning.provider_failure_noop"
    ]
    counts: dict[str, int] = {}
    for failure in failures:
        data = failure.get("data")
        if not isinstance(data, Mapping):
            raise ValueError("provider failure no-op event has no structured data")
        reason = str(data.get("reason") or "")
        if reason != "invalid_output" or data.get("fallback_strategy") != "none":
            raise ValueError("undeclared provider failure no-op reason or fallback strategy")
        source = str(failure.get("source") or "")
        correlation_id = failure.get("correlation_id")
        failure_seq = event_seq(failure)
        if source == "home_orchestrator":
            evidence = [
                event
                for event in events
                if event.get("event_type") == "reasoning.task_decomposition"
                and event.get("source") == source
                and event.get("event_id") == failure.get("causal_parent")
            ]
            if not evidence or any(
                event.get("data", {}).get("domain_tasks") for event in evidence
            ):
                raise ValueError("orchestrator provider failure was not a strict no-op")
        else:
            candidates = [
                event
                for event in events
                if event.get("event_type") == "reasoning.execution_plan"
                and event.get("source") == source
                and event.get("correlation_id") == correlation_id
                and event_seq(event) > failure_seq
            ]
            if not candidates:
                raise ValueError("domain provider failure was not a strict no-op")
            evidence = min(candidates, key=event_seq)
            evidence_data = evidence.get("data", {})
            if (
                evidence_data.get("execution_mode") != "provider_failure_noop"
                or evidence_data.get("commands")
                or evidence_data.get("provider_failure_reason") != reason
            ):
                raise ValueError("domain provider failure was not a strict no-op")

            evidence_seq = event_seq(evidence)
            later_plans = [
                event_seq(event)
                for event in events
                if event.get("event_type") == "reasoning.execution_plan"
                and event.get("source") == source
                and event.get("correlation_id") == correlation_id
                and event_seq(event) > evidence_seq
            ]
            boundary = min(later_plans) if later_plans else None
            if any(
                event.get("event_type") == "action.device_control"
                and event.get("source") == source
                and event.get("correlation_id") == correlation_id
                and event_seq(event) > evidence_seq
                and (boundary is None or event_seq(event) < boundary)
                for event in events
            ):
                raise ValueError("provider failure no-op emitted a device action")
        counts[reason] = counts.get(reason, 0) + 1
    return counts


def _schema_compliance_evidence(
    events: Sequence[Mapping[str, Any]],
) -> SchemaComplianceEvidence:
    """Count exact-schema responses from their sealed reasoning events."""

    attempts: dict[str, int] = {}
    invalid: dict[str, int] = {}
    for event in events:
        event_type = event.get("event_type")
        source = str(event.get("source") or "")
        data = event.get("data")
        is_attempt = event_type == "reasoning.task_decomposition" and (
            source == HOME_ORCHESTRATOR_AGENT_ID
        )
        if event_type == "reasoning.execution_plan" and isinstance(data, Mapping):
            is_attempt = data.get("execution_mode") in {
                "llm",
                "provider_failure_noop",
            }
        if is_attempt:
            attempts[source] = attempts.get(source, 0) + 1
        if event_type == "reasoning.provider_failure_noop":
            if not isinstance(data, Mapping) or data.get("reason") != "invalid_output":
                continue
            invalid[source] = invalid.get(source, 0) + 1

    by_agent: dict[str, AgentSchemaCompliance] = {}
    for agent_id in sorted(set(attempts) | set(invalid)):
        responses = attempts.get(agent_id, 0)
        invalid_output = invalid.get(agent_id, 0)
        if invalid_output > responses:
            raise ValueError(
                f"schema failures exceed response attempts for {agent_id}"
            )
        by_agent[agent_id] = AgentSchemaCompliance(
            responses=responses,
            compliant=responses - invalid_output,
            invalid_output=invalid_output,
        )
    return SchemaComplianceEvidence(
        responses=sum(item.responses for item in by_agent.values()),
        compliant=sum(item.compliant for item in by_agent.values()),
        invalid_output=sum(item.invalid_output for item in by_agent.values()),
        by_agent=by_agent,
    )


def _raw_plan_evidence(
    events: Sequence[Mapping[str, Any]],
    *,
    scenario: Any,
) -> RawPlanEvidence:
    """Diagnose raw domain proposals without changing benchmark pass/fail.

    Target matching is command-level: it asks whether a command aimed at a
    frozen target field chose a value accepted by that phase's contract.  It
    intentionally does not claim that omitted commands are unreasonable.
    """

    injection_seq = next(
        (
            event.get("seq")
            for event in events
            if event.get("event_type") == "benchmark.perturbation_injected"
            and isinstance(event.get("seq"), int)
        ),
        None,
    )

    base_targets = {
        (effect.device_id, path): expected
        for effect in scenario.expected_device_effects
        for path, expected in effect.expected.items()
    }
    intervention = getattr(scenario, "intervention_response", None)
    intervention_targets = {
        (effect.device_id, path): expected
        for effect in (
            intervention.expected_device_effects
            if intervention is not None
            else []
        )
        for path, expected in effect.expected.items()
    }

    proposed = 0
    admitted = 0
    whitelist_rejected = 0
    validation_failed = 0
    valid = 0
    invalid_reasons: dict[str, int] = {}
    target_commands = 0
    target_matches = 0
    for event in events:
        if event.get("event_type") != "reasoning.execution_plan":
            continue
        data = event.get("data")
        if not isinstance(data, Mapping) or data.get("execution_mode") != "llm":
            continue
        raw_commands = data.get("raw_commands")
        candidate_commands = data.get("candidate_commands")
        assessments = data.get("raw_command_assessments")
        if (
            not isinstance(raw_commands, list)
            or not isinstance(candidate_commands, list)
            or not isinstance(assessments, list)
            or len(assessments) != len(raw_commands)
            or len(raw_commands) > MAX_PROPOSED_COMMANDS
            or len(candidate_commands) > MAX_PROPOSED_COMMANDS
        ):
            raise ValueError("execution plan lacks raw/admitted command evidence")
        proposed += len(raw_commands)
        event_admitted = 0
        for index, assessment in enumerate(assessments):
            if not isinstance(assessment, Mapping):
                raise ValueError("raw command assessment must be an object")
            if assessment.get("command_index") != index:
                raise ValueError("raw command assessment indices are not canonical")
            admitted_by_agent = assessment.get("admitted_by_agent")
            valid_at_plan_time = assessment.get("valid_at_plan_time")
            failure_code = assessment.get("failure_code")
            if not isinstance(admitted_by_agent, bool) or not isinstance(
                valid_at_plan_time,
                bool,
            ):
                raise ValueError("raw command assessment flags must be booleans")
            if valid_at_plan_time != (admitted_by_agent and failure_code is None):
                raise ValueError("raw command assessment outcome is inconsistent")
            event_admitted += int(admitted_by_agent)
            valid += int(valid_at_plan_time)
            if failure_code is not None:
                if not isinstance(failure_code, str) or not failure_code:
                    raise ValueError("raw command failure code must be a string")
                invalid_reasons[failure_code] = invalid_reasons.get(failure_code, 0) + 1
                if admitted_by_agent:
                    validation_failed += 1
                elif failure_code == "agent_whitelist_rejected":
                    whitelist_rejected += 1
                else:
                    raise ValueError("non-admitted command has an invalid rejection code")
        if event_admitted != len(candidate_commands):
            raise ValueError("assessment admission differs from candidate commands")
        admitted += event_admitted
        seq = event.get("seq")
        after_intervention = (
            isinstance(seq, int)
            and injection_seq is not None
            and seq > injection_seq
        )
        targets = intervention_targets if after_intervention else base_targets
        for command in raw_commands:
            if not isinstance(command, Mapping):
                raise ValueError("raw command evidence must contain objects")
            target = targets.get(
                (str(command.get("device_id") or ""), str(command.get("property") or ""))
            )
            if target is None:
                continue
            target_commands += 1
            if target.matches(command.get("value")):
                target_matches += 1

    return RawPlanEvidence(
        proposed_commands=proposed,
        admitted_commands=admitted,
        whitelist_rejected_commands=whitelist_rejected,
        validation_failed_commands=validation_failed,
        valid_commands=valid,
        invalid_reasons=dict(sorted(invalid_reasons.items())),
        frozen_target_commands=target_commands,
        frozen_target_matches=target_matches,
    )


def _replay_equivalence_trace(events: Sequence[Mapping[str, Any]]) -> str:
    """Compare semantics while retaining visible capture/replay audit labels.

    ``recording_source_run_id`` and provider role are expected provenance
    differences, not simulated-world differences. Normalize only those two
    declared fields before applying the repository's normal canonical trace.
    """

    def normalize(value: Any) -> Any:
        if isinstance(value, Mapping):
            normalized = {
                str(key): normalize(item)
                for key, item in value.items()
            }
            if "recording_source_run_id" in normalized:
                normalized["recording_source_run_id"] = None
            for key in (
                "llm_endpoint",
                "llm_protocol_version",
                "llm_max_tokens",
                "llm_timeout_ms",
                "llm_strict_output",
                "llm_decision_schema_sha256",
            ):
                if key in normalized:
                    normalized[key] = None
            if normalized.get("provider") in {"recording", "replay"}:
                normalized["provider"] = "recorded_decision"
            return normalized
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    return canonical_trace_text(normalize(event) for event in events)


def _ratio(numerator: int, denominator: int) -> RatioMetric:
    return RatioMetric(
        numerator=numerator,
        denominator=denominator,
        rate=(None if denominator == 0 else round(numerator / denominator, 8)),
    )


def summarize_llm_substudy(
    study: ResolvedLLMSubstudy,
    *,
    output_dir: Path | str,
    provider_factory: Callable[[ResolvedLLMSubstudy], LLMProvider] = build_live_provider,
) -> Path:
    if read_source_revision() != study.source_revision:
        raise ValueError("source revision drift: resolve the substudy again")
    preflight = validate_preflight_receipt(study, output_dir=output_dir)
    runner = LLMSubstudyRunner(
        study,
        output_dir=output_dir,
        provider_factory=provider_factory,
    )
    by_kind = {
        kind: {"planned": 0, "admitted": 0, "invalid_evidence": 0, "failed": 0}
        for kind in ("live", "capture", "replay")
    }
    status_counts = {"admitted": 0, "invalid_evidence": 0, "failed": 0}
    outcomes: dict[str, int] = {}
    usage_totals: dict[str, float | int] = {
        "calls": 0,
        "billable_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    replay_outcomes: dict[str, int] = {}
    replay_usage_totals: dict[str, float | int] = {
        "calls": 0,
        "billable_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    replay_equivalent = 0
    model_failures_by_kind = {kind: 0 for kind in ("live", "capture", "replay")}
    model_failure_reasons: dict[str, int] = {}
    replay_model_failure_reasons: dict[str, int] = {}
    response_models: dict[str, int] = {}
    replay_response_models: dict[str, int] = {}
    schema_responses = 0
    schema_compliant = 0
    schema_by_agent: dict[str, list[int]] = {}
    conditional_eligible = 0
    conditional_passed = 0
    live_passed = 0
    raw_proposed = 0
    raw_valid = 0
    raw_target_commands = 0
    raw_target_matches = 0
    result_hashes: dict[str, str] = {}
    for slot in study.slots:
        by_kind[slot.kind]["planned"] += 1
        artifact = read_slot_result(output_dir, slot.slot_id)
        result = artifact.result
        if not runner.validate_result(slot, result):
            raise ValueError(f"slot evidence failed revalidation: {slot.slot_id}")
        status_counts[result.status] += 1
        by_kind[slot.kind][result.status] += 1
        result_hashes[slot.slot_id] = artifact.seal.sha256
        if result.evaluation is not None:
            outcome = str(result.evaluation.get("outcome"))
            target_outcomes = replay_outcomes if slot.kind == "replay" else outcomes
            target_outcomes[outcome] = target_outcomes.get(outcome, 0) + 1
            if slot.kind == "live" and outcome == "pass":
                live_passed += 1
        if result.usage is not None:
            target_usage = replay_usage_totals if slot.kind == "replay" else usage_totals
            for key in target_usage:
                target_usage[key] += result.usage.get(key, 0)
            target_models = (
                replay_response_models
                if slot.kind == "replay"
                else response_models
            )
            for model, count in result.usage.get("response_models", {}).items():
                target_models[model] = target_models.get(model, 0) + int(count)
        replay_equivalent += int(result.replay_equivalent is True)
        model_failures_by_kind[slot.kind] += result.model_failure_count
        target_failures = (
            replay_model_failure_reasons
            if slot.kind == "replay"
            else model_failure_reasons
        )
        for reason, count in result.model_failure_reasons.items():
            target_failures[reason] = target_failures.get(reason, 0) + count
        if slot.kind in {"live", "capture"} and result.status == "admitted":
            if result.schema_compliance is None or result.raw_plan is None:
                raise ValueError("source slot is missing capability evidence")
            schema_responses += result.schema_compliance.responses
            schema_compliant += result.schema_compliance.compliant
            for agent_id, evidence in result.schema_compliance.by_agent.items():
                counts = schema_by_agent.setdefault(agent_id, [0, 0])
                counts[0] += evidence.compliant
                counts[1] += evidence.responses
            if result.schema_compliance.invalid_output == 0:
                conditional_eligible += 1
                conditional_passed += int(
                    result.evaluation is not None
                    and result.evaluation.get("outcome") == "pass"
                )
            raw_proposed += result.raw_plan.proposed_commands
            raw_valid += result.raw_plan.valid_commands
            raw_target_commands += result.raw_plan.frozen_target_commands
            raw_target_matches += result.raw_plan.frozen_target_matches
    usage_totals["cost_usd"] = round(float(usage_totals["cost_usd"]), 8)
    replay_usage_totals["cost_usd"] = round(
        float(replay_usage_totals["cost_usd"]),
        8,
    )
    passed = status_counts == {"admitted": EXPECTED_SLOT_COUNT, "invalid_evidence": 0, "failed": 0} and replay_equivalent == 72
    capability_metrics = CapabilityMetrics(
        schema_compliance=_ratio(schema_compliant, schema_responses),
        schema_compliance_by_agent={
            agent_id: _ratio(counts[0], counts[1])
            for agent_id, counts in sorted(schema_by_agent.items())
        },
        conditional_task_success=_ratio(
            conditional_passed,
            conditional_eligible,
        ),
        live_only_success=_ratio(
            live_passed,
            by_kind["live"]["planned"],
        ),
        raw_command_validity=_ratio(raw_valid, raw_proposed),
        frozen_target_command_match=_ratio(
            raw_target_matches,
            raw_target_commands,
        ),
    )
    results = SubstudyResultsManifest(
        study_id=study.study_id,
        study_hash=study.study_hash,
        source_revision=study.source_revision,
        provider=study.provider,
        model=study.model,
        endpoint=study.endpoint,
        anthropic_version=study.anthropic_version,
        max_tokens=study.max_tokens,
        timeout_ms=study.timeout_ms,
        strict_output=study.strict_output,
        decision_schema_sha256=study.decision_schema_sha256,
        preflight_sha256=preflight.seal.sha256,
        billing_mode=study.billing_mode,
        planned=len(study.slots),
        **status_counts,
        by_kind=by_kind,
        evaluation_outcomes=outcomes,
        usage_totals=usage_totals,
        replay_equivalent=replay_equivalent,
        model_failures_by_kind=model_failures_by_kind,
        model_failure_reasons=model_failure_reasons,
        response_models=response_models,
        replay_diagnostics=ReplayDiagnostics(
            evaluation_outcomes=replay_outcomes,
            usage_totals=replay_usage_totals,
            model_failure_reasons=replay_model_failure_reasons,
            response_models=replay_response_models,
        ),
        capability_metrics=capability_metrics,
        scientific_gate="passed" if passed else "failed",
        slot_result_sha256=result_hashes,
    )
    sealed = SealedSubstudyResults(
        results=results,
        seal=ArtifactSeal(sha256=sha256_json(results.model_dump(mode="json"))),
    )
    return atomic_create_json(
        Path(output_dir) / RESULTS_MANIFEST_FILENAME,
        sealed.model_dump(mode="json"),
        max_bytes=MAX_SUBSTUDY_BYTES,
    )


__all__ = [
    "EXPECTED_INSTANCE_COUNT",
    "EXPECTED_SLOT_COUNT",
    "LLMSubstudyRunner",
    "LLMSubstudySpec",
    "MINIMAX_M3_ENDPOINT",
    "ResolvedLLMSubstudy",
    "SlotResult",
    "SlotResultArtifact",
    "build_live_provider",
    "load_llm_substudy",
    "preflight_llm_substudy",
    "read_resolved_llm_substudy",
    "read_slot_result",
    "resolve_llm_substudy",
    "summarize_llm_substudy",
    "validate_preflight_receipt",
    "write_resolved_llm_substudy",
]
