"""PR-1 static/dynamic single-intervention structural gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.scenarios.counterfactual import (
    CounterfactualPairError,
    validate_counterfactual_pairs,
)
from backend.scenarios.loader import load_library
from backend.scenarios.spec_v2 import ScenarioSpecV2

PILOT_DIR = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"
)


def _pair() -> tuple[ScenarioSpecV2, ScenarioSpecV2]:
    library = load_library([PILOT_DIR])
    return (
        library["read_then_leave_001_static"],  # type: ignore[return-value]
        library["read_then_leave_001_dynamic"],  # type: ignore[return-value]
    )


def test_valid_pair_has_stable_fingerprint() -> None:
    static, dynamic = _pair()
    first = validate_counterfactual_pairs([dynamic, static])
    second = validate_counterfactual_pairs([static, dynamic])
    assert len(first) == 1
    assert first[0].group_id == "read_then_leave_001"
    assert first[0].fingerprint == second[0].fingerprint
    assert first[0].fingerprint == (
        "13d919217d87c2896e5e2a84427bd4dc661954aa148c185306fd2356086edf4e"
    )


def test_missing_or_duplicate_variant_is_rejected() -> None:
    static, dynamic = _pair()
    with pytest.raises(CounterfactualPairError, match="missing variant"):
        validate_counterfactual_pairs([static])
    with pytest.raises(CounterfactualPairError, match="duplicate static"):
        validate_counterfactual_pairs([static, static, dynamic])


@pytest.mark.parametrize(
    "field,value",
    [
        ("seed", 999),
        ("duration_seconds", 61),
        ("metrics", ["command_failure_count"]),
    ],
)
def test_pair_rejects_any_non_intervention_drift(field, value) -> None:
    static, dynamic = _pair()
    changed = dynamic.model_copy(update={field: value})
    with pytest.raises(CounterfactualPairError, match="differ outside"):
        validate_counterfactual_pairs([static, changed])


def test_incomplete_groups_may_be_loaded_for_authoring_when_explicit() -> None:
    static, _ = _pair()
    assert validate_counterfactual_pairs([static], require_complete=False) == []
