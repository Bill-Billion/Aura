"""Validation for the committed AuraBench scientific pilot bundle."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.scenarios.counterfactual import validate_counterfactual_pairs
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.spec_v2 import ScenarioSpecV2, unsupported_perturbations

from .resolve import FileOrLibraryScenarioResolver, load_matrix_file
from .spec import sha256_json

MAX_PILOT_ARTIFACT_BYTES = 4 * 1024 * 1024
MAX_PILOT_PAIRS = 64


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactReference(_StrictModel):
    reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MatrixReference(_StrictModel):
    reference: str = Field(min_length=1)
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PilotScenarioReference(_StrictModel):
    reference: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PilotPairReference(_StrictModel):
    group_id: str = Field(min_length=1)
    pair_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    static: PilotScenarioReference
    dynamic: PilotScenarioReference


class PilotReviewReference(_StrictModel):
    protocol: ArtifactReference
    status: ArtifactReference


class PilotManifest(_StrictModel):
    pilot_manifest_schema_version: Literal["1.0"] = "1.0"
    benchmark_id: str = Field(min_length=1)
    matrix: MatrixReference
    seeds: list[int] = Field(min_length=1, max_length=256)
    expected_cells: int = Field(gt=0)
    pair_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pairs: list[PilotPairReference] = Field(
        min_length=1, max_length=MAX_PILOT_PAIRS
    )
    human_review: PilotReviewReference

    @field_validator("seeds")
    @classmethod
    def _unique_seeds(cls, values: list[int]) -> list[int]:
        if len(values) != len(set(values)):
            raise ValueError("pilot seeds must be unique")
        return sorted(values)

    @field_validator("pairs")
    @classmethod
    def _unique_pairs(
        cls, values: list[PilotPairReference]
    ) -> list[PilotPairReference]:
        group_ids = [item.group_id for item in values]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("pilot pair group_ids must be unique")
        return sorted(values, key=lambda item: item.group_id)


class ValidatedPilotPair(_FrozenStrictModel):
    group_id: str = Field(min_length=1)
    pair_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    static_scenario_id: str = Field(min_length=1)
    dynamic_scenario_id: str = Field(min_length=1)


class ValidatedPilotBundle(_FrozenStrictModel):
    """Typed, ordered projection of one fully validated pilot manifest."""

    benchmark_id: str = Field(min_length=1)
    matrix_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pair_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    gate_status: Literal["pending"]
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    seeds: tuple[int, ...] = Field(min_length=1, max_length=256)
    expected_cells: int = Field(gt=0)
    pairs: tuple[ValidatedPilotPair, ...] = Field(
        min_length=1, max_length=MAX_PILOT_PAIRS
    )


class ReviewerSlot(_StrictModel):
    slot: int = Field(ge=1, le=2)
    reviewer_id: None = None
    status: Literal["unassigned"] = "unassigned"
    submitted_at: None = None
    artifact_reference: None = None
    artifact_sha256: None = None


class ReviewStatus(_StrictModel):
    review_status_schema_version: Literal["1.0"] = "1.0"
    benchmark_id: str = Field(min_length=1)
    pair_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: None = None
    gate_status: Literal["pending"] = "pending"
    reviewer_slots: list[ReviewerSlot] = Field(min_length=2, max_length=2)


def _load_json_with_bytes(path: Path, model_type):
    encoded = _read_artifact_bytes(path)
    try:
        raw = json.loads(encoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"invalid JSON artifact {path}: {exc}") from exc
    return model_type.model_validate(raw), encoded


def _load_json(path: Path, model_type):
    value, _ = _load_json_with_bytes(path, model_type)
    return value


def _read_artifact_bytes(path: Path) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open pilot artifact {path}: {exc}") from exc
    with os.fdopen(descriptor, "rb") as artifact:
        metadata = os.fstat(artifact.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"pilot artifact must be a regular file: {path}")
        if metadata.st_size > MAX_PILOT_ARTIFACT_BYTES:
            raise ValueError(
                f"pilot artifact exceeds {MAX_PILOT_ARTIFACT_BYTES} bytes: {path}"
            )
        payload = artifact.read(MAX_PILOT_ARTIFACT_BYTES + 1)
    if len(payload) > MAX_PILOT_ARTIFACT_BYTES:
        raise ValueError(
            f"pilot artifact exceeds {MAX_PILOT_ARTIFACT_BYTES} bytes: {path}"
        )
    return payload


def _resolve_artifact(root: Path, reference: str) -> Path:
    candidate = Path(reference)
    if candidate.is_absolute():
        raise ValueError(f"pilot artifact reference must be relative: {reference}")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"pilot artifact escapes bundle root: {reference}")
    if not resolved.is_file():
        raise ValueError(f"pilot artifact does not exist: {reference}")
    return resolved


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(_read_artifact_bytes(path)).hexdigest()


def _verify_artifact(root: Path, artifact: ArtifactReference) -> Path:
    path = _resolve_artifact(root, artifact.reference)
    actual = _file_sha256(path)
    if actual != artifact.sha256:
        raise ValueError(
            f"artifact hash drift for {artifact.reference}: "
            f"expected {artifact.sha256}, got {actual}"
        )
    return path


def _episode_inventory(root: Path, *, expected_count: int) -> set[str]:
    episodes = root / "episodes"
    if not episodes.is_dir():
        raise ValueError("pilot episodes directory does not exist")
    inventory: set[str] = set()
    try:
        with os.scandir(episodes) as entries:
            for entry in entries:
                if Path(entry.name).suffix not in {".yaml", ".yml"}:
                    continue
                if len(inventory) >= expected_count:
                    raise ValueError("pilot episodes directory exceeds manifest inventory")
                inventory.add(f"episodes/{entry.name}")
    except OSError as exc:
        raise ValueError(f"cannot scan pilot episodes directory: {exc}") from exc
    return inventory


def _pair_set_hash(manifest: PilotManifest) -> str:
    return sha256_json(
        {
            "benchmark_id": manifest.benchmark_id,
            "seeds": manifest.seeds,
            "pairs": [item.model_dump(mode="json") for item in manifest.pairs],
        }
    )


def _validate_review_status(
    *, manifest: PilotManifest, status_path: Path
) -> ReviewStatus:
    status = _load_json(status_path, ReviewStatus)
    if status.benchmark_id != manifest.benchmark_id:
        raise ValueError("human-review benchmark_id does not match pilot manifest")
    if status.pair_set_hash != manifest.pair_set_hash:
        raise ValueError("human-review pair_set_hash does not match pilot manifest")
    if {slot.slot for slot in status.reviewer_slots} != {1, 2}:
        raise ValueError("human-review status must contain reviewer slots 1 and 2")
    return status


def load_validated_pilot_bundle(
    manifest_path: Path | str,
) -> ValidatedPilotBundle:
    """Validate a pilot bundle and return its immutable analysis projection."""

    manifest_path = Path(manifest_path).resolve()
    manifest, manifest_bytes = _load_json_with_bytes(manifest_path, PilotManifest)
    root = manifest_path.parent

    matrix_path = _resolve_artifact(root, manifest.matrix.reference)
    matrix = load_matrix_file(matrix_path)
    if matrix.matrix_id != manifest.benchmark_id:
        raise ValueError("matrix_id does not match pilot benchmark_id")
    if matrix.contract_hash() != manifest.matrix.contract_hash:
        raise ValueError("manifest matrix hash must use the MatrixSpec contract hash")
    if matrix.axes.seed != manifest.seeds:
        raise ValueError("matrix seed axis does not match pilot manifest")
    if len(matrix.combinations()) != manifest.expected_cells:
        raise ValueError("resolved matrix cell count does not match pilot manifest")

    resolver = FileOrLibraryScenarioResolver(base_dir=root)
    specs: list[ScenarioSpecV2] = []
    manifest_references = {
        scenario.reference
        for pair in manifest.pairs
        for scenario in (pair.static, pair.dynamic)
    }
    if _episode_inventory(
        root, expected_count=2 * len(manifest.pairs)
    ) != manifest_references:
        raise ValueError("pilot episodes directory does not match pilot manifest")
    for pair_reference in manifest.pairs:
        for variant, scenario_reference in (
            ("static", pair_reference.static),
            ("dynamic", pair_reference.dynamic),
        ):
            spec = resolver.resolve_spec(scenario_reference.reference)
            if not isinstance(spec, ScenarioSpecV2):
                raise ValueError(f"pilot scenario must use ScenarioSpec 2.x: {spec.id}")
            if spec.id != scenario_reference.scenario_id:
                raise ValueError(f"scenario id drift for {scenario_reference.reference}")
            if scenario_contract_fingerprint(spec) != scenario_reference.contract_hash:
                raise ValueError(
                    f"scenario contract hash drift for {scenario_reference.reference}"
                )
            if spec.counterfactual.group_id != pair_reference.group_id:
                raise ValueError(f"scenario group drift for {scenario_reference.reference}")
            if spec.counterfactual.variant != variant:
                raise ValueError(f"scenario variant drift for {scenario_reference.reference}")
            specs.append(spec)

    if manifest_references != set(matrix.axes.scenario):
        raise ValueError("matrix scenario axis does not match pilot manifest")
    pairs = validate_counterfactual_pairs(specs, require_complete=True)
    actual_pairs = {pair.group_id: pair for pair in pairs}
    if set(actual_pairs) != {item.group_id for item in manifest.pairs}:
        raise ValueError("validated counterfactual pairs do not match pilot manifest")
    for pair_reference in manifest.pairs:
        pair = actual_pairs[pair_reference.group_id]
        if pair.fingerprint != pair_reference.pair_fingerprint:
            raise ValueError(f"pair fingerprint drift for {pair_reference.group_id}")
        unsupported = unsupported_perturbations(pair.dynamic)
        if unsupported:
            names = ", ".join(sorted({item.type for item in unsupported}))
            raise ValueError(
                f"pilot pair {pair.group_id} uses unsupported perturbations: {names}"
            )

    if _pair_set_hash(manifest) != manifest.pair_set_hash:
        raise ValueError("pilot pair_set_hash does not match its pair contracts")
    _verify_artifact(root, manifest.human_review.protocol)
    status_path = _verify_artifact(root, manifest.human_review.status)
    review_status = _validate_review_status(
        manifest=manifest, status_path=status_path
    )

    return ValidatedPilotBundle(
        benchmark_id=manifest.benchmark_id,
        matrix_contract_hash=manifest.matrix.contract_hash,
        pair_set_hash=manifest.pair_set_hash,
        gate_status=review_status.gate_status,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        seeds=tuple(manifest.seeds),
        expected_cells=manifest.expected_cells,
        pairs=tuple(
            ValidatedPilotPair(
                group_id=pair.group_id,
                pair_fingerprint=pair.pair_fingerprint,
                static_scenario_id=pair.static.scenario_id,
                dynamic_scenario_id=pair.dynamic.scenario_id,
            )
            for pair in manifest.pairs
        ),
    )


def validate_pilot_bundle(manifest_path: Path | str) -> dict[str, object]:
    """Fail closed if scenarios, matrix, hashes, or review evidence drift."""

    bundle = load_validated_pilot_bundle(manifest_path)
    return {
        "benchmark_id": bundle.benchmark_id,
        "pairs": len(bundle.pairs),
        "seeds": len(bundle.seeds),
        "cells": bundle.expected_cells,
        "pair_set_hash": bundle.pair_set_hash,
        "gate_status": bundle.gate_status,
    }


__all__ = [
    "PilotManifest",
    "ReviewStatus",
    "ValidatedPilotBundle",
    "ValidatedPilotPair",
    "load_validated_pilot_bundle",
    "validate_pilot_bundle",
]
