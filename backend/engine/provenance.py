"""Typed scientific-condition provenance shared by runs and experiments."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.models.schemas import BaselinePolicy


class ExperimentProvenance(BaseModel):
    """Resolved matrix condition embedded in every produced ``run.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = Field(min_length=1, max_length=128)
    matrix_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    matrix_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cell_id: str = Field(pattern=r"^cell-[0-9a-f]{16,64}$")
    model: Literal["rule_based", "mocked"]
    topology: Literal["single", "domain_multi"]
    governance: Literal["none", "flat_priority", "aura"]
    observation: Literal["perfect", "stale_offline"]
    repetition: int = Field(ge=0)


class ExperimentRuntimeSelection(BaseModel):
    """Runtime-owned condition that was actually activated for a run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: Literal["rule_based", "mocked"]
    topology: Literal["domain_multi"] = "domain_multi"
    governance: Literal["aura"] = "aura"
    observation: Literal["stale_offline"] = "stale_offline"
    baseline_policy: BaselinePolicy

    @model_validator(mode="after")
    def _validate_model_policy(self) -> "ExperimentRuntimeSelection":
        expected = (
            BaselinePolicy.RULE_BASED
            if self.model == "rule_based"
            else BaselinePolicy.LLM_MOCKED
        )
        if self.baseline_policy is not expected:
            raise ValueError("runtime model does not match baseline policy")
        return self

    def validate_provenance(self, provenance: ExperimentProvenance) -> None:
        activated = (
            self.model,
            self.topology,
            self.governance,
            self.observation,
        )
        recorded = (
            provenance.model,
            provenance.topology,
            provenance.governance,
            provenance.observation,
        )
        if recorded != activated:
            raise ValueError(
                "experiment provenance does not match the activated runtime condition"
            )


__all__ = ["ExperimentProvenance", "ExperimentRuntimeSelection"]
