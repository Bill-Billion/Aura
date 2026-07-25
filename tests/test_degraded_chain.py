"""S3 阶段门（三）：拔掉 API key，整条推理链仍然完整——而且**看得出来它降级了**。

这条门对应 §15 的降级验收与 §11.1 的 mocked 模式。它盯的是两件容易此消彼长的事：

1. **不许断**：没有任何 LLM 可用时（``DisabledLLMProvider`` = 用户把 key 从
   ``backend/.env.local`` 里拿走的那一刻），同一个 arrive-home 场景仍要跑出完整的
   §4.3 六环。"没配 key 就没有推理链"意味着这个平台的可观测性依赖一张信用卡。
2. **不许装**：每一条决策事件都必须**标注自己是规则算出来的**——
   ``reasoning.fallback_rule_based`` 在场、``reasoning.execution_plan.execution_mode ==
   "fallback_rule_based"``、命令来源是 ``rule_fallback`` 而不是 ``agent``、
   ``/api/health`` 老实回答 ``llm.configured == false``。
   少了这一层，一份全程规则跑出来的实验记录会被读成"LLM 表现良好"，而这正是
   §11.1 要求每份 run 工件标注 llm_mode 的理由。

复用 :mod:`tests.test_episode_completeness` 的 :func:`assert_six_ring_complete`：
"六环齐不齐"只能有一份判据，两份迟早分叉。

跑法::

    ./backend/.venv/bin/python -m pytest tests/test_degraded_chain.py -q --timeout=60
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.agents.llm import LLMProviderError
from backend.agents.orchestrator import DEFAULT_ORCHESTRATOR_ID, ORCHESTRATOR_ENABLED_ENV
from backend.agents.runtime import AgentRuntime, DisabledLLMProvider
from backend.engine.run_manager import LLMMode
from backend.scenarios.runner import ScenarioRunResult, run_scenario
from tests.test_episode_completeness import (
    ARRIVE_HOME_SCENARIO_ID,
    assert_six_ring_complete,
    episode_events,
    root_correlation_id,
)

pytestmark = pytest.mark.anyio

# 会让 AgentRuntime._build_default_provider 挑到真 provider 的环境变量。
# 本地开发机上 backend/.env.local 里就有它们，"拔 key"这件事必须在测试里显式做到，
# 否则这条门在开发机上测的是"key 还在"的那条链。
PROVIDER_ENV_VARS = (
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_COMPAT_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
)


@pytest.fixture
def no_api_key(monkeypatch):
    """把 key 全部拔掉（= 用户从 .env.local 里删掉配置的那一刻）。"""

    monkeypatch.setenv(ORCHESTRATOR_ENABLED_ENV, "1")
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def degraded_run(no_api_key) -> ScenarioRunResult:
    """headless runner 的默认 provider 就是"一律失败"，即拔 key 后的真实姿态。"""

    return await run_scenario(ARRIVE_HOME_SCENARIO_ID)


# ------------------------------------------------- 0. "拔 key"确实变成了禁用 provider


def test_no_api_key_resolves_to_the_disabled_provider(no_api_key):
    """没有任何 key 时，runtime 挑到的必须是 DisabledLLMProvider，且自报未配置。"""

    runtime = AgentRuntime()
    assert isinstance(runtime.llm_provider, DisabledLLMProvider)
    assert runtime.is_provider_configured is False


async def test_disabled_provider_raises_instead_of_returning_a_fake_decision(no_api_key):
    """禁用 provider 必须**抛错**，不能返回一份看起来像模型输出的假决策。

    返回假决策是最坏的降级形态：链路照常走完，事件流里却没有任何"这是编的"的痕迹。
    """

    with pytest.raises(LLMProviderError):
        await DisabledLLMProvider().generate_decision(None)  # type: ignore[arg-type]


# --------------------------------------------------------- 1. 六环仍然完整


async def test_degraded_arrive_home_run_still_completes_all_six_rings(degraded_run):
    """门条款：没有 LLM，同一个 arrive-home 场景仍然产出完整六环。"""

    result = degraded_run
    correlation_id = root_correlation_id(result, "user.arrives_home")
    first_seq = assert_six_ring_complete(result.events, correlation_id)

    # 不只是"事件在"：动作真的落地了（有反馈 = 世界真的被改了）
    assert first_seq["action.device_control"] < first_seq["feedback.state_delta"]


# ----------------------------------------------- 2. 每条决策事件都标着"我降级了"


async def test_fallback_rule_based_ring_is_present(degraded_run):
    """``reasoning.fallback_rule_based`` 必须在场，并说清是**哪一步**失败才转的规则。"""

    result = degraded_run
    correlation_id = root_correlation_id(result, "user.arrives_home")
    scoped = episode_events(result.events, correlation_id)

    fallbacks = [
        event for event in scoped if event.event_type == "reasoning.fallback_rule_based"
    ]
    assert fallbacks, "拔掉 key 之后必须有一条可见的降级事件"
    for event in fallbacks:
        assert event.data["fallback_strategy"] == "rule_based"
        assert event.data["reason"], "降级必须写明原因（provider_error / timeout / …）"
        assert event.data["failed_step"], "降级必须写明是哪一步失败了"


async def test_every_decision_event_is_labelled_as_rule_based(degraded_run):
    """执行环、意图环、命令来源三处都要标注降级——只标一处等于没标。"""

    result = degraded_run
    correlation_id = root_correlation_id(result, "user.arrives_home")
    scoped = episode_events(result.events, correlation_id)

    plans = [event for event in scoped if event.event_type == "reasoning.execution_plan"]
    assert plans, "没有执行计划 = 没有决策事件可查"
    for plan in plans:
        assert plan.data["execution_mode"] == "fallback_rule_based"
        assert plan.data["fallback_reason"], "降级理由必须随执行环一起可见"
        assert plan.data["provider"] == "fallback"
        assert plan.data["model"] == "rule_based"

    # 编排层：mocked 模式压根不调 provider，因此它的置信度必须自报是规则来的。
    intents = [
        event for event in scoped if event.event_type == "reasoning.intent_recognized"
    ]
    assert len(intents) == 1
    intent = intents[0]
    assert intent.source == DEFAULT_ORCHESTRATOR_ID
    assert intent.data["llm_mode"] == LLMMode.RULE_BASED.value
    assert intent.data["confidence_source"] == "rule_based"
    assert intent.data["provider"] == "rule_based"

    # §11.1 的可审计化：confidence 不再是那个硬编码的 0.55 装饰数字。
    assert isinstance(intent.data["confidence"], float)
    assert intent.data["confidence"] != 0.55

    # 命令来源：规则回退与 LLM 决策在审计口径上必须分得开。
    sources = {
        event.data.get("source")
        for event in scoped
        if event.event_type == "command.lifecycle"
    }
    assert sources, "这条 episode 一条命令都没发"
    assert "agent" not in sources, "降级路径不得把命令记成 LLM agent 决策"
    assert "rule_fallback" in sources


async def test_degraded_run_is_reproducible(no_api_key):
    """降级路径同样必须可复现：规则算出来的链路两次运行形状一致。"""

    def shape(result: ScenarioRunResult) -> list[str]:
        correlation_id = root_correlation_id(result, "user.arrives_home")
        return [event.event_type for event in episode_events(result.events, correlation_id)]

    first = await run_scenario(ARRIVE_HOME_SCENARIO_ID)
    second = await run_scenario(ARRIVE_HOME_SCENARIO_ID)
    assert shape(first) == shape(second)


# ------------------------------------------------------------- 3. /api/health


def test_health_reports_llm_not_configured(no_api_key):
    """``/api/health`` 必须老实回答"没配 key"——面板据此显示降级横幅。"""

    from backend.main import app

    with TestClient(app) as client:
        payload = client.get("/api/health").json()

    assert payload["status"] == "ok"
    llm = payload["llm"]
    assert llm["configured"] is False
    assert llm["provider"] == "disabled"
    assert llm["model"] == "rule_based"
