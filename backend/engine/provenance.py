"""Typed scientific-condition provenance shared by runs and experiments."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.schemas import BaselinePolicy


ExperimentModel = Literal["rule_based", "mocked", "live", "recorded", "replay"]

_BASELINE_BY_EXPERIMENT_MODEL: Mapping[str, BaselinePolicy] = MappingProxyType(
    {
        "rule_based": BaselinePolicy.RULE_BASED,
        "mocked": BaselinePolicy.LLM_MOCKED,
        "live": BaselinePolicy.LLM_LIVE,
        "recorded": BaselinePolicy.LLM_RECORDED,
        "replay": BaselinePolicy.LLM_RECORDED,
    }
)


class ResearchRuntimeProfile(str, Enum):
    """The four runtime conditions that have an implemented research meaning."""

    SINGLE_DIRECT = "single_direct"
    NO_ARBITER = "no_arbiter"
    FLAT_PRIORITY = "flat_priority"
    AURA = "aura"


class ObservationCondition(str, Enum):
    """Independent agent-observation treatments implemented by the runtime."""

    PERFECT = "perfect"
    STALE_OFFLINE = "stale_offline"


SUPPORTED_OBSERVATION_CONDITIONS: frozenset[ObservationCondition] = frozenset(
    ObservationCondition
)


RuntimeAxes = tuple[
    Literal["single", "domain_multi"],
    Literal["none", "flat_priority", "aura"],
]

RESEARCH_RUNTIME_PROFILES: Mapping[ResearchRuntimeProfile, RuntimeAxes] = (
    MappingProxyType(
        {
            ResearchRuntimeProfile.SINGLE_DIRECT: (
                "single",
                "none",
            ),
            ResearchRuntimeProfile.NO_ARBITER: (
                "domain_multi",
                "none",
            ),
            ResearchRuntimeProfile.FLAT_PRIORITY: (
                "domain_multi",
                "flat_priority",
            ),
            ResearchRuntimeProfile.AURA: (
                "domain_multi",
                "aura",
            ),
        }
    )
)
_PROFILE_BY_AXES: dict[tuple[str, str], ResearchRuntimeProfile] = {
    axes: profile for profile, axes in RESEARCH_RUNTIME_PROFILES.items()
}


def research_runtime_profile_for_axes(
    *,
    topology: str,
    governance: str,
    observation: str | ObservationCondition = ObservationCondition.STALE_OFFLINE,
) -> ResearchRuntimeProfile:
    """Resolve topology/governance while independently validating observation.

    ``observation`` remains in the call signature so existing matrix/fairness
    callers do not need a flag day.  It is deliberately absent from the profile
    lookup key: observation is a separate experimental treatment, not a hidden
    part of controller topology.
    """

    try:
        ObservationCondition(observation)
    except ValueError as exc:
        supported_observations = ", ".join(
            sorted(item.value for item in SUPPORTED_OBSERVATION_CONDITIONS)
        )
        raise ValueError(
            f"unsupported observation condition {observation!r}; "
            f"implemented values: {supported_observations}"
        ) from exc

    axes = (topology, governance)
    try:
        return _PROFILE_BY_AXES[axes]
    except KeyError as exc:
        supported = ", ".join(
            f"{profile.value}=({', '.join(values)})"
            for profile, values in RESEARCH_RUNTIME_PROFILES.items()
        )
        raise ValueError(
            "runtime axes do not identify an implemented research profile: "
            f"topology={topology!r}, governance={governance!r}, "
            f"observation={str(observation)!r}; supported profiles: {supported}"
        ) from exc


class ExperimentProvenance(BaseModel):
    """Resolved matrix condition embedded in every produced ``run.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = Field(min_length=1, max_length=128)
    matrix_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cell_id: str = Field(pattern=r"^cell-[0-9a-f]{16,64}$")
    runtime_profile: ResearchRuntimeProfile = ResearchRuntimeProfile.AURA
    model: ExperimentModel
    topology: Literal["single", "domain_multi"]
    governance: Literal["none", "flat_priority", "aura"]
    observation: ObservationCondition
    repetition: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_runtime_profile(self) -> "ExperimentProvenance":
        resolved = research_runtime_profile_for_axes(
            topology=self.topology,
            governance=self.governance,
            observation=self.observation,
        )
        if self.runtime_profile is not resolved:
            raise ValueError(
                "experiment runtime_profile does not match topology/governance/observation"
            )
        return self


class ExperimentRuntimeSelection(BaseModel):
    """Runtime-owned condition that was actually activated for a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_profile: ResearchRuntimeProfile = ResearchRuntimeProfile.AURA
    model: ExperimentModel
    topology: Literal["single", "domain_multi"] = "domain_multi"
    governance: Literal["none", "flat_priority", "aura"] = "aura"
    observation: ObservationCondition = ObservationCondition.STALE_OFFLINE
    baseline_policy: BaselinePolicy

    @model_validator(mode="after")
    def _validate_profile_and_model_policy(self) -> "ExperimentRuntimeSelection":
        resolved = research_runtime_profile_for_axes(
            topology=self.topology,
            governance=self.governance,
            observation=self.observation,
        )
        if self.runtime_profile is not resolved:
            raise ValueError(
                "runtime_profile does not match topology/governance/observation"
            )
        expected = _BASELINE_BY_EXPERIMENT_MODEL[self.model]
        if self.baseline_policy is not expected:
            raise ValueError("runtime model does not match baseline policy")
        return self

    @classmethod
    def for_profile(
        cls,
        profile: ResearchRuntimeProfile,
        *,
        model: ExperimentModel,
        baseline_policy: BaselinePolicy,
        observation: ObservationCondition | Literal["perfect", "stale_offline"] = (
            ObservationCondition.STALE_OFFLINE
        ),
    ) -> "ExperimentRuntimeSelection":
        topology, governance = RESEARCH_RUNTIME_PROFILES[profile]
        return cls(
            runtime_profile=profile,
            model=model,
            topology=topology,
            governance=governance,
            observation=ObservationCondition(observation),
            baseline_policy=baseline_policy,
        )

    def validate_provenance(self, provenance: ExperimentProvenance) -> None:
        activated = (
            self.runtime_profile,
            self.model,
            self.topology,
            self.governance,
            self.observation,
        )
        recorded = (
            provenance.runtime_profile,
            provenance.model,
            provenance.topology,
            provenance.governance,
            provenance.observation,
        )
        if recorded != activated:
            raise ValueError(
                "experiment provenance does not match the activated runtime condition"
            )


__all__ = [
    "RESEARCH_RUNTIME_PROFILES",
    "SUPPORTED_OBSERVATION_CONDITIONS",
    "ExperimentProvenance",
    "ExperimentRuntimeSelection",
    "ExperimentModel",
    "ObservationCondition",
    "ResearchRuntimeProfile",
    "research_runtime_profile_for_axes",
]
