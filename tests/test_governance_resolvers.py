from __future__ import annotations

from enum import Enum

import pytest

from backend.agents.arbiter import ArbiterResult, ConflictClass
from backend.agents.contracts import AgentProposal, PriorityLevel, ProposalOutcome
from backend.agents.governance import (
    AuraResolver,
    FlatPriorityResolver,
    PassthroughResolver,
    build_governance_resolver,
)
from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent


def _command(device_id: str, property: str, value: object) -> AgentCommandProposal:
    return AgentCommandProposal(
        device_id=device_id,
        property=property,
        value=value,
        reason="test",
    )


def _proposal(
    agent_id: str,
    priority: PriorityLevel,
    *commands: AgentCommandProposal,
) -> AgentProposal:
    return AgentProposal(
        agent_id=agent_id,
        agent_role=agent_id.removesuffix("_agent"),
        intent="test proposal",
        priority=priority,
        confidence=1.0,
        commands=list(commands),
    )


def _event() -> SimEvent:
    return SimEvent(
        event_id="root-governance",
        event_type="user.activity_change",
        source="test",
        timestamp=1.0,
        correlation_id="corr-governance",
    )


def test_passthrough_approves_every_command_in_input_order_without_context_reads():
    proposals = [
        _proposal(
            "scene_agent",
            PriorityLevel.AMBIENCE,
            _command("light_01", "extra.brightness", 20),
            _command("hvac_01", "power", False),
        ),
        _proposal(
            "safety_agent",
            PriorityLevel.SAFETY,
            _command("light_01", "extra.brightness", 100),
        ),
    ]

    result = PassthroughResolver().resolve(
        proposals,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        energy_review=object(),  # type: ignore[arg-type]
        user_claims=object(),  # type: ignore[arg-type]
    )

    assert [command.value for command in result.approved_commands] == [20, False, 100]
    assert result.rejected_commands == []
    assert result.conflicts == []
    assert result.winning_priority is PriorityLevel.SAFETY


def test_passthrough_preserves_non_action_outcome_without_inventing_commands():
    quiet = AgentProposal(
        agent_id="energy_agent",
        intent="nothing to do",
        priority=PriorityLevel.ENERGY,
        confidence=1.0,
        outcome=ProposalOutcome.NO_ACTION_NEEDED,
        noop_reason="home occupied",
    )

    result = PassthroughResolver().resolve([quiet], _event())

    assert result.approved_commands == []
    assert result.per_agent[0].outcome == "no_action_needed"
    assert result.per_agent[0].noop_reason == "home occupied"


def test_flat_priority_uses_only_normalized_target_and_static_priority():
    proposals = [
        _proposal(
            "scene_agent",
            PriorityLevel.AMBIENCE,
            _command("missing_light", "extra.brightness", 20),
            _command("missing_light", "power", False),
        ),
        _proposal(
            "lighting_agent",
            PriorityLevel.COMFORT,
            _command("missing_light", "extra.brightness", 80),
        ),
    ]

    result = FlatPriorityResolver().resolve(
        proposals,
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        energy_review=object(),  # type: ignore[arg-type]
        user_claims=object(),  # type: ignore[arg-type]
    )

    assert [(item.property, item.value) for item in result.approved_commands] == [
        ("extra.brightness", 80),
        ("power", False),
    ]
    assert len(result.rejected_commands) == 1
    loser = result.rejected_commands[0]
    assert loser.agent_id == "scene_agent"
    assert loser.conflict_class is ConflictClass.SAME_DEVICE_PROPERTY
    assert loser.winner_agent_id == "lighting_agent"
    assert result.conflicts == []


@pytest.mark.parametrize("reverse", [False, True])
def test_flat_priority_ties_follow_deterministic_registration_order(reverse: bool):
    proposals = [
        _proposal(
            "z_agent",
            PriorityLevel.COMFORT,
            _command("light_01", "power", False),
        ),
        _proposal(
            "a_agent",
            PriorityLevel.COMFORT,
            _command("light_01", "power", True),
        ),
    ]
    if reverse:
        proposals.reverse()

    result = FlatPriorityResolver().resolve(proposals, _event())

    expected = ("a_agent", True) if reverse else ("z_agent", False)
    assert [(item.agent_id, item.value) for item in result.approved_commands] == [expected]
    assert result.rejected_commands[0].winner_agent_id == expected[0]


@pytest.mark.parametrize("reverse", [False, True])
def test_flat_priority_same_agent_ties_follow_proposal_order(reverse: bool):
    proposals = [
        _proposal(
            "same_agent",
            PriorityLevel.COMFORT,
            _command("light_01", "power", True),
        ),
        _proposal(
            "same_agent",
            PriorityLevel.COMFORT,
            _command("light_01", "power", False),
        ),
    ]
    if reverse:
        proposals.reverse()

    result = FlatPriorityResolver().resolve(proposals, _event())

    assert [item.value for item in result.approved_commands] == ([False] if reverse else [True])


def test_aura_resolver_delegates_all_inputs_unchanged():
    sentinel = ArbiterResult(explanation="delegated")

    class SpyArbiter:
        def __init__(self) -> None:
            self.call = None

        def resolve(self, *args, **kwargs):
            self.call = (args, kwargs)
            return sentinel

    arbiter = SpyArbiter()
    resolver = AuraResolver(arbiter)  # type: ignore[arg-type]
    proposals = [_proposal("agent", PriorityLevel.COMFORT)]
    event = _event()
    world = object()
    energy_review = object()
    claims = object()

    result = resolver.resolve(
        proposals,
        event,
        world,  # type: ignore[arg-type]
        energy_review=energy_review,  # type: ignore[arg-type]
        user_claims=claims,  # type: ignore[arg-type]
    )

    assert result is sentinel
    assert arbiter.call == (
        (proposals, event, world),
        {"energy_review": energy_review, "user_claims": claims},
    )


def test_resolver_metadata_and_factory_are_fail_closed():
    class GovernanceValue(str, Enum):
        NONE = "none"

    assert PassthroughResolver.strategy_id == "none"
    assert PassthroughResolver.event_source == "proposal_passthrough"
    assert FlatPriorityResolver.strategy_id == "flat_priority"
    assert FlatPriorityResolver.event_source == "flat_priority"
    assert AuraResolver.strategy_id == "aura"
    assert AuraResolver.event_source == "arbiter"
    assert PassthroughResolver.strategy_version == "1"

    assert isinstance(build_governance_resolver(GovernanceValue.NONE), PassthroughResolver)
    assert isinstance(build_governance_resolver("flat_priority"), FlatPriorityResolver)
    assert isinstance(build_governance_resolver("aura"), AuraResolver)

    class ArbiterSentinel:
        pass

    arbiter = ArbiterSentinel()
    aura = build_governance_resolver("aura", arbiter=arbiter)  # type: ignore[arg-type]
    assert isinstance(aura, AuraResolver)
    assert aura.arbiter is arbiter
    with pytest.raises(ValueError, match="unsupported governance resolver"):
        build_governance_resolver("pretend_aura")
