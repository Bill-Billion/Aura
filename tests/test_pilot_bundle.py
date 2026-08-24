from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from backend.experiments.pilot_bundle import (
    MAX_PILOT_ARTIFACT_BYTES,
    load_validated_pilot_bundle,
    validate_pilot_bundle,
)
from backend.experiments.resolve import load_matrix_file


PILOT_ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-dev"


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_committed_scientific_pilot_bundle_is_complete_and_pending_review() -> None:
    assert validate_pilot_bundle(PILOT_ROOT / "manifest.json") == {
        "benchmark_id": "aurabench_dev_pilot",
        "pairs": 8,
        "seeds": 3,
        "cells": 96,
        "pair_set_hash": "a4e1e42c490650dab491581b828aae07c74e9a5335e9afc84ec81b0fd6d8c7da",
        "gate_status": "pending",
    }


def test_load_validated_pilot_bundle_returns_sealed_ordered_pair_projection() -> None:
    manifest_path = PILOT_ROOT / "manifest.json"
    bundle = load_validated_pilot_bundle(manifest_path)

    assert bundle.benchmark_id == "aurabench_dev_pilot"
    assert bundle.matrix_contract_hash == load_matrix_file(
        PILOT_ROOT / "matrix.yaml"
    ).contract_hash()
    assert bundle.pair_set_hash == (
        "a4e1e42c490650dab491581b828aae07c74e9a5335e9afc84ec81b0fd6d8c7da"
    )
    assert bundle.gate_status == "pending"
    assert bundle.manifest_sha256 == hashlib.sha256(
        manifest_path.read_bytes()
    ).hexdigest()
    assert bundle.seeds == (21001, 21002, 21003)
    assert bundle.expected_cells == 96
    assert [item.value for item in bundle.observation_conditions] == [
        "perfect",
        "stale_offline",
    ]
    assert [pair.group_id for pair in bundle.pairs] == sorted(
        pair.group_id for pair in bundle.pairs
    )
    read_then_leave = next(
        pair for pair in bundle.pairs if pair.group_id == "read_then_leave_001"
    )
    assert read_then_leave.pair_fingerprint == (
        "33a9363862a3bf9f7f31817775678b7d86697c6844eadf5a700a110b81bdcd0e"
    )
    assert read_then_leave.static_scenario_id == "read_then_leave_001_static"
    assert read_then_leave.dynamic_scenario_id == "read_then_leave_001_dynamic"


def test_pilot_bundle_rejects_oversized_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(b" " * (MAX_PILOT_ARTIFACT_BYTES + 1))
    with pytest.raises(ValueError, match="artifact exceeds"):
        validate_pilot_bundle(manifest)


def test_pilot_bundle_rejects_extra_episode_without_parsing_it(tmp_path: Path) -> None:
    root = tmp_path / "aurabench-dev"
    shutil.copytree(PILOT_ROOT, root)
    (root / "episodes" / "extra.yaml").write_text("not: parsed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds manifest inventory"):
        validate_pilot_bundle(root / "manifest.json")


@pytest.mark.parametrize(
    ("tamper", "expected_error"),
    [
        ("scenario_hash", "scenario contract hash drift"),
        ("matrix_seed", "matrix seed axis"),
        ("reviewer_slots", "reviewer slots 1 and 2"),
    ],
)
def test_pilot_bundle_fails_closed_on_contract_drift(
    tmp_path: Path, tamper: str, expected_error: str
) -> None:
    root = tmp_path / "aurabench-dev"
    shutil.copytree(PILOT_ROOT, root)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if tamper == "scenario_hash":
        manifest["pairs"][0]["static"]["contract_hash"] = "0" * 64
    elif tamper == "matrix_seed":
        matrix_path = root / "matrix.yaml"
        matrix_path.write_text(
            matrix_path.read_text(encoding="utf-8").replace("21003", "21004"),
            encoding="utf-8",
        )
        manifest["matrix"]["contract_hash"] = load_matrix_file(
            matrix_path
        ).contract_hash()
    else:
        status_path = root / "reviews" / "status.json"
        status = json.loads(status_path.read_text(encoding="utf-8"))
        status["reviewer_slots"][1]["slot"] = 1
        _write_json(status_path, status)
        manifest["human_review"]["status"]["sha256"] = hashlib.sha256(
            status_path.read_bytes()
        ).hexdigest()

    _write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match=expected_error):
        validate_pilot_bundle(manifest_path)
