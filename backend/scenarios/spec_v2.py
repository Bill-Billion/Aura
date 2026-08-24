"""AuraBench ScenarioSpec 2.x data contract.

``ScenarioSpecV2`` subclasses the proven v1 runtime contract.  Existing apply,
generator, runner, and evaluator code can therefore consume it without a
parallel execution path.  The 2.1 minor adds an event-relative intervention
contract. Runtime support is deliberately fail-closed until the phase
controller consumes it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Annotated, Any, Literal, Protocol, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.engine.event_types import ALL_ROOT_EVENT_TYPES
from backend.models.versioning import (
    check_scenario_schema_compatibility,
    parse_schema_version,
)
from backend.scenarios.spec import ExpectedDeviceEffect, ScenarioSpec
from backend.scenarios.trace_spec import EventSelector, TraceSpec


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


class SharedGoalContract(_StrictModel):
    """Task semantics that must remain invariant across a counterfactual pair."""

    user_goal: str = Field(min_length=1, max_length=4096)
    relevant_room_ids: list[str] = Field(default_factory=list, max_length=64)
    forbidden_room_ids: list[str] = Field(default_factory=list, max_length=64)
    safety_constraints: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("user_goal")
    @classmethod
    def _non_blank_goal(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("shared goal user_goal cannot be blank")
        return value

    @field_validator(
        "relevant_room_ids",
        "forbidden_room_ids",
        "safety_constraints",
        mode="before",
    )
    @classmethod
    def _bounded_raw_lists(cls, value: Any, info: Any) -> Any:
        if isinstance(value, list) and len(value) > 64:
            raise ValueError(f"{info.field_name} cannot exceed 64 items")
        return value

    @field_validator(
        "relevant_room_ids", "forbidden_room_ids", "safety_constraints"
    )
    @classmethod
    def _unique_non_empty_values(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("shared goal lists cannot contain empty strings")
        if any(len(item) > 1024 for item in value):
            raise ValueError("shared goal list items cannot exceed 1024 characters")
        if len(value) != len(set(value)):
            raise ValueError("shared goal lists cannot contain duplicates")
        return value

    @model_validator(mode="after")
    def _room_sets_do_not_overlap(self) -> SharedGoalContract:
        overlap = sorted(set(self.relevant_room_ids) & set(self.forbidden_room_ids))
        if overlap:
            raise ValueError(
                "shared goal rooms cannot be both relevant and forbidden: "
                + ", ".join(overlap)
            )
        return self


class InterventionResponseContract(_StrictModel):
    """Dynamic-only oracle evaluated on the trace suffix starting at trigger."""

    trigger: EventSelector
    time_origin: Literal["trigger"] = "trigger"
    expected_device_effects: list[ExpectedDeviceEffect] = Field(
        default_factory=list, max_length=128
    )
    obligations: TraceSpec

    @field_validator("expected_device_effects", mode="before")
    @classmethod
    def _bounded_raw_effects(cls, value: Any) -> Any:
        if isinstance(value, list) and len(value) > 128:
            raise ValueError("expected_device_effects cannot exceed 128 items")
        return value

    @field_validator("expected_device_effects")
    @classmethod
    def _unique_effect_devices(
        cls, value: list[ExpectedDeviceEffect]
    ) -> list[ExpectedDeviceEffect]:
        device_ids = [effect.device_id for effect in value]
        if len(device_ids) != len(set(device_ids)):
            raise ValueError("intervention response cannot repeat a device_id")
        return value

    @model_validator(mode="after")
    def _trigger_is_persisted_intervention(self) -> InterventionResponseContract:
        if self.trigger.event_type != "benchmark.perturbation_injected":
            raise ValueError(
                "intervention response trigger must select "
                "benchmark.perturbation_injected"
            )
        _validate_selector_constraints(
            self.trigger,
            label="intervention trigger",
            allowed_paths=frozenset({"data.perturbation_type"}),
        )
        return self


PerturbationPhase: TypeAlias = Literal[
    "before_perception",
    "after_perception_before_plan",
    "after_plan_before_execution",
    "during_execution",
    "after_execution_before_feedback",
]


class PerturbationAnchor(EventSelector):
    """First matching event in the root episode correlation."""

    relation: Literal["same_correlation"] = "same_correlation"
    occurrence: Literal["first"] = "first"


class _PerturbationBase(_StrictModel):
    phase: PerturbationPhase
    at_sim_time_s: float | None = Field(default=None, ge=0.0)
    anchor: PerturbationAnchor | None = None
    offset_seconds: float | None = Field(default=None, ge=0.0)
    must_precede: EventSelector | None = None

    @field_validator("at_sim_time_s", "offset_seconds", mode="before")
    @classmethod
    def _finite_time(cls, value: Any, info: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{info.field_name} must be a finite number")
        resolved = float(value)
        if not math.isfinite(resolved):
            raise ValueError(f"{info.field_name} must be a finite number")
        return resolved

    @model_validator(mode="after")
    def _one_timing_contract(self) -> _PerturbationBase:
        if (self.at_sim_time_s is None) == (self.anchor is None):
            raise ValueError(
                "perturbation requires exactly one timing contract: "
                "at_sim_time_s or anchor"
            )
        if self.anchor is None:
            if self.offset_seconds is not None or self.must_precede is not None:
                raise ValueError(
                    "offset_seconds and must_precede require an event anchor"
                )
        elif self.offset_seconds is None or self.must_precede is None:
            raise ValueError(
                "event-relative perturbations require offset_seconds and must_precede"
            )
        return self


class ResidentStateChangePerturbation(_PerturbationBase):
    type: Literal["resident_state_change"] = "resident_state_change"
    user_id: str = Field(min_length=1)
    room_id: str | None = Field(default=None, min_length=1)
    activity: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _has_state_change(self) -> ResidentStateChangePerturbation:
        if self.room_id is None and self.activity is None:
            raise ValueError("resident state change requires room_id or activity")
        return self


class DeviceFailurePerturbation(_PerturbationBase):
    type: Literal["device_failure"] = "device_failure"
    device_id: str = Field(min_length=1)
    failure: Literal["offline"] = "offline"

class ConflictingRequestPerturbation(_PerturbationBase):
    type: Literal["conflicting_request"] = "conflicting_request"
    user_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    intent: str = Field(min_length=1)

class SafetyInterruptPerturbation(_PerturbationBase):
    type: Literal["safety_interrupt"] = "safety_interrupt"
    room_id: str = Field(min_length=1)
    event_type: Literal["safety.smoke_detected"] = "safety.smoke_detected"
    severity: Literal["warning", "critical"] = "critical"

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

    scenario_schema_version: str = "2.1"
    family: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    split: Literal["dev", "validation", "test"]
    difficulty: Literal["easy", "medium", "hard"]
    home: HomeReference
    residents: list[ResidentReference] = Field(default_factory=list)
    observation_model: ObservationModelReference = Field(
        default_factory=ObservationModelReference
    )
    counterfactual: CounterfactualReference
    shared_goal: SharedGoalContract | None = None
    perturbations: list[PerturbationSpec] = Field(default_factory=list)
    intervention_response: InterventionResponseContract | None = None
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
    def _counterfactual_shape(self) -> ScenarioSpecV2:
        _, minor = parse_schema_version(self.scenario_schema_version)
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

        if minor == 0:
            if self.shared_goal is not None or self.intervention_response is not None:
                raise ValueError(
                    "shared_goal and intervention_response require ScenarioSpec 2.1"
                )
            if any(item.anchor is not None for item in self.perturbations):
                raise ValueError("event-relative perturbations require ScenarioSpec 2.1")
            return self

        if self.shared_goal is None:
            raise ValueError("ScenarioSpec 2.1 requires shared_goal")
        if self.ground_truth is None:
            raise ValueError("ScenarioSpec 2.1 requires ground_truth")
        if (
            self.shared_goal.user_goal != self.ground_truth.user_goal
            or self.shared_goal.relevant_room_ids
            != self.ground_truth.primary_room_ids
            or self.shared_goal.safety_constraints
            != self.ground_truth.safety_constraints
        ):
            raise ValueError(
                "shared_goal must match ground_truth user_goal, primary_room_ids, "
                "and safety_constraints"
            )
        if self.counterfactual.variant == "static":
            if self.intervention_response is not None:
                raise ValueError(
                    "static variants cannot declare intervention_response"
                )
            return self

        if len(self.perturbations) != 1:
            raise ValueError(
                "ScenarioSpec 2.1 dynamic variants require exactly one perturbation"
            )
        perturbation = self.perturbations[0]
        if perturbation.anchor is None:
            raise ValueError(
                "ScenarioSpec 2.1 perturbations require an event-relative anchor"
            )
        self._validate_phase_boundary(perturbation)
        if self.intervention_response is None:
            raise ValueError(
                "ScenarioSpec 2.1 dynamic variants require intervention_response"
            )
        if not _selector_has_equality(
            self.intervention_response.trigger,
            "data.perturbation_type",
            self.counterfactual.factor,
        ):
            raise ValueError(
                "intervention_response trigger must constrain "
                "data.perturbation_type to counterfactual.factor"
            )
        return self

    @staticmethod
    def _validate_phase_boundary(perturbation: PerturbationSpec) -> None:
        assert perturbation.anchor is not None
        assert perturbation.must_precede is not None
        allowed_anchor_paths = (
            frozenset({"data.to_status"})
            if perturbation.phase == "during_execution"
            else frozenset()
        )
        _validate_selector_constraints(
            perturbation.anchor,
            label="perturbation anchor",
            allowed_paths=allowed_anchor_paths,
        )
        _validate_selector_constraints(
            perturbation.must_precede,
            label="must_precede selector",
            allowed_paths=frozenset(),
        )
        expected: dict[PerturbationPhase, tuple[set[str] | str, str]] = {
            "before_perception": (
                set(ALL_ROOT_EVENT_TYPES),
                "reasoning.perception_snapshot",
            ),
            "after_perception_before_plan": (
                "reasoning.perception_snapshot",
                "reasoning.execution_plan",
            ),
            "after_plan_before_execution": (
                "reasoning.execution_plan",
                "action.device_control",
            ),
            "during_execution": ("command.lifecycle", "device.effect_applied"),
            "after_execution_before_feedback": (
                "device.effect_applied",
                "feedback.state_delta",
            ),
        }
        anchor_type, successor_type = expected[perturbation.phase]
        actual_anchor = perturbation.anchor.event_type
        anchor_matches = (
            actual_anchor in anchor_type
            if isinstance(anchor_type, set)
            else actual_anchor == anchor_type
        )
        if not anchor_matches or perturbation.must_precede.event_type != successor_type:
            raise ValueError(
                f"phase {perturbation.phase!r} requires anchor {anchor_type!r} "
                f"and must_precede {successor_type!r}"
            )
        if perturbation.phase == "during_execution" and not _selector_has_equality(
            perturbation.anchor, "data.to_status", "executing"
        ):
            raise ValueError(
                "during_execution anchor must constrain data.to_status to executing"
            )

    def referenced_device_ids(self) -> set[str]:
        ids = super().referenced_device_ids()
        for item in self.perturbations:
            device_id = getattr(item, "device_id", None)
            if device_id:
                ids.add(device_id)
        if self.intervention_response is not None:
            ids.update(
                effect.device_id
                for effect in self.intervention_response.expected_device_effects
            )
        return ids

    def referenced_room_ids(self) -> set[str]:
        ids = super().referenced_room_ids()
        for item in self.perturbations:
            room_id = getattr(item, "room_id", None)
            if room_id and room_id != "outside":
                ids.add(room_id)
        if self.shared_goal is not None:
            ids.update(self.shared_goal.relevant_room_ids)
            ids.update(self.shared_goal.forbidden_room_ids)
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
            "has_intervention_response": self.intervention_response is not None,
        }


def _validate_selector_constraints(
    selector: EventSelector,
    *,
    label: str,
    allowed_paths: frozenset[str],
) -> None:
    """Keep phase-critical selectors unambiguous and mechanically checkable."""

    if selector.source is not None:
        raise ValueError(f"{label} cannot constrain source")
    grouped: dict[str, list[Any]] = {}
    for condition in selector.where:
        if condition.path not in allowed_paths:
            raise ValueError(f"{label} does not allow conditions on {condition.path}")
        grouped.setdefault(condition.path, []).append(condition)

    for path, conditions in grouped.items():
        if len(conditions) > 1:
            raise ValueError(
                f"{label} cannot declare multiple constraints on {path}"
            )


def _selector_has_equality(selector: EventSelector, path: str, value: object) -> bool:
    return any(
        condition.path == path
        and condition.comparator == "eq"
        and condition.value == value
        for condition in selector.where
    )


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
EVENT_RELATIVE_PHASE_RUNTIME = "event_relative_phase_runtime"


def unsupported_perturbations(spec: ScenarioSpecV2) -> list[PerturbationSpec]:
    return [
        item
        for item in spec.perturbations
        if item.type not in PERTURBATION_HANDLER_REGISTRY
    ]


def unavailable_perturbation_capabilities(spec: ScenarioSpecV2) -> tuple[str, ...]:
    if any(item.anchor is not None for item in spec.perturbations):
        return (EVENT_RELATIVE_PHASE_RUNTIME,)
    return ()


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
        raise ValueError(f"unsupported perturbation types: {names}")
    unavailable = unavailable_perturbation_capabilities(spec)
    if unavailable:
        raise ValueError(
            "unavailable perturbation runtime capabilities: " + ", ".join(unavailable)
        )
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
    "EVENT_RELATIVE_PHASE_RUNTIME",
    "PERTURBATION_HANDLER_REGISTRY",
    "BenchmarkMetadata",
    "ConflictingRequestPerturbation",
    "CounterfactualFactor",
    "CounterfactualReference",
    "DeviceFailurePerturbation",
    "FeedbackLossPerturbation",
    "HomeReference",
    "InterventionResponseContract",
    "ObservationDelayPerturbation",
    "ObservationModelReference",
    "PerturbationAnchor",
    "PerturbationPhase",
    "PerturbationSpec",
    "ResidentReference",
    "ResidentStateChangePerturbation",
    "SafetyInterruptPerturbation",
    "ScenarioSpecV2",
    "SharedGoalContract",
    "apply_compiled_device_perturbations",
    "apply_compiled_perturbations",
    "compile_device_perturbations",
    "compile_perturbations",
    "configure_device_perturbations",
    "configure_perturbations",
    "unavailable_perturbation_capabilities",
    "unsupported_perturbations",
]
