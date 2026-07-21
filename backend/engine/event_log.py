"""每 run 一份磁盘工件：``data/runs/{run_id}/``（spec §11 / §18 Q1）。

S2 之前，一次仿真跑完之后剩下的全部"记录"是 EventBus 里那 1000 条环形历史：
进程一停就没了，超过 1000 条开头就被挤掉，而且没有任何 REST/WS 端点能把它取出来。
研究者因此无法引用、无法回放、也无法归属一段事件流——可复现性缺的不只是 seed，
还有一份能被别人打开的文件。

**目录布局（跨阶段契约，不要改）**::

    data/runs/{run_id}/
        run.json            §11 九字段 + §11.1 llm_mode + ended_at/end_reason
        events.jsonl        本 run 的全部事件，一行一条，按 seq 升序
        llm_recordings.jsonl   S3 写在同一目录；S4 的回放两份一起读

写侧的三条纪律：

1. **按 seq 重排**。EventBus 先派发精确类型订阅者、后派发 wildcard，而本模块挂的正是
   wildcard——于是 "tick 的孩子先到、tick 自己后到" 是默认行为。写侧因此缓冲乱序到达的
   事件，只在序号连续时落盘（:class:`EventLogWriter`）。工件乱序的话，S4 的回放与 §18
   的因果链查询读到的是一条假历史。
2. **run 切换由 RunManager 驱动，而不是由调用方记得调**。
   :class:`RunArtifactRecorder` 包住 ``start_run``/``end_run`` 两个方法本身，因此无论
   是引擎 reset、场景 runner 直接 ``run_manager.end_run("completed")``、还是 start_run
   顺手把上一 run 顶成 ``superseded``，工件都会在同一时刻收尾。
3. **写失败要说话**。磁盘写不进去时降级为"停止记录 + 报错日志 + 置 failure 标记"，
   绝不静默丢事件（这正是 S1 在命令层修掉的那一类缺陷）。
4. **run_id 的形状在 :func:`run_dir` 这一个拼接点上校验**（:data:`RUN_ID_PATTERN`）。
   路由层的路径归一化挡得住 ``GET /api/runs/../../etc``，但 S4 的套件跑批与任何从
   manifest 里读 run_id 的 CLI 都不经过路由。

读侧（:func:`read_run_events` 等）是 S4 报告与 S5 面板的唯一入口，
``backend/api/routes.py`` 的 ``/api/runs*`` 只是它的一层薄 HTTP 投影。
"""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, TYPE_CHECKING

from backend.core.logging import log
from backend.engine.event_bus import SimEvent
from backend.engine.run_manager import RunMetadata, canonical_json

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免运行期循环导入
    from backend.engine.run_manager import RunManager

EVENTS_FILENAME = "events.jsonl"
RUN_METADATA_FILENAME = "run.json"
# S3 的 LLM 录制工件与 events.jsonl 同目录（critic 定死的路径），此处声明是为了让
# "同一个 run 目录下有哪些工件" 这件事只有一处说法。
LLM_RECORDINGS_FILENAME = "llm_recordings.jsonl"

# 仓库根锚定的绝对路径：uvicorn 的工作目录随启动方式漂移，用相对路径会让同一台机器上
# 的两次启动把工件写进两个地方。
DEFAULT_RUNS_ROOT = Path(__file__).resolve().parents[2] / "data" / "runs"
RUNS_ROOT_ENV = "AURA_RUNS_ROOT"
ARTIFACTS_ENABLED_ENV = "AURA_RUN_ARTIFACTS"
_DISABLED_VALUES = {"0", "false", "off", "no"}

_runs_root_override: Path | None = None


# ---------------------------------------------------------------------------
# 根目录与开关
# ---------------------------------------------------------------------------


def configure_runs_root(root: Path | str | None) -> None:
    """覆盖工件根目录；传 None 恢复默认（env > DEFAULT_RUNS_ROOT）。测试与 CLI 用。"""

    global _runs_root_override
    _runs_root_override = None if root is None else Path(root)


def runs_root() -> Path:
    if _runs_root_override is not None:
        return _runs_root_override
    env_value = os.environ.get(RUNS_ROOT_ENV)
    if env_value:
        return Path(env_value)
    return DEFAULT_RUNS_ROOT


def run_dir(run_id: str, *, root: Path | str | None = None) -> Path:
    """``{runs_root}/{run_id}``；run_id 必须是 RunManager 造的形状。

    校验放在这里而不是放在 HTTP 层：今天 ``GET /api/runs/../../etc`` 打不进来靠的是
    Starlette 的路径归一化，而 S4 的套件跑批与任何从 manifest 里读 run_id 的 CLI
    都不经过路由。工件根是被写、也被读的目录，形状在唯一的拼接点上挡住最省事。
    ``validate_run_id`` 定义在下面的错误段（同模块内延迟解析）。
    """

    validate_run_id(run_id)
    base = Path(root) if root is not None else runs_root()
    return base / run_id


def artifacts_enabled() -> bool:
    """``AURA_RUN_ARTIFACTS=0`` 关掉写盘（长批跑不想被磁盘绑架时）。"""

    return os.environ.get(ARTIFACTS_ENABLED_ENV, "1").strip().lower() not in _DISABLED_VALUES


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class RunArtifactErrorCode(str, Enum):
    run_not_found = "run_not_found"
    corrupt_event_log = "corrupt_event_log"
    corrupt_run_metadata = "corrupt_run_metadata"


class RunArtifactError(Exception):
    """读工件失败。坏工件是研究者要修的数据问题，必须带 code/path/details 透出去。"""

    def __init__(
        self,
        code: RunArtifactErrorCode,
        message: str,
        *,
        run_id: str | None = None,
        path: Path | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.run_id = run_id
        self.path = path
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "run_id": self.run_id,
            "path": str(self.path) if self.path is not None else None,
            "details": self.details,
        }


# ``run-20260721T093012-4f3a9c21``，即 run_manager.new_run_id() 的唯一产物形状。
# 形状之外的一切（``../``、绝对路径、旧格式目录名）都不是本仓库造出来的 run。
# 用 \Z 而非 $：$ 也匹配末尾换行前的位置，"run-…-0d8961ba\n" 会被放行并生成带换行的路径。
RUN_ID_PATTERN = r"^run-\d{8}T\d{6}-[0-9a-f]{8}\Z"
_RUN_ID_RE = re.compile(RUN_ID_PATTERN)


def validate_run_id(run_id: Any) -> str:
    """形状不对就是"没有这个 run"——与"目录不存在"同一个 code，调用方只需处理一种错误。"""

    if isinstance(run_id, str) and _RUN_ID_RE.match(run_id):
        return run_id
    raise RunArtifactError(
        RunArtifactErrorCode.run_not_found,
        f"run id {run_id!r} 不是合法的 run 标识",
        run_id=run_id if isinstance(run_id, str) else None,
        details={"expected": RUN_ID_PATTERN},
    )


# ---------------------------------------------------------------------------
# 写侧
# ---------------------------------------------------------------------------


class EventLogWriter:
    """一个 run 的工件写入器（一实例一目录，不复用）。

    ``start_seq`` 是本 run 第一条事件的期望 seq：换 run 时 EventBus 会清历史并把 seq
    归零，但引擎启动那次 run 刻意不清（``clear_event_history=False``），此时总线的
    ``next_seq`` 未必是 0。写侧不猜，直接由调用方把当前值传进来。
    """

    def __init__(
        self,
        metadata: RunMetadata,
        *,
        root: Path | str | None = None,
        start_seq: int = 0,
    ) -> None:
        self.metadata = metadata
        self.directory = run_dir(metadata.run_id, root=root)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.directory / EVENTS_FILENAME
        self.metadata_path = self.directory / RUN_METADATA_FILENAME
        # 行缓冲：读侧（/api/runs/{id}/events）可能在 run 还没结束时就来读。
        self._handle = self.events_path.open("a", encoding="utf-8", buffering=1)
        self._expected_seq = int(start_seq)
        self._buffer: dict[int, str] = {}
        self._written = 0
        self._closed = False
        self.failure: str | None = None
        self.write_metadata()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def written(self) -> int:
        return self._written

    @property
    def pending(self) -> int:
        """已收下但因序号断档还没落盘的事件数（正常收尾时必须为 0）。"""

        return len(self._buffer)

    def write_metadata(self) -> None:
        """把 RunMetadata 当前状态覆盖写进 run.json。

        RunMetadata 是可变对象且 ``end_run`` 原地补 ended_at/end_reason，所以收尾时
        重写一次就能拿到完整记录，不需要写侧自己跟踪 run 生命周期。
        """

        payload = self.metadata.model_dump(mode="json")
        try:
            self.metadata_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - 磁盘故障路径
            self._fail("run_metadata", exc)

    def append(self, event: SimEvent) -> bool:
        """收下一条事件；返回是否被本写入器接受（已关闭/已故障则 False）。"""

        if self._closed or self.failure is not None:
            return False

        line = canonical_json(event.model_dump(mode="json"))
        seq = event.seq
        if seq is None or seq < self._expected_seq:
            # 没有 seq（未经总线）或迟到的旧序号：立刻落盘。宁可错序也绝不丢——
            # 工件里少一条事件是无法被发现的谎，顺序异常至少还能从 seq 字段看出来。
            self._write_line(line)
            return True

        self._buffer[seq] = line
        while self._expected_seq in self._buffer:
            self._write_line(self._buffer.pop(self._expected_seq))
            self._expected_seq += 1
        return True

    def close(self) -> None:
        """冲掉缓冲、补写 run.json、关文件。可重复调用。"""

        if self._closed:
            return
        for seq in sorted(self._buffer):
            self._write_line(self._buffer[seq])
        self._buffer.clear()
        self._closed = True
        self.write_metadata()
        try:
            self._handle.close()
        except OSError:  # pragma: no cover - 磁盘故障路径
            pass

    # --- internal ----------------------------------------------------------

    def _write_line(self, line: str) -> None:
        try:
            self._handle.write(line + "\n")
        except OSError as exc:  # pragma: no cover - 磁盘故障路径
            self._fail("events", exc)
            return
        self._written += 1

    def _fail(self, artifact: str, exc: BaseException) -> None:
        self.failure = f"{artifact}: {exc}"
        log.error(
            "run_artifact_write_failed",
            run_id=self.metadata.run_id,
            artifact=artifact,
            error=str(exc),
        )


class RunArtifactRecorder:
    """把 :class:`~backend.engine.run_manager.RunManager` 的 run 生命周期接到磁盘上。

    刻意包住 ``start_run``/``end_run`` 这两个**方法本身**，而不是要求每个调用方
    在换 run 时记得配一次写盘：场景 runner 直接调 ``engine.run_manager.end_run()``，
    引擎 reset 走 ``_start_run``，``start_run`` 自己还会把上一 run 顶成 superseded——
    三条路径任意一条漏配，工件就永远停在"还在跑"的状态。
    """

    def __init__(
        self,
        run_manager: "RunManager",
        *,
        root: Path | str | None = None,
        enabled: bool | None = None,
    ) -> None:
        self.run_manager = run_manager
        self._root = Path(root) if root is not None else None
        self._enabled_override = enabled
        self.writer: EventLogWriter | None = None
        self._attached = False

    @property
    def enabled(self) -> bool:
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return artifacts_enabled()

    @property
    def directory(self) -> Path | None:
        return self.writer.directory if self.writer is not None else None

    def attach(self) -> "RunArtifactRecorder":
        if self._attached:
            return self

        inner_start = self.run_manager.start_run
        inner_end = self.run_manager.end_run

        def start_run(**kwargs: Any) -> RunMetadata:
            # inner_start 内部会先 end_run 掉上一 run —— 那次调用命中的是下面这个包装，
            # 于是旧工件在新工件开出来之前就已经收好尾。
            metadata = inner_start(**kwargs)
            self._open(metadata)
            return metadata

        def end_run(reason: str = "completed") -> RunMetadata | None:
            metadata = inner_end(reason)
            if metadata is not None:
                self._close_writer()
            return metadata

        self.run_manager.start_run = start_run  # type: ignore[method-assign]
        self.run_manager.end_run = end_run  # type: ignore[method-assign]
        self._attached = True

        # 挂上来的时候可能已经有活跃 run（引擎先开 run 再挂 recorder）：补开一份工件。
        if self.run_manager.current is not None:
            self._open(self.run_manager.current)
        return self

    def record(self, event: SimEvent) -> None:
        if self.writer is None:
            return
        self.writer.append(event)

    def close(self) -> None:
        self._close_writer()

    # --- internal ----------------------------------------------------------

    def _open(self, metadata: RunMetadata) -> None:
        self._close_writer()
        if not self.enabled:
            return
        bus = getattr(self.run_manager, "event_bus", None)
        start_seq = int(getattr(bus, "next_seq", 0) or 0) if bus is not None else 0
        try:
            self.writer = EventLogWriter(metadata, root=self._root, start_seq=start_seq)
        except OSError as exc:
            self.writer = None
            log.error("run_artifact_open_failed", run_id=metadata.run_id, error=str(exc))

    def _close_writer(self) -> None:
        if self.writer is None:
            return
        self.writer.close()
        self.writer = None


def attach_run_artifacts(
    run_manager: "RunManager",
    *,
    root: Path | str | None = None,
    enabled: bool | None = None,
) -> RunArtifactRecorder:
    """给 *run_manager* 挂一台 recorder 并返回它（SimulationEngine 的接线口）。"""

    return RunArtifactRecorder(run_manager, root=root, enabled=enabled).attach()


# ---------------------------------------------------------------------------
# 读侧
# ---------------------------------------------------------------------------


def list_run_ids(*, root: Path | str | None = None) -> list[str]:
    """名字合形状、且有 run.json 的目录才算一个 run（半截目录不冒充实验记录）。

    形状过滤是 :func:`run_dir` 校验的对偶：列表里若混进一个外来目录名，
    ``load_run_summaries`` 转头拿它去 :func:`read_run_metadata` 就会整份 /api/runs 报错。
    """

    base = Path(root) if root is not None else runs_root()
    if not base.is_dir():
        return []
    return sorted(
        entry.name
        for entry in base.iterdir()
        if entry.is_dir()
        and _RUN_ID_RE.match(entry.name)
        and (entry / RUN_METADATA_FILENAME).is_file()
    )


def _require_run_dir(run_id: str, *, root: Path | str | None = None) -> Path:
    directory = run_dir(run_id, root=root)
    if not directory.is_dir():
        raise RunArtifactError(
            RunArtifactErrorCode.run_not_found,
            f"run {run_id!r} 没有工件目录",
            run_id=run_id,
            path=directory,
        )
    return directory


def read_run_metadata(run_id: str, *, root: Path | str | None = None) -> dict[str, Any]:
    directory = _require_run_dir(run_id, root=root)
    path = directory / RUN_METADATA_FILENAME
    if not path.is_file():
        raise RunArtifactError(
            RunArtifactErrorCode.run_not_found,
            f"run {run_id!r} 缺少 {RUN_METADATA_FILENAME}",
            run_id=run_id,
            path=path,
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunArtifactError(
            RunArtifactErrorCode.corrupt_run_metadata,
            f"run {run_id!r} 的 {RUN_METADATA_FILENAME} 无法解析: {exc}",
            run_id=run_id,
            path=path,
            details={"error": str(exc)},
        ) from exc


def list_run_artifacts(run_id: str, *, root: Path | str | None = None) -> list[str]:
    """本 run 目录下的工件文件名（S3 的 llm_recordings.jsonl 会自动出现在这里）。"""

    directory = _require_run_dir(run_id, root=root)
    return sorted(entry.name for entry in directory.iterdir() if entry.is_file())


def iter_run_events(
    run_id: str, *, root: Path | str | None = None
) -> Iterator[dict[str, Any]]:
    directory = _require_run_dir(run_id, root=root)
    path = directory / EVENTS_FILENAME
    if not path.is_file():
        # run 开了但一条事件都没发：空流是合法结果，不是错误。
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError as exc:
                # 坏行不跳过：静默跳过会让"少了三条事件"的工件看起来完好无损。
                raise RunArtifactError(
                    RunArtifactErrorCode.corrupt_event_log,
                    f"run {run_id!r} 的 {EVENTS_FILENAME} 第 {line_number} 行无法解析: {exc}",
                    run_id=run_id,
                    path=path,
                    details={"line": line_number, "error": str(exc)},
                ) from exc


def count_run_events(run_id: str, *, root: Path | str | None = None) -> int:
    directory = _require_run_dir(run_id, root=root)
    path = directory / EVENTS_FILENAME
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def read_run_events(
    run_id: str,
    *,
    correlation_id: str | None = None,
    event_type: str | None = None,
    generation_mode: str | None = None,
    causal_parent: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    root: Path | str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """读并过滤一个 run 的事件，返回 ``(页内事件, 过滤后总数)``。

    分页在过滤**之后**：``total`` 回答"符合条件的一共多少条"，前端据此决定还要不要翻页。
    """

    matched: list[dict[str, Any]] = []
    for event in iter_run_events(run_id, root=root):
        if correlation_id is not None and event.get("correlation_id") != correlation_id:
            continue
        if event_type is not None and event.get("event_type") != event_type:
            continue
        if generation_mode is not None and event.get("event_generation_mode") != generation_mode:
            continue
        if causal_parent is not None and event.get("causal_parent") != causal_parent:
            continue
        matched.append(event)

    total = len(matched)
    start = max(0, int(offset))
    end = total if limit is None else start + max(0, int(limit))
    return matched[start:end], total


def load_run_summaries(
    *, root: Path | str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """/api/runs 的列表投影：§11 元数据 + 事件条数，新的在前。

    ``started_at`` 是 ISO-8601 UTC，字典序即时间序；同刻再用 run_id 兜底，
    保证同一批工件在任何机器上排出同一个顺序。
    """

    summaries: list[dict[str, Any]] = []
    for run_id in list_run_ids(root=root):
        metadata = read_run_metadata(run_id, root=root)
        metadata["event_count"] = count_run_events(run_id, root=root)
        summaries.append(metadata)

    summaries.sort(
        key=lambda item: (str(item.get("started_at") or ""), str(item.get("run_id") or "")),
        reverse=True,
    )
    if limit is not None:
        summaries = summaries[: max(0, int(limit))]
    return summaries
