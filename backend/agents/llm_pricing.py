"""LLM 价格表与 token 用量核算（S3-T8 的"数据层"）。

**为什么价格必须是数据**：单价散在代码里的护栏活不过一次调价——它会一直用去年的价格
算今年的账，而且没人能看出它算错了。这里把每条价格做成一条**带出处的记录**
（provider / 模型 / 单价 / 观测日期 / 出处 / 是否精确匹配），并允许用一份 JSON
（``AURA_LLM_PRICING_PATH``）整表替换。护栏本身（:class:`~backend.agents.llm_modes.EpisodeCostGuard`）
一个数字都不硬编码。

**为什么价格要归属到 provider**：同一个模型名在不同网关（官方 API / 兼容网关 / 云市场）
价格不同。一条只写 "claude-sonnet: 3/15" 的价格是不可核对的断言；写清"哪家、哪天、
从哪看的"，研究者才能自己复核，也能一眼看出哪条是我们**推定**的。

**用量从哪来**：优先读 provider 回包里的 usage 块（:func:`parse_usage`，认得 OpenAI
Responses / Anthropic / chat-completions 三种形状）；MiniMax 的 Anthropic 兼容端点已知
可能整个不给 usage，那时退到字符估算（:func:`estimate_usage`）。**估算绝不能估成 0**：
"没有账单"与"花了钱但没记账"是两回事，后者会让护栏一路放行。

跨阶段契约::

    AURA_LLM_PRICING_PATH        价格表覆盖文件（JSON：{"default": {...}, "models": [...]}）
    data/runs/{run_id}/llm_cost.json   成本工件（由 EpisodeCostGuard 写，与 events.jsonl 同目录）
"""

from __future__ import annotations

import json
import math
import os
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from backend.core.logging import log

__all__ = [
    "PRICING_ENV",
    "LLM_COST_FILENAME",
    "COST_DIGITS",
    "CHARS_PER_TOKEN_ASCII",
    "CHARS_PER_TOKEN_DENSE",
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "TOKENS_PER_MILLION",
    "UsageSource",
    "TokenUsage",
    "ModelPrice",
    "PricingTable",
    "DEFAULT_MODEL_PRICE",
    "PRICING_TABLE",
    "active_pricing_table",
    "load_pricing_table",
    "lookup_price",
    "parse_usage",
    "estimate_tokens",
    "estimate_usage",
    "call_cost_usd",
    "worst_case_call_cost_usd",
]


PRICING_ENV = "AURA_LLM_PRICING_PATH"

# 成本工件文件名。工件命名的"唯一说法"在 backend/engine/event_log.py（EVENTS_FILENAME /
# LLM_RECORDINGS_FILENAME 都在那里）；本常量落在这里只是因为 S3-T8 的文件范围不含
# event_log.py，后续应当搬过去与其它工件名并列。
LLM_COST_FILENAME = "llm_cost.json"

TOKENS_PER_MILLION = 1_000_000

# 成本保留位数。定位是为了让成本进 canonical trace / 工件时逐位可复现；
# 8 位足以让"一次几十万分之一美元"的调用也不被抹成 0（抹成 0 的护栏等于没有护栏）。
COST_DIGITS = 8

# 字符→token 的粗估比。ASCII 大约 4 字符/token；中日韩要密得多（约 1.5-2 字符/token），
# 本仓库的 world_summary / reason 大量是中文，用统一的 4 会系统性低估中文场景的花费。
CHARS_PER_TOKEN_ASCII = 4.0
CHARS_PER_TOKEN_DENSE = 2.0

# 单次调用的"最坏输出长度"缺省值：与 AnthropicCompatibleProvider 的 max_tokens 默认值
# 一致。provider 自己声明了 max_tokens 时以它为准（见 llm_modes.BudgetGuardedLLMProvider）。
DEFAULT_MAX_OUTPUT_TOKENS = 1200


class UsageSource(str, Enum):
    """这条用量是**读来的**还是**估的**——两者的可信度不同，必须一路带到工件里。"""

    REPORTED = "reported"
    PARTIAL = "partial"  # 回包只给了 total_tokens 之类，拆分是我们推定的
    ESTIMATED = "estimated"


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    source: UsageSource = UsageSource.ESTIMATED

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelPrice(BaseModel):
    """一条价格记录。``source``/``as_of`` 不是注释，是可核对性本身。"""

    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    input_usd_per_mtok: float = Field(ge=0.0)
    output_usd_per_mtok: float = Field(ge=0.0)
    source: str = Field(min_length=1)
    as_of: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    # True = 模型名与厂商定价页逐字对上；False = 按最接近的档位**推定**（含前缀匹配）。
    # 研究者据此知道哪几条数字需要自己复核，而不是全盘相信。
    verified: bool = False
    is_default: bool = False

    def cost_usd(self, usage: TokenUsage) -> float:
        raw = (
            usage.input_tokens * self.input_usd_per_mtok
            + usage.output_tokens * self.output_usd_per_mtok
        ) / TOKENS_PER_MILLION
        return round(raw, COST_DIGITS)


# 未知模型的兜底价：取"中档"（Sonnet 级）而不是最贵的一档。
# 取舍写清楚：兜底价越高护栏咬得越早，用最贵档会让任何没登记的模型在**第一次调用**
# 就被判超预算（plan_raw 风险条 #2 明写"太紧的最坏估计会把 live 演示饿死成纯规则"）；
# 取中档则可能低估一台真的很贵的未知模型——所以未知模型会在成本工件里带
# is_default=True，事后一眼能看出这份账是按兜底价算的。
DEFAULT_MODEL_PRICE = ModelPrice(
    provider="unknown",
    model="__default__",
    input_usd_per_mtok=3.0,
    output_usd_per_mtok=15.0,
    source="conservative default for unknown models (mid-tier / Sonnet-class)",
    as_of="2026-07-21",
    verified=False,
    is_default=True,
)


# 内置价格表。**只登记本仓库真的会用到的那几家**，其余交给兜底价 + JSON 覆盖。
# verified=False 的条目是按公开档位推定的，别拿它当账单。
_BUILTIN_PRICES: tuple[ModelPrice, ...] = (
    ModelPrice(
        provider="openai",
        model="gpt-4o",
        input_usd_per_mtok=2.5,
        output_usd_per_mtok=10.0,
        source="OpenAI 公开定价页（API pricing），按模型名逐字登记",
        as_of="2026-01-01",
        verified=True,
    ),
    ModelPrice(
        provider="openai",
        model="gpt-4o-mini",
        input_usd_per_mtok=0.15,
        output_usd_per_mtok=0.6,
        source="OpenAI 公开定价页（API pricing），按模型名逐字登记",
        as_of="2026-01-01",
        verified=True,
    ),
    ModelPrice(
        provider="anthropic",
        model="claude-3-5-haiku",
        input_usd_per_mtok=0.8,
        output_usd_per_mtok=4.0,
        source="Anthropic 公开定价页，按模型名前缀登记",
        as_of="2026-01-01",
        verified=True,
    ),
    ModelPrice(
        provider="anthropic",
        model="claude-3-5-sonnet",
        input_usd_per_mtok=3.0,
        output_usd_per_mtok=15.0,
        source="Anthropic 公开定价页，按模型名前缀登记",
        as_of="2026-01-01",
        verified=True,
    ),
    ModelPrice(
        provider="anthropic",
        model="claude-sonnet-4",
        input_usd_per_mtok=3.0,
        output_usd_per_mtok=15.0,
        source="Anthropic 公开定价页（Sonnet 档），按模型名前缀登记",
        as_of="2026-01-01",
        verified=True,
    ),
    ModelPrice(
        provider="anthropic",
        model="claude-opus-4",
        input_usd_per_mtok=15.0,
        output_usd_per_mtok=75.0,
        source="Anthropic 公开定价页（Opus 档），按模型名前缀登记",
        as_of="2026-01-01",
        verified=True,
    ),
    ModelPrice(
        provider="minimax",
        # 本仓库默认模型串是 MiniMax-M2.7，走 Anthropic 兼容端点；这里按 M2 档前缀登记。
        model="minimax-m2",
        input_usd_per_mtok=0.3,
        output_usd_per_mtok=1.2,
        source=(
            "MiniMax 开放平台 M2 档公开定价（推定：本仓库未离线核对 M2.7 是否同价，"
            "换模型请用 AURA_LLM_PRICING_PATH 覆盖）"
        ),
        as_of="2026-01-01",
        verified=False,
    ),
)


class PricingTable:
    """一张价格表 = 若干条 :class:`ModelPrice` + 一条兜底价。

    查找是"精确名 → 最长前缀 → 兜底"。前缀匹配是刻意的：厂商模型名带日期/版本后缀
    （``claude-sonnet-4-5-20260101``、``MiniMax-M2.7``），逐字登记会让每次小版本更新都
    静默掉回兜底价。前缀命中的条目 ``verified`` 保持登记时的取值，不会被伪装成精确匹配。
    """

    def __init__(
        self,
        entries: Sequence[ModelPrice],
        *,
        default: ModelPrice = DEFAULT_MODEL_PRICE,
        source_path: Path | None = None,
    ) -> None:
        self.entries: tuple[ModelPrice, ...] = tuple(entries)
        self.default = default.model_copy(update={"is_default": True})
        self.source_path = source_path

    def lookup(self, model: str | None, provider: str | None = None) -> ModelPrice:
        normalized = _normalize_model_name(model)
        if not normalized:
            return self.default

        candidates: list[ModelPrice] = list(self.entries)
        if provider:
            provider_key = provider.strip().lower()
            preferred = [
                price for price in candidates if price.provider.strip().lower() == provider_key
            ]
            candidates = preferred or candidates

        for price in candidates:
            if price.model.strip().lower() == normalized:
                return price

        best: ModelPrice | None = None
        for price in candidates:
            key = price.model.strip().lower()
            if normalized.startswith(key) or key.startswith(normalized):
                if best is None or len(key) > len(best.model.strip()):
                    best = price
        return best or self.default

    def as_json_dict(self) -> dict[str, Any]:
        return {
            "default": self.default.model_dump(mode="json"),
            "models": [price.model_dump(mode="json") for price in self.entries],
            "source_path": str(self.source_path) if self.source_path is not None else None,
        }


PRICING_TABLE = PricingTable(_BUILTIN_PRICES)

# 覆盖文件的缓存键是 (路径, mtime, 大小)：研究者改完价格文件不必重启进程，
# 但同一份文件也不会每次调用都重读。
_override_cache: dict[tuple[str, float, int], PricingTable] = {}


def _normalize_model_name(model: str | None) -> str:
    if not model:
        return ""
    name = str(model).strip().lower()
    # "anthropic/claude-…"、"openai:gpt-4o" 这类网关前缀不应导致"未知模型"
    for separator in ("/", ":"):
        if separator in name:
            name = name.rsplit(separator, 1)[-1]
    return name.strip()


def load_pricing_table(path: Path | str) -> PricingTable:
    """读一份 JSON 价格表：``{"default": {...}, "models": [...]}``。

    文件坏了就**退回内置表并记一条 error**，而不是抛：成本记账塌了不该把一次仿真跑
    整个带走；但它必须在日志里可见，否则会静默按内置价算另一台模型的账。
    """

    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        entries = [ModelPrice.model_validate(item) for item in payload.get("models", [])]
        default_payload = payload.get("default")
        default = (
            ModelPrice.model_validate({**default_payload, "is_default": True})
            if isinstance(default_payload, Mapping)
            else DEFAULT_MODEL_PRICE
        )
    except Exception as exc:  # json / pydantic / OSError 都归为"这份价格表不能用"
        log.error("llm_pricing_load_failed", path=str(file_path), error=str(exc))
        return PRICING_TABLE
    return PricingTable(entries, default=default, source_path=file_path)


def active_pricing_table(env: Mapping[str, str] | None = None) -> PricingTable:
    """当前生效的价格表：``AURA_LLM_PRICING_PATH`` 指定的那份，否则内置表。"""

    environ = os.environ if env is None else env
    raw = str(environ.get(PRICING_ENV, "")).strip()
    if not raw:
        return PRICING_TABLE

    path = Path(raw)
    try:
        stat = path.stat()
    except OSError:
        log.error("llm_pricing_path_missing", path=str(path))
        return PRICING_TABLE

    cache_key = (str(path), stat.st_mtime, stat.st_size)
    cached = _override_cache.get(cache_key)
    if cached is None:
        cached = load_pricing_table(path)
        _override_cache[cache_key] = cached
    return cached


def lookup_price(
    model: str | None,
    provider: str | None = None,
    *,
    table: PricingTable | None = None,
) -> ModelPrice:
    return (table or active_pricing_table()).lookup(model, provider)


# ---------------------------------------------------------------------------
# 用量：先读，读不到再估
# ---------------------------------------------------------------------------

_INPUT_KEYS = ("input_tokens", "prompt_tokens", "promptTokens", "inputTokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "completionTokens", "outputTokens")
_EXTRA_INPUT_KEYS = ("cache_creation_input_tokens", "cache_read_input_tokens")
_TOTAL_KEYS = ("total_tokens", "totalTokens")


def parse_usage(payload: Mapping[str, Any] | None) -> TokenUsage | None:
    """从 provider 回包（或裸 usage 块）里读用量；读不出来返回 ``None``。

    返回 ``None`` 而不是"全 0 的 TokenUsage"是本函数最重要的性质：全 0 会被下游当成
    "这次调用免费"，于是护栏永远不会咬。调用方拿到 None 必须去估
    （:func:`estimate_usage`）。

    认得三种形状：OpenAI Responses / Anthropic（``input_tokens``/``output_tokens``）、
    chat-completions（``prompt_tokens``/``completion_tokens``）、以及只有 ``total_tokens``
    的简化回包。缓存读/写 token 计入输入（略高估好过看起来很便宜）。
    """

    if not isinstance(payload, Mapping):
        return None

    block = payload.get("usage")
    usage = block if isinstance(block, Mapping) else payload

    input_tokens = _first_int(usage, _INPUT_KEYS)
    output_tokens = _first_int(usage, _OUTPUT_KEYS)
    extra_input = sum(_first_int(usage, (key,)) or 0 for key in _EXTRA_INPUT_KEYS)
    total_tokens = _first_int(usage, _TOTAL_KEYS)

    if input_tokens is None and output_tokens is None:
        if not total_tokens:
            return None
        # 只有总量：全算成输出。输出单价更高，因此这是**保守**的拆分。
        return TokenUsage(input_tokens=0, output_tokens=total_tokens, source=UsageSource.PARTIAL)

    resolved_input = (input_tokens or 0) + extra_input
    resolved_output = output_tokens
    source = UsageSource.REPORTED
    if resolved_output is None:
        # 有输入没输出：用总量补齐，补不出就判定这份回包不可用（去估）。
        if total_tokens:
            resolved_output = max(total_tokens - resolved_input, 0)
            source = UsageSource.PARTIAL
        else:
            return None
    if resolved_input == 0 and resolved_output == 0:
        return None

    return TokenUsage(
        input_tokens=resolved_input,
        output_tokens=resolved_output,
        source=source,
    )


def _first_int(usage: Mapping[str, Any], keys: Iterable[str]) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return max(int(value), 0)
    return None


def estimate_tokens(text: str | None) -> int:
    """字符 → token 的粗估。空文本才是 0，非空文本至少 1。

    中日韩比例高的文本用更密的比例：本仓库的 world_summary / reason 大量是中文，
    统一按 4 字符/token 估会把中文场景的花费系统性低估。
    """

    if not text:
        return 0
    dense = sum(1 for char in text if ord(char) > 0x2E7F)
    ratio = CHARS_PER_TOKEN_DENSE if dense * 2 >= len(text) else CHARS_PER_TOKEN_ASCII
    return max(1, math.ceil(len(text) / ratio))


def estimate_usage(*, request_text: str, response_text: str = "") -> TokenUsage:
    return TokenUsage(
        input_tokens=estimate_tokens(request_text),
        output_tokens=estimate_tokens(response_text),
        source=UsageSource.ESTIMATED,
    )


def call_cost_usd(
    usage: TokenUsage,
    *,
    model: str | None,
    provider: str | None = None,
    table: PricingTable | None = None,
) -> float:
    return lookup_price(model, provider, table=table).cost_usd(usage)


def worst_case_call_cost_usd(
    *,
    model: str | None,
    prompt_tokens: int,
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    provider: str | None = None,
    table: PricingTable | None = None,
) -> float:
    """下一次调用的**上界**成本：提示词按估算长度，输出按 provider 允许的上限。

    护栏用上界而不是均值：均值会让"最后一次调用刚好很长"这种情况稳定地超支，而超支的
    定义就是不可接受。上界的代价是偶尔提前一格降级，这在推理流里是可见且可调的
    （``AGENT_EPISODE_BUDGET_USD``）。
    """

    usage = TokenUsage(
        input_tokens=max(int(prompt_tokens), 0),
        output_tokens=max(int(max_output_tokens), 0),
        source=UsageSource.ESTIMATED,
    )
    return lookup_price(model, provider, table=table).cost_usd(usage)
