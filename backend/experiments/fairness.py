"""Fail-closed fairness checks for baseline comparison groups."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

from backend.engine.provenance import (
    ResearchRuntimeProfile,
    research_runtime_profile_for_axes,
)

from .spec import ExperimentCell, sha256_json


FAIRNESS_SCHEMA_VERSION = "1.0"
_SINGLE_DIRECT_AGENT_IDS = frozenset({"single_direct_agent"})
_DOMAIN_MULTI_AGENT_IDS = frozenset(
    {
        "lighting_agent",
        "hvac_agent",
        "security_agent",
        "energy_agent",
        "scene_agent",
    }
)


class FairnessPayload(BaseModel):
    """Complete fixed-condition attestation stored with one cell result."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    fairness_schema_version: Literal["1.0"] = FAIRNESS_SCHEMA_VERSION
    comparison_group_id: str = Field(pattern=r"^group-[0-9a-f]{32}$")
    runtime_profile: ResearchRuntimeProfile
    scenario_id: str = Field(min_length=1, max_length=512)
    scenario_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: StrictInt = Field(ge=0)
    model: Literal["rule_based", "mocked"]
    observation: Literal["stale_offline"]
    repetition: StrictInt = Field(ge=0)
    source_revision: str = Field(min_length=1, max_length=512)
    sim_version: str = Field(min_length=1, max_length=512)
    agent_versions: dict[str, str] = Field(min_length=1)
    llm_provider: str = Field(min_length=1, max_length=512)
    llm_model: str = Field(min_length=1, max_length=512)
    llm_mode: Literal["rule_based", "mocked"]
    baseline_policy: Literal["rule_based", "llm_mocked"]
    duration_seconds: StrictFloat = Field(gt=0)
    initial_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_schema_version: str = Field(min_length=1, max_length=64)
    event_schema_version: str = Field(min_length=1, max_length=64)
    command_schema_version: str = Field(min_length=1, max_length=64)
    device_registry_version: str = Field(min_length=1, max_length=64)
    trace_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluator_source_revision: str = Field(min_length=1, max_length=512)
    report_schema_version: str = Field(min_length=1, max_length=64)

    @field_validator("agent_versions")
    @classmethod
    def _valid_agent_versions(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key or not version for key, version in value.items()):
            raise ValueError("agent version keys and values must be non-empty")
        if any(len(key) > 128 or len(version) > 512 for key, version in value.items()):
            raise ValueError("agent version keys or values are too long")
        return dict(sorted(value.items()))

    @model_validator(mode="after")
    def _profile_agent_contract(self) -> "FairnessPayload":
        expected = (
            _SINGLE_DIRECT_AGENT_IDS
            if self.runtime_profile is ResearchRuntimeProfile.SINGLE_DIRECT
            else _DOMAIN_MULTI_AGENT_IDS
        )
        if set(self.agent_versions) != expected:
            raise ValueError(
                f"agent_versions do not match {self.runtime_profile.value} profile"
            )
        expected_mode = "rule_based" if self.model == "rule_based" else "mocked"
        expected_policy = (
            "rule_based" if self.model == "rule_based" else "llm_mocked"
        )
        if self.llm_mode != expected_mode or self.baseline_policy != expected_policy:
            raise ValueError("model does not match llm_mode/baseline_policy")
        return self


def comparison_group_id(cell: ExperimentCell) -> str:
    """Stable identity shared by cells that differ only by runtime profile."""

    payload = {
        "scenario_id": cell.scenario_id,
        "scenario_contract_hash": cell.scenario_contract_hash,
        "seed": cell.seed,
        "model": cell.model,
        "observation": cell.observation,
        "repetition": cell.repetition,
        "source_revision": cell.source_revision,
    }
    return f"group-{sha256_json(payload)[:32]}"


def runtime_profile_id(cell: ExperimentCell) -> ResearchRuntimeProfile:
    return research_runtime_profile_for_axes(
        topology=cell.topology,
        governance=cell.governance,
        observation=cell.observation,
    )


def _expected_profile_set(
    expected_profiles: Sequence[ResearchRuntimeProfile],
) -> set[ResearchRuntimeProfile]:
    expected = set(expected_profiles)
    if not expected:
        raise ValueError("expected runtime profiles must not be empty")
    if len(expected) != len(expected_profiles):
        raise ValueError("expected runtime profiles must be unique")
    return expected


def validate_comparison_plan(
    cells: Sequence[ExperimentCell],
    *,
    expected_profiles: Sequence[ResearchRuntimeProfile],
) -> None:
    """Reject missing, duplicate, or undeclared profile members in every group."""

    expected = _expected_profile_set(expected_profiles)
    if not cells:
        raise ValueError("comparison plan contains no cells")
    grouped: dict[str, list[ExperimentCell]] = defaultdict(list)
    for cell in cells:
        grouped[comparison_group_id(cell)].append(cell)

    for group_id, members in sorted(grouped.items()):
        profiles = [runtime_profile_id(cell) for cell in members]
        if len(profiles) != len(set(profiles)):
            raise ValueError(
                f"comparison group {group_id} contains a duplicate runtime profile"
            )
        actual = set(profiles)
        if actual != expected:
            missing = sorted(profile.value for profile in expected - actual)
            extra = sorted(profile.value for profile in actual - expected)
            raise ValueError(
                f"comparison group {group_id} is unbalanced; "
                f"missing={missing}, extra={extra}"
            )


def build_fairness_payload(
    cell: ExperimentCell,
    *,
    run_metadata: Mapping[str, Any],
    evaluation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a complete, typed fixed-condition attestation for one result."""

    provenance = evaluation.get("provenance")
    if not isinstance(provenance, Mapping):
        provenance = {}
    payload = FairnessPayload.model_validate(
        {
            "fairness_schema_version": FAIRNESS_SCHEMA_VERSION,
            "comparison_group_id": comparison_group_id(cell),
            "runtime_profile": runtime_profile_id(cell),
            "scenario_id": cell.scenario_id,
            "scenario_contract_hash": cell.scenario_contract_hash,
            "seed": cell.seed,
            "model": cell.model,
            "observation": cell.observation,
            "repetition": cell.repetition,
            "source_revision": run_metadata.get("source_revision"),
            "sim_version": run_metadata.get("sim_version"),
            "agent_versions": run_metadata.get("agent_versions"),
            "llm_provider": run_metadata.get("llm_provider"),
            "llm_model": run_metadata.get("llm_model"),
            "llm_mode": run_metadata.get("llm_mode"),
            "baseline_policy": run_metadata.get("baseline_policy"),
            "duration_seconds": run_metadata.get("duration_seconds"),
            "initial_state_hash": run_metadata.get("initial_state_hash"),
            "scenario_schema_version": run_metadata.get("scenario_schema_version"),
            "event_schema_version": run_metadata.get("event_schema_version"),
            "command_schema_version": run_metadata.get("command_schema_version"),
            "device_registry_version": run_metadata.get("device_registry_version"),
            "trace_spec_hash": run_metadata.get("trace_spec_hash"),
            "evaluator_source_revision": provenance.get(
                "evaluator_source_revision"
            ),
            "report_schema_version": evaluation.get("report_schema_version"),
        }
    )
    if (
        payload.source_revision != cell.source_revision
        or payload.evaluator_source_revision != cell.source_revision
    ):
        raise ValueError("runtime/evaluator revision does not match experiment cell")
    return payload.model_dump(mode="json")


@dataclass(frozen=True)
class FairnessAudit:
    valid_groups: int
    invalid_groups: int
    invalid_reasons: dict[str, list[str]]
    valid_cell_ids: tuple[str, ...] = ()


def _payload_matches_cell(payload: FairnessPayload, cell: ExperimentCell) -> bool:
    return (
        payload.comparison_group_id == comparison_group_id(cell)
        and payload.runtime_profile is runtime_profile_id(cell)
        and payload.scenario_id == cell.scenario_id
        and payload.scenario_contract_hash == cell.scenario_contract_hash
        and payload.seed == cell.seed
        and payload.model == cell.model
        and payload.observation == cell.observation
        and payload.repetition == cell.repetition
        and payload.source_revision == cell.source_revision
        and payload.evaluator_source_revision == cell.source_revision
    )


def _fixed_payload(payload: FairnessPayload) -> dict[str, Any]:
    return payload.model_dump(
        mode="json",
        exclude={"runtime_profile", "agent_versions"},
    )


def audit_comparison_outputs(
    cells: Sequence[ExperimentCell],
    completed_outputs: Mapping[str, Mapping[str, Any]],
    *,
    expected_profiles: Sequence[ResearchRuntimeProfile],
) -> FairnessAudit:
    """Audit full-matrix results; shard-local summaries must not call this."""

    expected = _expected_profile_set(expected_profiles)
    validate_comparison_plan(cells, expected_profiles=expected_profiles)
    if len(expected) == 1:
        return FairnessAudit(0, 0, {})

    grouped: dict[str, list[ExperimentCell]] = defaultdict(list)
    for cell in cells:
        grouped[comparison_group_id(cell)].append(cell)

    valid = 0
    valid_cell_ids: list[str] = []
    invalid_reasons: dict[str, list[str]] = {}
    for group_id, members in sorted(grouped.items()):
        reasons: list[str] = []
        payloads: dict[ResearchRuntimeProfile, FairnessPayload] = {}
        for cell in members:
            profile = runtime_profile_id(cell)
            output = completed_outputs.get(cell.cell_id)
            if output is None:
                reasons.append(
                    f"{profile.value}: missing or invalid completed evidence"
                )
                continue
            fairness = output.get("fairness")
            if not isinstance(fairness, Mapping):
                reasons.append(f"{profile.value}: missing fairness payload")
                continue
            try:
                payload = FairnessPayload.model_validate(fairness)
            except ValidationError as exc:
                reasons.append(
                    f"{profile.value}: invalid fairness payload: {exc.errors()[0]['msg']}"
                )
                continue
            if not _payload_matches_cell(payload, cell):
                reasons.append(f"{profile.value}: fairness payload does not match cell")
                continue
            payloads[profile] = payload

        if len(payloads) == len(members):
            ordered = [
                payloads[profile]
                for profile in sorted(payloads, key=lambda profile: profile.value)
            ]
            reference = _fixed_payload(ordered[0])
            for payload in ordered[1:]:
                comparable = _fixed_payload(payload)
                if comparable != reference:
                    differing = sorted(
                        key
                        for key in set(reference) | set(comparable)
                        if reference.get(key) != comparable.get(key)
                    )
                    reasons.append(
                        "fixed provenance mismatch: " + ", ".join(differing)
                    )
                    break

            multi_versions = {
                tuple(sorted(payload.agent_versions.items()))
                for payload in ordered
                if payload.runtime_profile is not ResearchRuntimeProfile.SINGLE_DIRECT
            }
            if len(multi_versions) > 1:
                reasons.append("fixed provenance mismatch: agent_versions")

        if reasons:
            invalid_reasons[group_id] = reasons
        else:
            valid += 1
            valid_cell_ids.extend(cell.cell_id for cell in members)

    return FairnessAudit(
        valid_groups=valid,
        invalid_groups=len(invalid_reasons),
        invalid_reasons=invalid_reasons,
        valid_cell_ids=tuple(sorted(valid_cell_ids)),
    )


__all__ = [
    "FAIRNESS_SCHEMA_VERSION",
    "FairnessAudit",
    "FairnessPayload",
    "audit_comparison_outputs",
    "build_fairness_payload",
    "comparison_group_id",
    "runtime_profile_id",
    "validate_comparison_plan",
]
