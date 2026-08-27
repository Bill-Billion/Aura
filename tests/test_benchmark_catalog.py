from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from backend.experiments.benchmark_catalog import validate_benchmark_catalog
from backend.experiments.cli import main


CATALOG_ROOT = Path(__file__).resolve().parents[1] / "benchmarks" / "aurabench-v1"


def _copy_catalog(tmp_path: Path) -> Path:
    benchmarks = tmp_path / "benchmarks"
    root = benchmarks / "aurabench-v1"
    shutil.copytree(CATALOG_ROOT, root)
    shutil.copytree(CATALOG_ROOT.parent / "aurabench-dev", benchmarks / "aurabench-dev")
    return root


def _read_catalog(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _write_catalog(path: Path, payload: dict) -> None:
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def test_committed_v1_catalog_is_balanced_evidence_backed_design() -> None:
    summary = validate_benchmark_catalog(CATALOG_ROOT / "catalog.yaml")

    assert summary["benchmark_id"] == "aurabench_v1"
    assert summary["release_stage"] == "design"
    assert summary["pairs"] == 48
    assert summary["scenarios"] == 96
    assert summary["seeds"] == [31001, 31002, 31003, 31004, 31005]
    assert summary["split_counts"] == {"dev": 24, "test": 16, "validation": 8}
    assert set(summary["family_counts"].values()) == {6}
    assert summary["negative_controls"] == 8
    assert summary["sources"] == 13
    assert set(summary["factor_counts"]) == {
        "conflicting_request",
        "device_failure",
        "feedback_loss",
        "observation_delay",
        "resident_state_change",
        "safety_interrupt",
    }
    assert summary["origin_counts"] == {
        "aurabench_dev_pilot": 8,
        "new": 40,
    }
    assert summary["pilot_manifest_sha256"] == (
        "bef4c7e3241adcdc381c2ab31421ca7bff9c640a28c7291f7236bbc8840826e2"
    )
    assert summary["implementation_status_counts"] == {"planned": 48}
    assert summary["review_status_counts"] == {"pending": 48}


def test_catalog_cli_reports_design_without_claiming_implementation(capsys) -> None:
    exit_code = main(
        ["validate-catalog", str(CATALOG_ROOT / "catalog.yaml")]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pairs"] == 48
    assert payload["implementation_status_counts"] == {"planned": 48}


def test_catalog_rejects_source_registry_hash_drift(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    source_registry = root / "sources.json"
    source_registry.write_bytes(source_registry.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="artifact hash drift"):
        validate_benchmark_catalog(root / "catalog.yaml")


def test_catalog_rejects_template_leakage_across_splits(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    catalog_path = root / "catalog.yaml"
    catalog = _read_catalog(catalog_path)
    dev_pair = next(pair for pair in catalog["pairs"] if pair["split"] == "dev")
    test_pair = next(pair for pair in catalog["pairs"] if pair["split"] == "test")
    test_pair["template_group"] = dev_pair["template_group"]
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValueError, match="template_groups must be unique"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_evidence_claim_not_supported_by_sources(
    tmp_path: Path,
) -> None:
    root = _copy_catalog(tmp_path)
    catalog_path = root / "catalog.yaml"
    catalog = _read_catalog(catalog_path)
    catalog["pairs"][0]["required_evidence_tags"].append("invented_world_rule")
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValueError, match="unsupported evidence tags"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_planned_pair_with_fake_episode_references(
    tmp_path: Path,
) -> None:
    root = _copy_catalog(tmp_path)
    catalog_path = root / "catalog.yaml"
    catalog = _read_catalog(catalog_path)
    catalog["pairs"][0]["static_reference"] = "dev/episodes/fake_static.yaml"
    catalog["pairs"][0]["dynamic_reference"] = "dev/episodes/fake_dynamic.yaml"
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValueError, match="planned pairs must not claim"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_artifact_reference_outside_bundle(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    catalog_path = root / "catalog.yaml"
    catalog = _read_catalog(catalog_path)
    catalog["source_registry"]["reference"] = "../sources.json"
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValueError, match="must stay inside bundle"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    catalog_path = root / "catalog.yaml"
    catalog_path.write_text(
        "benchmark_id: duplicate\n" + catalog_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate key"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    source_registry = root / "sources.json"
    source_registry.write_text(
        source_registry.read_text(encoding="utf-8").replace(
            "{",
            '{"registry_id":"duplicate",',
            1,
        ),
        encoding="utf-8",
    )
    catalog_path = root / "catalog.yaml"
    catalog = _read_catalog(catalog_path)
    catalog["source_registry"]["sha256"] = hashlib.sha256(
        source_registry.read_bytes()
    ).hexdigest()
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValueError, match="duplicate JSON key 'registry_id'"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_pilot_pair_fingerprint_drift(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    catalog_path = root / "catalog.yaml"
    catalog = _read_catalog(catalog_path)
    inherited = next(
        pair
        for pair in catalog["pairs"]
        if pair.get("origin") == "aurabench_dev_pilot"
    )
    inherited["pilot_pair_fingerprint"] = "0" * 64
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValueError, match="pilot pair fingerprint drift"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_pilot_metadata_drift(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    catalog_path = root / "catalog.yaml"
    catalog = _read_catalog(catalog_path)
    inherited = next(
        pair
        for pair in catalog["pairs"]
        if pair.get("pilot_group_id") == "single_feedback_loss_006"
    )
    inherited["factor"] = "observation_delay"
    _write_catalog(catalog_path, catalog)

    with pytest.raises(ValueError, match="pilot metadata drift"):
        validate_benchmark_catalog(catalog_path)


def test_catalog_rejects_pilot_manifest_hash_drift(tmp_path: Path) -> None:
    root = _copy_catalog(tmp_path)
    pilot_manifest = root.parent / "aurabench-dev" / "manifest.json"
    pilot_manifest.write_bytes(pilot_manifest.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="hash drift for pilot manifest"):
        validate_benchmark_catalog(root / "catalog.yaml")
