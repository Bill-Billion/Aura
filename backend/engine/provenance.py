"""Typed scientific-condition provenance shared by runs and experiments."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.schemas import BaselinePolicy


class ResearchRuntimeProfile(str, Enum):
    """The four runtime conditions that have an implemented research meaning."""

    SINGLE_DIRECT = "single_direct"
    NO_ARBITER = "no_arbiter"
    FLAT_PRIORITY = "flat_priority"
    AURA = "aura"


RuntimeAxes = tuple[
    Literal["single", "domain_multi"],
    Literal["none", "flat_priority", "aura"],
    Literal["stale_offline"],
]

RESEARCH_RUNTIME_PROFILES: Mapping[ResearchRuntimeProfile, RuntimeAxes] = (
    MappingProxyType(
        {
            ResearchRuntimeProfile.SINGLE_DIRECT: (
                "single",
                "none",
                "stale_offline",
            ),
            ResearchRuntimeProfile.NO_ARBITER: (
                "domain_multi",
                "none",
                "stale_offline",
            ),
            ResearchRuntimeProfile.FLAT_PRIORITY: (
                "domain_multi",
                "flat_priority",
                "stale_offline",
            ),
            ResearchRuntimeProfile.AURA: (
                "domain_multi",
                "aura",
                "stale_offline",
            ),
        }
    )
)
_PROFILE_BY_AXES: dict[tuple[str, str, str], ResearchRuntimeProfile] = {
    axes: profile for profile, axes in RESEARCH_RUNTIME_PROFILES.items()
}


def research_runtime_profile_for_axes(
    *,
    topology: str,
    governance: str,
    observation: str,
) -> ResearchRuntimeProfile:
    """Resolve one implemented profile; reject unsupported axis cross-products."""

    axes = (topology, governance, observation)
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
            f"observation={observation!r}; supported profiles: {supported}"
        ) from exc


class ExperimentProvenance(BaseModel):
    """Resolved matrix condition embedded in every produced ``run.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = Field(min_length=1, max_length=128)
    matrix_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cell_id: str = Field(pattern=r"^cell-[0-9a-f]{16,64}$")
    runtime_profile: ResearchRuntimeProfile = ResearchRuntimeProfile.AURA
    model: Literal["rule_based", "mocked"]
    topology: Literal["single", "domain_multi"]
    governance: Literal["none", "flat_priority", "aura"]
    observation: Literal["perfect", "stale_offline"]
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
    model: Literal["rule_based", "mocked"]
    topology: Literal["single", "domain_multi"] = "domain_multi"
    governance: Literal["none", "flat_priority", "aura"] = "aura"
    observation: Literal["stale_offline"] = "stale_offline"
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
        expected = (
            BaselinePolicy.RULE_BASED
            if self.model == "rule_based"
            else BaselinePolicy.LLM_MOCKED
        )
        if self.baseline_policy is not expected:
            raise ValueError("runtime model does not match baseline policy")
        return self

    @classmethod
    def for_profile(
        cls,
        profile: ResearchRuntimeProfile,
        *,
        model: Literal["rule_based", "mocked"],
        baseline_policy: BaselinePolicy,
    ) -> "ExperimentRuntimeSelection":
        topology, governance, observation = RESEARCH_RUNTIME_PROFILES[profile]
        return cls(
            runtime_profile=profile,
            model=model,
            topology=topology,
            governance=governance,
            observation=observation,
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
    "ExperimentProvenance",
    "ExperimentRuntimeSelection",
    "ResearchRuntimeProfile",
    "research_runtime_profile_for_axes",
]
