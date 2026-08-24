"""§11.1 三种 LLM 模式 + 陈旧决策丢弃（S3-T7）。

本模块**不重新实现 provider**：真正会打网的两台（``OpenAIResponsesProvider`` /
``AnthropicCompatibleProvider``，含超时与非法输出降级）在 :mod:`backend.agents.llm` 里，
这里只在它们外面套三层可复现性语义。

**三种模式（§11.1；每份 run 工件必须记下用的是哪一种）**

``mocked``
    确定性罐头决策。给测试与"不需要真模型也要跑通链路"的场景用。
    同场景同 seed 跑两次，决策载荷必须逐位相同——这是本阶段门的一条，也是 S2 落地的
    字节一致性门（tests/test_replay_determinism.py）在 S3 接入 LLM 之后还能站住的前提。

``recorded``
    先用真 provider 跑一遍并把 "canonical 请求指纹 → 原始决策" 落到
    ``data/runs/{run_id}/llm_recordings.jsonl``，同时写入带请求数、成功数与内容 hash 的
    完整性 manifest，之后只从通过校验的文件确定性回放。
    **DECISION #7：只有 recorded 能用于 benchmark 声明**，live 只用于产品验证——
    因为 live 的结果依赖"当天那个模型"，不可被第三方复现。

``live``
    直接用既有 provider。

**回放为什么必须"未命中就报错"**：回放最经典的塌法不是打不开文件，而是
canonicalization 有缺口 → 键对不上 → 悄悄返回了另一条录制，于是研究者拿到一份看起来
正常、其实张冠李戴的轨迹。所以 :class:`ReplayLLMProvider` 在未命中时抛
``LLMProviderError("recording_miss")``。运行时仍可执行规则 fallback 保住设备安全，但会
同步把 run 工件标为 invalid；评估、稳定导出与后续 source admission 都 fail closed，不能把
这份混合策略结果继续声称为可复现 recorded baseline。

**陈旧决策丢弃（evolution-review 风险 #2）**：LLM 一轮要 1-5 秒，世界不会停下来等它。
决策带上它据以推理的世界版本（:class:`VersionedDecision`），落地前比一次
（:func:`check_stale_decision`）：**只比它自己要碰的那几台设备**的版本，不比全局计数器
——环境每 tick 都在动，用全局版本判陈旧等于把所有决策都判死。
命中就丢弃并发 ``reasoning.decision_discarded``（reason=stale），零状态改动。

跨阶段契约（S3-T3/T8/T9、S4 回放、S5 面板都消费这些名字）::

    LLM_MODE=mocked|recorded|live        模式选择环境变量（缺省：pytest 下 mocked，否则 live）
    LLM_RECORDINGS_PATH=<file>           回放一份既有 run 的录制（不给则用当前 run 目录）
    data/runs/{run_id}/llm_recordings.jsonl   录制工件（一行一条 LLMRecording）
    reasoning.decision_discarded         陈旧丢弃事件，data.reason == "stale"

生产接线点（S3 review major-2/3/4 的整改落点，别再让本模块变回一座图书馆）::

    AgentRuntime._resolve_llm_provider   LLM_MODE → build_provider_for_mode / RunScopedRecordedProvider
    AgentRuntime._episode_llm_provider   每条 episode 现套一层 BudgetGuardedLLMProvider
    AgentRuntime._discard_stale_decisions  落地前 check_stale_decision → reasoning.decision_discarded
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.agents.contracts import ProposalAssumption
from backend.agents.llm import (
    LLMProvider,
    LLMProviderError,
    build_compact_request_payload,
)
from backend.agents.llm_pricing import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    LLM_COST_FILENAME,
    COST_DIGITS,
    ModelPrice,
    PricingTable,
    TokenUsage,
    UsageSource,
    active_pricing_table,
    estimate_usage,
    parse_usage,
    worst_case_call_cost_usd,
)
from backend.agents.types import AgentCommandProposal, AgentLLMDecision, LLMDecisionRequest
from backend.core.logging import log
from backend.engine.event_bus import SimEvent
from backend.engine.event_log import (
    LLM_RECORDINGS_FILENAME,
    LLM_RECORDINGS_MANIFEST_FILENAME,
    artifacts_enabled,
    run_dir,
)
from backend.engine.run_manager import LLMMode, canonical_json, resolve_llm_mode
from backend.engine.state import WorldState
from backend.engine.state_manager import DeltaChange, StateManager

__all__ = [
    "ALLOW_LIVE_LLM_ENV",
    "LLM_MODE_ENV",
    "LLM_MODE_VALUES",
    "LLMMode",
    "CANONICAL_FLOAT_DIGITS",
    "RECORDING_SCHEMA",
    "RECORDING_MISS_REASON",
    "RECORDING_CORRUPT_REASON",
    "RECORDING_UNAVAILABLE_REASON",
    "RECORDING_WRITE_REASON",
    "RECORDING_MANIFEST_SCHEMA",
    "RECORDINGS_PATH_ENV",
    "MOCK_FIXTURE_MISS_REASON",
    "STALE_DECISION_EVENT_TYPE",
    "STALE_DECISION_REASON",
    "INVALIDATED_ASSUMPTION_REASON",
    "BUDGET_EXCEEDED_REASON",
    "EPISODE_BUDGET_ENV",
    "DEFAULT_EPISODE_BUDGET_USD",
    "FREE_PROVIDER_NAMES",
    "resolve_episode_budget_usd",
    "EpisodeCost",
    "EpisodeCostGuard",
    "BudgetGuardedLLMProvider",
    "canonical_request_payload",
    "request_key",
    "resolve_llm_mode_from_env",
    "resolve_mode_for_provider",
    "llm_mode_health",
    "recordings_path",
    "LLMRecording",
    "LLMRecordingManifest",
    "recording_manifest_path",
    "validate_recording_artifact",
    "load_recordings",
    "default_mock_decision",
    "MockedLLMProvider",
    "RuleBasedLLMProvider",
    "RecordingLLMProvider",
    "ReplayLLMProvider",
    "RunScopedRecordedProvider",
    "build_provider_for_mode",
    "resolve_recordings_path_override",
    "WorldVersionTracker",
    "VersionedDecision",
    "StaleDecisionCheck",
    "check_stale_decision",
    "build_stale_decision_event",
]


# ---------------------------------------------------------------------------
# 常量（跨阶段契约）
# ---------------------------------------------------------------------------

LLM_MODE_ENV = "LLM_MODE"
LLM_MODE_VALUES: tuple[str, ...] = tuple(mode.value for mode in LLMMode)

# 所有进程都必须显式设置这个变量，才能搭一台会打真网的 provider。
# API key 存在不等于授权消费；缺省永远回到 disabled → rule fallback。
ALLOW_LIVE_LLM_ENV = "AURA_ALLOW_LIVE_LLM"
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})

# 测试进程里

# recorded 模式的录制文件位置覆盖。缺省是 ``data/runs/{当前 run_id}/llm_recordings.jsonl``，
# 而每个 run 的 id 都是新的 —— 也就是说"缺省行为恒等于录制，永远回放不了"。研究者复现
# 一份既有 run 的方法因此是把这个变量指到那一份录制上：
#
#     LLM_MODE=recorded LLM_RECORDINGS_PATH=data/runs/<run>/llm_recordings.jsonl
#
# 指到已存在的文件 → 回放（零网络）；指到不存在的路径 → 录制到那里。
RECORDINGS_PATH_ENV = "LLM_RECORDINGS_PATH"
RECORDING_WRITE_REASON = "recording_write_failed"
RECORDING_MANIFEST_SCHEMA = "aura.llm-recording-manifest/1"

# recorded 模式拿不到 run_id（runtime 还没绑 RunManager，且没给 LLM_RECORDINGS_PATH）时
# 的降级标签。刻意不静默改用 live：那等于研究者以为自己在录，实际什么都没留下。
RECORDING_UNAVAILABLE_REASON = "recording_unavailable"

RECORDING_SCHEMA = "aura.llm_recording.v1"
RECORDING_MISS_REASON = "recording_miss"
RECORDING_CORRUPT_REASON = "recording_corrupt"
MOCK_FIXTURE_MISS_REASON = "mock_fixture_miss"

STALE_DECISION_EVENT_TYPE = "reasoning.decision_discarded"
STALE_DECISION_REASON = "stale"
INVALIDATED_ASSUMPTION_REASON = "invalidated_assumption"

# canonical 化时浮点保留的位数。这一位数是"回放命中率 / 语义保真"的取舍点：
# 3 位刚好抹掉 0.30000000000000004 这类二进制尾巴（真正的不确定性来源），
# 又不会把 21.312 与 21.318 这种语义上不同的世界读数混为一谈。
CANONICAL_FLOAT_DIGITS = 3

# 字符串里嵌的浮点也要抹尾巴：world_summary 是拼好的一句话（"temp=21.3, light_level=0.4"），
# 它的数字不会经过 JSON 的 float 通道，纯靠 sort_keys 是治不了的。
_FLOAT_TOKEN_RE = re.compile(r"-?\d+\.\d+")

_DEVICE_PATH_RE = re.compile(r"^devices\[([^\]]+)\]")


# ---------------------------------------------------------------------------
# 请求 canonical 化
# ---------------------------------------------------------------------------


def _canonicalize(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        rounded = round(value, CANONICAL_FLOAT_DIGITS)
        # -0.0 与 0.0 序列化不同，统一掉
        return rounded + 0.0
    if isinstance(value, str):
        return _FLOAT_TOKEN_RE.sub(_round_float_token, value)
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        # 顺序是语义的一部分（allowed_commands 的顺序决定 agent 看到的可选项顺序），不排序。
        return [_canonicalize(item) for item in value]
    return value


def _round_float_token(match: re.Match[str]) -> str:
    rounded = round(float(match.group(0)), CANONICAL_FLOAT_DIGITS)
    return format(rounded + 0.0, "f").rstrip("0").rstrip(".") or "0"


def canonical_request_payload(request: LLMDecisionRequest) -> dict[str, Any]:
    """把 :func:`build_compact_request_payload` 的产物确定化。

    确定化 = 键排序 + 浮点定位（含字符串里嵌的浮点）。审计里已知的两处不确定性
    （world summary 浮点尾巴、dict 迭代序）正是这里要中和掉的东西——它们不中和，
    录制回放会在"看起来一样的世界"上系统性未命中。
    """

    return _canonicalize(build_compact_request_payload(request))


def request_key(request: LLMDecisionRequest) -> str:
    """canonical 请求的 sha256。既是录制的查找键，也是回放的 prompt 指纹。"""

    payload = canonical_request_payload(request)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 模式选择
# ---------------------------------------------------------------------------


def running_under_test(env: Mapping[str, str] | None = None) -> bool:
    """本进程是不是 pytest 进程。

    只此一处定义"在测试里"，模式缺省（:func:`resolve_llm_mode_from_env`）与凭证闸门
    （:func:`live_llm_allowed`）共用它——两处各判一次的话，总有一天只有一处被改对。
    """

    environ = os.environ if env is None else env
    return "PYTEST_CURRENT_TEST" in environ or "PYTEST_VERSION" in environ


def live_llm_allowed(env: Mapping[str, str] | None = None) -> bool:
    """服务端是否显式授权构造一台**会打真网**的 provider。

    S3 review-2 blocker：仓库根的 ``.env.local`` 带着真 key，``backend.main`` 在 import
    期就 :func:`~backend.core.local_env.load_local_env` 把它灌进 ``os.environ``，于是
    :meth:`~backend.agents.runtime.AgentRuntime._build_default_provider` 在 pytest 里
    也照样搭出一台真 provider——实测一趟全量套件 40 次真 HTTPS POST。这不只是"花钱、
    慢、看网络脸色"：**本机跑的和 CI 跑的是两条不同的代码路径**（CI 没有 .env.local，
    走的是无 key → :class:`DisabledLLMProvider` → 规则回退那条），本机的绿灯因此
    根本不是 CI 验证的那盏灯。

    所以缺省闸门在所有环境都关闭。API key 只表示凭证存在，不表示进程所有者同意消费；
    真要跑 live/首次 recorded 录制必须显式设 ``AURA_ALLOW_LIVE_LLM=1``。测试仍由
    tests/conftest.py 的传输层哨兵做第二层防线。

    刻意**不**在这里洗 ``os.environ``：清 key 是打地鼠（provider 一多就漏一个），而这
    条闸门管的是唯一的出口——``_build_default_provider`` 是全仓库唯一按环境变量造
    provider 的地方。兜底那一层由 tests/conftest.py 的传输层哨兵负责。
    """

    environ = os.environ if env is None else env
    return str(environ.get(ALLOW_LIVE_LLM_ENV, "")).strip().lower() in _TRUTHY


def resolve_llm_mode_from_env(
    env: Mapping[str, str] | None = None,
    *,
    under_test: bool | None = None,
) -> LLMMode:
    """读 ``LLM_MODE``；缺省值按"是不是在测试里"分岔。

    缺省值刻意不对称：测试进程默认 ``mocked``（测试里绝不能因为本机 .env.local 配了 key
    就意外打真网），开发/生产进程默认 ``live``。``under_test`` 显式传值可覆盖判定。
    """

    environ = os.environ if env is None else env
    raw = str(environ.get(LLM_MODE_ENV, "")).strip().lower()
    if raw:
        try:
            return LLMMode(raw)
        except ValueError as exc:
            raise ValueError(
                f"{LLM_MODE_ENV}={raw!r} 不是合法模式，可选：{', '.join(LLM_MODE_VALUES)}"
            ) from exc

    if under_test is None:
        under_test = running_under_test(environ)
    return LLMMode.MOCKED if under_test else LLMMode.LIVE


def resolve_mode_for_provider(provider: Any) -> LLMMode:
    """由 provider 实例反推它实际处在哪种模式（写进 run 元数据用）。

    只有本模块的三层包装会显式声明 ``llm_mode``；裸 provider 仍交给 S2 的
    :func:`backend.engine.run_manager.resolve_llm_mode` 判（它还要看 api_key 在不在，
    "配了 provider 名但没 key"实际上跑的是规则回退，不是 live）。
    """

    declared = getattr(provider, "llm_mode", None)
    if isinstance(declared, LLMMode):
        if declared is not LLMMode.LIVE:
            return declared
    elif isinstance(declared, str) and declared in LLM_MODE_VALUES:
        if declared != LLMMode.LIVE.value:
            return LLMMode(declared)
    return resolve_llm_mode(provider)


def llm_mode_health(provider: Any) -> dict[str, Any]:
    """``/api/health`` 的 ``_llm_health`` 片段（S3-T3 接线时并进去）。"""

    mode = resolve_mode_for_provider(provider)
    payload: dict[str, Any] = {
        "mode": mode.value,
        "provider": str(getattr(provider, "provider_name", "disabled") or "disabled"),
        "model": str(getattr(provider, "model", "") or "rule_based"),
        "benchmark_safe": mode is not LLMMode.LIVE,  # DECISION #7
    }
    recordings = getattr(provider, "recordings_path", None)
    if recordings is not None:
        payload["recordings_path"] = str(recordings)
    return payload


def recordings_path(run_id: str, *, root: Path | str | None = None) -> Path:
    """``data/runs/{run_id}/llm_recordings.jsonl``——与 events.jsonl 同目录。"""

    return run_dir(run_id, root=root) / LLM_RECORDINGS_FILENAME


# ---------------------------------------------------------------------------
# 录制条目
# ---------------------------------------------------------------------------


class LLMRecording(BaseModel):
    """一条 LLM 决策录制（jsonl 一行一条）。

    同时存 ``request_key`` 与整份 canonical ``request``：前者是查找键，后者让
    "回放时对不上"这件事可诊断——:func:`load_recordings` 会用 request 重算一遍键，
    对不上就判工件损坏，而不是把一条张冠李戴的录制放出去。
    """

    # protected_namespaces=()：本模型有一个正当的 ``model`` 字段（哪台模型说的话），
    # 与 pydantic 默认保留的 ``model_`` 前缀无关。
    model_config = ConfigDict(extra="forbid", populate_by_name=True, protected_namespaces=())

    schema_version: str = Field(default=RECORDING_SCHEMA, alias="schema")
    request_key: str
    prompt_hash: str
    agent_id: str = ""
    root_event_type: str = ""
    provider: str = ""
    model: str = ""
    request: dict[str, Any] = Field(default_factory=dict)
    decision: AgentLLMDecision

    @classmethod
    def decision_from_payload(cls, payload: Mapping[str, Any]) -> AgentLLMDecision:
        """原始 JSON → AgentLLMDecision（复用 llm.py 的归一化，不另立一套解析）。"""

        from backend.agents.llm import _normalize_agent_decision_payload  # noqa: PLC0415

        return AgentLLMDecision.model_validate(_normalize_agent_decision_payload(dict(payload)))

    def to_json_dict(self) -> dict[str, Any]:
        payload = self.model_dump(by_alias=True, mode="json")
        return payload

    def to_json_line(self) -> str:
        return json.dumps(self.to_json_dict(), ensure_ascii=False, sort_keys=True)


class LLMRecordingManifest(BaseModel):
    """Proof that every attempted live decision was durably recorded."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(default=RECORDING_MANIFEST_SCHEMA, alias="schema")
    requested: int = Field(ge=0)
    recorded: int = Field(ge=0)
    failed: int = Field(ge=0)
    complete: bool
    recording_sha256: str
    last_error: str | None = None


def recording_manifest_path(path: Path | str) -> Path:
    return Path(path).with_name(LLM_RECORDINGS_MANIFEST_FILENAME)


def validate_recording_artifact(path: Path | str) -> LLMRecordingManifest:
    """Validate manifest counts, digest, and every JSONL recording entry."""

    recording_path = Path(path)
    manifest_path = recording_manifest_path(recording_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = LLMRecordingManifest.model_validate(payload)
        raw = recording_path.read_bytes()
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise LLMProviderError(
            RECORDING_CORRUPT_REASON,
            f"录制 manifest 不存在或损坏：{manifest_path}: {exc}",
        ) from exc

    lines = [line for line in raw.splitlines() if line.strip()]
    digest = hashlib.sha256(raw).hexdigest()
    if (
        not manifest.complete
        or manifest.requested <= 0
        or manifest.failed != 0
        or manifest.recorded != manifest.requested
        or manifest.recorded != len(lines)
        or manifest.recording_sha256 != digest
    ):
        raise LLMProviderError(
            RECORDING_CORRUPT_REASON,
            "录制工件不完整：请求数、成功数、行数或内容摘要不一致",
        )
    load_recordings(recording_path)
    return manifest


def load_recordings(path: Path | str) -> dict[str, LLMRecording]:
    """读一份 jsonl 录制，返回 ``{request_key: LLMRecording}``。

    回放契约是"canonical 请求 → 决策"的纯函数。同键同决策的重复 occurrence 因而
    可以安全折叠；同键不同决策则没有唯一可复现答案，必须把整份工件判为损坏，而不是
    静默保留第一条并让 A/A 回放偏离原始 run。
    """

    file_path = Path(path)
    if not file_path.exists():
        raise LLMProviderError(
            RECORDING_MISS_REASON,
            f"LLM 录制文件不存在：{file_path}",
        )

    recordings: dict[str, LLMRecording] = {}
    for line_no, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            record = LLMRecording.model_validate(payload)
        except Exception as exc:  # json / pydantic 都归为"工件坏了"
            raise LLMProviderError(
                RECORDING_CORRUPT_REASON,
                f"{file_path}:{line_no} 不是一条合法录制：{exc}",
            ) from exc

        recomputed = hashlib.sha256(
            canonical_json(_canonicalize(record.request)).encode("utf-8")
        ).hexdigest()
        if record.request and recomputed != record.request_key:
            # 录制被改过 / 是别的 canonicalizer 写的：宁可整份拒绝，也不放一条
            # 键与内容对不上的录制进回放——那正是"悄悄返回错录制"的入口。
            raise LLMProviderError(
                RECORDING_CORRUPT_REASON,
                f"{file_path}:{line_no} 的 request 与 request_key 不符"
                f"（记录 {record.request_key[:12]}…，实算 {recomputed[:12]}…）",
            )

        existing = recordings.get(record.request_key)
        if existing is None:
            recordings[record.request_key] = record
        elif existing.decision != record.decision:
            raise LLMProviderError(
                RECORDING_CORRUPT_REASON,
                f"{file_path}:{line_no} 的 request_key {record.request_key[:12]}… "
                "对应多个不同决策，无法确定性回放",
            )
    return recordings


# ---------------------------------------------------------------------------
# 三种模式
# ---------------------------------------------------------------------------


def default_mock_decision(request: LLMDecisionRequest) -> AgentLLMDecision:
    """无 fixture 时的确定性罐头决策：不提任何命令。

    刻意"什么都不做"而不是瞎编几条命令：mocked 模式的用途是让链路可复现地跑通，
    编出来的命令会把 S4 的评估指标污染成"模型表现"。需要真实决策的评估跑法请喂
    fixture，或者直接用 :class:`ReplayLLMProvider`（录制即最好的 mock）。
    """

    return AgentLLMDecision(
        intent=f"mocked: no action for {request.root_event_type}",
        confidence=0.5,
        task_steps=["review current context"],
        proposed_commands=[],
        explanation="Deterministic mocked decision (no fixture matched).",
        needs_coordination=False,
    )


class RuleBasedLLMProvider(LLMProvider):
    """显式的无 LLM provider；调用即进入既有规则回退链。"""

    provider_name = "disabled"
    model = "rule_based"
    llm_mode = LLMMode.RULE_BASED

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        raise LLMProviderError("provider_error", "LLM provider is disabled")


class MockedLLMProvider(LLMProvider):
    """确定性罐头 provider（§11.1 mocked）。

    三级查找：先按 canonical 请求指纹（``fixtures``），再按 agent_id
    （``fixtures_by_agent``，写测试时最顺手），最后落到 ``default_factory``。
    ``strict=True`` 时不落默认值，直接抛 ``mock_fixture_miss``——评估跑法应该开着它，
    "没配到 fixture"必须是一次失败，而不是一条静悄悄的空决策。
    """

    provider_name = "mocked"
    llm_mode = LLMMode.MOCKED

    def __init__(
        self,
        fixtures: Mapping[str, AgentLLMDecision] | None = None,
        *,
        fixtures_by_agent: Mapping[str, AgentLLMDecision] | None = None,
        default_factory: Callable[[LLMDecisionRequest], AgentLLMDecision] | None = None,
        strict: bool = False,
        model: str = "mocked",
    ) -> None:
        self.fixtures = dict(fixtures or {})
        self.fixtures_by_agent = dict(fixtures_by_agent or {})
        self.default_factory = default_factory or default_mock_decision
        self.strict = strict
        self.model = model
        self.calls: list[str] = []

    @classmethod
    def from_recordings(cls, path: Path | str, **kwargs: Any) -> "MockedLLMProvider":
        """把一份录制当 fixture 用（录制即最真实的 mock）。"""

        records = load_recordings(path)
        return cls({key: record.decision for key, record in records.items()}, **kwargs)

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        key = request_key(request)
        self.calls.append(key)

        fixture = self.fixtures.get(key) or self.fixtures_by_agent.get(request.agent_id)
        if fixture is not None:
            # 深拷贝：调用方拿到的决策不能是 fixture 本体，否则一次原地修改会污染后续调用，
            # "两次跑出同一结果"就成了假的。
            return fixture.model_copy(deep=True)

        if self.strict:
            raise LLMProviderError(
                MOCK_FIXTURE_MISS_REASON,
                f"mocked 模式没有匹配 fixture：agent={request.agent_id} key={key[:12]}…",
            )
        return self.default_factory(request)


class RecordingLLMProvider(LLMProvider):
    """真 provider 的录制包装（§11.1 recorded 的写侧）。

    只在真 provider **成功**时落一条记录：失败路径已有自己的可观测出口
    （fallback 事件），把失败也录进来会让回放悄悄复现出一堆降级链。
    """

    provider_name = "recording"
    llm_mode = LLMMode.RECORDED

    def __init__(
        self,
        inner: LLMProvider,
        *,
        path: Path | str,
        integrity_error_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.inner = inner
        self.recordings_path = Path(path)
        self.integrity_error_handler = integrity_error_handler
        self.requested = 0
        self.written = 0
        self.failed = 0
        self.last_error: str | None = None

    # 元数据穿透：run 元数据要记的是"真正说话的那台模型"，不是包装层。
    @property
    def model(self) -> str:  # type: ignore[override]
        return str(getattr(self.inner, "model", "") or "")

    @property
    def api_key(self) -> Any:
        return getattr(self.inner, "api_key", None)

    @property
    def timeout_ms(self) -> Any:
        return getattr(self.inner, "timeout_ms", None)

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        self.requested += 1
        try:
            # Mark the artifact incomplete before making a live request.  If the
            # process or disk fails afterwards, this source can never be accepted
            # as a complete replay corpus.
            self._write_manifest()
        except LLMProviderError:
            self.failed += 1
            self._mark_integrity_error("recording manifest could not be initialized")
            raise
        try:
            decision = await self.inner.generate_decision(request)
            self._append(request, decision)
            self.written += 1
            self._write_manifest()
        except asyncio.CancelledError as exc:
            self.failed += 1
            cancellation_error = str(exc).strip() or "recording request cancelled"
            self.last_error = cancellation_error
            # Cancellation is a failed capture, not an invisible control-flow
            # detail: the live request may already have reached the provider, but
            # no durable decision was recorded.  Cleanup is deliberately best
            # effort so a callback/disk failure cannot replace the original
            # CancelledError observed by the caller.
            try:
                self._mark_integrity_error(cancellation_error)
            except Exception as cleanup_exc:
                log.error(
                    "llm_recording_cancel_integrity_handler_failed",
                    path=str(self.recordings_path),
                    error=str(cleanup_exc),
                )
            try:
                self._write_manifest()
            except Exception as cleanup_exc:
                log.error(
                    "llm_recording_cancel_manifest_write_failed",
                    path=str(self.recordings_path),
                    error=str(cleanup_exc),
                )
            raise
        except Exception as exc:
            self.failed += 1
            self.last_error = str(getattr(exc, "reason", "") or exc)
            # A recorded baseline is reproducible only when every requested
            # decision was captured.  A normal provider failure is just as
            # disqualifying as a disk failure: rule fallback may keep the world
            # safe, but the resulting mixed-policy run must not be evaluated or
            # exported as a complete recording.
            self._mark_integrity_error(self.last_error)
            try:
                self._write_manifest()
            except LLMProviderError:
                pass
            raise
        return decision

    def _mark_integrity_error(self, message: str) -> None:
        self.last_error = message
        if self.integrity_error_handler is not None:
            self.integrity_error_handler(message)

    def _append(self, request: LLMDecisionRequest, decision: AgentLLMDecision) -> None:
        payload = canonical_request_payload(request)
        key = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        record = LLMRecording(
            request_key=key,
            prompt_hash=key,
            agent_id=request.agent_id,
            root_event_type=request.root_event_type,
            provider=str(getattr(self.inner, "provider_name", "unknown")),
            model=self.model,
            request=payload,
            decision=decision,
        )
        try:
            self.recordings_path.parent.mkdir(parents=True, exist_ok=True)
            with self.recordings_path.open("a", encoding="utf-8") as handle:
                handle.write(record.to_json_line() + "\n")
                handle.flush()
        except OSError as exc:
            # 录不下来就说话：一份缺行的录制会让 S4 的回放静默变成"部分 live"。
            log.error(
                "llm_recording_write_failed",
                path=str(self.recordings_path),
                error=str(exc),
            )
            raise LLMProviderError(
                RECORDING_WRITE_REASON,
                f"LLM 决策成功但录制写入失败：{self.recordings_path}: {exc}",
            ) from exc

    def _write_manifest(self) -> None:
        manifest_path = recording_manifest_path(self.recordings_path)
        temp_path = manifest_path.with_name(f".{manifest_path.name}.tmp")
        try:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            raw = self.recordings_path.read_bytes() if self.recordings_path.is_file() else b""
            manifest = LLMRecordingManifest(
                requested=self.requested,
                recorded=self.written,
                failed=self.failed,
                complete=(
                    self.requested > 0
                    and self.requested == self.written
                    and self.failed == 0
                ),
                recording_sha256=hashlib.sha256(raw).hexdigest(),
                last_error=self.last_error,
            )
            temp_path.write_text(
                json.dumps(
                    manifest.model_dump(mode="json", by_alias=True),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            temp_path.replace(manifest_path)
        except OSError as exc:
            log.error(
                "llm_recording_manifest_write_failed",
                path=str(manifest_path),
                error=str(exc),
            )
            raise LLMProviderError(
                RECORDING_WRITE_REASON,
                f"无法持久化 LLM 录制完整性 manifest：{manifest_path}: {exc}",
            ) from exc


class ReplayLLMProvider(LLMProvider):
    """从录制文件确定性回放（§11.1 recorded 的读侧）。

    构造上就不持有任何 HTTP 客户端——"回放不会打网"是结构性质，不是纪律。
    未命中抛 ``recording_miss``，由既有 fallback 路径带标签降级。
    """

    provider_name = "replay"
    llm_mode = LLMMode.RECORDED

    def __init__(
        self,
        recordings: Mapping[str, LLMRecording],
        *,
        path: Path | str | None = None,
        model: str = "",
    ) -> None:
        self.recordings = dict(recordings)
        self.recordings_path = Path(path) if path is not None else None
        self.hits = 0
        self.misses = 0
        self.model = model or self._infer_model()

    @classmethod
    def from_file(cls, path: Path | str) -> "ReplayLLMProvider":
        return cls(load_recordings(path), path=path)

    def _infer_model(self) -> str:
        models = sorted({record.model for record in self.recordings.values() if record.model})
        return models[0] if len(models) == 1 else ",".join(models)

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        key = request_key(request)
        record = self.recordings.get(key)
        if record is None:
            self.misses += 1
            raise LLMProviderError(
                RECORDING_MISS_REASON,
                (
                    f"录制里没有这条请求：agent={request.agent_id} "
                    f"event={request.root_event_type} key={key[:12]}…"
                ),
            )
        self.hits += 1
        return record.decision.model_copy(deep=True)


def build_provider_for_mode(
    mode: LLMMode | str,
    *,
    live_provider_factory: Callable[[], LLMProvider],
    recordings_path: Path | str | None = None,
    fixtures: Mapping[str, AgentLLMDecision] | None = None,
    strict_mock: bool = True,
) -> LLMProvider:
    """按模式装配 provider（S3-T3 的接线口）。

    ``recorded`` 的读写分岔靠"文件在不在"：不存在就录（包住 live provider），
    已存在就回放。研究者的操作因此是"跑一次留下 run 目录，之后拿同一个目录复现"，
    不必再记第四个开关。
    """

    resolved = LLMMode(mode)
    if resolved is LLMMode.RULE_BASED:
        # ``rule_based`` 过去落到函数末尾，误用了 live factory；这会让显式无 LLM
        # baseline 在有服务端凭证时悄悄变成真实网络调用。
        return RuleBasedLLMProvider()
    if resolved is LLMMode.MOCKED:
        return MockedLLMProvider(fixtures, strict=strict_mock)

    if resolved is LLMMode.RECORDED:
        if recordings_path is None:
            raise ValueError("recorded 模式必须给 recordings_path（data/runs/{run_id}/llm_recordings.jsonl）")
        path = Path(recordings_path)
        if path.exists():
            return ReplayLLMProvider.from_file(path)
        return RecordingLLMProvider(live_provider_factory(), path=path)

    return live_provider_factory()


def resolve_recordings_path_override(env: Mapping[str, str] | None = None) -> Path | None:
    """读 ``LLM_RECORDINGS_PATH``（没配就 None）。"""

    environ = os.environ if env is None else env
    raw = str(environ.get(RECORDINGS_PATH_ENV, "")).strip()
    return Path(raw) if raw else None


class RunScopedRecordedProvider(LLMProvider):
    """recorded 模式的生产接线口：按**当前 run_id** 惰性解析录制/回放。

    为什么必须惰性：录制文件的位置是 ``data/runs/{run_id}/llm_recordings.jsonl``，而
    :class:`~backend.agents.runtime.AgentRuntime` 在 :class:`RunManager` **之前**就构造好了
    ——构造那一刻还没有 run_id。而且 ``reset`` / 场景连跑会换 run，一个 run 一份录制。
    所以这里不预先绑路径，而是每次用到时问一次 run_id，并按 run 缓存内层 provider
    （缓存是必须的：``RecordingLLMProvider`` 以追加方式写文件，每次调用新建一台会让
    ``written`` 计数与"这份录制是谁写的"都失去意义）。

    ``LLM_RECORDINGS_PATH`` 覆盖优先于 run 目录——它就是"回放一份既有 run"的开关
    （新 run 的 run_id 恒不同，不给覆盖就永远只会录、不会放）。

    拿不到 run_id 又没给覆盖时**不悄悄改用 live**：抛
    ``LLMProviderError(recording_unavailable)``，由既有 fallback 路径带标签降级。
    静默改用 live 等于"以为在录，其实什么都没留下"，而这正是 DECISION #7 要防的那件事。
    """

    llm_mode = LLMMode.RECORDED

    def __init__(
        self,
        *,
        live_provider_factory: Callable[[], LLMProvider],
        run_id_source: Callable[[], str | None] | None = None,
        recordings_root: Path | str | None = None,
        path_override: Path | str | None = None,
        allow_env_override: bool = True,
        integrity_error_handler: Callable[[str], None] | None = None,
    ) -> None:
        self._live_provider_factory = live_provider_factory
        self._run_id_source = run_id_source
        self._recordings_root = recordings_root
        self._path_override = Path(path_override) if path_override is not None else None
        self._allow_env_override = bool(allow_env_override)
        self._integrity_error_handler = integrity_error_handler
        self._bound_run_id: str | None = None
        self._resolved: dict[str, LLMProvider] = {}

    # --- 解析 ---------------------------------------------------------------

    def _target_path(self) -> tuple[str, Path] | None:
        override = self._path_override
        if override is None and self._allow_env_override:
            override = resolve_recordings_path_override()
        if override is not None:
            return (f"override:{override}", override)
        run_id = self._bound_run_id
        if run_id is None and self._run_id_source is not None:
            run_id = self._run_id_source()
        if not run_id:
            return None
        return (run_id, recordings_path(run_id, root=self._recordings_root))

    def bind_run(self, run_id: str) -> None:
        """在 run 元数据读取 provider 属性前，先钉住新 run 的录制目录。

        reset 期间 ``run_id_source`` 仍指向旧 run；若依赖它，provider_name/model 的
        属性访问会提前解析旧目录，随后新 run 也继续写进旧工件。
        """

        self._bound_run_id = run_id

    def resolve(self) -> LLMProvider | None:
        """当前 run 对应的内层 provider（录制或回放）；拿不到位置时 None。"""

        target = self._target_path()
        if target is None:
            return None
        key, path = target
        existing = self._resolved.get(key)
        if existing is not None:
            return existing
        provider = build_provider_for_mode(
            LLMMode.RECORDED,
            live_provider_factory=self._live_provider_factory,
            recordings_path=path,
        )
        if isinstance(provider, RecordingLLMProvider):
            provider.integrity_error_handler = self._integrity_error_handler
        log.info(
            "llm_recorded_provider_resolved",
            run_key=key,
            path=str(path),
            role=str(getattr(provider, "provider_name", "")),
        )
        self._resolved[key] = provider
        return provider

    @property
    def resolved_providers(self) -> dict[str, LLMProvider]:
        """已解析的内层 provider（按 run 键）。测试与诊断读它，不改它。"""

        return dict(self._resolved)

    # --- 元数据穿透 ----------------------------------------------------------

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        inner = self.resolve()
        return str(getattr(inner, "provider_name", "recorded") or "recorded")

    @property
    def model(self) -> str:  # type: ignore[override]
        inner = self.resolve()
        return str(getattr(inner, "model", "") or "")

    @property
    def api_key(self) -> Any:
        inner = self.resolve()
        return getattr(inner, "api_key", None)

    @property
    def timeout_ms(self) -> Any:
        inner = self.resolve()
        return getattr(inner, "timeout_ms", None)

    @property
    def recordings_path(self) -> Any:
        inner = self.resolve()
        return getattr(inner, "recordings_path", None)

    # --- 调用 ---------------------------------------------------------------

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        inner = self.resolve()
        if inner is None:
            raise LLMProviderError(
                RECORDING_UNAVAILABLE_REASON,
                (
                    f"{LLM_MODE_ENV}=recorded 但既没有活跃 run_id 也没有 {RECORDINGS_PATH_ENV}，"
                    "录制无处可落"
                ),
            )
        return await inner.generate_decision(request)


# ---------------------------------------------------------------------------
# 世界版本 + 陈旧决策丢弃
# ---------------------------------------------------------------------------


class WorldVersionTracker:
    """世界改动的单调计数器：一个全局版本 + 一张 per-device 版本表。

    **不进 WorldState**（plan_raw 风险条）：版本簿记是运行期的因果台账，塞进世界快照会
    让每条 STATE_FULL 都胖一圈，还会污染 S2 的 initial_state_hash。

    接线方式是包住 ``StateManager.apply_path_update``——那是状态层唯一的写入口
    （S1 已经把命令侧收敛到 CommandExecutor，仿真器与场景加载也都从这里过），
    因此"有改动却没记版本"这件事在结构上不可能发生。
    """

    _ATTR = "_aura_world_version_tracker"

    def __init__(self) -> None:
        self._version = 0
        self._device_versions: dict[str, int] = {}

    # --- 查询 --------------------------------------------------------------

    @property
    def version(self) -> int:
        return self._version

    def device_version(self, device_id: str) -> int:
        """设备最后一次被改动时的全局版本；从未被改过则 0。"""

        return self._device_versions.get(device_id, 0)

    def snapshot_device_versions(self, device_ids: Iterable[str]) -> dict[str, int]:
        return {device_id: self.device_version(device_id) for device_id in sorted(set(device_ids))}

    # --- 写侧 --------------------------------------------------------------

    def observe(self, deltas: Sequence[DeltaChange] | None) -> int:
        """吃一批 delta，推进版本号；返回推进后的全局版本。

        空批次（同值写入 → 无 delta）不推进：不然每次"没变化的写入"都会把在飞决策判死。
        """

        if not deltas:
            return self._version
        for delta in deltas:
            self._version += 1
            device_id = _device_id_from_path(delta.path)
            if device_id is not None:
                self._device_versions[device_id] = self._version
        return self._version

    # --- 接线 --------------------------------------------------------------

    @classmethod
    def attach(cls, state_manager: StateManager) -> "WorldVersionTracker":
        """给一台 StateManager 装上版本簿记（幂等）。

        幂等很重要：引擎、runtime、测试都可能各调一次；套两层 wrapper 会让同一次改动
        把版本推进两格，per-device 与全局版本随即对不上。
        """

        existing = getattr(state_manager, cls._ATTR, None)
        if isinstance(existing, cls):
            return existing

        tracker = cls()
        inner = state_manager.apply_path_update

        def apply_path_update(*args: Any, **kwargs: Any) -> list[DeltaChange]:
            deltas = inner(*args, **kwargs)
            tracker.observe(deltas)
            return deltas

        state_manager.apply_path_update = apply_path_update  # type: ignore[method-assign]
        setattr(state_manager, cls._ATTR, tracker)
        return tracker

    @classmethod
    def of(cls, state_manager: StateManager) -> "WorldVersionTracker | None":
        tracker = getattr(state_manager, cls._ATTR, None)
        return tracker if isinstance(tracker, cls) else None


def _device_id_from_path(path: str) -> str | None:
    match = _DEVICE_PATH_RE.match(path)
    return match.group(1) if match else None


class VersionedDecision(BaseModel):
    """一份"带着它据以推理的世界版本"的决策（S3-T7 的对外形状）。

    ``device_versions`` 只记这条决策要碰的设备；``assumptions`` 记录整份提案共享的
    可观测事实。
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = ""
    decided_at_version: int
    device_versions: dict[str, int] = Field(default_factory=dict)
    commands: list[AgentCommandProposal] = Field(default_factory=list)
    assumptions: list[ProposalAssumption] = Field(default_factory=list)
    correlation_id: str | None = None
    root_event_id: str | None = None

    @classmethod
    def snapshot(
        cls,
        tracker: WorldVersionTracker,
        *,
        commands: Sequence[AgentCommandProposal],
        agent_id: str = "",
        correlation_id: str | None = None,
        root_event_id: str | None = None,
        assumptions: Sequence[ProposalAssumption] = (),
    ) -> "VersionedDecision":
        """在**发起 LLM 调用之前**拍一张版本快照，落地前再比。"""

        return cls(
            agent_id=agent_id,
            decided_at_version=tracker.version,
            device_versions=tracker.snapshot_device_versions(
                command.device_id for command in commands
            ),
            commands=[command.model_copy(deep=True) for command in commands],
            assumptions=[item.model_copy(deep=True) for item in assumptions],
            correlation_id=correlation_id,
            root_event_id=root_event_id,
        )

    @classmethod
    def at_version(
        cls,
        observed_version: int,
        *,
        commands: Sequence[AgentCommandProposal],
        agent_id: str = "",
        correlation_id: str | None = None,
        root_event_id: str | None = None,
        assumptions: Sequence[ProposalAssumption] = (),
    ) -> "VersionedDecision":
        """用一个**过去的**全局版本建快照——真实链路里的顺序就是反的。

        :meth:`snapshot` 要求"拍快照时命令已经算出来了"，但生产链路是：先读世界（版本在
        那一刻定格）→ LLM 想 1-5 秒 → 命令才出来。所以运行时记下的是**读世界那一刻的
        全局版本**，落地前问"这台设备是不是在那之后被改过"。

        语义与 per-device 快照完全等价：``device_version(d)`` 就是"d 最后一次被改动时的
        全局版本"，因此 ``device_version(d) > 读世界时的全局版本`` ⟺ d 在那之后动过。
        """

        version = int(observed_version)
        return cls(
            agent_id=agent_id,
            decided_at_version=version,
            device_versions={command.device_id: version for command in commands},
            commands=[command.model_copy(deep=True) for command in commands],
            assumptions=[item.model_copy(deep=True) for item in assumptions],
            correlation_id=correlation_id,
            root_event_id=root_event_id,
        )


class StaleDecisionCheck(BaseModel):
    """:func:`check_stale_decision` 的判定结果（不含任何副作用）。"""

    model_config = ConfigDict(extra="forbid")

    is_stale: bool
    decided_at_version: int
    current_version: int
    fresh_commands: list[AgentCommandProposal] = Field(default_factory=list)
    discarded_commands: list[AgentCommandProposal] = Field(default_factory=list)
    stale_device_ids: list[str] = Field(default_factory=list)
    invalidated_assumptions: list[dict[str, Any]] = Field(default_factory=list)


def check_stale_decision(
    decision: VersionedDecision,
    tracker: WorldVersionTracker,
    observable_world: WorldState | None = None,
) -> StaleDecisionCheck:
    """决策落地前复核设备版本和提案依赖的可观测事实。

    设备版本冲突只淘汰对应设备上的命令。共享假设失效则淘汰整个提案，因为运行时
    无法证明剩余命令仍符合原始意图。命令顺序原样保留——顺序是仲裁与执行的语义。
    """

    invalidated: list[dict[str, Any]] = []
    if decision.assumptions:
        if observable_world is None:
            invalidated = [
                {
                    "path": item.path,
                    "expected": item.equals,
                    "actual": None,
                    "missing": True,
                }
                for item in decision.assumptions
            ]
        else:
            for item in decision.assumptions:
                try:
                    actual = StateManager.read_path(observable_world, item.path)
                    missing = False
                except (AttributeError, KeyError, TypeError):
                    actual = None
                    missing = True
                if missing or type(actual) is not type(item.equals) or actual != item.equals:
                    invalidated.append(
                        {
                            "path": item.path,
                            "expected": item.equals,
                            "actual": actual,
                            "missing": missing,
                        }
                    )

    stale_devices: set[str] = set()
    fresh: list[AgentCommandProposal] = []
    discarded: list[AgentCommandProposal] = []

    for command in decision.commands:
        snapshot_version = decision.device_versions.get(command.device_id, 0)
        if tracker.device_version(command.device_id) > snapshot_version:
            stale_devices.add(command.device_id)
            discarded.append(command)
        else:
            fresh.append(command)

    if invalidated:
        discarded = list(decision.commands)
        fresh = []

    return StaleDecisionCheck(
        is_stale=bool(discarded),
        decided_at_version=decision.decided_at_version,
        current_version=tracker.version,
        fresh_commands=fresh,
        discarded_commands=discarded,
        stale_device_ids=sorted(stale_devices),
        invalidated_assumptions=sorted(
            invalidated, key=lambda item: str(item["path"])
        ),
    )


def build_stale_decision_event(
    decision: VersionedDecision,
    check: StaleDecisionCheck,
    *,
    root_event: SimEvent | None = None,
    source: str = "agent_runtime",
    sim_time_s: float | None = None,
) -> SimEvent:
    """构造执行前复核失败的 ``reasoning.decision_discarded`` 事件。

    没有丢弃就没有事件——传一个 ``is_stale=False`` 的判定进来是调用方的逻辑错误，
    直接抛，而不是发一条"丢了 0 条命令"的空事件去污染推理链。
    """

    if not check.is_stale:
        raise ValueError("没有被丢弃的命令，不该发 decision_discarded 事件")

    return SimEvent(
        event_type=STALE_DECISION_EVENT_TYPE,
        source=source,
        timestamp=root_event.timestamp if root_event is not None else 0.0,
        correlation_id=(
            decision.correlation_id
            or (root_event.correlation_id if root_event is not None else None)
        )
        or "",
        causal_parent=decision.root_event_id
        or (root_event.event_id if root_event is not None else None),
        priority=2,
        run_id=root_event.run_id if root_event is not None else None,
        scenario_id=root_event.scenario_id if root_event is not None else None,
        sim_time_s=sim_time_s if sim_time_s is not None else (
            root_event.sim_time_s if root_event is not None else None
        ),
        event_generation_mode="system",
        data={
            "reason": (
                INVALIDATED_ASSUMPTION_REASON
                if check.invalidated_assumptions
                else STALE_DECISION_REASON
            ),
            "agent_id": decision.agent_id,
            "decided_at_version": check.decided_at_version,
            "current_version": check.current_version,
            "stale_device_ids": check.stale_device_ids,
            "invalidated_assumptions": check.invalidated_assumptions,
            "discarded_commands": [
                command.model_dump(mode="json") for command in check.discarded_commands
            ],
            "kept_commands": [
                command.model_dump(mode="json") for command in check.fresh_commands
            ],
        },
    )


# ---------------------------------------------------------------------------
# LLM 成本护栏（S3-T8）
# ---------------------------------------------------------------------------

BUDGET_EXCEEDED_REASON = "budget_exceeded"
EPISODE_BUDGET_ENV = "AGENT_EPISODE_BUDGET_USD"
# GSTACK §6：一条 episode 的花费上限。默认 $0.10 —— 一次"事件 → 编排 → 若干 agent"的
# 完整推理链在这个量级内应当绰绰有余；跑不完说明要么模型太贵，要么链路在打转，
# 两种情况都该让研究者看见，而不是让账单默默长出来。
DEFAULT_EPISODE_BUDGET_USD = 0.10

# 这些 provider 不产生账单：mocked 是罐头，replay 从文件读，disabled 压根不调用。
# 它们必须**免检**——不然预算会改写 mocked/回放跑法的行为，而那正是 S2 落地的字节
# 一致性门（tests/test_replay_determinism.py）赖以成立的路径。
FREE_PROVIDER_NAMES = frozenset({"mocked", "replay", "disabled"})


def resolve_episode_budget_usd(env: Mapping[str, str] | None = None) -> float:
    """读 ``AGENT_EPISODE_BUDGET_USD``；非法值直接抛。

    刻意不"看不懂就用默认值"：把 ``AGENT_EPISODE_BUDGET_USD=0.O1``（字母 O）静默当成
    0.10，等于研究者以为自己压了预算、实际没压，而账单要到月底才会说话。
    """

    environ = os.environ if env is None else env
    raw = str(environ.get(EPISODE_BUDGET_ENV, "")).strip()
    if not raw:
        return DEFAULT_EPISODE_BUDGET_USD
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{EPISODE_BUDGET_ENV}={raw!r} 不是合法金额（美元浮点数）") from exc
    if value < 0:
        raise ValueError(f"{EPISODE_BUDGET_ENV}={raw!r} 不能为负")
    return value


class EpisodeCost(BaseModel):
    """一条 episode（按 correlation_id 归集）的花费台账。

    直接进 ``reasoning.coordination_decision`` 的 data 与 run 成本工件，因此字段就是
    对外契约：``first_blocked_agent_id`` / ``first_blocked_at_call`` 回答的是
    "预算是在**哪一步**咬下去的"——只报总额的护栏没法调。
    """

    model_config = ConfigDict(extra="forbid")

    correlation_id: str = ""
    budget_usd: float = DEFAULT_EPISODE_BUDGET_USD
    calls: int = 0
    billable_calls: int = 0
    blocked_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cost_by_model: dict[str, float] = Field(default_factory=dict)
    usage_sources: dict[str, int] = Field(default_factory=dict)
    first_blocked_agent_id: str | None = None
    first_blocked_at_call: int | None = None

    @property
    def budget_exceeded(self) -> bool:
        """"护栏真的咬下去了吗"。

        只看有没有调用被拦，不看"花费是否已经超过预算"：后者在预算被设成 0 的实验里
        恒为真，会让这个字段失去信号。被拦一次 = 这条 episode 里有推理链是降级产物，
        这正是研究者读轨迹时要知道的那件事。
        """

        return self.blocked_calls > 0

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
        payload = super().model_dump(*args, **kwargs)
        payload["budget_exceeded"] = self.budget_exceeded
        return payload


class EpisodeCostGuard:
    """按 episode 累计 LLM 花费，超预算就把后续调用挡在门外。

    挡的方式是**抛既有的** :class:`LLMProviderError`（reason=``budget_exceeded``）：
    agent 侧（``BaseAgent._build_fallback_envelope``）与编排器侧（``HomeOrchestratorAgent.plan``）
    早就有"provider 失败 → 规则回退 + 带标签"的路径，护栏因此不需要在 runtime 里另开
    一条降级分支，``reasoning.fallback_rule_based`` 的 data.reason 直接就是 budget_exceeded。

    判定用的是**上界**：``已花 + 下一次调用的最坏成本 >= 预算`` 就不发这一次。
    先花再算等于允许超支，而超支是本护栏唯一要防的事。
    """

    def __init__(
        self,
        *,
        budget_usd: float | None = None,
        pricing: PricingTable | None = None,
        env: Mapping[str, str] | None = None,
        default_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        self.budget_usd = (
            float(budget_usd) if budget_usd is not None else resolve_episode_budget_usd(env)
        )
        self._pricing = pricing
        self.default_max_output_tokens = default_max_output_tokens
        self._episodes: dict[str, EpisodeCost] = {}
        # Worst-case cost already admitted but not yet settled.  check + reserve
        # is synchronous (no await), so concurrent domain-agent tasks sharing
        # one event loop cannot all observe the same stale spent balance.
        self._reserved_usd: dict[str, float] = {}
        # 记下每台模型当时用的是哪条价格：事后没人能重算"当时按什么价算的"。
        self._prices_used: dict[str, ModelPrice] = {}

    # --- 查询 --------------------------------------------------------------

    @property
    def pricing(self) -> PricingTable:
        return self._pricing or active_pricing_table()

    def episode(self, correlation_id: str = "") -> EpisodeCost:
        key = correlation_id or ""
        entry = self._episodes.get(key)
        if entry is None:
            entry = EpisodeCost(correlation_id=key, budget_usd=self.budget_usd)
            self._episodes[key] = entry
        return entry

    @property
    def episodes(self) -> dict[str, EpisodeCost]:
        return dict(self._episodes)

    # --- 判定与记账 --------------------------------------------------------

    def check_affordable(
        self,
        correlation_id: str = "",
        *,
        model: str | None = None,
        provider: str | None = None,
        prompt_tokens: int = 0,
        max_output_tokens: int | None = None,
        agent_id: str = "",
    ) -> float:
        """发这一次调用之前问一句。买不起就抛 ``budget_exceeded``；买得起返回最坏成本。"""

        episode = self.episode(correlation_id)
        worst_case = worst_case_call_cost_usd(
            model=model,
            provider=provider,
            prompt_tokens=prompt_tokens,
            max_output_tokens=(
                self.default_max_output_tokens if max_output_tokens is None else max_output_tokens
            ),
            table=self.pricing,
        )
        key = correlation_id or ""
        reserved = self._reserved_usd.get(key, 0.0)
        projected = round(episode.cost_usd + reserved + worst_case, COST_DIGITS)
        if projected < self.budget_usd:
            self._reserved_usd[key] = round(reserved + worst_case, COST_DIGITS)
            return worst_case

        episode.blocked_calls += 1
        if episode.first_blocked_agent_id is None:
            episode.first_blocked_agent_id = agent_id or None
            episode.first_blocked_at_call = episode.calls + 1
        # 护栏的每一次咬合都要留日志：plan_raw 风险条 #2 要求阈值调优是有证据的，
        # 而不是"感觉太紧了就调大一点"。
        log.warning(
            "llm_budget_exceeded",
            correlation_id=correlation_id,
            agent_id=agent_id,
            model=str(model or ""),
            spent_usd=episode.cost_usd,
            reserved_usd=reserved,
            worst_case_usd=worst_case,
            budget_usd=self.budget_usd,
            calls=episode.calls,
        )
        raise LLMProviderError(
            BUDGET_EXCEEDED_REASON,
            (
                f"episode 预算已用尽：已花 ${episode.cost_usd:.6f} + 在飞预留 ${reserved:.6f} "
                f"+ 本次最坏 ${worst_case:.6f} "
                f">= 预算 ${self.budget_usd:.6f}（{EPISODE_BUDGET_ENV}）；"
                f"agent={agent_id or '-'} model={model or '-'}"
            ),
        )

    def record_call(
        self,
        correlation_id: str = "",
        *,
        usage: TokenUsage,
        model: str | None = None,
        provider: str | None = None,
        billable: bool = True,
        reserved_usd: float | None = None,
    ) -> EpisodeCost:
        """Settle one call; release its preflight reservation before recording."""

        episode = self.episode(correlation_id)
        if billable and reserved_usd is not None:
            key = correlation_id or ""
            remaining = round(
                max(0.0, self._reserved_usd.get(key, 0.0) - reserved_usd),
                COST_DIGITS,
            )
            if remaining:
                self._reserved_usd[key] = remaining
            else:
                self._reserved_usd.pop(key, None)
        episode.calls += 1
        episode.input_tokens += usage.input_tokens
        episode.output_tokens += usage.output_tokens
        episode.usage_sources[usage.source.value] = (
            episode.usage_sources.get(usage.source.value, 0) + 1
        )
        if not billable:
            return episode

        price = self.pricing.lookup(model, provider)
        cost = price.cost_usd(usage)
        model_key = str(model or price.model)
        episode.billable_calls += 1
        episode.cost_usd = round(episode.cost_usd + cost, COST_DIGITS)
        episode.cost_by_model[model_key] = round(
            episode.cost_by_model.get(model_key, 0.0) + cost, COST_DIGITS
        )
        self._prices_used.setdefault(model_key, price)
        return episode

    def reset(self, correlation_id: str | None = None) -> None:
        if correlation_id is None:
            self._episodes.clear()
            self._prices_used.clear()
            self._reserved_usd.clear()
            return
        self._episodes.pop(correlation_id or "", None)
        self._reserved_usd.pop(correlation_id or "", None)

    # --- 对外载荷 ----------------------------------------------------------

    def episode_payload(self, correlation_id: str = "") -> dict[str, Any]:
        """``reasoning.coordination_decision`` 的 data 里那一块。"""

        return self.episode(correlation_id).model_dump(mode="json")

    def run_payload(self) -> dict[str, Any]:
        """整个 run 的成本汇总（成本工件的内容）。"""

        episodes = [
            self._episodes[key].model_dump(mode="json") for key in sorted(self._episodes)
        ]
        totals = {
            "episodes": len(episodes),
            "calls": sum(int(item["calls"]) for item in episodes),
            "billable_calls": sum(int(item["billable_calls"]) for item in episodes),
            "blocked_calls": sum(int(item["blocked_calls"]) for item in episodes),
            "input_tokens": sum(int(item["input_tokens"]) for item in episodes),
            "output_tokens": sum(int(item["output_tokens"]) for item in episodes),
            "cost_usd": round(sum(float(item["cost_usd"]) for item in episodes), COST_DIGITS),
            "budget_exceeded_episodes": sum(1 for item in episodes if item["budget_exceeded"]),
        }
        return {
            "schema": "aura.llm_cost.v1",
            "budget_usd": self.budget_usd,
            "budget_env": EPISODE_BUDGET_ENV,
            "totals": totals,
            "episodes": episodes,
            # 价格随工件一起走：没有价格出处的账单是不可复核的。
            "pricing": {
                "default": self.pricing.default.model_dump(mode="json"),
                "source_path": (
                    str(self.pricing.source_path) if self.pricing.source_path else None
                ),
                # 每台**被调用的模型名**对上它当时用的价格记录。两者刻意分开：
                # 未知模型用的是兜底价，此时 price.model == "__default__" 而 model 是
                # 真正被调用的模型名——"这份账是按兜底价算的"因此一眼可见。
                "used": [
                    {
                        "model": key,
                        "price": self._prices_used[key].model_dump(mode="json"),
                    }
                    for key in sorted(self._prices_used)
                ],
            },
        }

    def write_run_artifact(self, run_id: str, *, root: Path | str | None = None) -> Path | None:
        """把成本汇总落到 ``data/runs/{run_id}/llm_cost.json``。

        写不下去只记 error 不抛：记账塌了不该把一次仿真跑整个带走（与
        :class:`RecordingLLMProvider` 的取舍一致）。
        """

        if not artifacts_enabled():
            return None
        try:
            directory = run_dir(run_id, root=root)
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / LLM_COST_FILENAME
            path.write_text(canonical_json(self.run_payload()) + "\n", encoding="utf-8")
        except (OSError, ValueError) as exc:
            log.error("llm_cost_artifact_write_failed", run_id=run_id, error=str(exc))
            return None
        return path


class BudgetGuardedLLMProvider(LLMProvider):
    """给任意 provider 套上 episode 预算（S3-T8）。

    做成 provider 包装而不是 runtime 里的一段分支，是为了让**每一条**真会花钱的调用
    （编排器的 intent 调用 + 每个领域 agent 的调用）都必须经过同一个收费口——
    "有人新加了一处 LLM 调用却忘了记账"因此在结构上不可能发生。

    透明性是硬要求：``provider_name`` / ``model`` / ``llm_mode`` 一律穿透到内层，
    否则 run 元数据会把"budget_guarded"记成模型名，§11 的九字段就成了假的。
    """

    def __init__(
        self,
        inner: LLMProvider,
        guard: EpisodeCostGuard,
        *,
        correlation_id: str = "",
        max_output_tokens: int | None = None,
    ) -> None:
        self.inner = inner
        self.guard = guard
        self.correlation_id = correlation_id
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens 必须为正整数")
        self._max_output_tokens = max_output_tokens

    # --- 元数据穿透 --------------------------------------------------------

    @property
    def provider_name(self) -> str:  # type: ignore[override]
        return str(getattr(self.inner, "provider_name", "unknown") or "unknown")

    @property
    def model(self) -> str:  # type: ignore[override]
        return str(getattr(self.inner, "model", "") or "")

    @property
    def llm_mode(self) -> Any:  # type: ignore[override]
        return getattr(self.inner, "llm_mode", LLMMode.LIVE)

    @property
    def api_key(self) -> Any:
        return getattr(self.inner, "api_key", None)

    @property
    def timeout_ms(self) -> Any:
        return getattr(self.inner, "timeout_ms", None)

    @property
    def recordings_path(self) -> Any:
        return getattr(self.inner, "recordings_path", None)

    # --- 接线 --------------------------------------------------------------

    def for_episode(self, correlation_id: str) -> "BudgetGuardedLLMProvider":
        """换一条 episode 的收费口，共用同一台 guard（台账不重置）。"""

        return BudgetGuardedLLMProvider(
            self.inner,
            self.guard,
            correlation_id=correlation_id,
            max_output_tokens=self._max_output_tokens,
        )

    def is_billable(self) -> bool:
        if self.provider_name in FREE_PROVIDER_NAMES:
            return False
        return resolve_mode_for_provider(self.inner) in {LLMMode.LIVE, LLMMode.RECORDED}

    async def generate_decision(self, request: LLMDecisionRequest) -> AgentLLMDecision:
        try:
            request = LLMDecisionRequest.model_validate(request.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            raise LLMProviderError(
                "invalid_request",
                f"LLM 请求超过安全边界，未发送到 provider：{exc}",
            ) from exc
        request_text = canonical_json(canonical_request_payload(request))
        billable = self.is_billable()
        resolved_max_output_tokens: int | None = None
        reservation_usd: float | None = None

        if billable:
            resolved_max_output_tokens = self._resolve_max_output_tokens()
            # 买得起才发。抛出的是 LLMProviderError(budget_exceeded)，由既有 fallback
            # 路径接住并盖标签。
            reservation_usd = self.guard.check_affordable(
                self.correlation_id,
                model=self.model,
                provider=self.provider_name,
                # A token cannot contain more information than its UTF-8 bytes.
                # Counting every byte as a token is deliberately pessimistic and,
                # unlike chars/4 heuristics, never undercounts CJK or punctuation.
                prompt_tokens=self._conservative_input_token_bound(request_text),
                max_output_tokens=resolved_max_output_tokens,
                agent_id=request.agent_id,
            )

        # Clear the whole wrapper chain, not just concrete HTTP providers. A
        # recording wrapper can fail before it reaches its inner provider; without
        # this reset that failure would be charged using the previous call's usage.
        self._clear_reported_usage()
        try:
            decision = await self.inner.generate_decision(request)
        except BaseException:
            if billable and resolved_max_output_tokens is not None:
                self._record_attempt(
                    self._usage_for(
                        request_text,
                        decision=None,
                        max_output_tokens=resolved_max_output_tokens,
                    ),
                    billable=True,
                    reserved_usd=reservation_usd,
                )
            raise

        # Revalidate even an object already typed as AgentLLMDecision. A custom
        # provider can bypass Pydantic with model_construct(); the paid boundary
        # must not let that turn a 100k explanation into an accepted trace.
        try:
            decision = AgentLLMDecision.model_validate(decision.model_dump(mode="python"))
        except (AttributeError, ValidationError) as exc:
            if billable and resolved_max_output_tokens is not None:
                self._record_attempt(
                    self._usage_for(
                        request_text,
                        decision=None,
                        max_output_tokens=resolved_max_output_tokens,
                    ),
                    billable=True,
                    reserved_usd=reservation_usd,
                )
            raise LLMProviderError(
                "invalid_output",
                f"provider 返回的结构化决策超过安全边界：{exc}",
            ) from exc

        usage = self._usage_for(
            request_text,
            decision=decision,
            max_output_tokens=resolved_max_output_tokens,
        )
        if (
            billable
            and resolved_max_output_tokens is not None
            and usage.output_tokens > resolved_max_output_tokens
        ):
            self._record_attempt(
                usage,
                billable=True,
                reserved_usd=reservation_usd,
            )
            raise LLMProviderError(
                "provider_error",
                (
                    f"provider 报告 output_tokens={usage.output_tokens}，超过声明上限 "
                    f"{resolved_max_output_tokens}；拒绝接受该决策"
                ),
            )

        self._record_attempt(
            usage,
            billable=billable,
            reserved_usd=reservation_usd,
        )
        return decision

    def _record_attempt(
        self,
        usage: TokenUsage,
        *,
        billable: bool,
        reserved_usd: float | None = None,
    ) -> None:
        self.guard.record_call(
            self.correlation_id,
            usage=usage,
            model=self.model,
            provider=self.provider_name,
            billable=billable,
            reserved_usd=reserved_usd,
        )

    # --- 内部 ---------------------------------------------------------------

    def _resolve_max_output_tokens(self) -> int:
        declared_limits: list[int] = []
        for provider in self._provider_chain():
            for attribute in ("max_output_tokens", "max_tokens"):
                declared = getattr(provider, attribute, None)
                if isinstance(declared, int) and not isinstance(declared, bool) and declared > 0:
                    declared_limits.append(declared)

        if not declared_limits:
            # A wrapper-side default does not constrain what the remote provider
            # can actually emit. Sending here would make the preflight fictional.
            raise LLMProviderError(
                "provider_error",
                (
                    f"付费 provider {self.provider_name!r} 未声明可执行的输出 token 上限；"
                    "为避免预算低估，本次调用已在发出前拒绝"
                ),
            )

        declared_limit = max(declared_limits)
        if self._max_output_tokens is None:
            return declared_limit
        # An override is only a budgeting bound; it cannot silently lower the
        # provider's independently enforced cap. Use the safer larger value.
        return max(self._max_output_tokens, declared_limit)

    def _provider_chain(self) -> list[Any]:
        """Return wrappers plus the concrete provider without importing internals."""

        chain: list[Any] = []
        seen: set[int] = set()
        current: Any = self.inner
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(current)
            if isinstance(current, RunScopedRecordedProvider):
                current = current.resolve()
                continue
            current = getattr(current, "inner", None)
        return chain

    @staticmethod
    def _conservative_input_token_bound(request_text: str) -> int:
        return len(request_text.encode("utf-8"))

    def _reported_usage(self) -> TokenUsage | None:
        for provider in self._provider_chain():
            reported = getattr(provider, "last_usage", None)
            if isinstance(reported, TokenUsage):
                return reported
            if isinstance(reported, Mapping):
                parsed = parse_usage(reported)
                if parsed is not None:
                    return parsed
        return None

    def _clear_reported_usage(self) -> None:
        for provider in self._provider_chain():
            if not hasattr(provider, "last_usage"):
                continue
            try:
                setattr(provider, "last_usage", None)
            except (AttributeError, TypeError):
                # Read-only telemetry properties cannot carry mutable stale state.
                continue

    def _usage_for(
        self,
        request_text: str,
        *,
        decision: AgentLLMDecision | None,
        max_output_tokens: int | None,
    ) -> TokenUsage:
        """先读 provider 报的账，读不到再估。

        付费 provider 没给 usage 时不能用平均字符比率补账：预检依赖的是最坏上界，
        记账若又缩回均值，后续调用仍可能越过 episode 预算。因此付费路径按输入字节数 +
        provider 的硬输出上限记一笔保守账；mock/replay 仍可按实际载荷估算。
        """

        reported = self._reported_usage()
        if reported is not None:
            return reported

        if max_output_tokens is not None:
            return TokenUsage(
                input_tokens=self._conservative_input_token_bound(request_text),
                output_tokens=max_output_tokens,
                source=UsageSource.ESTIMATED,
            )

        if decision is None:
            return TokenUsage(source=UsageSource.ESTIMATED)
        response_text = json.dumps(
            decision.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        )
        return estimate_usage(request_text=request_text, response_text=response_text)
