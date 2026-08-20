"""Regression coverage for the S4 multi-scenario suite contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.evaluation import suite as suite_module
from backend.engine.event_log import read_run_metadata, runs_root
from backend.evaluation.evaluator import EvalOutcome, evaluate_run
from backend.evaluation.suite import SeedSet, SuiteRunner
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import load_library


class _FakeEvaluator:
    def __init__(self, outcomes: list[EvalOutcome]) -> None:
        self._outcomes = iter(outcomes)

    def evaluate(self, *args, **kwargs):
        del args, kwargs
        return SimpleNamespace(outcome=next(self._outcomes))


def _result(seed: int):
    return SimpleNamespace(run_id=f"run-{seed}", events=())


@pytest.mark.parametrize(
    ("current", "candidate", "expected"),
    [
        (EvalOutcome.PASS, EvalOutcome.FAIL, EvalOutcome.FAIL),
        (EvalOutcome.FAIL, EvalOutcome.PASS, EvalOutcome.FAIL),
        (EvalOutcome.PASS, EvalOutcome.ERROR, EvalOutcome.ERROR),
        (EvalOutcome.ERROR, EvalOutcome.FAIL, EvalOutcome.ERROR),
    ],
)
def test_suite_outcome_severity_is_monotonic(current, candidate, expected) -> None:
    assert suite_module._merge_outcome(current, candidate) is expected


@pytest.mark.anyio
async def test_seed_exception_marks_scenario_error_and_cannot_be_downgraded(
    monkeypatch,
) -> None:
    """One infrastructure error invalidates the scenario even if later seeds fail normally."""

    spec = load_library()["user_arrives_home_evening"]

    async def fake_run_scenario(run_spec, *, seed):
        assert run_spec is spec
        if seed == 1001:
            raise RuntimeError("worker crashed")
        return _result(seed)

    evaluator = _FakeEvaluator([EvalOutcome.FAIL, EvalOutcome.FAIL])
    monkeypatch.setattr(suite_module, "get_scenario", lambda scenario_id, dirs=None: spec)
    monkeypatch.setattr(suite_module, "run_scenario", fake_run_scenario)
    monkeypatch.setattr(
        suite_module,
        "evaluate_run",
        lambda run_id, **kwargs: evaluator.evaluate(run_id, **kwargs),
    )

    report = await SuiteRunner(
        "error-dominates-failure",
        SeedSet.DEV,
        scenario_ids=[spec.id],
    ).run()

    entry = report.entries[0]
    assert entry.aggregate_outcome is EvalOutcome.ERROR
    assert entry.errors == ["seed=1001: worker crashed"]
    assert report.passed == 0
    assert report.failed == 0
    assert report.errors == 1


@pytest.mark.anyio
async def test_explicit_scenario_dirs_are_used_for_loading(monkeypatch, tmp_path) -> None:
    spec = load_library()["user_arrives_home_evening"]
    observed_dirs = []

    def fake_get_scenario(scenario_id, dirs=None):
        assert scenario_id == "private-scenario"
        observed_dirs.append(dirs)
        return spec

    async def fake_run_scenario(run_spec, *, seed):
        assert run_spec is spec
        return _result(seed)

    monkeypatch.setattr(suite_module, "get_scenario", fake_get_scenario)
    monkeypatch.setattr(suite_module, "run_scenario", fake_run_scenario)
    monkeypatch.setattr(
        suite_module,
        "evaluate_run",
        lambda run_id, **kwargs: _FakeEvaluator([EvalOutcome.PASS]).evaluate(
            run_id, **kwargs
        ),
    )

    report = await suite_module.run_suite(
        "private-library",
        SeedSet.SMOKE,
        scenario_ids=["private-scenario"],
        scenario_dirs=[tmp_path],
    )

    assert observed_dirs == [[tmp_path]]
    assert report.passed == 1


def test_default_scenario_ids_are_enumerated_from_configured_library(
    monkeypatch, tmp_path
) -> None:
    sentinel = object()
    observed_dirs = []

    def fake_load_library(dirs=None):
        observed_dirs.append(dirs)
        return {"scenario-b": sentinel, "scenario-a": sentinel}

    monkeypatch.setattr(suite_module, "load_library", fake_load_library)
    runner = SuiteRunner("configured-library", scenario_dirs=[tmp_path])

    assert runner._default_scenario_ids() == ["scenario-b", "scenario-a"]
    assert observed_dirs == [[tmp_path]]


@pytest.mark.anyio
async def test_smoke_suite_runs_a_real_scenario_through_the_evaluator() -> None:
    spec = load_library()["user_arrives_home_evening"]
    assert SeedSet.SMOKE.seeds()[0] != spec.seed

    report = await SuiteRunner(
        "one-real-scenario",
        SeedSet.SMOKE,
        scenario_ids=["user_arrives_home_evening"],
    ).run()

    assert report.total_scenarios == 1
    assert report.total_runs == 1
    assert report.passed + report.failed + report.errors == 1
    entry = report.entries[0]
    assert entry.errors == []
    assert len(entry.runs) == 1
    assert len(entry.reports) == 1
    assert entry.runs[0].seed == 42
    assert entry.reports[0].seed == 42
    assert entry.aggregate_outcome is entry.reports[0].outcome

    metadata = read_run_metadata(entry.runs[0].run_id, root=runs_root())
    assert metadata["seed"] == 42
    assert metadata["scenario_contract_hash"] == scenario_contract_fingerprint(spec)

    offline_report = evaluate_run(entry.runs[0].run_id, data_root=runs_root())
    assert entry.reports[0].to_dict() == offline_report.to_dict()
    assert entry.reports[0].provenance
    assert entry.reports[0].provenance["scenario_contract_hash"] == metadata[
        "scenario_contract_hash"
    ]
