"""§12.2 suite 运行器：一条命令跑多场景 → 聚合报告。

消费 S2 的 ScenarioSpec 库与 S4 的 ScenarioEvaluator，
输出 suite 级 pass/fail 报告 + 每场景 baseline/split 指标。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from backend.engine.event_log import runs_root
from backend.evaluation.evaluator import EvalOutcome, EvalReport, evaluate_run
from backend.scenarios.loader import get_scenario, load_library
from backend.scenarios.runner import ScenarioRunResult, run_scenario


_OUTCOME_SEVERITY = {
    EvalOutcome.PASS: 0,
    EvalOutcome.FAIL: 1,
    EvalOutcome.ERROR: 2,
}


def _merge_outcome(current: EvalOutcome, candidate: EvalOutcome) -> EvalOutcome:
    """Aggregate without ever downgrading ERROR to FAIL or FAIL to PASS."""

    if _OUTCOME_SEVERITY[candidate] > _OUTCOME_SEVERITY[current]:
        return candidate
    return current


class SeedSet(str, Enum):
    """预定义种子集（§12.2）。"""

    DEV = "dev"
    SMOKE = "smoke"
    FULL = "full"

    def seeds(self) -> list[int]:
        if self is SeedSet.DEV:
            return [1001, 1002, 1003]
        if self is SeedSet.SMOKE:
            return [42]
        # FULL: 每个 canonical 场景跑 5 个种子
        return [1, 42, 100, 999, 2024]


@dataclass
class ScenarioSuiteEntry:
    """一个场景在一次 suite 中的全部 run 结果。"""

    scenario_id: str
    seed_set: SeedSet
    runs: list[ScenarioRunResult] = field(default_factory=list)
    reports: list[EvalReport] = field(default_factory=list)
    aggregate_outcome: EvalOutcome = EvalOutcome.PASS
    errors: list[str] = field(default_factory=list)


@dataclass
class SuiteReport:
    """一次 suite 运行的聚合报告。"""

    suite_name: str
    seed_set: SeedSet
    entries: list[ScenarioSuiteEntry] = field(default_factory=list)
    total_scenarios: int = 0
    total_runs: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite_name": self.suite_name,
            "seed_set": self.seed_set.value,
            "total_scenarios": self.total_scenarios,
            "total_runs": self.total_runs,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "entries": [
                {
                    "scenario_id": entry.scenario_id,
                    "aggregate_outcome": entry.aggregate_outcome.value,
                    "run_count": len(entry.runs),
                    "errors": entry.errors,
                    "reports": [r.to_dict() for r in entry.reports],
                }
                for entry in self.entries
            ],
        }

    def save(self, path: Path | str) -> Path:
        """序列化到 JSON 文件（S4-T4 输出工件）。"""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return path


class SuiteRunner:
    """§12.2 suite 运行器。

    用法::

        runner = SuiteRunner("canonical-v1", SeedSet.DEV)
        report = await runner.run()
        report.save("data/suites/canonical-v1_dev.json")
    """

    def __init__(
        self,
        suite_name: str,
        seed_set: SeedSet = SeedSet.DEV,
        *,
        scenario_ids: list[str] | None = None,
        scenario_dirs: list[Path | str] | None = None,
    ) -> None:
        self.suite_name = suite_name
        self.seed_set = seed_set
        self._scenario_ids = scenario_ids
        self._scenario_dirs = (
            None if scenario_dirs is None else [Path(d) for d in scenario_dirs]
        )

    async def run(self) -> SuiteReport:
        """跑完全部场景 × 种子，汇总报告。"""

        scenario_ids = (
            self._default_scenario_ids()
            if self._scenario_ids is None
            else self._scenario_ids
        )
        seeds = self.seed_set.seeds()

        report = SuiteReport(
            suite_name=self.suite_name,
            seed_set=self.seed_set,
            total_scenarios=len(scenario_ids),
            total_runs=len(scenario_ids) * len(seeds),
        )
        artifact_root = runs_root()

        for sid in scenario_ids:
            entry = ScenarioSuiteEntry(
                scenario_id=sid,
                seed_set=self.seed_set,
            )
            try:
                spec = get_scenario(sid, dirs=self._scenario_dirs)
                if spec is None:
                    entry.errors.append(f"scenario {sid} not found")
                    entry.aggregate_outcome = EvalOutcome.ERROR
                    report.entries.append(entry)
                    report.errors += 1
                    continue
            except Exception as exc:
                entry.errors.append(f"failed to load {sid}: {exc}")
                entry.aggregate_outcome = EvalOutcome.ERROR
                report.entries.append(entry)
                report.errors += 1
                continue

            for seed in seeds:
                try:
                    # Seed 是一次实验的运行参数，不得复制并篡改 ScenarioSpec；否则
                    # run.json 会把 suite seed 写进场景契约指纹，离线重评时必然漂移。
                    result = await run_scenario(spec, seed=seed)
                    entry.runs.append(result)

                    # Suite、离线 CLI 与 API 都只评持久化且已封口的工件。这样版本、
                    # 完整性、场景指纹及 provenance 只有 evaluate_run 一条真相来源。
                    eval_report = evaluate_run(
                        result.run_id,
                        data_root=artifact_root,
                        scenario_dirs=self._scenario_dirs,
                    )
                    entry.reports.append(eval_report)

                    entry.aggregate_outcome = _merge_outcome(
                        entry.aggregate_outcome, eval_report.outcome
                    )

                except Exception as exc:
                    entry.errors.append(f"seed={seed}: {exc}")
                    entry.aggregate_outcome = _merge_outcome(
                        entry.aggregate_outcome, EvalOutcome.ERROR
                    )

            report.entries.append(entry)
            if entry.aggregate_outcome == EvalOutcome.PASS:
                report.passed += 1
            elif entry.aggregate_outcome == EvalOutcome.FAIL:
                report.failed += 1
            else:
                report.errors += 1

        return report

    def _default_scenario_ids(self) -> list[str]:
        """Enumerate the configured production library instead of importing tests."""

        return list(load_library(self._scenario_dirs))


async def run_suite(
    suite_name: str = "canonical-v1",
    seed_set: SeedSet = SeedSet.DEV,
    *,
    scenario_ids: list[str] | None = None,
    scenario_dirs: list[Path | str] | None = None,
    output_dir: Path | str | None = None,
) -> SuiteReport:
    """便利入口：跑一次 suite 并保存报告。

    S4-T4 CLI: ``aura run-suite canonical-v1 --seed-set dev`` 调的就是这里。
    """

    runner = SuiteRunner(
        suite_name,
        seed_set,
        scenario_ids=scenario_ids,
        scenario_dirs=scenario_dirs,
    )
    report = await runner.run()

    if output_dir is not None:
        output = Path(output_dir) / f"{suite_name}_{seed_set.value}.json"
        report.save(output)

    return report
