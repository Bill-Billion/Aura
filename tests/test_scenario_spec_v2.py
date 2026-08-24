"""PR-1 ScenarioSpecV2 loading and v1 compatibility."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.models.versioning import LEGACY_SCENARIO_SCHEMA_VERSION
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import (
    ScenarioLoadError,
    load_library,
    parse_scenario_mapping,
)
from backend.scenarios.spec import ScenarioSpec
from backend.scenarios.spec_v2 import ScenarioSpecV2

PILOT_DIR = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev" / "episodes"
)


def test_omitted_version_remains_legacy_v1_1() -> None:
    spec = ScenarioSpec.model_validate(
        {
            "id": "legacy_default",
            "name": "legacy default",
            "description": "version compatibility fixture",
            "seed": 1,
            "initial_state": {},
            "timeline": [],
            "expected_device_effects": [],
            "involved_agents": [],
            "success_criteria": {},
        }
    )
    assert spec.scenario_schema_version == LEGACY_SCENARIO_SCHEMA_VERSION == "1.1"


def test_explicit_v1_contract_fingerprint_is_unchanged() -> None:
    spec = load_library()["user_arrives_home_evening"]
    assert spec.scenario_schema_version == "1.0"
    assert scenario_contract_fingerprint(spec) == (
        "b30df448122793586931748246af2ed2b27f6c5f84f7624f842e222b2101ef1c"
    )


def test_pilot_library_dispatches_v2_and_validates_complete_pair() -> None:
    library = load_library([PILOT_DIR], validate_pairs=True)
    assert set(library) == {
        "read_then_leave_001_static",
        "read_then_leave_001_dynamic",
    }
    assert all(isinstance(spec, ScenarioSpecV2) for spec in library.values())
    static = library["read_then_leave_001_static"]
    assert static.summary()["counterfactual_group_id"] == "read_then_leave_001"
    assert static.summary()["trace_property_count"] == 2


def test_unknown_scenario_major_is_rejected() -> None:
    with pytest.raises(ScenarioLoadError) as excinfo:
        parse_scenario_mapping(
            {
                "scenario_schema_version": "9.0",
                "id": "unknown",
            },
            check_registry=False,
        )
    assert excinfo.value.code == "unsupported_schema_version"
