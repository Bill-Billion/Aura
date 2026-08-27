"""Strict design-time contract for the 48-pair AuraBench-v1 catalog."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.core.safe_io import read_bounded_regular_file
from backend.experiments.pilot_bundle import (
    ValidatedPilotBundle,
    load_validated_pilot_bundle,
)


MAX_CATALOG_ARTIFACT_BYTES = 4 * 1024 * 1024

ScenarioFamily = Literal[
    "state_revalidation",
    "implicit_intent_noop",
    "temporal_dependency",
    "multi_resident_authority",
    "cross_domain_safety",
    "user_nonstationarity",
    "partial_observability",
    "device_failure_recovery",
]
DatasetSplit = Literal["dev", "validation", "test"]
PerturbationFactor = Literal[
    "resident_state_change",
    "device_failure",
    "conflicting_request",
    "safety_interrupt",
    "observation_delay",
    "feedback_loss",
]

EXPECTED_FAMILIES: tuple[str, ...] = (
    "state_revalidation",
    "implicit_intent_noop",
    "temporal_dependency",
    "multi_resident_authority",
    "cross_domain_safety",
    "user_nonstationarity",
    "partial_observability",
    "device_failure_recovery",
)
EXPECTED_SPLIT_COUNTS = {"dev": 24, "validation": 8, "test": 16}
EXPECTED_FAMILY_SPLIT_COUNTS = {"dev": 3, "validation": 1, "test": 2}
EXPECTED_FACTORS: frozenset[str] = frozenset(
    {
        "resident_state_change",
        "device_failure",
        "conflicting_request",
        "safety_interrupt",
        "observation_delay",
        "feedback_loss",
    }
)
EXPECTED_PILOT_METADATA: dict[str, tuple[str, str, bool]] = {
    "comfort_smoke_preemption_008": (
        "cross_domain_safety",
        "safety_interrupt",
        False,
    ),
    "partial_feedback_loss_007": (
        "partial_observability",
        "feedback_loss",
        False,
    ),
    "read_then_leave_001": (
        "user_nonstationarity",
        "resident_state_change",
        False,
    ),
    "single_feedback_loss_006": (
        "partial_observability",
        "feedback_loss",
        False,
    ),
    "target_failure_before_execution_001": (
        "device_failure_recovery",
        "device_failure",
        False,
    ),
    "target_failure_during_execution_001": (
        "device_failure_recovery",
        "device_failure",
        False,
    ),
    "unrelated_camera_failure_001": (
        "device_failure_recovery",
        "device_failure",
        True,
    ),
    "unrelated_resident_activity_002": (
        "user_nonstationarity",
        "resident_state_change",
        True,
    ),
}
EXPECTED_PILOT_GROUP_IDS: frozenset[str] = frozenset(EXPECTED_PILOT_METADATA)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogArtifactReference(_StrictModel):
    reference: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SourceRecord(_StrictModel):
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    title: str = Field(min_length=1)
    authors: str = Field(min_length=1)
    year: int = Field(ge=2000, le=2100)
    source_type: Literal[
        "peer_reviewed_paper",
        "preprint",
        "official_dataset",
        "official_standard_guidance",
    ]
    venue: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")
    doi: str | None = None
    evidence_tags: tuple[str, ...] = Field(min_length=1)
    use_in_aura: str = Field(min_length=20)
    limits: str = Field(min_length=20)

    @field_validator("evidence_tags")
    @classmethod
    def _unique_evidence_tags(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("source evidence_tags must be unique")
        return tuple(sorted(values))


class SourceRegistry(_StrictModel):
    source_registry_schema_version: Literal["1.0"] = "1.0"
    registry_id: Literal["aurabench_v1_scenario_sources"]
    accessed_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sources: tuple[SourceRecord, ...] = Field(min_length=1)

    @field_validator("sources")
    @classmethod
    def _unique_sources(cls, values: tuple[SourceRecord, ...]) -> tuple[SourceRecord, ...]:
        source_ids = [source.source_id for source in values]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_ids must be unique")
        return tuple(sorted(values, key=lambda source: source.source_id))


class CatalogPair(_StrictModel):
    pair_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    template_group: str = Field(pattern=r"^[a-z0-9][a-z0-9_]*$")
    title: str = Field(min_length=1)
    family: ScenarioFamily
    split: DatasetSplit
    difficulty: Literal["easy", "medium", "hard"]
    factor: PerturbationFactor
    negative_control: bool = False
    origin: Literal["new", "aurabench_dev_pilot"] = "new"
    pilot_group_id: str | None = None
    pilot_pair_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    implementation_status: Literal["planned", "implemented"] = "planned"
    review_status: Literal["pending", "approved"] = "pending"
    source_ids: tuple[str, ...] = Field(min_length=2)
    required_evidence_tags: tuple[str, ...] = Field(min_length=2)
    realism_basis: str = Field(min_length=20)
    runtime_requirements: tuple[str, ...] = Field(min_length=1)
    static_reference: str | None = None
    dynamic_reference: str | None = None

    @field_validator("source_ids", "required_evidence_tags", "runtime_requirements")
    @classmethod
    def _unique_tuple(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("catalog pair list values must be unique")
        return tuple(sorted(values))

    @model_validator(mode="after")
    def _lifecycle_contract(self) -> "CatalogPair":
        if self.origin == "aurabench_dev_pilot" and (
            not self.pilot_group_id or not self.pilot_pair_fingerprint
        ):
            raise ValueError(
                "pilot-origin pairs require pilot_group_id and pilot_pair_fingerprint"
            )
        if (
            self.origin == "aurabench_dev_pilot"
            and self.pair_id != self.pilot_group_id
        ):
            raise ValueError("pilot-origin pair_id must preserve pilot_group_id")
        if self.origin == "new" and (
            self.pilot_group_id is not None
            or self.pilot_pair_fingerprint is not None
        ):
            raise ValueError("new pairs must not declare pilot inheritance metadata")
        references = (self.static_reference, self.dynamic_reference)
        if self.implementation_status == "planned" and any(references):
            raise ValueError("planned pairs must not claim scenario references")
        if self.implementation_status == "implemented" and not all(references):
            raise ValueError("implemented pairs require static and dynamic references")
        if self.review_status == "approved" and self.implementation_status != "implemented":
            raise ValueError("review approval requires implemented scenarios")
        return self


class BenchmarkCatalog(_StrictModel):
    catalog_schema_version: Literal["1.0"] = "1.0"
    benchmark_id: Literal["aurabench_v1"]
    release_stage: Literal["design", "implementation", "sealed"] = "design"
    expected_pairs: Literal[48] = 48
    seeds: tuple[int, ...] = Field(min_length=5, max_length=5)
    pilot_manifest: CatalogArtifactReference
    source_registry: CatalogArtifactReference
    evidence_dossier: CatalogArtifactReference
    pairs: tuple[CatalogPair, ...] = Field(min_length=48, max_length=48)

    @field_validator("seeds")
    @classmethod
    def _unique_seeds(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if len(values) != len(set(values)):
            raise ValueError("catalog seeds must be unique")
        return tuple(sorted(values))

    @field_validator("pairs")
    @classmethod
    def _unique_pairs(cls, values: tuple[CatalogPair, ...]) -> tuple[CatalogPair, ...]:
        pair_ids = [pair.pair_id for pair in values]
        if len(pair_ids) != len(set(pair_ids)):
            raise ValueError("catalog pair_ids must be unique")
        template_groups = [pair.template_group for pair in values]
        if len(template_groups) != len(set(template_groups)):
            raise ValueError("catalog template_groups must be unique")
        return tuple(sorted(values, key=lambda pair: pair.pair_id))

    @model_validator(mode="after")
    def _balanced_design(self) -> "BenchmarkCatalog":
        split_counts = Counter(pair.split for pair in self.pairs)
        if dict(split_counts) != EXPECTED_SPLIT_COUNTS:
            raise ValueError(
                f"catalog split counts must be {EXPECTED_SPLIT_COUNTS}, got {dict(split_counts)}"
            )

        family_counts = Counter(pair.family for pair in self.pairs)
        expected_family_counts = {family: 6 for family in EXPECTED_FAMILIES}
        if dict(family_counts) != expected_family_counts:
            raise ValueError(
                "catalog must contain exactly six pairs per family; "
                f"got {dict(family_counts)}"
            )

        by_family: dict[str, list[CatalogPair]] = defaultdict(list)
        for pair in self.pairs:
            by_family[pair.family].append(pair)
        for family, pairs in by_family.items():
            counts = Counter(pair.split for pair in pairs)
            if dict(counts) != EXPECTED_FAMILY_SPLIT_COUNTS:
                raise ValueError(
                    f"family {family} split counts must be "
                    f"{EXPECTED_FAMILY_SPLIT_COUNTS}, got {dict(counts)}"
                )
            controls = sum(pair.negative_control for pair in pairs)
            if controls != 1:
                raise ValueError(
                    f"family {family} must contain exactly one negative control"
                )

        factors = {pair.factor for pair in self.pairs}
        if factors != EXPECTED_FACTORS:
            missing = sorted(EXPECTED_FACTORS - factors)
            extra = sorted(factors - EXPECTED_FACTORS)
            raise ValueError(
                f"catalog perturbation coverage drift; missing={missing}, extra={extra}"
            )

        inherited_pairs = [
            pair for pair in self.pairs if pair.origin == "aurabench_dev_pilot"
        ]
        if len(inherited_pairs) != 8:
            raise ValueError("catalog must inherit exactly eight pilot pairs")
        inherited = {pair.pilot_group_id for pair in inherited_pairs}
        if inherited != EXPECTED_PILOT_GROUP_IDS:
            raise ValueError(
                "catalog pilot inheritance drift; "
                f"expected={sorted(EXPECTED_PILOT_GROUP_IDS)}, "
                f"got={sorted(value for value in inherited if value is not None)}"
            )

        if self.release_stage == "design" and any(
            pair.implementation_status != "planned" for pair in self.pairs
        ):
            raise ValueError("design-stage catalog may only contain planned pairs")
        if self.release_stage == "sealed" and any(
            pair.review_status != "approved" for pair in self.pairs
        ):
            raise ValueError("sealed catalog requires every pair to be approved")
        return self


class ValidatedBenchmarkCatalog(_FrozenStrictModel):
    benchmark_id: str
    release_stage: str
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pilot_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_dossier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pairs: int
    scenarios: int
    seeds: tuple[int, ...]
    split_counts: dict[str, int]
    family_counts: dict[str, int]
    negative_controls: int
    sources: int
    factor_counts: dict[str, int]
    origin_counts: dict[str, int]
    implementation_status_counts: dict[str, int]
    review_status_counts: dict[str, int]


def _read_artifact(path: Path) -> bytes:
    return read_bounded_regular_file(path, max_bytes=MAX_CATALOG_ARTIFACT_BYTES)


class _StrictCatalogLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects aliases and duplicate mapping keys."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "YAML aliases are not allowed in benchmark catalogs",
                self.peek_event().start_mark,
            )
        return super().compose_node(parent, index)


def _construct_unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictCatalogLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_yaml(path: Path, model_type):
    encoded = _read_artifact(path)
    try:
        raw = yaml.load(encoded.decode("utf-8"), Loader=_StrictCatalogLoader)
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"invalid YAML artifact {path}: {exc}") from exc
    return model_type.model_validate(raw), encoded


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _resolve_reference(root: Path, reference: str) -> Path:
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"catalog artifact reference must stay inside bundle: {reference}")
    return root / candidate


def _verify_hash(path: Path, expected: str) -> bytes:
    encoded = _read_artifact(path)
    actual = hashlib.sha256(encoded).hexdigest()
    if actual != expected:
        raise ValueError(
            f"catalog artifact hash drift for {path.name}: expected {expected}, got {actual}"
        )
    return encoded


def _validate_evidence(catalog: BenchmarkCatalog, registry: SourceRegistry) -> None:
    sources = {source.source_id: source for source in registry.sources}
    for pair in catalog.pairs:
        unknown = sorted(set(pair.source_ids) - set(sources))
        if unknown:
            raise ValueError(
                f"pair {pair.pair_id} references unknown sources: {', '.join(unknown)}"
            )
        supported_tags = {
            tag
            for source_id in pair.source_ids
            for tag in sources[source_id].evidence_tags
        }
        unsupported = sorted(set(pair.required_evidence_tags) - supported_tags)
        if unsupported:
            raise ValueError(
                f"pair {pair.pair_id} has unsupported evidence tags: "
                + ", ".join(unsupported)
            )
        source_types = {sources[source_id].source_type for source_id in pair.source_ids}
        if source_types == {"preprint"}:
            raise ValueError(
                f"pair {pair.pair_id} cannot rely only on preprints"
            )


def _validate_pilot_inheritance(
    catalog: BenchmarkCatalog,
    pilot: ValidatedPilotBundle,
) -> None:
    if pilot.benchmark_id != "aurabench_dev_pilot":
        raise ValueError("pilot inheritance must reference aurabench_dev_pilot")
    pilot_pairs = {pair.group_id: pair for pair in pilot.pairs}
    if set(pilot_pairs) != EXPECTED_PILOT_GROUP_IDS:
        raise ValueError("validated pilot pair inventory does not match PR23 contract")

    inherited = [
        pair for pair in catalog.pairs if pair.origin == "aurabench_dev_pilot"
    ]
    for pair in inherited:
        group_id = pair.pilot_group_id
        if group_id is None:
            raise ValueError("pilot-origin pair is missing pilot_group_id")
        expected_family, expected_factor, expected_control = EXPECTED_PILOT_METADATA[
            group_id
        ]
        actual_metadata = (pair.family, pair.factor, pair.negative_control)
        expected_metadata = (expected_family, expected_factor, expected_control)
        if actual_metadata != expected_metadata:
            raise ValueError(
                f"pilot metadata drift for {group_id}: "
                f"expected={expected_metadata}, got={actual_metadata}"
            )
        if pair.pilot_pair_fingerprint != pilot_pairs[group_id].pair_fingerprint:
            raise ValueError(f"pilot pair fingerprint drift for {group_id}")


def load_validated_benchmark_catalog(
    catalog_path: Path | str,
) -> ValidatedBenchmarkCatalog:
    """Validate design balance, source coverage, hashes, and lifecycle claims."""

    path = Path(catalog_path).absolute()
    catalog, catalog_bytes = _load_yaml(path, BenchmarkCatalog)
    root = path.parent

    benchmarks_root = root.parent
    pilot_manifest_path = _resolve_reference(
        benchmarks_root,
        catalog.pilot_manifest.reference,
    )
    pilot = load_validated_pilot_bundle(pilot_manifest_path)
    if pilot.manifest_sha256 != catalog.pilot_manifest.sha256:
        raise ValueError(
            "catalog artifact hash drift for pilot manifest: "
            f"expected {catalog.pilot_manifest.sha256}, "
            f"got {pilot.manifest_sha256}"
        )
    _validate_pilot_inheritance(catalog, pilot)

    registry_path = _resolve_reference(root, catalog.source_registry.reference)
    registry_bytes = _verify_hash(registry_path, catalog.source_registry.sha256)
    try:
        registry_raw = json.loads(
            registry_bytes,
            object_pairs_hook=_unique_json_object,
        )
        registry = SourceRegistry.model_validate(registry_raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"invalid source registry {registry_path}: {exc}") from exc
    _validate_evidence(catalog, registry)

    dossier_path = _resolve_reference(root, catalog.evidence_dossier.reference)
    dossier_bytes = _verify_hash(dossier_path, catalog.evidence_dossier.sha256)

    return ValidatedBenchmarkCatalog(
        benchmark_id=catalog.benchmark_id,
        release_stage=catalog.release_stage,
        catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(),
        pilot_manifest_sha256=pilot.manifest_sha256,
        source_registry_sha256=hashlib.sha256(registry_bytes).hexdigest(),
        evidence_dossier_sha256=hashlib.sha256(dossier_bytes).hexdigest(),
        pairs=len(catalog.pairs),
        scenarios=2 * len(catalog.pairs),
        seeds=catalog.seeds,
        split_counts=dict(sorted(Counter(pair.split for pair in catalog.pairs).items())),
        family_counts=dict(sorted(Counter(pair.family for pair in catalog.pairs).items())),
        negative_controls=sum(pair.negative_control for pair in catalog.pairs),
        sources=len(registry.sources),
        factor_counts=dict(sorted(Counter(pair.factor for pair in catalog.pairs).items())),
        origin_counts=dict(sorted(Counter(pair.origin for pair in catalog.pairs).items())),
        implementation_status_counts=dict(
            sorted(Counter(pair.implementation_status for pair in catalog.pairs).items())
        ),
        review_status_counts=dict(
            sorted(Counter(pair.review_status for pair in catalog.pairs).items())
        ),
    )


def validate_benchmark_catalog(catalog_path: Path | str) -> dict[str, Any]:
    """Return a stable CLI summary after fail-closed catalog validation."""

    return load_validated_benchmark_catalog(catalog_path).model_dump(mode="json")


__all__ = [
    "BenchmarkCatalog",
    "CatalogPair",
    "MAX_CATALOG_ARTIFACT_BYTES",
    "SourceRegistry",
    "ValidatedBenchmarkCatalog",
    "load_validated_benchmark_catalog",
    "validate_benchmark_catalog",
]
