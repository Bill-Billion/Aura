"""Command-line interface for matrix execution and reproducible analysis."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from .adapters import AuraCellExecutor
from .analysis import AnalysisPlan, analyze_matrix_results, render_analysis_bundle
from .artifacts import read_resolved_matrix, write_resolved_matrix
from .benchmark_catalog import validate_benchmark_catalog
from .pilot_bundle import validate_pilot_bundle
from .pilot_freeze import (
    validate_pilot_freeze,
    write_pilot_freeze,
    write_pilot_run_inventory,
)
from .llm_substudy import (
    LLMSubstudyRunner,
    preflight_llm_substudy,
    read_resolved_llm_substudy,
    resolve_llm_substudy,
    summarize_llm_substudy,
    validate_preflight_receipt,
    write_resolved_llm_substudy,
)
from .resolve import load_and_resolve_matrix
from .runner import MatrixRunner, summarize_results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m backend.experiments")
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", help="freeze a YAML matrix")
    resolve.add_argument("matrix", type=Path)
    resolve.add_argument("--output", type=Path, required=True)

    run = commands.add_parser("run", help="run a frozen matrix serially")
    run.add_argument("resolved_matrix", type=Path)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--shard-index", type=int, default=0)
    run.add_argument("--shard-count", type=int, default=1)
    run.add_argument("--no-resume", action="store_true")
    run.add_argument("--continue-on-error", action="store_true")
    run.add_argument(
        "--retry-results",
        action="store_true",
        help="archive failed or invalid cell evidence before retrying it",
    )

    summarize = commands.add_parser(
        "summarize", help="validate cell artifacts and write a global summary"
    )
    summarize.add_argument("resolved_matrix", type=Path)
    summarize.add_argument("--output", type=Path, required=True)

    analyze = commands.add_parser(
        "analyze", help="seal validated results or rebuild paper artifacts"
    )
    analyze.add_argument("--resolved-matrix", type=Path)
    analyze.add_argument("--result-root", type=Path)
    analyze.add_argument("--benchmark-manifest", type=Path)
    analyze.add_argument("--results-manifest", type=Path)
    analyze.add_argument("--output", type=Path, required=True)
    analyze.add_argument("--bootstrap-seed", type=int, default=0)
    analyze.add_argument("--bootstrap-resamples", type=int, default=10_000)

    validate_pilot = commands.add_parser(
        "validate-pilot", help="validate a scientific pilot manifest"
    )
    validate_pilot.add_argument("manifest", type=Path)
    validate_pilot.add_argument("--require-approved", action="store_true")

    validate_catalog = commands.add_parser(
        "validate-catalog",
        help="validate the evidence-backed AuraBench-v1 design catalog",
    )
    validate_catalog.add_argument("catalog", type=Path)

    inventory = commands.add_parser(
        "inventory-pilot", help="seal the raw evidence for a completed pilot"
    )
    inventory.add_argument("--resolved-matrix", type=Path, required=True)
    inventory.add_argument("--result-root", type=Path, required=True)
    inventory.add_argument("--benchmark-manifest", type=Path, required=True)
    inventory.add_argument("--results-manifest", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)

    freeze = commands.add_parser(
        "freeze-pilot", help="bind a completed pilot to two independent reviews"
    )
    freeze.add_argument("--bundle-root", type=Path, required=True)
    freeze.add_argument("--result-root", type=Path, required=True)
    freeze.add_argument("--benchmark-manifest", type=Path, required=True)
    freeze.add_argument("--resolved-matrix", type=Path, required=True)
    freeze.add_argument("--results-manifest", type=Path, required=True)
    freeze.add_argument("--run-inventory", type=Path, required=True)
    freeze.add_argument("--review", type=Path, action="append", required=True)
    freeze.add_argument("--output", type=Path, required=True)

    validate_freeze = commands.add_parser(
        "validate-freeze", help="validate a sealed pilot and human-review gate"
    )
    validate_freeze.add_argument("freeze", type=Path)
    validate_freeze.add_argument("--result-root", type=Path, required=True)
    validate_freeze.add_argument("--require-approved", action="store_true")

    resolve_llm = commands.add_parser(
        "resolve-llm-substudy",
        help="freeze the Option B MiniMax-M3 substudy",
    )
    resolve_llm.add_argument("manifest", type=Path)
    resolve_llm.add_argument("--output", type=Path, required=True)

    preflight_llm = commands.add_parser(
        "preflight-llm-substudy",
        help="make one sealed provider/model access check",
    )
    preflight_llm.add_argument("resolved_substudy", type=Path)
    preflight_llm.add_argument("--output", type=Path, required=True)

    run_llm = commands.add_parser(
        "run-llm-substudy",
        help="run the frozen live/capture/replay substudy serially",
    )
    run_llm.add_argument("resolved_substudy", type=Path)
    run_llm.add_argument("--output", type=Path, required=True)
    run_llm.add_argument("--no-resume", action="store_true")
    run_llm.add_argument("--continue-on-error", action="store_true")

    summarize_llm = commands.add_parser(
        "summarize-llm-substudy",
        help="revalidate all 168 slots and seal the scientific gate",
    )
    summarize_llm.add_argument("resolved_substudy", type=Path)
    summarize_llm.add_argument("--output", type=Path, required=True)
    return parser


async def _run_command(args: argparse.Namespace) -> dict[str, object]:
    matrix = read_resolved_matrix(args.resolved_matrix)
    executor = AuraCellExecutor(data_root=args.output / "runs")
    summary = await MatrixRunner(executor).run(
        matrix,
        output_dir=args.output,
        shard_index=args.shard_index,
        shard_count=args.shard_count,
        resume=not args.no_resume,
        continue_on_error=args.continue_on_error,
        retry_results=args.retry_results,
    )
    return summary.model_dump(mode="json")


async def _run_llm_substudy_command(args: argparse.Namespace) -> dict[str, object]:
    study = read_resolved_llm_substudy(args.resolved_substudy)
    validate_preflight_receipt(study, output_dir=args.output)
    return await LLMSubstudyRunner(study, output_dir=args.output).run(
        resume=not args.no_resume,
        continue_on_error=args.continue_on_error,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve":
            matrix = load_and_resolve_matrix(args.matrix)
            path = write_resolved_matrix(args.output, matrix)
            payload: dict[str, object] = {
                "matrix_hash": matrix.matrix_hash,
                "cells": len(matrix.cells),
                "path": str(path),
            }
        elif args.command == "run":
            payload = _asyncio_run(_run_command(args))
        elif args.command == "summarize":
            matrix = read_resolved_matrix(args.resolved_matrix)
            summary = summarize_results(
                matrix,
                output_dir=args.output,
                validator=AuraCellExecutor(data_root=args.output / "runs"),
            )
            payload = summary.model_dump(mode="json")
        elif args.command == "analyze":
            if args.results_manifest is not None:
                if any(
                    value is not None
                    for value in (
                        args.resolved_matrix,
                        args.result_root,
                        args.benchmark_manifest,
                    )
                ):
                    raise ValueError(
                        "--results-manifest rebuild mode forbids raw-result inputs"
                    )
                if args.bootstrap_seed != 0 or args.bootstrap_resamples != 10_000:
                    raise ValueError(
                        "manifest-only rebuild cannot override the sealed analysis plan"
                    )
                payload = render_analysis_bundle(
                    args.results_manifest,
                    output_dir=args.output,
                )
            else:
                missing = [
                    option
                    for option, value in (
                        ("--resolved-matrix", args.resolved_matrix),
                        ("--result-root", args.result_root),
                        ("--benchmark-manifest", args.benchmark_manifest),
                    )
                    if value is None
                ]
                if missing:
                    raise ValueError(
                        "raw analysis mode requires " + ", ".join(missing)
                    )
                matrix = read_resolved_matrix(args.resolved_matrix)
                payload = analyze_matrix_results(
                    matrix,
                    result_root=args.result_root,
                    validator=AuraCellExecutor(data_root=args.result_root / "runs"),
                    benchmark_manifest=args.benchmark_manifest,
                    output_dir=args.output,
                    analysis_plan=AnalysisPlan(
                        bootstrap_root_seed=args.bootstrap_seed,
                        bootstrap_resamples=args.bootstrap_resamples,
                    ),
                )
        elif args.command == "validate-pilot":
            payload = validate_pilot_bundle(args.manifest)
            if args.require_approved and payload["gate_status"] != "approved":
                raise ValueError("pilot human-review gate is not approved")
        elif args.command == "validate-catalog":
            payload = validate_benchmark_catalog(args.catalog)
        elif args.command == "inventory-pilot":
            path = write_pilot_run_inventory(
                resolved_matrix=args.resolved_matrix,
                result_root=args.result_root,
                benchmark_manifest=args.benchmark_manifest,
                results_manifest=args.results_manifest,
                output=args.output,
            )
            payload = {"path": str(path)}
        elif args.command == "freeze-pilot":
            path = write_pilot_freeze(
                bundle_root=args.bundle_root,
                result_root=args.result_root,
                benchmark_manifest=args.benchmark_manifest,
                resolved_matrix=args.resolved_matrix,
                results_manifest=args.results_manifest,
                run_inventory=args.run_inventory,
                review_artifacts=args.review,
                output=args.output,
            )
            payload = {"path": str(path)}
        elif args.command == "resolve-llm-substudy":
            study = resolve_llm_substudy(args.manifest)
            path = write_resolved_llm_substudy(args.output, study)
            payload = {
                "study_hash": study.study_hash,
                "instances": len(study.instances),
                "slots": len(study.slots),
                "path": str(path),
            }
        elif args.command == "preflight-llm-substudy":
            study = read_resolved_llm_substudy(args.resolved_substudy)
            path = _asyncio_run(
                preflight_llm_substudy(study, output_dir=args.output)
            )
            payload = {"study_hash": study.study_hash, "path": str(path)}
        elif args.command == "run-llm-substudy":
            payload = _asyncio_run(_run_llm_substudy_command(args))
        elif args.command == "summarize-llm-substudy":
            study = read_resolved_llm_substudy(args.resolved_substudy)
            path = summarize_llm_substudy(study, output_dir=args.output)
            payload = {"study_hash": study.study_hash, "path": str(path)}
        else:
            payload = validate_pilot_freeze(
                args.freeze,
                result_root=args.result_root,
            )
            if args.require_approved and payload["gate_status"] != "approved":
                raise ValueError("pilot freeze gate is not approved")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _asyncio_run(coro):
    """Small seam kept separate so CLI tests can call ``main`` synchronously."""

    return asyncio.run(coro)


__all__ = ["main"]
