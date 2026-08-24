"""Event-relative perturbation injection for ScenarioSpec 2.1."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from backend.engine.event_bus import SimEvent
from backend.scenarios.spec_v2 import PerturbationSpec, ScenarioSpecV2
from backend.scenarios.trace_spec import EventSelector

PERTURBATION_INJECTED_EVENT_TYPE = "benchmark.perturbation_injected"
PERTURBATION_PHASE_VIOLATION_EVENT_TYPE = "benchmark.perturbation_phase_violation"

PublishEvent = Callable[[SimEvent], Awaitable[SimEvent]]
InjectPerturbation = Callable[[PerturbationSpec, SimEvent, float], Awaitable[None]]
StampEvent = Callable[[SimEvent], SimEvent]
TickSource = Callable[[], int]


class PerturbationPhaseError(RuntimeError):
    code = "perturbation_phase_invalid"

    def __init__(self, scenario_id: str, reason: str, details: dict[str, Any]) -> None:
        self.scenario_id = scenario_id
        self.reason = reason
        self.details = dict(details)
        super().__init__(
            f"scenario {scenario_id!r} has an invalid perturbation phase: {reason}"
        )


class PhasePerturbationController:
    """Inject one dynamic perturbation after its first matching anchor."""

    def __init__(
        self,
        spec: ScenarioSpecV2,
        *,
        run_id: str,
        publish: PublishEvent,
        stamp: StampEvent,
        inject: InjectPerturbation,
        tick_source: TickSource,
    ) -> None:
        if spec.counterfactual.variant != "dynamic" or len(spec.perturbations) != 1:
            raise ValueError("phase controller requires one dynamic perturbation")
        perturbation = spec.perturbations[0]
        if perturbation.anchor is None or perturbation.must_precede is None:
            raise ValueError("phase controller requires an event-relative perturbation")

        self.scenario_id = spec.id
        self.run_id = run_id
        self.perturbation = perturbation
        self.publish = publish
        self.stamp = stamp
        self.inject = inject
        self.tick_source = tick_source
        self.anchor_event: SimEvent | None = None
        self.injection_event: SimEvent | None = None
        self.successor_event: SimEvent | None = None
        self.due_at_s: float | None = None
        self.violation_reason: str | None = None
        self.violation_details: dict[str, Any] = {}

    @property
    def next_due_at_s(self) -> float | None:
        if self.injection_event is not None or self.violation_reason is not None:
            return None
        return self.due_at_s

    async def handle_event(self, event: SimEvent) -> None:
        if event.run_id != self.run_id or event.scenario_id != self.scenario_id:
            return

        anchor = self.perturbation.anchor
        successor = self.perturbation.must_precede
        assert anchor is not None and successor is not None

        if self.anchor_event is None and _matches(anchor, event):
            if event.sim_time_s is None or event.seq is None:
                await self._record_violation(
                    "anchor_missing_canonical_time",
                    event=event,
                )
                return
            self.anchor_event = event
            self.due_at_s = float(event.sim_time_s) + float(
                self.perturbation.offset_seconds or 0.0
            )
            if self.due_at_s == event.sim_time_s:
                await self._inject(self.due_at_s)
            return

        if (
            self.anchor_event is not None
            and event.correlation_id == self.anchor_event.correlation_id
            and _matches(successor, event)
        ):
            self.successor_event = self.successor_event or event
            if self.injection_event is None:
                await self._record_violation(
                    "successor_observed_before_injection",
                    event=event,
                )
                return
            if not (
                self.anchor_event.seq is not None
                and self.injection_event.seq is not None
                and event.seq is not None
                and self.anchor_event.seq < self.injection_event.seq < event.seq
            ):
                await self._record_violation(
                    "non_monotonic_phase_sequence",
                    event=event,
                )

    async def advance(self, sim_time_s: float) -> None:
        if (
            self.anchor_event is not None
            and self.injection_event is None
            and self.violation_reason is None
            and self.due_at_s is not None
            and self.due_at_s <= sim_time_s
        ):
            await self._inject(self.due_at_s)

    async def finalize(self) -> None:
        if self.violation_reason is None and self.anchor_event is None:
            await self._record_violation("anchor_not_observed")
        elif self.violation_reason is None and self.injection_event is None:
            await self._record_violation("perturbation_not_injected")
        if self.violation_reason is not None:
            raise PerturbationPhaseError(
                self.scenario_id,
                self.violation_reason,
                self.violation_details,
            )

    async def _inject(self, sim_time_s: float) -> None:
        if self.injection_event is not None or self.violation_reason is not None:
            return
        assert self.anchor_event is not None
        anchor = self.perturbation.anchor
        successor = self.perturbation.must_precede
        assert anchor is not None and successor is not None

        evidence = SimEvent(
            event_type=PERTURBATION_INJECTED_EVENT_TYPE,
            source="benchmark_phase_controller",
            timestamp=float(self.tick_source()),
            sim_time_s=sim_time_s,
            correlation_id=self.anchor_event.correlation_id,
            causal_parent=self.anchor_event.event_id,
            priority=3,
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            data={
                "declared_phase": self.perturbation.phase,
                "perturbation_type": self.perturbation.type,
                "anchor_event_id": self.anchor_event.event_id,
                "anchor_event_type": self.anchor_event.event_type,
                "anchor_seq": self.anchor_event.seq,
                "actual_sim_time_s": sim_time_s,
                "offset_seconds": self.perturbation.offset_seconds,
                "expected_predecessor": anchor.model_dump(mode="json"),
                "expected_successor": successor.model_dump(mode="json"),
            },
        )
        self.stamp(evidence)
        evidence.data["actual_seq"] = evidence.seq

        try:
            visible = await self.publish(evidence)
            if (
                visible.event_id != evidence.event_id
                or visible.event_type != PERTURBATION_INJECTED_EVENT_TYPE
            ):
                await self._record_violation(
                    "injection_evidence_suppressed",
                    event=visible,
                )
                return
            self.injection_event = visible
            await self.inject(self.perturbation, self.injection_event, sim_time_s)
        except Exception as exc:
            await self._record_violation(
                "perturbation_injection_failed",
                error=repr(exc),
            )

    async def _record_violation(
        self,
        reason: str,
        *,
        event: SimEvent | None = None,
        error: str | None = None,
    ) -> None:
        if self.violation_reason is not None:
            return
        self.violation_reason = reason
        anchor = self.anchor_event
        details = {
            "reason": reason,
            "declared_phase": self.perturbation.phase,
            "perturbation_type": self.perturbation.type,
            "anchor_event_id": anchor.event_id if anchor is not None else None,
            "anchor_seq": anchor.seq if anchor is not None else None,
            "injection_event_id": (
                self.injection_event.event_id
                if self.injection_event is not None
                else None
            ),
            "injection_seq": (
                self.injection_event.seq if self.injection_event is not None else None
            ),
            "observed_event_id": event.event_id if event is not None else None,
            "observed_event_type": event.event_type if event is not None else None,
            "observed_seq": event.seq if event is not None else None,
            "error": error,
        }
        self.violation_details = details

        violation = SimEvent(
            event_type=PERTURBATION_PHASE_VIOLATION_EVENT_TYPE,
            source="benchmark_phase_controller",
            timestamp=float(self.tick_source()),
            sim_time_s=(event.sim_time_s if event is not None else self.due_at_s),
            correlation_id=(
                anchor.correlation_id
                if anchor is not None
                else f"phase:{self.scenario_id}"
            ),
            priority=3,
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            data=details,
        )
        await self.publish(violation)


def _matches(selector: EventSelector, event: SimEvent) -> bool:
    if selector.event_type is not None and event.event_type != selector.event_type:
        return False
    if selector.source is not None and event.source != selector.source:
        return False
    for condition in selector.where:
        if condition.comparator != "eq" or not condition.path.startswith("data."):
            return False
        key = condition.path.removeprefix("data.")
        actual = event.data.get(key, _MISSING)
        if type(actual) is not type(condition.value) or actual != condition.value:
            return False
    return True


_MISSING = object()


__all__ = [
    "PERTURBATION_INJECTED_EVENT_TYPE",
    "PERTURBATION_PHASE_VIOLATION_EVENT_TYPE",
    "PerturbationPhaseError",
    "PhasePerturbationController",
]
