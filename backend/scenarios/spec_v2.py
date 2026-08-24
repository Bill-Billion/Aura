"""AuraBench ScenarioSpec 2.x data contract.

``ScenarioSpecV2`` subclasses the proven v1 runtime contract.  Existing apply,
generator, runner, and evaluator code can therefore consume it without a
parallel execution path.  The new fields describe benchmark identity,
counterfactual interventions, resident references, and typed trace properties;
they do not execute perturbations or verify traces in PR-1.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Annotated, Any, Literal, Protocol, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.models.versioning import check_scenario_schema_compatibility
from backend.scenarios.spec import ScenarioSpec
from backend.scenarios.trace_spec import TraceSpec


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HomeReference(_StrictModel):
    """Home configuration currently implemented by the Aura runtime."""

    topology_id: Literal["apartment_v1"] = "apartment_v1"
    device_configuration_id: Literal["default_registry_v1"] = "default_registry_v1"


class ResidentReference(_StrictModel):
    user_id: str = Field(min_length=1)
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    authority_level: Literal["child", "guest", "adult", "owner", "administrator"]


class ObservationModelReference(_StrictModel):
    """Only the observation behavior implemented before AuraBench PR-1."""

    id: Literal["current_projector_v1"] = "current_projector_v1"


class BenchmarkMetadata(_StrictModel):
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    generator_version: str = Field(pattern=r"^\d+\.\d+$")
    query_paraphrase_id: int = Field(default=0, ge=0, strict=True)


CounterfactualFactor: TypeAlias = Literal[
    "resident_state_change",
    "device_failure",
    "conflicting_request",
    "safety_interrupt",
    "observation_delay",
    "feedback_loss",
]


class CounterfactualReference(_StrictModel):
    group_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,95}$")
    variant: Literal["static", "dynamic"]
    factor: CounterfactualFactor


PerturbationPhase: TypeAlias = Literal[
    "before_perception",
    "after_perception_before_plan",
    "after_plan_before_execution",
    "during_execution",
    "after_execution_before_feedback",
]


class _PerturbationBase(_StrictModel):
    phase: PerturbationPhase
    at_sim_time_s: float | None = Field(default=None, ge=0.0)

    @field_validator("at_sim_time_s", mode="before")
    @classmethod
    def _finite_time(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("at_sim_time_s must be a finite number")
        resolved = float(value)
        if not math.isfinite(resolved):
            raise ValueError("at_sim_time_s must be a finite number")
        return resolved


class ResidentStateChangePerturbation(_PerturbationBase):
    type: Literal["resident_state_change"] = "resident_state_change"
    user_id: str = Field(min_length=1)
    room_id: str | None = Field(default=None, min_length=1)
    activity: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _has_state_change(self) -> "ResidentStateChangePerturbation":
        if self.room_id is None and self.activity is None:
            raise ValueError("resident state change requires room_id or activity")
        if self.at_sim_time_s is None:
            raise ValueError("resident_state_change requires at_sim_time_s")
        return self


class DeviceFailurePerturbation(_PerturbationBase):
    type: Literal["device_failure"] = "device_failure"
    device_id: str = Field(min_length=1)
    failure: Literal["offline"] = "offline"

    @model_validator(mode="after")
    def _requires_absolute_time(self) -> "DeviceFailurePerturbation":
        if self.at_sim_time_s is None:
            raise ValueError("device_failure requires at_sim_time_s")
        return self


class ConflictingRequestPerturbation(_PerturbationBase):
    type: Literal["conflicting_request"] = "conflicting_request"
    user_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    intent: str = Field(min_length=1)

    @model_validator(mode="after")
    def _requires_absolute_time(self) -> "ConflictingRequestPerturbation":
        if self.at_sim_time_s is None:
            raise ValueError("conflicting_request requires at_sim_time_s")
        return self


class SafetyInterruptPerturbation(_PerturbationBase):
    type: Literal["safety_interrupt"] = "safety_interrupt"
    room_id: str = Field(min_length=1)
    event_type: Literal["safety.smoke_detected"] = "safety.smoke_detected"
    severity: Literal["warning", "critical"] = "critical"

    @model_validator(mode="after")
    def _requires_absolute_time(self) -> "SafetyInterruptPerturbation":
        if self.at_sim_time_s is None:
            raise ValueError("safety_interrupt requires at_sim_time_s")
        return self


class ObservationDelayPerturbation(_PerturbationBase):
    type: Literal["observation_delay"] = "observation_delay"
    device_id: str = Field(min_length=1)
    delay_seconds: float = Field(gt=0.0)

    @field_validator("delay_seconds", mode="before")
    @classmethod
    def _finite_delay(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("delay_seconds must be a finite positive number")
        resolved = float(value)
        if not math.isfinite(resolved):
            raise ValueError("delay_seconds must be a finite positive number")
        return resolved


class FeedbackLossPerturbation(_PerturbationBase):
    type: Literal["feedback_loss"] = "feedback_loss"
    device_id: str = Field(min_length=1)
    drop_count: int = Field(default=1, ge=1, strict=True)

    @model_validator(mode="after")
    def _requires_absolute_time(self) -> "FeedbackLossPerturbation":
        if self.at_sim_time_s is None:
            raise ValueError("feedback_loss requires at_sim_time_s")
        return self


PerturbationSpec: TypeAlias = Annotated[
    ResidentStateChangePerturbation
    | DeviceFailurePerturbation
    | ConflictingRequestPerturbation
    | SafetyInterruptPerturbation
    | ObservationDelayPerturbation
    | FeedbackLossPerturbation,
    Field(discriminator="type"),
]


class ScenarioSpecV2(ScenarioSpec):
    """Strict ScenarioSpec 2.x; runtime-compatible because it extends v1."""

    scenario_schema_version: str = "2.0"
    family: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    split: Literal["dev", "validation", "test"]
    difficulty: Literal["easy", "medium", "hard"]
    home: HomeReference
    residents: list[ResidentReference] = Field(default_factory=list)
    observation_model: ObservationModelReference = Field(
        default_factory=ObservationModelReference
    )
    counterfactual: CounterfactualReference
    perturbations: list[PerturbationSpec] = Field(default_factory=list)
    trace_spec: TraceSpec
    benchmark: BenchmarkMetadata

    @field_validator("scenario_schema_version", mode="before")
    @classmethod
    def _v2_version(cls, value: Any) -> str:
        resolved = str(value)
        compatibility = check_scenario_schema_compatibility(resolved)
        if compatibility.declared[0] != 2:
            raise ValueError("ScenarioSpecV2 requires schema major 2")
        return resolved

    @field_validator("residents")
    @classmethod
    def _unique_residents(
        cls, value: list[ResidentReference]
    ) -> list[ResidentReference]:
        user_ids = [resident.user_id for resident in value]
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("residents must not repeat user_id")
        return value

    @model_validator(mode="after")
    def _counterfactual_shape(self) -> "ScenarioSpecV2":
        declared_users = set(self.initial_state.users)
        unknown_residents = sorted(
            resident.user_id
            for resident in self.residents
            if resident.user_id not in declared_users
        )
        if unknown_residents:
            raise ValueError(
                "resident references must exist in initial_state.users: "
                + ", ".join(unknown_residents)
            )
        unknown_perturbation_users = sorted(
            {
                user_id
                for item in self.perturbations
                if (user_id := getattr(item, "user_id", None))
                and user_id not in declared_users
            }
        )
        if unknown_perturbation_users:
            raise ValueError(
                "resident perturbations must reference initial_state.users: "
                + ", ".join(unknown_perturbation_users)
            )

        if self.counterfactual.variant == "static" and self.perturbations:
            raise ValueError(
                "static counterfactual variants cannot declare perturbations"
            )
        if self.counterfactual.variant == "dynamic" and not self.perturbations:
            raise ValueError("dynamic counterfactual variants require perturbations")
        unexpected = sorted(
            item.type
            for item in self.perturbations
            if item.type != self.counterfactual.factor
        )
        if unexpected:
            raise ValueError(
                "perturbation types must match counterfactual.factor: "
                + ", ".join(unexpected)
            )
        return self

    def referenced_device_ids(self) -> set[str]:
        ids = super().referenced_device_ids()
        for item in self.perturbations:
            device_id = getattr(item, "device_id", None)
            if device_id:
                ids.add(device_id)
        return ids

    def referenced_room_ids(self) -> set[str]:
        ids = super().referenced_room_ids()
        for item in self.perturbations:
            room_id = getattr(item, "room_id", None)
            if room_id and room_id != "outside":
                ids.add(room_id)
        return ids

    def summary(self) -> dict[str, Any]:
        return {
            **super().summary(),
            "family": self.family,
            "split": self.split,
            "difficulty": self.difficulty,
            "counterfactual_group_id": self.counterfactual.group_id,
            "counterfactual_variant": self.counterfactual.variant,
            "trace_property_count": len(self.trace_spec.properties),
        }


class PerturbationRuntime(Protocol):
    def inject_device_failure(
        self, device_id: str, *, at_sim_time_s: float = 0.0
    ) -> None: ...

    def inject_feedback_loss(
        self,
        device_id: str,
        *,
        drop_count: int = 1,
        at_sim_time_s: float = 0.0,
    ) -> None: ...

    def inject_resident_state_change(
        self,
        user_id: str,
        *,
        room_id: str | None,
        activity: str | None,
        at_sim_time_s: float,
    ) -> None: ...

    def inject_conflicting_request(
        self,
        user_id: str,
        *,
        room_id: str,
        intent: str,
        at_sim_time_s: float,
    ) -> None: ...

    def inject_safety_interrupt(
        self,
        *,
        room_id: str,
        event_type: str,
        severity: str,
        at_sim_time_s: float,
    ) -> None: ...


def _handle_device_failure(
    runtime: PerturbationRuntime, perturbation: PerturbationSpec
) -> None:
    item = cast(DeviceFailurePerturbation, perturbation)
    runtime.inject_device_failure(
        item.device_id, at_sim_time_s=item.at_sim_time_s or 0.0
    )


def _handle_feedback_loss(
    runtime: PerturbationRuntime, perturbation: PerturbationSpec
) -> None:
    item = cast(FeedbackLossPerturbation, perturbation)
    runtime.inject_feedback_loss(
        item.device_id,
        drop_count=item.drop_count,
        at_sim_time_s=item.at_sim_time_s or 0.0,
    )


def _handle_resident_state_change(
    runtime: PerturbationRuntime, perturbation: PerturbationSpec
) -> None:
    item = cast(ResidentStateChangePerturbation, perturbation)
    runtime.inject_resident_state_change(
        item.user_id,
        room_id=item.room_id,
        activity=item.activity,
        at_sim_time_s=item.at_sim_time_s or 0.0,
    )


def _handle_conflicting_request(
    runtime: PerturbationRuntime, perturbation: PerturbationSpec
) -> None:
    item = cast(ConflictingRequestPerturbation, perturbation)
    runtime.inject_conflicting_request(
        item.user_id,
        room_id=item.room_id,
        intent=item.intent,
        at_sim_time_s=item.at_sim_time_s or 0.0,
    )


def _handle_safety_interrupt(
    runtime: PerturbationRuntime, perturbation: PerturbationSpec
) -> None:
    item = cast(SafetyInterruptPerturbation, perturbation)
    runtime.inject_safety_interrupt(
        room_id=item.room_id,
        event_type=item.event_type,
        severity=item.severity,
        at_sim_time_s=item.at_sim_time_s or 0.0,
    )


# Deliberately explicit and closed: PR-3 resident interventions are not silently ignored.
PERTURBATION_HANDLER_REGISTRY = {
    "device_failure": _handle_device_failure,
    "feedback_loss": _handle_feedback_loss,
    "resident_state_change": _handle_resident_state_change,
    "conflicting_request": _handle_conflicting_request,
    "safety_interrupt": _handle_safety_interrupt,
}
PerturbationHandler: TypeAlias = Callable[
    [PerturbationRuntime, PerturbationSpec], None
]
CompiledPerturbation: TypeAlias = tuple[PerturbationHandler, PerturbationSpec]


def unsupported_perturbations(spec: ScenarioSpecV2) -> list[PerturbationSpec]:
    return [
        item
        for item in spec.perturbations
        if item.type not in PERTURBATION_HANDLER_REGISTRY
    ]


def configure_perturbations(
    spec: ScenarioSpecV2, runtime: PerturbationRuntime
) -> None:
    apply_compiled_perturbations(compile_perturbations(spec), runtime)


def compile_perturbations(
    spec: ScenarioSpecV2,
) -> tuple[CompiledPerturbation, ...]:
    """Resolve every handler before a run is committed."""

    unsupported = unsupported_perturbations(spec)
    if unsupported:
        names = ", ".join(sorted({item.type for item in unsupported}))
        raise ValueError(f"unsupported perturbation handlers: {names}")
    return tuple(
        (PERTURBATION_HANDLER_REGISTRY[item.type], item)
        for item in spec.perturbations
    )


def apply_compiled_perturbations(
    compiled: tuple[CompiledPerturbation, ...],
    runtime: PerturbationRuntime,
) -> None:
    for handler, item in compiled:
        handler(runtime, item)


# Compatibility names introduced by PR-2.  Keep them while callers migrate to
# the now-generic registry that also includes resident perturbations.
configure_device_perturbations = configure_perturbations
compile_device_perturbations = compile_perturbations
apply_compiled_device_perturbations = apply_compiled_perturbations


__all__ = [
    "PERTURBATION_HANDLER_REGISTRY",
    "BenchmarkMetadata",
    "ConflictingRequestPerturbation",
    "CounterfactualFactor",
    "CounterfactualReference",
    "DeviceFailurePerturbation",
    "FeedbackLossPerturbation",
    "HomeReference",
    "ObservationDelayPerturbation",
    "ObservationModelReference",
    "PerturbationPhase",
    "PerturbationSpec",
    "ResidentReference",
    "ResidentStateChangePerturbation",
    "SafetyInterruptPerturbation",
    "ScenarioSpecV2",
    "apply_compiled_perturbations",
    "apply_compiled_device_perturbations",
    "compile_perturbations",
    "compile_device_perturbations",
    "configure_perturbations",
    "configure_device_perturbations",
    "unsupported_perturbations",
]
