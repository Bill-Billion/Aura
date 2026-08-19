from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from backend.api.access_control import ResearchAccessError, authorize_run_launch
from backend.engine.event_log import (
    RunArtifactError,
    RunArtifactErrorCode,
    count_run_events,
    list_run_artifacts,
    list_run_ids,
    load_run_summaries,
    read_run_events,
    read_run_metadata,
    read_verified_event_log_bytes,
)
from backend.engine.rng import MAX_JSON_SAFE_SEED
from backend.engine.run_manager import LLMMode
from backend.models.schemas import (
    BaselinePolicy,
    RunScenarioPayload,
    ScenarioLaunchError,
    ScenarioLaunchErrorCode,
)
from backend.scenarios.loader import (
    DEFAULT_LIBRARY_DIRS,
    ScenarioLoadError,
    load_library,
)
from backend.scenarios.trace import export_canonical_trace

# 场景启动器：由 backend.main 在装配期注入（routes 不能反向 import main）。
# 形参与 WS CMD_RUN_SCENARIO 完全一致，两条入口共用同一条实现。
ScenarioLauncher = Callable[[RunScenarioPayload], Awaitable[dict[str, Any]]]

router = APIRouter()
_health_provider: Callable[[], dict[str, Any]] | None = None
_scenario_launcher: ScenarioLauncher | None = None
# None = 用 loader 的默认库目录。S3/S4 追加 eval/failures/suites 目录时在此注入，
# 不要在端点内硬编码路径（loader 的多目录契约见 backend/scenarios/loader.py）。
_scenario_dirs: list[Path] | None = None


def configure_health_provider(provider: Callable[[], dict[str, Any]]) -> None:
    global _health_provider
    _health_provider = provider


def configure_scenario_launcher(launcher: ScenarioLauncher | None) -> None:
    """注入 ``POST /api/runs`` 的场景启动实现（backend.main.start_scenario_run）。"""
    global _scenario_launcher
    _scenario_launcher = launcher


def configure_scenario_dirs(dirs: Iterable[Path | str] | None) -> None:
    """覆盖 /api/scenarios 的扫描目录；传 None 恢复默认库目录。"""
    global _scenario_dirs
    _scenario_dirs = None if dirs is None else [Path(d) for d in dirs]


def get_scenario_dirs() -> list[Path]:
    return list(_scenario_dirs) if _scenario_dirs is not None else list(DEFAULT_LIBRARY_DIRS)


def _load_scenarios():
    try:
        return load_library(get_scenario_dirs())
    except ScenarioLoadError as exc:
        # 场景库坏了是研究者要修的数据问题，必须原样把 code/path/details 透出去，
        # 不能退化成一句 "Internal Server Error"。
        raise HTTPException(status_code=500, detail=exc.to_dict()) from exc


@router.get("/api/scenes")
async def get_scenes():
    return {
        "scenes": [
            {"id": "apartment_v1", "name": "测试公寓"},
        ]
    }


@router.get("/api/scenarios")
async def get_scenarios():
    """§5.1 场景枚举。投影不含 ground_truth（§2.3 ground truth 不随枚举外泄）。"""
    library = _load_scenarios()
    scenarios = [spec.summary() for spec in library.values()]
    return {"count": len(scenarios), "scenarios": scenarios}


@router.get("/api/scenarios/{scenario_id}")
async def get_scenario(scenario_id: str):
    """单个场景全文（含 §5.3 ground truth）——面向研究者与评估工具。"""
    library = _load_scenarios()
    spec = library.get(scenario_id)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "scenario_not_found",
                "message": f"场景 {scenario_id!r} 不在已加载的场景库中",
                "known_ids": sorted(library),
            },
        )
    return {"scenario": spec.model_dump()}


# ---------------------------------------------------------------------------
# /api/runs*（S2-T7 读侧 + S2 review major-1 的 POST 启动入口）
#
# 本文件是这四个端点的**唯一**所有者。S4 之后会在同一前缀下加
# GET /api/runs/{run_id}/report（评估报告），S5 只消费、不重建。
#   POST /api/runs                  按场景开一个 run（写侧，实现由 main 注入）
#   GET  /api/runs                  §11 元数据列表
#   GET  /api/runs/{run_id}         单 run 元数据 + 事件条数 + 工件清单
#   GET  /api/runs/{run_id}/events  本 run 的事件流
#   GET  /api/runs/{run_id}/report  §18 评估报告（S4-T5，S5 对比视图消费）
# ---------------------------------------------------------------------------


def _run_artifact_error(exc: RunArtifactError) -> HTTPException:
    """不存在＝404，旧且未封口＝422，已封口但损坏＝500。"""

    if exc.code is RunArtifactErrorCode.run_not_found:
        status = 404
    elif exc.code is RunArtifactErrorCode.unsupported_run_artifact:
        status = 422
    else:
        status = 500
    detail = exc.to_dict()
    if status == 404:
        # 打错 run_id 是最常见的用法错误，直接把可选项列出来（与 /api/scenarios 同口径）。
        detail["known_ids"] = list_run_ids()
    return HTTPException(status_code=status, detail=detail)


def _reject_invalid_research_artifact(metadata: dict[str, Any], run_id: str) -> None:
    """Invalid traces remain readable as JSON for diagnosis, never as evidence."""

    artifact_error = metadata.get("artifact_error")
    if artifact_error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "run_artifact_invalid",
                "message": "run 工件写入失败，不能作为稳定 trace 或评估依据",
                "details": {"run_id": run_id, "artifact_error": str(artifact_error)},
            },
        )


# 启动失败码 → HTTP 状态。启动期词表与 §10.2 命令失败码正交（见 models/schemas.py）。
_LAUNCH_ERROR_STATUS: dict[ScenarioLaunchErrorCode, int] = {
    ScenarioLaunchErrorCode.SCENARIO_NOT_FOUND: 404,
    ScenarioLaunchErrorCode.SCENARIO_LIBRARY_INVALID: 500,
    ScenarioLaunchErrorCode.INITIAL_STATE_INVALID: 400,
    ScenarioLaunchErrorCode.INVALID_SEED: 400,
    ScenarioLaunchErrorCode.ENGINE_UNAVAILABLE: 503,
    ScenarioLaunchErrorCode.RUN_ALREADY_ACTIVE: 409,
    ScenarioLaunchErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ScenarioLaunchErrorCode.BASELINE_POLICY_UNAVAILABLE: 503,
    ScenarioLaunchErrorCode.RECORDING_SOURCE_NOT_FOUND: 404,
    ScenarioLaunchErrorCode.RECORDING_SOURCE_NOT_FINALIZED: 409,
    ScenarioLaunchErrorCode.RECORDING_SOURCE_MISMATCH: 409,
    ScenarioLaunchErrorCode.RECORDING_SOURCE_INVALID: 422,
}


@router.post("/api/runs", status_code=201)
async def post_run(payload: RunScenarioPayload, request: Request):
    """按场景在**当前进程的仿真引擎**上开一个新 run（S2 review major-1）。

    实现由 backend.main 注入（``configure_scenario_launcher``），与 WS ``CMD_RUN_SCENARIO``
    是同一条路径：换 run、取消在飞 episode/命令、摆 initial_state、装 §4.5 三条产线全在
    ``SimulationEngine.reset`` 里，本端点只负责 HTTP 形状与错误码映射。
    停止/重置走既有的 WS ``CMD_SIM_PAUSE`` / ``CMD_SIM_RESET``，此处不另开取消入口。
    """

    try:
        authorize_run_launch(
            payload,
            headers=request.headers,
            client_host=request.client.host if request.client is not None else None,
        )
    except ResearchAccessError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message, "details": exc.details},
        ) from exc

    if _scenario_launcher is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": ScenarioLaunchErrorCode.ENGINE_UNAVAILABLE.value,
                "message": "仿真引擎尚未启动，无法运行场景",
                "details": {"scenario_id": payload.scenario_id},
            },
        )
    try:
        run = await _scenario_launcher(payload)
    except ScenarioLaunchError as exc:
        raise HTTPException(
            status_code=_LAUNCH_ERROR_STATUS.get(exc.code, 400), detail=exc.to_dict()
        ) from exc
    return {"run": run}


@router.get("/api/runs")
async def get_runs(
    limit: int = Query(default=50, ge=1, le=500),
    scenario_id: str | None = None,
    seed: int | None = Query(default=None, ge=0, le=MAX_JSON_SAFE_SEED),
    baseline_policy: BaselinePolicy | None = None,
    llm_mode: LLMMode | None = None,
    finalized: bool | None = None,
):
    """§11 run 元数据列表，新的在前。工件目录即数据源——无进程内缓存。"""
    try:
        runs = load_run_summaries()
    except RunArtifactError as exc:
        raise _run_artifact_error(exc) from exc
    for item in runs:
        item["finalized"] = item.get("ended_at") is not None
    if scenario_id is not None:
        runs = [item for item in runs if item.get("scenario_id") == scenario_id]
    if seed is not None:
        runs = [item for item in runs if item.get("seed") == seed]
    if baseline_policy is not None:
        runs = [
            item for item in runs if item.get("baseline_policy") == baseline_policy.value
        ]
    if llm_mode is not None:
        runs = [item for item in runs if item.get("llm_mode") == llm_mode.value]
    if finalized is not None:
        runs = [
            item for item in runs if (item.get("ended_at") is not None) is finalized
        ]
    runs = runs[:limit]
    return {"count": len(runs), "runs": runs}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """单个 run 的 §11 元数据 + 事件条数 + 同目录工件清单。"""
    try:
        metadata = read_run_metadata(run_id)
        metadata["finalized"] = metadata.get("ended_at") is not None
        return {
            "run": metadata,
            "event_count": count_run_events(run_id),
            # S3 的 llm_recordings.jsonl 与 events.jsonl 同目录，S4 的回放两份一起读；
            # 清单自描述，调用方不必猜哪些工件已经生成。
            "artifacts": list_run_artifacts(run_id),
        }
    except RunArtifactError as exc:
        raise _run_artifact_error(exc) from exc


@router.get("/api/runs/{run_id}/events")
async def get_run_events(
    run_id: str,
    correlation_id: str | None = None,
    event_type: str | None = None,
    generation_mode: str | None = None,
    causal_parent: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int | None = Query(default=None, ge=1, le=5000),
    trace_format: Literal["json", "raw", "canonical"] = Query(
        default="json", alias="format"
    ),
):
    """本 run 的事件流（seq 序）。``correlation_id`` 过滤即 §18 的整条因果链查询。"""
    try:
        metadata = read_run_metadata(run_id)
        if trace_format != "json":
            has_projection = any(
                value is not None
                for value in (
                    correlation_id,
                    event_type,
                    generation_mode,
                    causal_parent,
                    limit,
                )
            ) or offset != 0
            if has_projection:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "trace_export_must_be_complete",
                        "message": "raw/canonical trace 导出不接受过滤或分页参数",
                        "details": {"run_id": run_id, "format": trace_format},
                    },
                )
            if metadata.get("ended_at") is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "run_not_finalized",
                        "message": "run 尚未 finalized，不能导出稳定 trace",
                        "details": {"run_id": run_id},
                    },
                )
            _reject_invalid_research_artifact(metadata, run_id)
            if trace_format == "canonical":
                content: str | bytes = export_canonical_trace(run_id)
            else:
                content = read_verified_event_log_bytes(run_id, metadata=metadata)
            filename = f"{run_id}.{trace_format}.jsonl"
            event_count = count_run_events(run_id)
            return Response(
                content=content,
                media_type="application/x-ndjson",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "X-Run-Id": run_id,
                    "X-Trace-Format": trace_format,
                    "X-Trace-Event-Count": str(event_count),
                },
            )
        events, total = read_run_events(
            run_id,
            correlation_id=correlation_id,
            event_type=event_type,
            generation_mode=generation_mode,
            causal_parent=causal_parent,
            offset=offset,
            limit=limit,
        )
    except RunArtifactError as exc:
        raise _run_artifact_error(exc) from exc
    return {
        "run_id": run_id,
        "count": len(events),
        "total": total,
        "offset": offset,
        "events": events,
    }


@router.get("/api/health")
async def health_check():
    if _health_provider is None:
        return {"status": "ok"}
    return _health_provider()


@router.get("/api/runs/{run_id}/report")
async def get_run_report(run_id: str):
    """§18 评估报告（S4-T5）。S5 对比视图只消费这个端点，不自造指标。

    返回七指标 + success_criteria 判定 + §18 九问的可答字段。
    如果 run 还带有 scenario 元数据，同时返回场景级 success_criteria 与 ground_truth。
    """

    from backend.evaluation.evaluator import EvalOutcome, evaluate_run
    from backend.scenarios.loader import get_scenario

    try:
        metadata = read_run_metadata(run_id)
        if metadata.get("ended_at") is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_not_finalized",
                    "message": "run 尚未 finalized，评估报告不可用",
                    "details": {"run_id": run_id},
                },
            )
        # 先由带结构化错误码的工件读侧校验事件文件；Evaluator 的 ERROR 是领域结果，
        # 路由不再靠英文 failure_reasons substring 猜 404/500。
        read_run_events(run_id)
    except RunArtifactError as exc:
        raise _run_artifact_error(exc) from exc

    try:
        report = evaluate_run(run_id, scenario_dirs=get_scenario_dirs())
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "eval_error",
                "message": f"评估失败：{exc}",
                "details": {"run_id": run_id},
            },
        ) from exc

    if report.outcome == EvalOutcome.ERROR:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "evaluation_input_invalid",
                "message": report.failure_reasons[0] if report.failure_reasons else "评估失败",
                "details": {
                    "run_id": run_id,
                    "failure_reasons": list(report.failure_reasons),
                },
            },
        )

    # 如果 run 有 scenario_id，附上场景级信息（§18 九问中的 scenario 相关字段）
    scenario_info = None
    if report.scenario_id:
        try:
            spec = get_scenario(report.scenario_id, dirs=get_scenario_dirs())
        except ScenarioLoadError as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "scenario_library_invalid",
                    "message": f"评估场景库加载失败：{exc}",
                    "details": exc.to_dict(),
                },
            ) from exc
        if spec is None:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "evaluation_scenario_not_found",
                    "message": "run 元数据引用的场景在当前场景库中不存在",
                    "details": {
                        "run_id": run_id,
                        "scenario_id": report.scenario_id,
                    },
                },
            )
        scenario_info = {
            "name": spec.name,
            "description": spec.description,
            "involved_agents": spec.involved_agents,
            "success_criteria": spec.success_criteria.model_dump(),
            "expected_device_effects_count": len(spec.expected_device_effects),
            "expected_failures": [
                {"category": f.category, "device_id": f.device_id, "error_code": f.error_code}
                for f in (spec.expected_failures or [])
            ],
        }
        if spec.ground_truth:
            scenario_info["ground_truth"] = spec.ground_truth.model_dump()

    return {
        **report.to_dict(),
        "scenario": scenario_info,
    }
