from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.agents.llm import (
    AGENT_DECISION_SCHEMA_SHA256,
    LLMProvider,
    LLMProviderError,
)
from backend.agents.types import AgentCommandProposal, AgentLLMDecision
from backend.engine.run_manager import read_source_revision
from backend.experiments.llm_substudy import (
    LLMSubstudyRunner,
    MINIMAX_M3_ENDPOINT,
    PreflightReceipt,
    ResolvedLLMSubstudy,
    SlotError,
    SlotResult,
    SlotResultArtifact,
    preflight_llm_substudy,
    read_slot_result,
    read_resolved_llm_substudy,
    resolve_llm_substudy,
    summarize_llm_substudy,
    validate_preflight_receipt,
    write_resolved_llm_substudy,
    write_slot_result,
    _evaluation_semantics,
    _validate_model_failure_noops,
)
from backend.experiments.spec import sha256_json


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "aurabench-m3-substudy" / "manifest.yaml"


class _StubM3Provider(LLMProvider):
    provider_name = "anthropic_compatible"
    model = "MiniMax-M3"
    api_key = "test-key"
    max_tokens = 1200
    base_url = MINIMAX_M3_ENDPOINT
    anthropic_version = "2023-06-01"
    timeout_ms = 45000
    strict_output = True
    decision_schema_sha256 = AGENT_DECISION_SCHEMA_SHA256

    def __init__(self) -> None:
        self.calls = 0
        self.last_usage = None
        self.last_decision_transport = None
        self.last_response_model = None

    async def generate_decision(self, request):  # type: ignore[override]
        self.calls += 1
        self.last_usage = {"input_tokens": 40, "output_tokens": 12}
        self.last_decision_transport = "tool_use"
        self.last_response_model = self.model
        proposals: list[AgentCommandProposal] = []
        if request.agent_id == "lighting_agent":
            targets = {
                "power": True,
                "extra.brightness": (
                    100 if request.root_event_type == "safety.smoke_detected" else 70
                ),
                "extra.color_temp": 3000,
            }
            proposals = [
                AgentCommandProposal(
                    device_id=item["device_id"],
                    property=item["property"],
                    value=targets[item["property"]],
                    reason="deterministic test fixture",
                )
                for item in request.allowed_commands
                if item["device_id"] == "light_living_01"
            ]
        elif (
            request.agent_id == "security_agent"
            and request.root_event_type == "safety.smoke_detected"
        ):
            targets = {"power": True, "extra.brightness": 100}
            proposals = [
                AgentCommandProposal(
                    device_id=item["device_id"],
                    property=item["property"],
                    value=targets[item["property"]],
                    reason="deterministic evacuation fixture",
                )
                for item in request.allowed_commands
                if item["device_id"] == "light_living_01"
            ]
        return AgentLLMDecision(
            intent="observe safely",
            confidence=0.9,
            task_steps=[],
            proposed_commands=proposals,
            explanation="No immediate device action is required.",
            needs_coordination=False,
        )


class _NoActionM3Provider(_StubM3Provider):
    async def generate_decision(self, request):  # type: ignore[override]
        self.calls += 1
        self.last_usage = {"input_tokens": 40, "output_tokens": 12}
        self.last_decision_transport = "tool_use"
        self.last_response_model = self.model
        return AgentLLMDecision(
            intent="observe without acting",
            confidence=0.8,
            task_steps=[],
            proposed_commands=[],
            explanation="No action selected by the model.",
            needs_coordination=False,
        )


class _InvalidOutputM3Provider(_StubM3Provider):
    async def generate_decision(self, request):  # type: ignore[override]
        self.calls += 1
        self.last_usage = {"input_tokens": 40, "output_tokens": 12}
        self.last_decision_transport = "text_json"
        self.last_response_model = self.model
        raise LLMProviderError("invalid_output", "truncated model JSON")


class _DomainInvalidOutputM3Provider(_StubM3Provider):
    async def generate_decision(self, request):  # type: ignore[override]
        if request.agent_id == "home_orchestrator":
            return await super().generate_decision(request)
        self.calls += 1
        self.last_usage = {"input_tokens": 40, "output_tokens": 12}
        self.last_decision_transport = "text_json"
        self.last_response_model = self.model
        raise LLMProviderError("invalid_output", "truncated domain decision")


async def _write_test_preflight(study, output_dir: Path) -> None:
    await preflight_llm_substudy(
        study,
        output_dir=output_dir,
        provider_factory=lambda _: _StubM3Provider(),
    )


def test_option_b_resolves_to_fixed_24_instance_168_slot_contract():
    study = resolve_llm_substudy(MANIFEST, source_revision="build:test")

    assert len(study.scenarios) == 8
    assert len(study.instances) == 24
    assert len(study.slots) == 168
    assert {
        kind: sum(slot.kind == kind for slot in study.slots)
        for kind in ("live", "capture", "replay")
    } == {"live": 72, "capture": 24, "replay": 72}
    assert study.provider == "anthropic_compatible"
    assert study.model == "MiniMax-M3"
    assert study.endpoint == MINIMAX_M3_ENDPOINT
    assert study.billing_mode == "token_plan"


def test_resolved_contract_rejects_rehashed_slot_design_tampering():
    study = resolve_llm_substudy(MANIFEST, source_revision="build:test")
    payload = study.model_dump(mode="json")
    payload["slots"][0]["repetition"] = 9
    contract = {key: value for key, value in payload.items() if key != "study_hash"}
    payload["study_hash"] = sha256_json(contract)

    with pytest.raises(ValueError, match=r"frozen 3\+1\+3 design"):
        ResolvedLLMSubstudy.model_validate(payload)


def test_replay_equivalence_ignores_transport_latency_but_not_behavior_metrics():
    capture = {
        "outcome": "pass",
        "criteria_checks": {"device_state_match_rate": True},
        "metrics": {
            "first_action_latency_ms": {"value": 10_000.0, "unit": "ms"},
            "device_state_match_rate": {"value": 1.0, "unit": "ratio"},
        },
    }
    replay = {
        **capture,
        "metrics": {
            "first_action_latency_ms": {"value": 10.0, "unit": "ms"},
            "device_state_match_rate": {"value": 1.0, "unit": "ratio"},
        },
    }

    assert _evaluation_semantics(capture) == _evaluation_semantics(replay)
    replay["metrics"]["device_state_match_rate"]["value"] = 0.5
    assert _evaluation_semantics(capture) != _evaluation_semantics(replay)


def test_provider_failure_noop_ignores_earlier_plan_in_same_correlation():
    events = [
        {
            "seq": 1,
            "event_id": "old-plan",
            "event_type": "reasoning.execution_plan",
            "source": "lighting_agent",
            "correlation_id": "shared-correlation",
            "data": {"execution_mode": "llm", "commands": [{"device_id": "light"}]},
        },
        {
            "seq": 2,
            "event_id": "old-action",
            "event_type": "action.device_control",
            "source": "lighting_agent",
            "correlation_id": "shared-correlation",
            "data": {"device_id": "light"},
        },
        {
            "seq": 3,
            "event_id": "failure",
            "event_type": "reasoning.provider_failure_noop",
            "source": "lighting_agent",
            "correlation_id": "shared-correlation",
            "data": {
                "reason": "invalid_output",
                "fallback_strategy": "none",
            },
        },
        {
            "seq": 4,
            "event_id": "noop-plan",
            "event_type": "reasoning.execution_plan",
            "source": "lighting_agent",
            "correlation_id": "shared-correlation",
            "data": {
                "execution_mode": "provider_failure_noop",
                "commands": [],
                "provider_failure_reason": "invalid_output",
            },
        },
    ]

    assert _validate_model_failure_noops(events) == {"invalid_output": 1}


@pytest.mark.anyio
async def test_preflight_is_sealed_and_bound_to_exact_provider_model(tmp_path):
    study = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    provider = _StubM3Provider()

    path = await preflight_llm_substudy(
        study,
        output_dir=tmp_path,
        provider_factory=lambda _: provider,
    )

    assert path.name == "preflight.json"
    receipt = validate_preflight_receipt(study, output_dir=tmp_path).receipt
    assert receipt.provider == "anthropic_compatible"
    assert receipt.model == "MiniMax-M3"
    assert receipt.endpoint == MINIMAX_M3_ENDPOINT
    assert receipt.response_model == "MiniMax-M3"
    assert receipt.decision_transport == "tool_use"
    assert receipt.input_tokens == 40
    assert provider.calls == 1


@pytest.mark.anyio
async def test_preflight_rejects_endpoint_drift_before_call(tmp_path):
    study = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    provider = _StubM3Provider()
    provider.base_url = "https://example.invalid/anthropic"

    with pytest.raises(ValueError, match="endpoint does not match"):
        await preflight_llm_substudy(
            study,
            output_dir=tmp_path,
            provider_factory=lambda _: provider,
        )

    assert provider.calls == 0


def test_resolved_substudy_reader_rejects_symlink(tmp_path):
    study = resolve_llm_substudy(MANIFEST, source_revision="build:test")
    real_path = write_resolved_llm_substudy(tmp_path / "real", study)
    link_path = tmp_path / "resolved-link.json"
    link_path.symlink_to(real_path)

    with pytest.raises(ValueError, match="cannot read artifact"):
        read_resolved_llm_substudy(link_path)


@pytest.mark.anyio
async def test_runner_refuses_paid_slots_without_preflight(tmp_path):
    full = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    instance = full.instances[0]
    study = full.model_copy(
        update={
            "instances": [instance],
            "slots": [
                slot for slot in full.slots if slot.instance_id == instance.instance_id
            ],
        }
    )
    provider = _StubM3Provider()

    with pytest.raises(ValueError, match="cannot read artifact"):
        await LLMSubstudyRunner(
            study,
            output_dir=tmp_path,
            provider_factory=lambda _: provider,
        ).run()

    assert provider.calls == 0


@pytest.mark.anyio
async def test_invalid_evidence_is_create_only_and_never_retried(tmp_path):
    full = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    slot = full.slots[0]
    instance = next(item for item in full.instances if item.instance_id == slot.instance_id)
    study = full.model_copy(update={"instances": [instance], "slots": [slot]})
    write_slot_result(
        tmp_path,
        SlotResultArtifact.build(
            SlotResult(
                study_hash=study.study_hash,
                slot_id=slot.slot_id,
                status="invalid_evidence",
                error=SlotError(type="invalid_output", message="frozen failure"),
            )
        ),
    )
    provider = _StubM3Provider()
    await _write_test_preflight(study, tmp_path)

    counts = await LLMSubstudyRunner(
        study,
        output_dir=tmp_path,
        provider_factory=lambda _: provider,
    ).run(continue_on_error=True)

    assert counts == {
        "planned": 1,
        "admitted": 0,
        "invalid_evidence": 1,
        "failed": 0,
        "skipped": 1,
    }
    assert provider.calls == 0


@pytest.mark.anyio
async def test_missing_action_anchor_is_admitted_as_a_failed_model_outcome(tmp_path):
    full = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    slot = full.slots[0]
    instance = next(item for item in full.instances if item.instance_id == slot.instance_id)
    study = full.model_copy(update={"instances": [instance], "slots": [slot]})
    provider = _NoActionM3Provider()
    await _write_test_preflight(study, tmp_path)

    counts = await LLMSubstudyRunner(
        study,
        output_dir=tmp_path,
        provider_factory=lambda _: provider,
    ).run()

    assert counts["admitted"] == 1
    result = read_slot_result(tmp_path, slot.slot_id).result
    assert result.evaluation["outcome"] == "fail"
    assert result.evaluation["criteria_checks"]["perturbation_anchor_observed"] is False


@pytest.mark.anyio
async def test_invalid_model_output_is_admitted_as_strict_noop_not_rule_fallback(tmp_path):
    full = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    slot = full.slots[0]
    instance = next(item for item in full.instances if item.instance_id == slot.instance_id)
    study = full.model_copy(update={"instances": [instance], "slots": [slot]})
    provider = _InvalidOutputM3Provider()
    await _write_test_preflight(study, tmp_path)

    counts = await LLMSubstudyRunner(
        study,
        output_dir=tmp_path,
        provider_factory=lambda _: provider,
    ).run()

    assert counts["admitted"] == 1
    result = read_slot_result(tmp_path, slot.slot_id).result
    assert result.model_failure_count > 0
    assert result.model_failure_reasons == {"invalid_output": result.model_failure_count}
    assert result.evaluation["outcome"] == "fail"
    events = (tmp_path / "runs" / result.run_id / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "reasoning.provider_failure_noop" in events
    assert "reasoning.fallback_rule_based" not in events
    assert "action.device_control" not in events


@pytest.mark.anyio
async def test_one_instance_runs_3_live_capture_and_3_offline_replays_resumably(
    tmp_path,
):
    full = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    instance = full.instances[0]
    slots = [slot for slot in full.slots if slot.instance_id == instance.instance_id]
    # Keep the production contract strict at 24/168; this test selects one complete
    # dependency group after resolution so it exercises the real executor cheaply.
    study = full.model_copy(update={"instances": [instance], "slots": slots})
    providers: list[_StubM3Provider] = []

    def factory(_):
        provider = _StubM3Provider()
        providers.append(provider)
        return provider

    await _write_test_preflight(study, tmp_path)

    runner = LLMSubstudyRunner(
        study,
        output_dir=tmp_path,
        provider_factory=factory,
    )
    first = await runner.run()

    assert first == {
        "planned": 7,
        "admitted": 7,
        "invalid_evidence": 0,
        "failed": 0,
        "skipped": 0,
    }
    assert len(providers) == 4  # three live + one capture; replay never builds live I/O
    capture = next(slot for slot in slots if slot.kind == "capture")
    capture_result = read_slot_result(tmp_path, capture.slot_id).result
    assert capture_result.recording["complete"] is True
    for slot in slots:
        result = read_slot_result(tmp_path, slot.slot_id).result
        assert result.status == "admitted"
        if slot.kind == "replay":
            assert result.capture_source_run_id == capture_result.run_id
            assert result.replay_equivalent is True
            assert result.usage["billable_calls"] == 0

    second = await runner.run()
    assert second["admitted"] == 7
    assert second["skipped"] == 7
    assert len(providers) == 4

    summary_path = summarize_llm_substudy(study, output_dir=tmp_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    preflight = validate_preflight_receipt(study, output_dir=tmp_path)
    assert summary["results"]["preflight_sha256"] == preflight.seal.sha256


@pytest.mark.anyio
async def test_capture_replay_preserves_domain_invalid_output_noop_evidence(tmp_path):
    full = resolve_llm_substudy(MANIFEST, source_revision=read_source_revision())
    instance = full.instances[0]
    slots = [slot for slot in full.slots if slot.instance_id == instance.instance_id]
    study = full.model_copy(update={"instances": [instance], "slots": slots})
    await _write_test_preflight(study, tmp_path)

    counts = await LLMSubstudyRunner(
        study,
        output_dir=tmp_path,
        provider_factory=lambda _: _DomainInvalidOutputM3Provider(),
    ).run()

    assert counts["admitted"] == 7
    capture_slot = next(slot for slot in slots if slot.kind == "capture")
    capture = read_slot_result(tmp_path, capture_slot.slot_id).result
    assert capture.model_failure_count > 0
    for slot in slots:
        if slot.kind != "replay":
            continue
        replay = read_slot_result(tmp_path, slot.slot_id).result
        assert replay.replay_equivalent is True
        assert replay.model_failure_count == capture.model_failure_count
        assert replay.model_failure_reasons == capture.model_failure_reasons


def test_preflight_receipt_contains_no_api_key_field():
    # Schema-level regression: secrets are not representable in the receipt.
    assert "api_key" not in PreflightReceipt.model_fields
