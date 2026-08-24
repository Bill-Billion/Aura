from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from backend.experiments.pilot_bundle import (
    MAX_PILOT_ARTIFACT_BYTES,
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
        "cells": 48,
        "pair_set_hash": "a4e1e42c490650dab491581b828aae07c74e9a5335e9afc84ec81b0fd6d8c7da",
        "gate_status": "pending",
    }


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
