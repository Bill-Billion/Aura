from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from backend.engine.event_log import (
    RunArtifactError,
    RunArtifactErrorCode,
    count_run_events,
    list_run_artifacts,
    list_run_ids,
    load_run_summaries,
    read_run_events,
    read_run_metadata,
)
from backend.models.schemas import (
    RunScenarioPayload,
    ScenarioLaunchError,
    ScenarioLaunchErrorCode,
)
from backend.scenarios.loader import (
    DEFAULT_LIBRARY_DIRS,
    ScenarioLoadError,
    load_library,
)

# 场景启动器：由 backend.main 在装配期注入（routes 不能反向 import main）。
# 形参与 WS CMD_RUN_SCENARIO 完全一致，两条入口共用同一条实现。
ScenarioLauncher = Callable[[str, int | None], Awaitable[dict[str, Any]]]

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
    """run 不存在＝404，工件坏了＝500；两种都原样带上 code/details。"""

    status = 404 if exc.code is RunArtifactErrorCode.run_not_found else 500
    detail = exc.to_dict()
    if status == 404:
        # 打错 run_id 是最常见的用法错误，直接把可选项列出来（与 /api/scenarios 同口径）。
        detail["known_ids"] = list_run_ids()
    return HTTPException(status_code=status, detail=detail)


# 启动失败码 → HTTP 状态。启动期词表与 §10.2 命令失败码正交（见 models/schemas.py）。
_LAUNCH_ERROR_STATUS: dict[ScenarioLaunchErrorCode, int] = {
    ScenarioLaunchErrorCode.SCENARIO_NOT_FOUND: 404,
    ScenarioLaunchErrorCode.SCENARIO_LIBRARY_INVALID: 500,
    ScenarioLaunchErrorCode.INITIAL_STATE_INVALID: 400,
    ScenarioLaunchErrorCode.INVALID_SEED: 400,
    ScenarioLaunchErrorCode.ENGINE_UNAVAILABLE: 503,
}


@router.post("/api/runs", status_code=201)
async def post_run(payload: RunScenarioPayload):
    """按场景在**当前进程的仿真引擎**上开一个新 run（S2 review major-1）。

    实现由 backend.main 注入（``configure_scenario_launcher``），与 WS ``CMD_RUN_SCENARIO``
    是同一条路径：换 run、取消在飞 episode/命令、摆 initial_state、装 §4.5 三条产线全在
    ``SimulationEngine.reset`` 里，本端点只负责 HTTP 形状与错误码映射。
    停止/重置走既有的 WS ``CMD_SIM_PAUSE`` / ``CMD_SIM_RESET``，此处不另开取消入口。
    """

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
        run = await _scenario_launcher(payload.scenario_id, payload.seed)
    except ScenarioLaunchError as exc:
        raise HTTPException(
            status_code=_LAUNCH_ERROR_STATUS.get(exc.code, 400), detail=exc.to_dict()
        ) from exc
    return {"run": run}


@router.get("/api/runs")
async def get_runs(limit: int = Query(default=50, ge=1, le=500)):
    """§11 run 元数据列表，新的在前。工件目录即数据源——无进程内缓存。"""
    try:
        runs = load_run_summaries(limit=limit)
    except RunArtifactError as exc:
        raise _run_artifact_error(exc) from exc
    return {"count": len(runs), "runs": runs}


@router.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """单个 run 的 §11 元数据 + 事件条数 + 同目录工件清单。"""
    try:
        metadata = read_run_metadata(run_id)
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
):
    """本 run 的事件流（seq 序）。``correlation_id`` 过滤即 §18 的整条因果链查询。"""
    try:
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
        report = evaluate_run(run_id)
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
            status_code=404 if "cannot read events" in (report.failure_reasons[0] if report.failure_reasons else "") else 500,
            detail={
                "code": "eval_error",
                "message": report.failure_reasons[0] if report.failure_reasons else "评估失败",
                "details": {"run_id": run_id},
            },
        )

    # 如果 run 有 scenario_id，附上场景级信息（§18 九问中的 scenario 相关字段）
    scenario_info = None
    if report.scenario_id:
        try:
            spec = get_scenario(report.scenario_id)
            if spec is not None:
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
        except Exception:
            pass

    return {
        **report.to_dict(),
        "scenario": scenario_info,
    }
