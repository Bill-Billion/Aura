"""Run 模型（spec §11 Reproducibility And Run Model）。

S2 之前本仿真没有 "run" 这个概念：事件流是一条永不结束的连续带，reset 只是把世界
换掉。于是研究者最先问的两个问题（§18）——"这批事件是哪个场景、哪个 seed 跑出来的"
"这条链属于哪次实验"——在代码里根本没有承载它们的对象。本模块补上那个对象：

  - :class:`RunMetadata` —— §11 九个必填字段的单一来源，可直接序列化进 S2-T7 的
    ``data/runs/{run_id}/run.json``；
  - :class:`RunManager` —— run 生命周期（start/current/end）与三件接线：
    ① 给 EventBus 换 run 上下文（此后每条事件自动盖 run_id/scenario_id 章）；
    ② 清 EventBus 历史（修审计发现③：1000 条环形历史 reset 从不清空，
       ``get_causal_chain`` 会把两个 run 的链焊在一起）；
    ③ 给本 run 建唯一 :class:`~backend.engine.rng.SimRandom`（一 run 一 seed）。
  - :func:`resolve_run_scenario` —— provenance 门（S2 review major-2）：写进元数据的
    scenario_id 必须指向场景库里真实存在的一份 ScenarioSpec，否则 run 干脆不许开。

**为什么 run 身份必须能被"判旧"**（:meth:`RunManager.is_stale`）：§2.2 不变式
"A mutation from an old ``run_id`` must not be applied to the active run"。取消在飞
episode（S0-5）是第一道防线，但它只覆盖"经过 cancel_active_episodes 的换 run 路径"；
场景连跑、S2-T6 的 headless runner 都可以在不取消任何任务的情况下换 run。没有 run 身份
门，那些路径下旧世界算出来的命令会安静地写进新 run，且事件流里毫无痕迹。

刻意的边界：
  - 本模块不写工件、不碰文件系统（S2-T7 的 EventLogWriter 负责），只产出元数据；
  - 不持有世界，只在 ``start_run`` 时对世界取一次指纹——RunMetadata 是不可变的实验身份，
    不是运行期状态镜像。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from backend.engine.event_bus import EventBus
from backend.engine.rng import SimRandom, validate_seed
from backend.engine.state import WorldState
from backend.scenarios.loader import ScenarioLoadError, load_library
from backend.scenarios.spec import ScenarioSpec

# §11 必填九字段。测试与 S2-T7 的 run.json 写入方都以本元组为准，
# 少一个字段就不是一份合法的 run 记录。
SPEC11_REQUIRED_FIELDS: tuple[str, ...] = (
    "run_id",
    "scenario_id",
    "seed",
    "started_at",
    "sim_version",
    "agent_versions",
    "llm_provider",
    "llm_model",
    "initial_state_hash",
)

# 「有东西被丢弃了」事件：event_type 是丢弃动作，data.reason 是丢弃原因，
# 合起来读作 ``discarded:stale_run``。刻意不为每种原因造一个事件类型——
# 前端只需订阅一个类型就能把所有"本该发生却被丢掉"的东西显示出来。
STALE_RUN_DISCARD_EVENT_TYPE = "system.discarded"
STALE_RUN_DISCARD_REASON = "stale_run"

# 起始世界指纹只覆盖"世界本身"：场景、环境、设备、房间、用户。
# 刻意排除：
#   - agents（provider/latency/last_action 是运行期诊断，且 llm_provider 已是 §11 独立字段，
#     不排除的话同一场景换个 provider 就变成"另一个起始世界"）；
#   - simulation_tick/speed/mode/wall_tick_ms/is_running（run 的运行参数，不是世界状态）。
_HASHED_WORLD_FIELDS = ("scene_id", "environment", "devices", "rooms", "users")

_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"
_UNKNOWN_VERSION = "0.0.0-unknown"


class RunProvenanceErrorCode(str, Enum):
    """run 出身校验失败词表（面向研究者：这个 run 为什么不许开）。"""

    SCENARIO_NOT_FOUND = "scenario_not_found"
    SCENARIO_LIBRARY_INVALID = "scenario_library_invalid"


class RunProvenanceError(Exception):
    """run 元数据会说谎，因此这个 run 不许开（§11 + §18 Q1）。

    S2 review major-2：``reset(scenario_id="随便写")`` 曾把任意字符串盖进 RunMetadata /
    run.json / 每条事件。对复现性工件而言，"字段在但是假的"比"字段缺失"危险得多——
    缺失时研究者知道自己不知道，撒谎时不知道。
    """

    def __init__(
        self,
        code: RunProvenanceErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": dict(self.details)}


def resolve_run_scenario(
    scenario: ScenarioSpec | str | None,
    *,
    dirs: Iterable[Path | str] | None = None,
) -> ScenarioSpec | None:
    """把"这个 run 跑的是哪个场景"解析成一份**真实存在**的 ScenarioSpec。

    - ``None``：匿名 run（交互式使用），元数据里 scenario_id 留空；
    - :class:`~backend.scenarios.spec.ScenarioSpec`：调用方已经加载/校验过，原样采信；
    - ``str``：必须在场景库里查得到，否则 :class:`RunProvenanceError`。

    为什么裸字符串必须过库：run.json 的 scenario_id 是 §18 Q1 的答案，它只有在能被
    重新加载、重新跑一遍时才有意义。查不到的 id 既复现不了，也证伪不了。
    """

    if scenario is None:
        return None
    if isinstance(scenario, ScenarioSpec):
        return scenario

    try:
        library = load_library(dirs)
    except ScenarioLoadError as exc:
        raise RunProvenanceError(
            RunProvenanceErrorCode.SCENARIO_LIBRARY_INVALID,
            f"场景库加载失败，无法核对 scenario_id {scenario!r}：{exc}",
            details={"scenario_id": scenario, **exc.to_dict()},
        ) from exc

    spec = library.get(scenario)
    if spec is None:
        raise RunProvenanceError(
            RunProvenanceErrorCode.SCENARIO_NOT_FOUND,
            f"场景 {scenario!r} 不在已加载的场景库中，拒绝把它写进 run 元数据",
            details={"scenario_id": scenario, "known_ids": sorted(library)},
        )
    return spec


class LLMMode(str, Enum):
    """§11.1 三种 LLM 决定性模式；每份 run 工件都必须标注用的是哪种。"""

    MOCKED = "mocked"
    RECORDED = "recorded"
    LIVE = "live"


@lru_cache(maxsize=1)
def read_sim_version() -> str:
    """仓库根 VERSION 即 sim_version（§11）：代码版本变了，复现承诺就得重新论证。"""

    try:
        version = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return _UNKNOWN_VERSION
    return version or _UNKNOWN_VERSION


def canonical_json(payload: Any) -> str:
    """确定性 JSON：排序键、无多余空白。指纹与工件写入共用同一套规矩。"""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_initial_state_hash(world: WorldState) -> str:
    """起始世界的 sha256 指纹（§11 initial_state_hash）。

    有它才能回答"两个 run 是不是从同一个世界出发的"——没有指纹时，"同 seed 同场景"
    的复现声明会被一次不起眼的 device_registry 改动静默证伪。
    """

    payload = world.model_dump(mode="json", include=set(_HASHED_WORLD_FIELDS))
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def resolve_llm_mode(provider: Any) -> LLMMode:
    """由 provider 推断 §11.1 模式（S3-T7 接入录制回放后会显式传 RECORDED）。

    鸭子类型而非 isinstance：本模块在 engine 层，不能反向依赖 backend.agents。
    """

    if provider is None:
        return LLMMode.MOCKED
    provider_name = str(getattr(provider, "provider_name", "") or "")
    if provider_name in {"", "unknown", "disabled"}:
        return LLMMode.MOCKED
    if not getattr(provider, "api_key", None):
        # 配了 provider 名但没有 key：实际走的是规则回退，不是 live 调用。
        return LLMMode.MOCKED
    return LLMMode.LIVE


def new_run_id() -> str:
    """``run-20260721T093012-4f3a9c21``：时间前缀让 data/runs/ 目录天然按时间排序。

    run_id 是墙钟派生的，因此**绝不能**进 S2-T9 的 canonical trace（与 event_id 同类）。
    """

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"run-{stamp}-{uuid.uuid4().hex[:8]}"


class RunMetadata(BaseModel):
    """一次仿真 run 的不可变身份（§11 九字段 + §11.1 模式 + 结束信息）。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    scenario_id: str | None = None
    seed: int
    started_at: str  # ISO-8601 UTC
    sim_version: str
    agent_versions: dict[str, str] = Field(default_factory=dict)
    llm_provider: str
    llm_model: str
    llm_mode: LLMMode = LLMMode.LIVE
    initial_state_hash: str
    ended_at: str | None = None
    end_reason: str | None = None

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


class RunManager:
    """run 生命周期的唯一持有者（SimulationEngine 持一台，非全局单例）。

    ``event_bus`` 可为 None（纯元数据用法 / 单测）；给了就由本类负责换上下文与清历史，
    调用方不必记得配对调用这两步——这正是审计里"reset 忘了清历史"那类缺陷的根因。
    """

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
        sim_version: str | None = None,
        max_finished: int = 50,
    ) -> None:
        self.event_bus = event_bus
        self.sim_version = sim_version or read_sim_version()
        self._current: RunMetadata | None = None
        self._rng: SimRandom | None = None
        self._finished: list[RunMetadata] = []
        self._max_finished = max_finished

    # --- 查询 --------------------------------------------------------------

    @property
    def current(self) -> RunMetadata | None:
        return self._current

    @property
    def run_id(self) -> str | None:
        return self._current.run_id if self._current is not None else None

    @property
    def scenario_id(self) -> str | None:
        return self._current.scenario_id if self._current is not None else None

    @property
    def rng(self) -> SimRandom | None:
        """本 run 的唯一随机源（一 run 一 seed，见 backend/engine/rng.py）。"""

        return self._rng

    @property
    def finished(self) -> list[RunMetadata]:
        """已结束的 run（进程内、有上限；持久化归 S2-T7 的工件）。"""

        return list(self._finished)

    def is_stale(self, run_id: str | None) -> bool:
        """*run_id* 是否属于一个已经不是当前 run 的旧 run（§2.2 stale 判定）。

        ``None`` 恒不算 stale：那是 S2 之前就存在的、根本没带 run_id 的调用方，
        对它们判 stale 等于凭空开始丢事件。
        """

        if run_id is None:
            return False
        return self.run_id != run_id

    # --- 生命周期 ----------------------------------------------------------

    def start_run(
        self,
        *,
        world: WorldState,
        scenario_id: str | None = None,
        seed: int | None = None,
        llm_provider: Any = None,
        llm_mode: LLMMode | str | None = None,
        agent_versions: Mapping[str, str] | None = None,
        run_id: str | None = None,
        clear_event_history: bool = True,
    ) -> RunMetadata:
        """开启新 run：结束上一 run → 记元数据 → 换总线上下文并清历史。

        ``seed=None`` 时自动生成一个并记录——§11 要求每个 run 都有 seed，
        "没设 seed" 不是一种合法状态，只是"没记下来"。
        """

        if self._current is not None:
            self.end_run("superseded")

        resolved_seed = SimRandom(None).seed if seed is None else validate_seed(seed)
        metadata = RunMetadata(
            run_id=run_id or new_run_id(),
            scenario_id=scenario_id,
            seed=resolved_seed,
            started_at=datetime.now(timezone.utc).isoformat(),
            sim_version=self.sim_version,
            agent_versions=dict(agent_versions or {}),
            llm_provider=str(getattr(llm_provider, "provider_name", "disabled") or "disabled"),
            llm_model=str(getattr(llm_provider, "model", "rule_based") or "rule_based"),
            llm_mode=LLMMode(llm_mode) if llm_mode is not None else resolve_llm_mode(llm_provider),
            initial_state_hash=compute_initial_state_hash(world),
        )

        self._current = metadata
        self._rng = SimRandom(resolved_seed)

        if self.event_bus is not None:
            self.event_bus.set_run_context(metadata.run_id, metadata.scenario_id)
            if clear_event_history:
                # 换 run 必须清历史：旧 run 的事件留在环形缓冲里会被
                # get_causal_chain / correlation 查询当成同一条链的一部分。
                self.event_bus.clear()

        return metadata

    def end_run(self, reason: str = "completed") -> RunMetadata | None:
        """结束当前 run；返回被结束的元数据（没有活跃 run 则 None）。

        结束后 :attr:`run_id` 为 None，于是**任何**带 run_id 的在飞产物都判 stale——
        run 已经结束还往里写，与写进另一个 run 一样错。
        """

        metadata = self._current
        if metadata is None:
            return None

        metadata.ended_at = datetime.now(timezone.utc).isoformat()
        metadata.end_reason = reason
        self._finished.append(metadata)
        if len(self._finished) > self._max_finished:
            self._finished = self._finished[-self._max_finished :]
        self._current = None
        self._rng = None
        return metadata
