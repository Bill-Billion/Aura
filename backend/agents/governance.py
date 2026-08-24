"""Proposal-selection strategies used by controlled governance experiments.

The strategies in this module only decide which proposal commands are admitted.
They never execute commands or mutate the world; every admitted command still has
to pass through the shared :class:`~backend.execution.executor.CommandExecutor`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Protocol

from backend.agents.arbiter import (
    ARBITER_ID,
    AgentArbitrationOutcome,
    Arbiter,
    ArbiterResult,
    ArbitratedCommand,
    ConflictClass,
    ConflictResolution,
    ExplicitUserClaim,
    RejectedCommand,
)
from backend.agents.contracts import AgentProposal, PriorityLevel, priority_rank
from backend.agents.energy import EnergyVetoReview
from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent
from backend.engine.state import WorldState
from backend.execution.executor import CommandTarget

__all__ = [
    "AuraResolver",
    "FlatPriorityResolver",
    "PassthroughResolver",
    "ProposalResolver",
    "build_governance_resolver",
]


class ProposalResolver(Protocol):
    """Common proposal-selection contract consumed by ``AgentRuntime``."""

    strategy_id: str
    event_source: str
    strategy_version: str

    def resolve(
        self,
        proposals: Sequence[AgentProposal],
        root_event: SimEvent,
        world_snapshot: WorldState | None = None,
        *,
        energy_review: EnergyVetoReview | None = None,
        user_claims: Mapping[CommandTarget, ExplicitUserClaim] | None = None,
    ) -> ArbiterResult: ...


def _approved(proposal: AgentProposal, command: AgentCommandProposal) -> ArbitratedCommand:
    return ArbitratedCommand(
        agent_id=proposal.agent_id,
        agent_role=proposal.agent_role,
        priority=proposal.priority,
        device_id=command.device_id,
        property=command.property,
        value=command.value,
        reason=command.reason,
    )


def _per_agent(
    proposals: Sequence[AgentProposal],
    approved: Sequence[ArbitratedCommand],
    rejected: Sequence[RejectedCommand],
) -> list[AgentArbitrationOutcome]:
    outcomes: list[AgentArbitrationOutcome] = []
    for proposal in proposals:
        wins = sum(item.agent_id == proposal.agent_id for item in approved)
        losses = sum(item.agent_id == proposal.agent_id for item in rejected)
        if proposal.is_non_action:
            outcome = proposal.outcome.value
        elif wins and losses:
            outcome = "partial"
        elif wins:
            outcome = "approved"
        elif losses:
            outcome = "rejected"
        else:
            outcome = "no_commands"
        outcomes.append(
            AgentArbitrationOutcome(
                agent_id=proposal.agent_id,
                agent_role=proposal.agent_role,
                priority=proposal.priority,
                outcome=outcome,
                approved=wins,
                rejected=losses,
                noop_reason=proposal.noop_reason,
            )
        )
    return sorted(outcomes, key=lambda item: item.agent_id)


def _winning_priority(commands: Sequence[ArbitratedCommand]) -> PriorityLevel | None:
    if not commands:
        return None
    return max((command.priority for command in commands), key=priority_rank)


class PassthroughResolver:
    """Admit every command in proposal and command input order.

    ``root_event`` and all contextual inputs are deliberately ignored.  This is
    the no-governance baseline, not a permissive configuration of Aura's arbiter.
    """

    strategy_id: ClassVar[str] = "none"
    event_source: ClassVar[str] = "proposal_passthrough"
    strategy_version: ClassVar[str] = "1"

    def resolve(
        self,
        proposals: Sequence[AgentProposal],
        root_event: SimEvent,
        world_snapshot: WorldState | None = None,
        *,
        energy_review: EnergyVetoReview | None = None,
        user_claims: Mapping[CommandTarget, ExplicitUserClaim] | None = None,
    ) -> ArbiterResult:
        del root_event, world_snapshot, energy_review, user_claims
        approved = [
            _approved(proposal, command)
            for proposal in proposals
            for command in proposal.commands
        ]
        return ArbiterResult(
            approved_commands=approved,
            winning_priority=_winning_priority(approved),
            explanation=(
                "governance=none: admitted "
                f"{len(approved)} commands in proposal input order"
            ),
            per_agent=_per_agent(proposals, approved, ()),
        )


@dataclass(frozen=True)
class _FlatEntry:
    proposal: AgentProposal
    command: AgentCommandProposal
    proposal_index: int
    command_index: int

    @property
    def target(self) -> CommandTarget:
        return (self.command.device_id, self.command.property.removeprefix("extra."))

    def selection_key(self) -> tuple[int, int, int]:
        """Static priority first; runtime registration order breaks ties."""

        return (
            -priority_rank(self.proposal.priority),
            self.proposal_index,
            self.command_index,
        )


class FlatPriorityResolver:
    """Select one command per target using only the static priority order."""

    strategy_id: ClassVar[str] = "flat_priority"
    event_source: ClassVar[str] = "flat_priority"
    strategy_version: ClassVar[str] = "1"

    def resolve(
        self,
        proposals: Sequence[AgentProposal],
        root_event: SimEvent,
        world_snapshot: WorldState | None = None,
        *,
        energy_review: EnergyVetoReview | None = None,
        user_claims: Mapping[CommandTarget, ExplicitUserClaim] | None = None,
    ) -> ArbiterResult:
        del root_event, world_snapshot, energy_review, user_claims
        entries = [
            _FlatEntry(proposal, command, proposal_index, command_index)
            for proposal_index, proposal in enumerate(proposals)
            for command_index, command in enumerate(proposal.commands)
        ]
        ordered = sorted(entries, key=_FlatEntry.selection_key)

        winners: dict[CommandTarget, _FlatEntry] = {}
        for entry in ordered:
            winners.setdefault(entry.target, entry)

        approved = [
            _approved(entry.proposal, entry.command)
            for entry in ordered
            if winners[entry.target] is entry
        ]
        rejected: list[RejectedCommand] = []
        for entry in ordered:
            winner = winners[entry.target]
            if winner is entry:
                continue
            rejected.append(
                RejectedCommand(
                    **_approved(entry.proposal, entry.command).model_dump(),
                    conflict_class=ConflictClass.SAME_DEVICE_PROPERTY,
                    rejection_reason=(
                        "flat_priority selected "
                        f"{winner.proposal.agent_id} for "
                        f"{entry.command.device_id}.{entry.target[1]}"
                    ),
                    winner_agent_id=winner.proposal.agent_id,
                    winner_priority=winner.proposal.priority,
                    resolution=ConflictResolution.TOTAL_ORDER,
                )
            )

        return ArbiterResult(
            approved_commands=approved,
            rejected_commands=rejected,
            # Target selection is deliberately not presented as Aura conflict
            # classification; this baseline performs no contextual governance.
            conflicts=[],
            winning_priority=_winning_priority(approved),
            explanation=(
                "governance=flat_priority: selected "
                f"{len(approved)} target winners and rejected {len(rejected)} commands"
            ),
            per_agent=_per_agent(proposals, approved, rejected),
        )


class AuraResolver:
    """Delegate unchanged to Aura's existing context-aware ``Arbiter``."""

    strategy_id: ClassVar[str] = "aura"
    event_source: ClassVar[str] = ARBITER_ID
    strategy_version: ClassVar[str] = "1"

    def __init__(self, arbiter: Arbiter | None = None) -> None:
        self.arbiter = arbiter if arbiter is not None else Arbiter()

    def resolve(
        self,
        proposals: Sequence[AgentProposal],
        root_event: SimEvent,
        world_snapshot: WorldState | None = None,
        *,
        energy_review: EnergyVetoReview | None = None,
        user_claims: Mapping[CommandTarget, ExplicitUserClaim] | None = None,
    ) -> ArbiterResult:
        return self.arbiter.resolve(
            proposals,
            root_event,
            world_snapshot,
            energy_review=energy_review,
            user_claims=user_claims,
        )


def build_governance_resolver(
    value: str | Enum, *, arbiter: Arbiter | None = None
) -> ProposalResolver:
    """Build one implemented resolver and reject unknown provenance values."""

    raw = value.value if isinstance(value, Enum) else value
    if raw == "none":
        return PassthroughResolver()
    if raw == "flat_priority":
        return FlatPriorityResolver()
    if raw == "aura":
        return AuraResolver(arbiter)
    raise ValueError(f"unsupported governance resolver: {raw!r}")
