"""Canonical trace：一次 run 的**确定性投影**（S2-T9 字节一致性门的比对对象）。

一份原始事件流不能直接拿来比对两次运行是否一致：里面混着每次都不同、却与仿真语义无关
的东西——uuid、墙钟、run 目录名。直接比对必然永远不等；随手把 data 删光又会让门变成
一句空话。canonical trace 是这两者之间那条唯一正确的线：

  **易变的删掉（只删声明过的），身份的改写（双射，不删），语义的原样保留。**

三类字段，规矩各不相同：

``VOLATILE_EVENT_FIELDS`` / ``VOLATILE_DATA_KEYS``
    真·墙钟量（``wall_time``、``latency_ms``），删。排除域**必须最小且具名**——
    tests/test_replay_determinism.py 会断言这两个集合的确切内容，把"靠扩大排除域
    让门变绿"这条捷径提前堵死。

``event_id`` / ``correlation_id`` / ``causal_parent`` / ``run_id`` 以及藏在 ``data``
里的同类 id（``command_id`` / ``caused_by_event_id`` / ``root_event_id`` …）
    每次运行都不同，但**出现顺序**是确定的，因此按首次出现顺序改写成
    ``id:0``、``id:1`` …。这是双射：因果边一条不少（父事件拿到的符号，就是子事件
    ``causal_parent`` 上的符号），S5 的因果树想验证时照样能在 trace 上验证。
    按值的**形状**识别（32 位十六进制 = ``uuid4().hex``；``run-<时间戳>-<8 位十六进制>``），
    而不是按键名白名单——键名白名单会在下一个生产方加一个 id 字段时静默漏掉它。

``seq``
    重基成 trace 内序号。引擎启动那次 run 刻意不清总线历史，两个 run 的起点 seq 未必
    相同；比的是"顺序"，不是"从几号开始"。

其余（``event_type``/``source``/``timestamp``/``sim_time_s``/``priority``/``scenario_id``/
§4.5 三项生成元数据/``data``）原样进 trace。

序列化用 :func:`~backend.engine.run_manager.canonical_json`（与 events.jsonl 同一套规矩），
一行一条事件，因此 trace 既能整体做字节比对，也能用 ``diff`` 逐行读——门失败时研究者要
看的是"第几条事件开始分叉"，不是一个哈希值。

**范围声明**：本模块只投影确定性子集。§11.1 的 live LLM 模式不在门内（规格原文
"LLM outputs may vary unless mocked or recorded"）；mocked（规则回退）与 S3 的 recorded
模式才是它的对象。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from backend.engine.event_bus import SimEvent
from backend.engine.event_log import RUN_ID_PATTERN, iter_run_events
from backend.engine.run_manager import canonical_json

__all__ = [
    "TRACE_FORMAT_VERSION",
    "VOLATILE_EVENT_FIELDS",
    "VOLATILE_DATA_KEYS",
    "IDENTIFIER_SYMBOL_PREFIX",
    "canonical_trace",
    "canonical_trace_text",
    "export_canonical_trace",
    "trace_digest",
]

# 投影格式版本。改动投影规则（新增排除、改符号格式）必须 +1，否则跨版本比对会静默错判。
TRACE_FORMAT_VERSION = 1

# —— 排除域（唯一允许被丢弃的东西）——
# 只有真·墙钟量。两个集合的确切内容被阶段门测试断言，不要随手往里加字段：
# 每加一项，门就少验一分。
VOLATILE_EVENT_FIELDS: frozenset[str] = frozenset({"wall_time"})
VOLATILE_DATA_KEYS: frozenset[str] = frozenset({"wall_time", "latency_ms"})

# 会被重基（而非删除）的顶层字段，见模块 docstring。
NORMALIZED_EVENT_FIELDS: frozenset[str] = frozenset(
    {"seq", "event_id", "correlation_id", "causal_parent", "run_id"}
)

IDENTIFIER_SYMBOL_PREFIX = "id:"

# uuid4().hex（事件 id / correlation / command_id …）。
_HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# RunManager 的 run_id：run-20260721T073632-0d8961ba。形状是承重的（决定哪些串要被
# 重符号化），因此复用 event_log 的唯一定义，不再各留一份正则。
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


def _is_opaque_identifier(value: str) -> bool:
    """这个字符串是"每次运行都不同、但顺序确定"的身份值吗？

    刻意按形状判定而不是按键名白名单：白名单会在下一个生产方新增一个 id 字段的那天
    静默失效，而失效的表现是"门依旧全绿"——最坏的一种失效。
    """

    return bool(_HEX_ID_RE.match(value) or _RUN_ID_RE.match(value))


class _SymbolTable:
    """身份值 → 稳定符号（按首次出现顺序）。

    扫描顺序是确定的（事件按 seq 升序、字段名排序、data 递归按键排序），因此只要底层
    事件流本身确定，符号分配就确定；反过来，事件流一旦乱序，符号会**整体错位**，
    门会立刻炸得很响——这正是想要的。
    """

    def __init__(self) -> None:
        self._symbols: dict[str, str] = {}

    def symbol(self, value: str) -> str:
        existing = self._symbols.get(value)
        if existing is not None:
            return existing
        symbol = f"{IDENTIFIER_SYMBOL_PREFIX}{len(self._symbols)}"
        self._symbols[value] = symbol
        return symbol

    def __len__(self) -> int:  # pragma: no cover - 调试用
        return len(self._symbols)


def _normalize(value: Any, symbols: _SymbolTable) -> Any:
    """递归归一：删易变键、改写身份值、其余原样。"""

    if isinstance(value, str):
        # Device operations deliberately retain their command-derived identity
        # for auditability.  Symbolize the opaque suffix with the same table so
        # ``operation:<command_id>`` preserves that relationship across runs.
        if value.startswith("operation:") and _is_opaque_identifier(value[10:]):
            return f"operation:{symbols.symbol(value[10:])}"
        return symbols.symbol(value) if _is_opaque_identifier(value) else value
    if isinstance(value, Mapping):
        return {
            key: _normalize(item, symbols)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if key not in VOLATILE_DATA_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item, symbols) for item in value]
    return value


def _as_payload(event: SimEvent | Mapping[str, Any]) -> dict[str, Any]:
    """SimEvent 或 events.jsonl 的一行 → 同一种 dict 形态。

    两条输入路径（内存事件流 / 磁盘工件）必须投影出同一份 trace，否则"可复现"只在
    进程内成立，而研究者手里拿到的是磁盘上那份。
    """

    if isinstance(event, SimEvent):
        return event.model_dump(mode="json")
    return dict(event)


def _sort_key(payload: Mapping[str, Any]) -> tuple[int, float]:
    """排序锚：seq 优先（总线唯一权威），缺 seq 的退回 timestamp。

    timestamp 是 tick 计数，同一拍内全部并列，单靠它排不出稳定顺序——所以 seq 才是
    canonical trace 的排序依据（见 SimEvent.seq 的字段注释）。
    """

    seq = payload.get("seq")
    return (0, float(seq)) if isinstance(seq, (int, float)) else (1, float(payload.get("timestamp") or 0.0))


def canonical_trace(
    events: Iterable[SimEvent | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把一段事件流投影成 canonical trace（list of dict，顺序即比对顺序）。"""

    payloads = sorted((_as_payload(event) for event in events), key=_sort_key)
    symbols = _SymbolTable()
    trace: list[dict[str, Any]] = []

    for index, payload in enumerate(payloads):
        entry: dict[str, Any] = {}
        for field in sorted(SimEvent.model_fields):
            if field in VOLATILE_EVENT_FIELDS:
                continue
            if field == "seq":
                # 重基：比的是顺序，不是起点号码
                entry["seq"] = index
                continue
            value = payload.get(field)
            if field in {"event_id", "correlation_id", "run_id"} and isinstance(value, str):
                entry[field] = symbols.symbol(value)
                continue
            if field == "causal_parent":
                entry[field] = symbols.symbol(value) if isinstance(value, str) and value else None
                continue
            entry[field] = _normalize(value, symbols)
        trace.append(entry)

    return trace


def canonical_trace_text(events: Iterable[SimEvent | Mapping[str, Any]]) -> str:
    """canonical trace 的**字节比对形态**：一行一条事件，行内键排序。

    刻意不做成单个哈希：门失败时要能 ``diff`` 出"从第几条事件开始分叉"，
    一个对不上的 sha256 只能告诉研究者"有问题"，没法告诉他问题在哪。
    """

    lines = [canonical_json(entry) for entry in canonical_trace(events)]
    return "\n".join(lines) + ("\n" if lines else "")


def trace_digest(events: Iterable[SimEvent | Mapping[str, Any]]) -> str:
    """canonical trace 的 sha256（写进报告/日志时用，比对本身仍走全文）。"""

    return hashlib.sha256(canonical_trace_text(events).encode("utf-8")).hexdigest()


def export_canonical_trace(run_id: str, *, root: Path | str | None = None) -> str:
    """从磁盘工件（``data/runs/{run_id}/events.jsonl``）导出 canonical trace。

    S4 的回放比对与 CI 的"两次运行一致"检查都走这条路：它读的是研究者真正拿得到的
    那份工件，而不是某个进程内还活着的对象。
    """

    return canonical_trace_text(iter_run_events(run_id, root=root))
