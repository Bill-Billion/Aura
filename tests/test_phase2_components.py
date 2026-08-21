from __future__ import annotations

from backend.agents.arbiter import Arbiter, ConflictClass
from backend.agents.contracts import AgentProposal, PriorityLevel
from backend.agents.memory import AgentMemoryStore
from backend.agents.llm import AnthropicCompatibleProvider
from backend.agents.runtime import AgentRuntime, TriggerClassifier
from backend.agents.types import AgentCommandProposal
from backend.engine.event_bus import SimEvent


def _event(
    event_type: str,
    *,
    correlation_id: str = "corr-1",
    timestamp: float = 1.0,
    data: dict | None = None,
) -> SimEvent:
    return SimEvent(
        event_type=event_type,
        source="test",
        timestamp=timestamp,
        wall_time=timestamp,
        correlation_id=correlation_id,
        priority=1,
        data=data or {},
    )


def test_trigger_classifier_rejects_timer_tick():
    classifier = TriggerClassifier()

    assert classifier.should_start_episode(_event("system.timer_tick")) is False


def test_trigger_classifier_accepts_user_and_significant_environment_events():
    classifier = TriggerClassifier()

    assert classifier.should_start_episode(_event("user.command")) is True
    assert classifier.should_start_episode(_event("user.activity_change")) is True
    assert classifier.should_start_episode(
        _event(
            "environment.state_refresh",
            data={"significant_change_reasons": ["time_bucket"]},
        )
    ) is True
    assert classifier.should_start_episode(
        _event(
            "environment.state_refresh",
            data={"significant_change_reasons": []},
        )
    ) is False


def test_memory_store_trims_correlation_and_agent_history():
    store = AgentMemoryStore(max_events_per_correlation=2, max_agent_events=3)

    root = _event("user.command", correlation_id="corr-1", timestamp=1.0)
    intent = _event("reasoning.intent_recognized", correlation_id="corr-1", timestamp=2.0)
    action = _event("action.device_control", correlation_id="corr-1", timestamp=3.0)
    action.source = "lighting_agent"

    store.remember(root)
    store.remember(intent)
    store.remember(action, agent_id="lighting_agent")

    correlation_events = store.get_correlation_history("corr-1")
    assert [event.event_type for event in correlation_events] == [
        "reasoning.intent_recognized",
        "action.device_control",
    ]

    store.remember(_event("reasoning.execution_plan", timestamp=4.0), agent_id="lighting_agent")
    store.remember(_event("reasoning.coordination_decision", timestamp=5.0), agent_id="lighting_agent")
    store.remember(_event("reasoning.perception_snapshot", timestamp=6.0), agent_id="lighting_agent")

    recent_agent_events = store.get_agent_recent_events("lighting_agent")
    assert len(recent_agent_events) == 3
    assert recent_agent_events[-1].event_type == "reasoning.perception_snapshot"


def test_memory_prompt_omits_volatile_event_identities():
    store = AgentMemoryStore()
    root = _event(
        "environment.light_level_threshold",
        correlation_id="corr-1",
        data={
            "room_id": "living_room",
            "trigger_event_id": "random-event-id",
            "nested": {"caused_by_event_id": "another-random-id", "value": 10},
        },
    )
    store.remember(root)

    lines = store.build_recent_event_lines("lighting_agent", "corr-1")

    assert lines == [
        "environment.light_level_threshold: "
        "{'nested': {'value': 10}, 'room_id': 'living_room'}"
    ]


def test_arbiter_prefers_higher_priority():
    arbiter = Arbiter()

    comfort = AgentProposal(
        agent_id="hvac_agent",
        intent="cool occupied room",
        priority=PriorityLevel.COMFORT,
        confidence=0.91,
        commands=[
            AgentCommandProposal(
                device_id="ac_living_01",
                property="extra.target_temp",
                value=22,
                reason="keep occupied room comfortable",
            )
        ],
    )
    energy = AgentProposal(
        agent_id="energy_agent",
        intent="save energy",
        priority=PriorityLevel.ENERGY,
        confidence=0.88,
        commands=[
            AgentCommandProposal(
                device_id="ac_living_01",
                property="extra.target_temp",
                value=27,
                reason="reduce compressor usage",
            )
        ],
    )

    result = arbiter.resolve([energy, comfort], root_event=_event("environment.state_refresh"))

    assert len(result.approved_commands) == 1
    assert result.approved_commands[0].value == 22
    assert result.winning_priority is PriorityLevel.COMFORT
    assert len(result.conflicts) == 1
    assert result.conflicts[0].winner_agent_id == "hvac_agent"
    assert result.conflicts[0].loser_agent_id == "energy_agent"
    assert result.conflicts[0].conflict_class is ConflictClass.SAME_DEVICE_PROPERTY
def test_agent_runtime_builds_anthropic_provider_from_env(monkeypatch):
    monkeypatch.setenv("AURA_ALLOW_LIVE_LLM", "1")
    monkeypatch.setenv("LLM_PROVIDER", "anthropic_compatible")
    monkeypatch.setenv("ANTHROPIC_COMPAT_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.minimax.io/anthropic")

    runtime = AgentRuntime()

    assert isinstance(runtime.llm_provider, AnthropicCompatibleProvider)
