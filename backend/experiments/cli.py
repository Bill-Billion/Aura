"""Command-line interface for resolving, running, and summarizing matrices."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Sequence

from .adapters import AuraCellExecutor
from .artifacts import read_resolved_matrix, write_resolved_matrix
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
        else:
            matrix = read_resolved_matrix(args.resolved_matrix)
            summary = summarize_results(
                matrix,
                output_dir=args.output,
                validator=AuraCellExecutor(data_root=args.output / "runs"),
            )
            payload = summary.model_dump(mode="json")
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _asyncio_run(coro):
    """Small seam kept separate so CLI tests can call ``main`` synchronously."""

    return asyncio.run(coro)


__all__ = ["main"]
