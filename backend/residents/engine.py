"""Simulated-time resident runtime that emits events but never mutates the world."""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Protocol

from backend.engine.event_bus import SimEvent
from backend.engine.event_types import (
    SAFETY_SMOKE_DETECTED,
    USER_ACTIVITY_CHANGE,
    USER_COMMAND,
)
from backend.engine.rng import RngStream, SimRandom
from backend.engine.state import WorldState

from .policy import (
    RESPONSIVE_EVENT_TYPES,
    DeterministicResponsivePolicy,
    ResidentPolicy,
    ResidentResponse,
    ScriptedResidentPolicy,
    SeededStochasticResidentPolicy,
)
from .state import (
    ResidentAuthority,
    ResidentState,
    goal_for_activity,
    permissions_for,
    resolve_profile,
)


PolicyKind = Literal["scripted", "responsive", "seeded_stochastic"]


class ResidentReferenceLike(Protocol):
    user_id: str
    profile_id: str
    authority_level: ResidentAuthority


@dataclass(order=True, slots=True)
class _ScheduledIntervention:
    at_sim_time_s: float
    sequence: int
    kind: str = field(compare=False)
    data: dict[str, Any] = field(compare=False)


class ResidentEngine:
    """Owns hidden resident state and deterministic response scheduling."""

    def __init__(
        self,
        residents: Iterable[ResidentReferenceLike] = (),
        *,
        world: WorldState,
        policy_kind: PolicyKind = "responsive",
        rng: SimRandom | None = None,
    ) -> None:
        self.states: dict[str, ResidentState] = {}
        self.policies: dict[str, ResidentPolicy] = {}
        self._scheduled: list[_ScheduledIntervention] = []
        self._sequence = 0
        rng = rng or SimRandom(0)
        scripted_policy = (
            ScriptedResidentPolicy() if policy_kind == "scripted" else None
        )

        for reference in sorted(residents, key=lambda item: item.user_id):
            user = world.users[reference.user_id]
            profile = resolve_profile(reference.profile_id)
            activity = user.activity
            self.states[reference.user_id] = ResidentState(
                user_id=reference.user_id,
                profile_id=reference.profile_id,
                authority_level=reference.authority_level,
                location=user.location.room if user.location is not None else None,
                activity=activity,
                goal=goal_for_activity(activity),
                satisfaction=float(user.comfort_score),
                long_term_preferences=profile.preferences,
                permissions=permissions_for(reference.authority_level),
            )
            if policy_kind == "scripted":
                assert scripted_policy is not None
                policy: ResidentPolicy = scripted_policy
            elif policy_kind == "seeded_stochastic":
                policy = SeededStochasticResidentPolicy(
                    rng.stream(f"{RngStream.USER_SIM.value}:{reference.user_id}")
                )
            else:
                policy = DeterministicResponsivePolicy()
            self.policies[reference.user_id] = policy

    @property
    def enabled(self) -> bool:
        return bool(self.states)

    def state_for(self, user_id: str) -> ResidentState | None:
        return self.states.get(user_id)

    @property
    def next_due_at_s(self) -> float | None:
        return self._scheduled[0].at_sim_time_s if self._scheduled else None

    def inject_resident_state_change(
        self,
        user_id: str,
        *,
        room_id: str | None,
        activity: str | None,
        at_sim_time_s: float,
    ) -> None:
        self._schedule(
            at_sim_time_s,
            "resident_state_change",
            {"user_id": user_id, "room_id": room_id, "activity": activity},
        )

    def inject_conflicting_request(
        self,
        user_id: str,
        *,
        room_id: str,
        intent: str,
        at_sim_time_s: float,
    ) -> None:
        self._schedule(
            at_sim_time_s,
            "conflicting_request",
            {"user_id": user_id, "room_id": room_id, "intent": intent},
        )

    def inject_safety_interrupt(
        self,
        *,
        room_id: str,
        event_type: str,
        severity: str,
        at_sim_time_s: float,
    ) -> None:
        self._schedule(
            at_sim_time_s,
            "safety_interrupt",
            {"room_id": room_id, "event_type": event_type, "severity": severity},
        )

    def _schedule(self, at_sim_time_s: float, kind: str, data: dict[str, Any]) -> None:
        self._sequence += 1
        heapq.heappush(
            self._scheduled,
            _ScheduledIntervention(float(at_sim_time_s), self._sequence, kind, data),
        )

    def advance(
        self, world: WorldState, *, sim_time_s: float, tick: int
    ) -> list[SimEvent]:
        events: list[SimEvent] = []
        scripted = next(
            (
                policy
                for policy in self.policies.values()
                if isinstance(policy, ScriptedResidentPolicy)
            ),
            None,
        )
        if scripted is not None:
            events.extend(
                SimEvent.from_world_event(
                    event,
                    timestamp=float(tick),
                    sim_time_s=sim_time_s,
                    event_generation_mode="rule_based",
                    generation_rule_id="resident.schedule",
                )
                for event in scripted.step(world)
            )
        while self._scheduled and self._scheduled[0].at_sim_time_s <= sim_time_s:
            intervention = heapq.heappop(self._scheduled)
            events.append(self._intervention_event(intervention, world, tick))
        return events

    def handle_event(
        self, world: WorldState, event: SimEvent, *, sim_time_s: float
    ) -> list[SimEvent]:
        if event.event_type not in RESPONSIVE_EVENT_TYPES:
            return []
        events: list[SimEvent] = []
        for user_id in sorted(self.states):
            state = self.states[user_id]
            self._sync_visible_state(state, world)
            policy = self.policies[user_id]
            for response in policy.respond(
                state, world, event, sim_time_s=sim_time_s
            ):
                events.extend(self._response_events(response, event))
        return events

    @staticmethod
    def _sync_visible_state(state: ResidentState, world: WorldState) -> None:
        user = world.users.get(state.user_id)
        if user is None:
            return
        state.location = user.location.room if user.location is not None else None
        if user.activity != state.activity:
            state.activity = user.activity
            state.goal = goal_for_activity(user.activity)

    @staticmethod
    def _base_event(
        *,
        event_type: str,
        data: dict[str, Any],
        tick: float,
        sim_time_s: float,
        source: str,
        priority: int = 2,
        causal_parent: str | None = None,
        correlation_id: str | None = None,
        generation_rule_id: str,
        generation_mode: (
            Literal["scripted", "rule_based", "stochastic", "system"] | None
        ) = None,
        rng_stream: str | None = None,
    ) -> SimEvent:
        kwargs: dict[str, Any] = {}
        if correlation_id is not None:
            kwargs["correlation_id"] = correlation_id
        return SimEvent(
            event_type=event_type,
            source=source,
            timestamp=tick,
            wall_time=time.time(),
            priority=priority,
            causal_parent=causal_parent,
            event_generation_mode=generation_mode,
            generation_rule_id=generation_rule_id,
            rng_stream=rng_stream,
            sim_time_s=sim_time_s,
            data=data,
            **kwargs,
        )

    def _intervention_event(
        self, item: _ScheduledIntervention, world: WorldState, tick: int
    ) -> SimEvent:
        data = dict(item.data)
        user_id = str(data.get("user_id") or "")
        if item.kind == "resident_state_change":
            user = world.users.get(user_id)
            data.update(
                {
                    "from_room": (
                        user.location.room if user is not None and user.location else ""
                    ),
                    "to_room": data.pop("room_id", None) or "",
                    "activity": data.get("activity") or "",
                    "at_sim_time_s": item.at_sim_time_s,
                    "perturbation_type": item.kind,
                }
            )
            event_type = USER_ACTIVITY_CHANGE
        elif item.kind == "conflicting_request":
            data.update(
                {
                    "response_kind": "conflicting_request",
                    "at_sim_time_s": item.at_sim_time_s,
                    "perturbation_type": item.kind,
                }
            )
            event_type = USER_COMMAND
        else:
            event_type = str(data.pop("event_type", SAFETY_SMOKE_DETECTED))
            data.update(
                {
                    "at_sim_time_s": item.at_sim_time_s,
                    "perturbation_type": item.kind,
                }
            )
        return self._base_event(
            event_type=event_type,
            data=data,
            tick=float(tick),
            sim_time_s=item.at_sim_time_s,
            source="resident_perturbation",
            priority=3 if item.kind == "safety_interrupt" else 2,
            generation_rule_id=f"perturbation.{item.kind}",
            generation_mode="scripted",
        )

    def _response_events(
        self, response: ResidentResponse, trigger: SimEvent
    ) -> list[SimEvent]:
        metadata = dict(response.metadata)
        rng_stream = metadata.pop("rng_stream", None)
        common = {
            "user_id": response.user_id,
            "room_id": response.room_id,
            "goal": response.goal,
            "satisfaction": response.satisfaction,
            "response_kind": response.kind,
            "intent": response.intent,
            **metadata,
        }
        observed = self._base_event(
            event_type=f"resident.{response.kind}",
            data=common,
            tick=trigger.timestamp,
            sim_time_s=float(trigger.sim_time_s or 0.0),
            source="resident_engine",
            priority=1,
            causal_parent=trigger.event_id,
            correlation_id=trigger.correlation_id,
            generation_rule_id=f"resident.response.{response.kind}",
            generation_mode="stochastic" if rng_stream is not None else None,
            rng_stream=str(rng_stream) if rng_stream is not None else None,
        )
        if response.kind not in {"correction", "override"}:
            return [observed]
        request = self._base_event(
            event_type=USER_COMMAND,
            data={
                **common,
                "resident_request": True,
                "device_id": response.device_id,
                "capability": response.capability,
                "value": response.value,
                "requested_action": {
                    "device_id": response.device_id,
                    "capability": response.capability,
                    "value": response.value,
                },
            },
            tick=trigger.timestamp,
            sim_time_s=float(trigger.sim_time_s or 0.0),
            source="resident_engine",
            priority=2,
            causal_parent=observed.event_id,
            correlation_id=trigger.correlation_id,
            generation_rule_id=f"resident.request.{response.kind}",
            generation_mode=None,
        )
        return [observed, request]
