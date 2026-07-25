"""§12.2 suite 运行器：一条命令跑多场景 → 聚合报告。

消费 S2 的 ScenarioSpec 库与 S4 的 ScenarioEvaluator，
输出 suite 级 pass/fail 报告 + 每场景 baseline/split 指标。
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from backend.engine.event_log import run_dir
from backend.evaluation.evaluator import EvalOutcome, EvalReport, ScenarioEvaluator, evaluate_run
from backend.scenarios.loader import get_scenario
from backend.scenarios.runner import ScenarioRunResult, run_scenario
from backend.scenarios.spec import ScenarioSpec


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
        self._scenario_dirs = [Path(d) for d in (scenario_dirs or [])]

    async def run(self) -> SuiteReport:
        """跑完全部场景 × 种子，汇总报告。"""

        scenario_ids = self._scenario_ids or self._default_scenario_ids()
        seeds = self.seed_set.seeds()

        report = SuiteReport(
            suite_name=self.suite_name,
            seed_set=self.seed_set,
            total_scenarios=len(scenario_ids),
            total_runs=len(scenario_ids) * len(seeds),
        )

        for sid in scenario_ids:
            entry = ScenarioSuiteEntry(
                scenario_id=sid,
                seed_set=self.seed_set,
            )
            try:
                spec = get_scenario(sid)
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

            evaluator = ScenarioEvaluator(
                success_criteria=spec.success_criteria.model_dump()
                if spec.success_criteria
                else None
            )

            for seed in seeds:
                try:
                    # 用指定 seed 覆盖场景内置 seed
                    spec_with_seed = spec.model_copy(update={"seed": seed})
                    result = await run_scenario(spec_with_seed)
                    entry.runs.append(result)

                    # 评估
                    eval_report = evaluator.evaluate(
                        list(result.events),
                        run_id=result.run_id,
                        scenario_id=spec.id,
                        seed=seed,
                        expected_failure_device_ids=set(
                            f.device_id for f in (spec.expected_failures or []) if f.device_id
                        ),
                        expected_failure_categories=set(
                            f.category for f in (spec.expected_failures or [])
                        ),
                        expected_device_effects=[
                            {
                                "device_id": e.device_id,
                                "expected": {
                                    k: v.model_dump() if hasattr(v, "model_dump") else v
                                    for k, v in (e.expected or {}).items()
                                },
                            }
                            for e in spec.expected_device_effects
                        ],
                    )
                    entry.reports.append(eval_report)

                    if eval_report.outcome == EvalOutcome.FAIL:
                        entry.aggregate_outcome = EvalOutcome.FAIL
                    elif eval_report.outcome == EvalOutcome.ERROR:
                        entry.aggregate_outcome = EvalOutcome.ERROR

                except Exception as exc:
                    entry.errors.append(f"seed={seed}: {exc}")

            report.entries.append(entry)
            if entry.aggregate_outcome == EvalOutcome.PASS:
                report.passed += 1
            elif entry.aggregate_outcome == EvalOutcome.FAIL:
                report.failed += 1
            else:
                report.errors += 1

        return report

    @staticmethod
    def _default_scenario_ids() -> list[str]:
        """Canonical 场景库的 ID 列表（S2-T8 八场景 + S3 两项增量）。

        与 tests/test_canonical_scenarios.py 的 CANONICAL_SCENARIO_IDS 共用同一词表。
        """

        from tests.test_canonical_scenarios import CANONICAL_SCENARIO_IDS

        return list(CANONICAL_SCENARIO_IDS)


async def run_suite(
    suite_name: str = "canonical-v1",
    seed_set: SeedSet = SeedSet.DEV,
    *,
    scenario_ids: list[str] | None = None,
    output_dir: Path | str | None = None,
) -> SuiteReport:
    """便利入口：跑一次 suite 并保存报告。

    S4-T4 CLI: ``aura run-suite canonical-v1 --seed-set dev`` 调的就是这里。
    """

    runner = SuiteRunner(suite_name, seed_set, scenario_ids=scenario_ids)
    report = await runner.run()

    if output_dir is not None:
        output = Path(output_dir) / f"{suite_name}_{seed_set.value}.json"
        report.save(output)

    return report
