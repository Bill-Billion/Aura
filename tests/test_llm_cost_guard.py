"""LLM 成本护栏：一条 episode 的花费上限与 budget_exceeded 降级（S3-T8）。

这里断的四件事，每一件都对应"不断就会静默塌掉"的性质：

1. **用量要从 provider 的回包里读出来**，读不到再估。两家（OpenAI Responses /
   Anthropic 兼容，含 MiniMax）的 usage 块形状不同，形状变了就得在这里红。
2. **估算绝不能估成 0**。用量缺失时若按 0 计，护栏就会一路放行——"没有账单"与
   "花了钱但没记账"是两回事，后者才是本任务要防的。
3. **预算在 episode 中途用尽时，剩下的 agent 必须带标签降级**：
   ``LLMProviderError("budget_exceeded")`` → 既有 fallback 路径 →
   ``reasoning.fallback_rule_based`` 的 data.reason 就是 budget_exceeded，
   研究者能从推理流里数出"预算是在哪一步咬下去的"。
4. **花费要能被工件记住**（coordination_decision 的 data + run 目录下的成本工件），
   否则 benchmark 跑完只有"跑通了"，没有"花了多少"。

本文件一次真网络调用都不发：所有 provider 都是本地桩或本仓库的 mocked/replay 实现。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend.agents.contracts import RootEventContext
from backend.agents.hvac import HVACAgent
from backend.agents.lighting import LightingAgent
from backend.agents.llm import LLMProvider, LLMProviderError, OpenAIResponsesProvider
from backend.agents.llm_modes import (
    BUDGET_EXCEEDED_REASON,
    DEFAULT_EPISODE_BUDGET_USD,
    EPISODE_BUDGET_ENV,
    BudgetGuardedLLMProvider,
    EpisodeCostGuard,
    LLMMode,
    MockedLLMProvider,
    canonical_request_payload,
    llm_mode_health,
    resolve_episode_budget_usd,
    resolve_mode_for_provider,
)
from backend.agents.llm_pricing import (
    LLM_COST_FILENAME,
    PRICING_ENV,
    PRICING_TABLE,
    TokenUsage,
    UsageSource,
    call_cost_usd,
    estimate_tokens,
    estimate_usage,
    load_pricing_table,
    lookup_price,
    parse_usage,
    worst_case_call_cost_usd,
)
from backend.agents.memory import AgentMemoryStore
from backend.agents.orchestrator import DomainAgentBinding, HomeOrchestratorAgent
from backend.agents.types import (
    MAX_EXPLANATION_CHARS,
    MAX_WORLD_SUMMARY_CHARS,
    AgentLLMDecision,
    LLMDecisionRequest,
)
from backend.engine.event_bus import SimEvent
from backend.engine.run_manager import canonical_json
from backend.main import _init_default_state

pytestmark = pytest.mark.anyio

RUN_ID = "run-20260721T093012-4f3a9c21"


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------- 夹具


def _world():
    return _init_default_state().world


def _arrive_home_event() -> SimEvent:
    return SimEvent(
        event_id="root-arrive-home",
        event_type="user.arrives_home",
        source="user_behavior_sim",
        timestamp=12.0,
        wall_time=12.0,
        correlation_id="corr-arrive-home",
        priority=2,
        data={"user_id": "user_01", "to_room": "living_room"},
    )


def _request(agent_id: str = "lighting_agent") -> LLMDecisionRequest:
    return LLMDecisionRequest(
        agent_id=agent_id,
        agent_name=agent_id,
        root_event_type="user.arrives_home",
        world_summary="time=evening; temp=21.3; light_level=0.4",
        recent_events=[],
        available_devices=[],
        allowed_commands=[],
    )


class _BillableStubProvider(LLMProvider):
    """声明 RECORDED（=会真花钱的写侧）的本地桩，零网络。"""

    provider_name = "stub_billable"
    llm_mode = LLMMode.RECORDED

    def __init__(self, *, model: str = "stub-priced-1", usage: dict | None = None) -> None:
        self.model = model
        self.api_key = "stub"
        self.max_tokens = 1200
        self.calls: list[LLMDecisionRequest] = []
        # provider 侧回包里的 usage 块（None = 这家不给用量，走字符估算）
        self._usage = usage
        self.last_usage = None

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.calls.append(request)
        self.last_usage = self._usage
        return AgentLLMDecision(
            intent="stub intent",
            confidence=0.9,
            task_steps=["step"],
            proposed_commands=[],
            explanation="stub explanation",
        )


# ------------------------------------------------- 1. 用量解析（两家回包形状）


def test_usage_parsed_from_openai_and_anthropic_response_fixtures():
    """两家 usage 块的形状都要认得；认不得就必须让上层去估，而不是记 0。"""

    openai_payload = {
        "id": "resp_123",
        "output_text": '{"intent":"x"}',
        "usage": {"input_tokens": 1234, "output_tokens": 321, "total_tokens": 1555},
    }
    anthropic_payload = {
        "id": "msg_123",
        "content": [{"type": "text", "text": "{}"}],
        "usage": {
            "input_tokens": 900,
            "output_tokens": 210,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 50,
        },
    }
    chat_completions_payload = {
        "choices": [],
        "usage": {"prompt_tokens": 700, "completion_tokens": 90, "total_tokens": 790},
    }

    openai_usage = parse_usage(openai_payload)
    assert openai_usage == TokenUsage(
        input_tokens=1234, output_tokens=321, source=UsageSource.REPORTED
    )

    anthropic_usage = parse_usage(anthropic_payload)
    # 缓存读/写的 token 计入输入：宁可略高估，也不要让"看起来很便宜"的账单放行。
    assert anthropic_usage is not None
    assert anthropic_usage.input_tokens == 900 + 100 + 50
    assert anthropic_usage.output_tokens == 210
    assert anthropic_usage.source is UsageSource.REPORTED

    chat_usage = parse_usage(chat_completions_payload)
    assert chat_usage is not None
    assert (chat_usage.input_tokens, chat_usage.output_tokens) == (700, 90)

    # 直接给 usage 块本身也认（provider 只把 usage 存下来时的形状）
    assert parse_usage({"input_tokens": 5, "output_tokens": 6}) == TokenUsage(
        input_tokens=5, output_tokens=6, source=UsageSource.REPORTED
    )

    # MiniMax 在 Anthropic 兼容路径上可能整个不给 usage：必须返回 None（=去估），
    # 而不是返回一条全 0 的用量。
    assert parse_usage({"content": [{"type": "text", "text": "{}"}]}) is None
    assert parse_usage({"usage": {}}) is None
    assert parse_usage(None) is None

    # 只有 total_tokens 时保守地全算成输出（输出更贵），并标成 partial。
    partial = parse_usage({"usage": {"total_tokens": 400}})
    assert partial is not None
    assert partial.total_tokens == 400
    assert partial.output_tokens == 400
    assert partial.source is UsageSource.PARTIAL


def test_missing_usage_falls_back_to_char_estimate_not_zero():
    """用量缺失 → 字符估算，且结果必须 > 0 且计出正的成本。"""

    request_text = "x" * 4000
    response_text = "y" * 400
    usage = estimate_usage(request_text=request_text, response_text=response_text)

    assert usage.source is UsageSource.ESTIMATED
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.input_tokens == estimate_tokens(request_text)

    cost = call_cost_usd(usage, model="stub-unknown-model")
    assert cost > 0.0, "估算出的用量必须产生正成本，否则护栏会一路放行"

    # 中文比 ASCII 更"token 密"：同样字符数不能估得更少，否则中文场景会被系统性低估。
    cjk = estimate_tokens("温度" * 1000)
    ascii_tokens = estimate_tokens("t" * 2000)
    assert cjk >= ascii_tokens

    # 空文本是真的 0（没有内容就没有账单），这与"用量缺失"是两回事。
    assert estimate_tokens("") == 0


# ------------------------------------------------------- 2. 价格表是数据


def test_pricing_entries_carry_provenance_and_unknown_model_uses_marked_default():
    """价格是数据：每条都要说清属于谁、多少钱、哪天看的、出处在哪。"""

    assert PRICING_TABLE.entries, "价格表不能为空"
    for price in PRICING_TABLE.entries:
        assert price.provider, "价格必须归属到 provider（同名模型在不同网关不同价）"
        assert price.model
        assert price.input_usd_per_mtok >= 0.0
        assert price.output_usd_per_mtok >= price.input_usd_per_mtok * 0.5
        assert price.source, "没有出处的价格不可核对"
        assert len(price.as_of) == 10 and price.as_of[4] == "-", "as_of 必须是 YYYY-MM-DD"

    known = lookup_price("MiniMax-M2.7")
    assert known.provider
    assert known.is_default is False

    unknown = lookup_price("totally-unknown-model-9000")
    assert unknown.is_default is True
    assert unknown.verified is False
    assert "unknown" in unknown.source.lower() or "default" in unknown.source.lower()

    # 大小写/网关前缀不该造成"未知模型"
    assert lookup_price("minimax-m2.7").model == known.model


def test_pricing_table_can_be_overridden_by_json_file(tmp_path, monkeypatch):
    """研究者换模型/换网关时改数据文件，不改代码。"""

    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "default": {
                    "provider": "lab",
                    "model": "default",
                    "input_usd_per_mtok": 1.0,
                    "output_usd_per_mtok": 2.0,
                    "source": "lab default (unknown model)",
                    "as_of": "2026-07-21",
                },
                "models": [
                    {
                        "provider": "lab",
                        "model": "lab-tiny",
                        "input_usd_per_mtok": 0.5,
                        "output_usd_per_mtok": 1.5,
                        "source": "https://example.invalid/pricing",
                        "as_of": "2026-07-21",
                        "verified": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    table = load_pricing_table(path)
    price = table.lookup("lab-tiny")
    assert price.verified is True
    assert price.input_usd_per_mtok == 0.5
    assert table.lookup("nope").is_default is True

    monkeypatch.setenv(PRICING_ENV, str(path))
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, source=UsageSource.REPORTED)
    assert call_cost_usd(usage, model="lab-tiny") == pytest.approx(2.0)


# ------------------------------------------------------- 3. 预算与降级


def test_budget_env_override_and_default():
    assert resolve_episode_budget_usd({}) == DEFAULT_EPISODE_BUDGET_USD
    assert DEFAULT_EPISODE_BUDGET_USD == 0.10
    assert resolve_episode_budget_usd({EPISODE_BUDGET_ENV: "0.02"}) == pytest.approx(0.02)
    with pytest.raises(ValueError):
        resolve_episode_budget_usd({EPISODE_BUDGET_ENV: "not-a-number"})


async def test_budget_breach_mid_episode_switches_remaining_agents_to_rule_fallback_with_budget_exceeded_reason():
    """预算在 episode 中途咬下去：后续 agent 走规则回退，且理由是 budget_exceeded。"""

    inner = _BillableStubProvider(usage={"input_tokens": 20_000, "output_tokens": 1_000})
    guard = EpisodeCostGuard(budget_usd=0.05)
    provider = BudgetGuardedLLMProvider(inner, guard, correlation_id="corr-arrive-home")

    # 前若干次调用真的走到了 provider
    await provider.generate_decision(_request())
    assert len(inner.calls) == 1

    calls_before_block = 1
    with pytest.raises(LLMProviderError) as excinfo:
        for _ in range(20):
            await provider.generate_decision(_request())
            calls_before_block += 1
    assert excinfo.value.reason == BUDGET_EXCEEDED_REASON
    assert len(inner.calls) == calls_before_block, "被拦下的那次不能真的打到 provider"

    episode = guard.episode("corr-arrive-home")
    assert episode.budget_exceeded is True
    assert episode.blocked_calls >= 1
    assert episode.cost_usd > 0.0

    # —— agent 侧：同一台被拦的 provider，envelope 必须带标签降级 ——
    envelope = await LightingAgent().handle_event(
        root_event=_arrive_home_event(),
        world_state=_world(),
        memory_store=AgentMemoryStore(),
        llm_provider=provider,
    )
    assert envelope is not None
    assert envelope.mode == "fallback_rule_based"
    assert envelope.fallback_reason == BUDGET_EXCEEDED_REASON

    # —— 编排器侧：intent 步骤同样带 budget_exceeded 落到 plan 上 ——
    context = RootEventContext.from_observable_world(
        root_event=_arrive_home_event(),
        observable_world=_world(),
        run_id=RUN_ID,
        scenario_id="user_arrives_home_evening",
    )
    bindings = [
        DomainAgentBinding.from_agent(LightingAgent()),
        DomainAgentBinding.from_agent(HVACAgent()),
    ]
    decision = await HomeOrchestratorAgent(llm_intent_enabled=True).plan(
        context, bindings, llm_provider=provider
    )
    assert decision.plan.fallback_reason == BUDGET_EXCEEDED_REASON
    # 措辞必须说清"是我们按预算主动没调"，而不是含糊的"LLM 不可用"——
    # 后者会被读成 provider 故障。
    assert "预算" in decision.explanation
    assert EPISODE_BUDGET_ENV in decision.explanation
    assert "LLM 不可用" not in decision.explanation
    intent_data = decision.intent_event_data()
    assert intent_data["fallback_reason"] == BUDGET_EXCEEDED_REASON
    assert intent_data["llm_mode"] == LLMMode.RECORDED.value


async def test_first_call_of_an_episode_is_never_blocked_by_worst_case_alone():
    """护栏不能把 episode 的第一次调用饿死（plan_raw 风险条 #2）。"""

    inner = _BillableStubProvider(usage={"input_tokens": 1000, "output_tokens": 200})
    guard = EpisodeCostGuard(budget_usd=DEFAULT_EPISODE_BUDGET_USD)
    provider = BudgetGuardedLLMProvider(inner, guard, correlation_id="corr-1")

    await provider.generate_decision(_request())
    assert len(inner.calls) == 1
    assert guard.episode("corr-1").budget_exceeded is False


async def test_budget_is_per_episode_not_global():
    """一条 episode 用超，不能连带把另一条 episode 判死。"""

    inner = _BillableStubProvider(usage={"input_tokens": 40_000, "output_tokens": 1_000})
    guard = EpisodeCostGuard(budget_usd=0.05)
    hot = BudgetGuardedLLMProvider(inner, guard, correlation_id="corr-hot")

    with pytest.raises(LLMProviderError):
        for _ in range(20):
            await hot.generate_decision(_request())

    cool = hot.for_episode("corr-cool")
    decision = await cool.generate_decision(_request())
    assert decision.intent == "stub intent"
    assert guard.episode("corr-cool").budget_exceeded is False
    assert guard.episode("corr-hot").budget_exceeded is True


async def test_mocked_provider_calls_are_free_and_never_blocked():
    """mocked/replay 不花钱，也就不能被预算改写行为——否则 S2 的字节一致性门会被污染。"""

    guard = EpisodeCostGuard(budget_usd=0.0)
    provider = BudgetGuardedLLMProvider(MockedLLMProvider(), guard, correlation_id="corr-mock")

    for _ in range(5):
        await provider.generate_decision(_request())

    episode = guard.episode("corr-mock")
    assert episode.cost_usd == 0.0
    assert episode.blocked_calls == 0
    assert episode.billable_calls == 0
    assert episode.calls == 5


async def test_guard_wrapper_is_transparent_to_run_metadata():
    """包一层不能把 run 元数据里的模式/模型改写成包装层自己。"""

    inner = _BillableStubProvider(model="stub-priced-1")
    guard = EpisodeCostGuard()
    provider = BudgetGuardedLLMProvider(inner, guard)

    assert provider.model == "stub-priced-1"
    assert provider.provider_name == "stub_billable"
    assert resolve_mode_for_provider(provider) is LLMMode.RECORDED
    health = llm_mode_health(provider)
    assert health["mode"] == LLMMode.RECORDED.value
    assert health["model"] == "stub-priced-1"


async def test_reported_usage_wins_over_estimate():
    """provider 给了账单就用账单；给不出才估——两条路都要留下 usage_sources 痕迹。"""

    reported = _BillableStubProvider(usage={"input_tokens": 1000, "output_tokens": 100})
    guard = EpisodeCostGuard()
    await BudgetGuardedLLMProvider(reported, guard, correlation_id="a").generate_decision(_request())
    assert guard.episode("a").input_tokens == 1000
    assert guard.episode("a").usage_sources == {UsageSource.REPORTED.value: 1}

    estimated = _BillableStubProvider(usage=None)
    request = _request()
    await BudgetGuardedLLMProvider(estimated, guard, correlation_id="b").generate_decision(request)
    estimated_episode = guard.episode("b")
    request_text = canonical_json(canonical_request_payload(request))
    assert estimated_episode.usage_sources == {UsageSource.ESTIMATED.value: 1}
    assert estimated_episode.input_tokens == len(request_text.encode("utf-8"))
    assert estimated_episode.output_tokens == estimated.max_tokens
    assert estimated_episode.cost_usd > 0.0


async def test_preflight_counts_utf8_bytes_as_the_non_underestimating_input_bound(monkeypatch):
    request = _request().model_copy(update={"world_summary": "温度与照明" * 100})
    request_text = canonical_json(canonical_request_payload(request))
    inner = _BillableStubProvider(usage={"input_tokens": 100, "output_tokens": 20})
    guard = EpisodeCostGuard(budget_usd=10.0)
    captured: dict[str, int] = {}
    real_check = guard.check_affordable

    def capture_check(*args, **kwargs):
        captured["prompt_tokens"] = kwargs["prompt_tokens"]
        return real_check(*args, **kwargs)

    monkeypatch.setattr(guard, "check_affordable", capture_check)
    await BudgetGuardedLLMProvider(inner, guard, correlation_id="utf8").generate_decision(request)

    assert captured["prompt_tokens"] == len(request_text.encode("utf-8"))
    assert captured["prompt_tokens"] > estimate_tokens(request_text)


async def test_billable_provider_without_enforced_output_cap_fails_before_call():
    class UnboundedProvider(LLMProvider):
        provider_name = "unbounded_live"
        llm_mode = LLMMode.LIVE
        model = "gpt-4o-mini"
        api_key = "test-key"

        def __init__(self) -> None:
            self.calls = 0

        async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
            self.calls += 1
            return AgentLLMDecision(intent="x", confidence=1.0, explanation="x")

    inner = UnboundedProvider()
    guard = EpisodeCostGuard(budget_usd=1.0)
    provider = BudgetGuardedLLMProvider(inner, guard, correlation_id="unbounded")

    with pytest.raises(LLMProviderError) as excinfo:
        await provider.generate_decision(_request())

    assert excinfo.value.reason == "provider_error"
    assert inner.calls == 0
    assert guard.episode("unbounded").calls == 0


async def test_billed_invalid_response_is_recorded_and_failed_retry_does_not_reuse_usage():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                json={
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "output_text": json.dumps({"intent": "missing required fields"}),
                },
            )
        raise httpx.ReadTimeout("timed out", request=request)

    inner = OpenAIResponsesProvider(
        api_key="test-key",
        model="gpt-4o-mini",
        transport=httpx.MockTransport(handler),
    )
    guard = EpisodeCostGuard(budget_usd=1.0)
    provider = BudgetGuardedLLMProvider(inner, guard, correlation_id="invalid-billed")

    with pytest.raises(LLMProviderError) as first_error:
        await provider.generate_decision(_request())
    assert first_error.value.reason == "invalid_output"
    first = guard.episode("invalid-billed")
    assert (first.calls, first.input_tokens, first.output_tokens) == (1, 100, 50)
    assert first.usage_sources == {UsageSource.REPORTED.value: 1}

    request = _request()
    request_text = canonical_json(canonical_request_payload(request))
    with pytest.raises(LLMProviderError) as second_error:
        await provider.generate_decision(request)
    assert second_error.value.reason == "timeout"
    assert inner.last_usage is None

    after_retry = guard.episode("invalid-billed")
    assert after_retry.calls == 2
    assert after_retry.input_tokens == 100 + len(request_text.encode("utf-8"))
    assert after_retry.output_tokens == 50 + inner.max_output_tokens
    assert after_retry.usage_sources == {
        UsageSource.REPORTED.value: 1,
        UsageSource.ESTIMATED.value: 1,
    }


async def test_100k_explanation_is_never_returned_even_if_provider_bypasses_validation():
    class OversizedProvider(LLMProvider):
        provider_name = "oversized_live"
        llm_mode = LLMMode.LIVE
        model = "gpt-4o-mini"
        api_key = "test-key"
        max_tokens = 1200
        last_usage = {"input_tokens": 100, "output_tokens": 1200}

        async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
            self.last_usage = {"input_tokens": 100, "output_tokens": 1200}
            return AgentLLMDecision.model_construct(
                intent="malicious",
                confidence=1.0,
                task_steps=[],
                proposed_commands=[],
                explanation="x" * 100_000,
                needs_coordination=False,
            )

    guard = EpisodeCostGuard(budget_usd=1.0)
    provider = BudgetGuardedLLMProvider(
        OversizedProvider(),
        guard,
        correlation_id="oversized",
    )

    with pytest.raises(LLMProviderError) as excinfo:
        await provider.generate_decision(_request())

    assert excinfo.value.reason == "invalid_output"
    assert guard.episode("oversized").calls == 1
    assert guard.episode("oversized").output_tokens == 1200

    with pytest.raises(ValueError):
        AgentLLMDecision(
            intent="malicious",
            confidence=1.0,
            explanation="x" * (MAX_EXPLANATION_CHARS + 1),
        )


def test_request_free_text_has_a_hard_size_limit():
    payload = _request().model_dump(mode="python")
    payload["world_summary"] = "x" * (MAX_WORLD_SUMMARY_CHARS + 1)
    with pytest.raises(ValueError):
        LLMDecisionRequest.model_validate(payload)


def test_worst_case_call_cost_is_monotonic_in_output_budget():
    cheap = worst_case_call_cost_usd(model="MiniMax-M2.7", prompt_tokens=1000, max_output_tokens=200)
    dear = worst_case_call_cost_usd(model="MiniMax-M2.7", prompt_tokens=1000, max_output_tokens=2000)
    assert 0.0 < cheap < dear


# ------------------------------------------------- 4. 花费要能被工件记住


async def test_episode_cost_payload_is_serializable_for_coordination_decision_and_written_to_run_artifact(
    tmp_path,
):
    """花费要能进 coordination_decision 的 data，也要能落到 run 目录下的成本工件。"""

    inner = _BillableStubProvider(usage={"input_tokens": 20_000, "output_tokens": 1_000})
    guard = EpisodeCostGuard(budget_usd=0.05)
    provider = BudgetGuardedLLMProvider(inner, guard, correlation_id="corr-arrive-home")

    with pytest.raises(LLMProviderError):
        for _ in range(20):
            await provider.generate_decision(_request())

    payload = guard.episode_payload("corr-arrive-home")
    assert payload["correlation_id"] == "corr-arrive-home"
    assert payload["cost_usd"] > 0.0
    assert payload["budget_usd"] == pytest.approx(0.05)
    assert payload["budget_exceeded"] is True
    assert payload["calls"] >= 1
    assert payload["blocked_calls"] >= 1
    # "预算在哪一步咬下去的"必须能被读出来
    assert payload["first_blocked_agent_id"] == "lighting_agent"
    assert payload["cost_by_model"]["stub-priced-1"] > 0.0

    # 能直接塞进推理事件的 data（canonical 序列化不炸 = 可进 trace/工件）
    event = SimEvent(
        event_type="reasoning.coordination_decision",
        source="arbiter",
        timestamp=1.0,
        data={"episode_cost": payload},
    )
    assert json.loads(canonical_json(event.data))["episode_cost"]["budget_exceeded"] is True

    # run 目录下的成本工件（与 events.jsonl / llm_recordings.jsonl 同目录）
    written = guard.write_run_artifact(RUN_ID, root=tmp_path)
    assert written is not None
    assert written == Path(tmp_path) / RUN_ID / LLM_COST_FILENAME
    artifact = json.loads(written.read_text(encoding="utf-8"))
    assert artifact["budget_usd"] == pytest.approx(0.05)
    assert artifact["totals"]["cost_usd"] == pytest.approx(payload["cost_usd"])
    assert artifact["totals"]["budget_exceeded_episodes"] == 1
    assert artifact["episodes"][0]["correlation_id"] == "corr-arrive-home"
    # 价格来源要跟着工件走：事后没人能重算一份"当时用的什么价"
    assert artifact["pricing"]["default"]["source"]
    used = {entry["model"]: entry["price"] for entry in artifact["pricing"]["used"]}
    assert "stub-priced-1" in used
    # 未登记的模型按兜底价算——这件事必须写在工件里，不能只体现为一个数字
    assert used["stub-priced-1"]["is_default"] is True
    assert used["stub-priced-1"]["source"]


# ------------------------------------------------- 5. 生产接线（S3 review major-4）
#
# 上面四节测的是护栏这台机器本身。本节测的是**它真的装在了链路上**：审计原文说
# "EpisodeCostGuard / BudgetGuardedLLMProvider 零生产调用点，$0.10 预算从没被执行过，
# llm_cost.json 从没被写过"。所以这里不再自己 new 一台 guard，而是跑一条真 episode，
# 断"预算咬下去了"这件事出现在**事件流与 run 工件**里。


class _ExpensiveProvider(LLMProvider):
    """会真花钱的本地桩（零网络）：provider_name/api_key 都齐，因此不吃免检。"""

    provider_name = "openai_responses"
    llm_mode = LLMMode.LIVE

    def __init__(self) -> None:
        self.model = "gpt-4o-mini"
        self.api_key = "test-key"
        self.max_tokens = 1200
        self.calls = 0

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.calls += 1
        return AgentLLMDecision(
            intent="expensive intent",
            confidence=0.9,
            task_steps=["step"],
            proposed_commands=[],
            explanation="expensive explanation",
        )


def _cost_engine(provider: LLMProvider):
    from unittest.mock import AsyncMock

    from backend.api.ws import ConnectionManager
    from backend.engine.event_bus import EventBus
    from backend.engine.simulation import SimulationEngine
    from backend.main import _init_default_state

    engine = SimulationEngine(
        event_bus=EventBus(),
        state_manager=_init_default_state(),
        connection_manager=ConnectionManager(),
        llm_provider=provider,
    )
    engine.conn.broadcast = AsyncMock()  # type: ignore[method-assign]
    return engine


def _arrive_home_root() -> SimEvent:
    return SimEvent(
        event_type="user.arrives_home",
        source="test",
        timestamp=1.0,
        data={"user_id": "user_01", "to_room": "living_room"},
    )


async def test_runtime_attaches_the_cost_guard_so_a_real_episode_falls_back_with_budget_exceeded(
    monkeypatch, runs_root
):
    """一条真 episode：预算压到 0 之后，剩下的推理必须带 budget_exceeded 标签降级。

    这是 S3 复审 major-4 的整改验收点。断的不是"guard 类工作正常"（第 3 节已经断过），
    而是**runtime 真的把它装在了自己用的那台 provider 上**：没装的话下面这条
    ``reasoning.fallback_rule_based`` 里的 reason 永远不可能是 budget_exceeded。
    """

    monkeypatch.setenv(EPISODE_BUDGET_ENV, "0")
    provider = _ExpensiveProvider()
    engine = _cost_engine(provider)

    root = _arrive_home_root()
    await engine.event_bus.publish(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=20.0)

    history = engine.event_bus.get_history(correlation_id=root.correlation_id)
    fallbacks = [
        event
        for event in history
        if event.event_type == "reasoning.fallback_rule_based"
        and event.data.get("reason") == BUDGET_EXCEEDED_REASON
    ]
    assert fallbacks, (
        "预算用尽的 episode 里没有一条 budget_exceeded 降级事件——"
        "护栏没有装在 runtime 实际使用的 provider 上"
    )
    # 预算为 0 ⇒ 一次调用都不该真的发出去（护栏是"发之前问"，不是"花完再算"）
    assert provider.calls == 0

    guard = engine.agent_runtime.cost_guard
    episode = guard.episode(root.correlation_id)
    assert episode.blocked_calls > 0
    assert episode.budget_exceeded is True
    assert episode.first_blocked_agent_id  # "在哪一步咬下去的"

    # run 工件：llm_cost.json 与 events.jsonl 同目录
    assert engine.run_id is not None
    artifact = Path(runs_root) / engine.run_id / LLM_COST_FILENAME
    assert artifact.exists(), "llm_cost.json 没有被真实 run 写出来"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["budget_usd"] == pytest.approx(0.0)
    assert payload["totals"]["blocked_calls"] > 0
    assert payload["totals"]["budget_exceeded_episodes"] >= 1


async def test_concurrent_calls_reserve_worst_case_before_provider_await():
    """One in-flight call must consume budget capacity before peers preflight."""

    entered = asyncio.Event()
    release = asyncio.Event()
    request = _request()
    request_text = canonical_json(canonical_request_payload(request))
    provider = _BillableStubProvider(
        model="stub-priced-1",
        usage={
            "input_tokens": len(request_text.encode("utf-8")),
            "output_tokens": 1200,
        },
    )
    original_generate = provider.generate_decision

    async def blocked_generate(call_request):
        entered.set()
        await release.wait()
        return await original_generate(call_request)

    provider.generate_decision = blocked_generate  # type: ignore[method-assign]
    one_call_worst = worst_case_call_cost_usd(
        model=provider.model,
        provider=provider.provider_name,
        prompt_tokens=len(request_text.encode("utf-8")),
        max_output_tokens=provider.max_tokens,
    )
    guard = EpisodeCostGuard(budget_usd=one_call_worst * 1.5)
    first = BudgetGuardedLLMProvider(provider, guard, correlation_id="shared")
    second = BudgetGuardedLLMProvider(provider, guard, correlation_id="shared")

    first_task = asyncio.create_task(first.generate_decision(request))
    await entered.wait()
    with pytest.raises(LLMProviderError) as blocked:
        await second.generate_decision(_request("hvac_agent"))
    assert blocked.value.reason == BUDGET_EXCEEDED_REASON
    assert len(provider.calls) == 0, "first call is still awaiting the synthetic response"

    release.set()
    await first_task
    assert len(provider.calls) == 1
    episode = guard.episode("shared")
    assert episode.billable_calls == 1
    assert episode.blocked_calls == 1


async def test_concurrent_failure_cannot_charge_a_sibling_calls_reported_usage():
    """Usage telemetry must belong to the task that made the provider call.

    The order is intentional: A is in flight, B succeeds with a tiny reported
    usage, then A fails without a usage payload.  A must settle its reservation
    using the conservative maximum instead of inheriting B's report.  That
    conservative settlement must leave too little capacity for C.
    """

    a_entered = asyncio.Event()
    release_a = asyncio.Event()
    small_usage = {"input_tokens": 1, "output_tokens": 1}
    request = _request("agent-a")
    request_text = canonical_json(canonical_request_payload(request))

    class InterleavingProvider(LLMProvider):
        provider_name = "stub_billable"
        llm_mode = LLMMode.RECORDED
        model = "stub-priced-1"
        api_key = "stub"
        max_tokens = 1200

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.last_usage = None

        async def generate_decision(
            self,
            call_request: LLMDecisionRequest,
        ) -> AgentLLMDecision:
            self.calls.append(call_request.agent_id)
            self.last_usage = None
            if call_request.agent_id == "agent-a":
                a_entered.set()
                await release_a.wait()
                raise LLMProviderError("timeout", "A timed out without usage")

            self.last_usage = small_usage
            return AgentLLMDecision(
                intent="stub intent",
                confidence=0.9,
                explanation="stub explanation",
            )

    inner = InterleavingProvider()
    worst_case = worst_case_call_cost_usd(
        model=inner.model,
        provider=inner.provider_name,
        prompt_tokens=len(request_text.encode("utf-8")),
        max_output_tokens=inner.max_tokens,
    )
    small_cost = call_cost_usd(
        TokenUsage(
            input_tokens=small_usage["input_tokens"],
            output_tokens=small_usage["output_tokens"],
            source=UsageSource.REPORTED,
        ),
        model=inner.model,
        provider=inner.provider_name,
    )
    # Two worst-case reservations fit while A and B overlap.  After B settles,
    # A's conservative settlement makes C unaffordable; mischarging A with B's
    # tiny report would incorrectly admit C.
    guard = EpisodeCostGuard(budget_usd=(2 * worst_case) + (small_cost / 2))
    wrappers = {
        agent_id: BudgetGuardedLLMProvider(inner, guard, correlation_id="shared")
        for agent_id in ("agent-a", "agent-b", "agent-c")
    }

    a_task = asyncio.create_task(wrappers["agent-a"].generate_decision(request))
    await a_entered.wait()
    await wrappers["agent-b"].generate_decision(_request("agent-b"))
    release_a.set()
    with pytest.raises(LLMProviderError) as a_error:
        await a_task
    assert a_error.value.reason == "timeout"

    episode = guard.episode("shared")
    assert episode.usage_sources == {
        UsageSource.REPORTED.value: 1,
        UsageSource.ESTIMATED.value: 1,
    }
    assert episode.input_tokens == 1 + len(request_text.encode("utf-8"))
    assert episode.output_tokens == 1 + inner.max_tokens

    with pytest.raises(LLMProviderError) as c_error:
        await wrappers["agent-c"].generate_decision(_request("agent-c"))
    assert c_error.value.reason == BUDGET_EXCEEDED_REASON
    assert inner.calls == ["agent-a", "agent-b"]


async def test_a_generous_budget_lets_the_same_episode_through_and_still_books_the_cost(
    monkeypatch, runs_root
):
    """阴性对照：预算够用时不该有任何 budget_exceeded，但账仍要记。

    没有这一条，上面那条门可以被"永远降级"糊弄过去——而那等于把 LLM 关掉。
    """

    monkeypatch.setenv(EPISODE_BUDGET_ENV, "10")
    provider = _ExpensiveProvider()
    engine = _cost_engine(provider)

    root = _arrive_home_root()
    await engine.event_bus.publish(root)
    assert await engine.agent_runtime.wait_for_idle(timeout=20.0)

    history = engine.event_bus.get_history(correlation_id=root.correlation_id)
    assert not [
        event
        for event in history
        if event.data.get("reason") == BUDGET_EXCEEDED_REASON
    ]
    assert provider.calls > 0

    episode = engine.agent_runtime.cost_guard.episode(root.correlation_id)
    assert episode.blocked_calls == 0
    assert episode.billable_calls == provider.calls
    assert episode.cost_usd > 0.0

    artifact = Path(runs_root) / str(engine.run_id) / LLM_COST_FILENAME
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["totals"]["cost_usd"] > 0.0
    assert payload["totals"]["budget_exceeded_episodes"] == 0
