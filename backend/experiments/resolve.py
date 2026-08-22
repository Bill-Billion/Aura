"""Resolve author-written matrices into immutable, hash-addressed cells."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import yaml

from backend.engine.run_manager import read_source_revision
from backend.scenarios.fingerprint import scenario_contract_fingerprint
from backend.scenarios.loader import get_scenario, load_scenario_file
from backend.scenarios.spec import ScenarioSpec

from .spec import ExperimentCell, MatrixSpec, ResolvedMatrix, ScenarioContract

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MAX_MATRIX_YAML_BYTES = 4 * 1024 * 1024


class ScenarioResolver(Protocol):
    def resolve(self, reference: str) -> ScenarioContract: ...


class FileOrLibraryScenarioResolver:
    """Resolve a YAML path first, then an id from explicitly supplied libraries."""

    def __init__(
        self,
        *,
        base_dir: Path | str | None = None,
        scenario_dirs: Sequence[Path | str] | None = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else Path.cwd()
        self.scenario_dirs = tuple(Path(path) for path in (scenario_dirs or ()))
        self._cache: dict[str, ScenarioContract] = {}

    def resolve_spec(self, reference: str) -> ScenarioSpec:
        direct_path = Path(reference)
        if not direct_path.is_absolute():
            direct_path = self.base_dir / direct_path
        if direct_path.is_file():
            return load_scenario_file(direct_path)

        spec = get_scenario(
            reference,
            dirs=self.scenario_dirs if self.scenario_dirs else None,
        )
        if spec is None:
            searched = [str(direct_path), *(str(path) for path in self.scenario_dirs)]
            raise ValueError(
                f"scenario reference {reference!r} was not found; searched: "
                + ", ".join(searched)
            )
        return spec

    def resolve(self, reference: str) -> ScenarioContract:
        cached = self._cache.get(reference)
        if cached is not None:
            return cached
        spec = self.resolve_spec(reference)
        direct_path = Path(reference)
        if not direct_path.is_absolute():
            direct_path = self.base_dir / direct_path
        normalized_reference = reference
        if direct_path.is_file():
            resolved_path = direct_path.resolve()
            try:
                normalized_reference = resolved_path.relative_to(
                    _REPOSITORY_ROOT
                ).as_posix()
            except ValueError:
                normalized_reference = resolved_path.as_posix()
        contract = ScenarioContract(
            reference=normalized_reference,
            scenario_id=spec.id,
            scenario_contract_hash=scenario_contract_fingerprint(spec),
        )
        self._cache[reference] = contract
        return contract


def load_matrix_file(path: Path | str) -> MatrixSpec:
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"matrix file does not exist: {path}")
    if path.stat().st_size > MAX_MATRIX_YAML_BYTES:
        raise ValueError(f"matrix YAML exceeds {MAX_MATRIX_YAML_BYTES} bytes: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"matrix YAML is invalid: {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"matrix YAML must contain a mapping: {path}")
    return MatrixSpec.model_validate(raw)


def resolve_matrix(
    spec: MatrixSpec,
    *,
    scenario_resolver: ScenarioResolver | None = None,
    source_revision: str | None = None,
) -> ResolvedMatrix:
    """Expand, fingerprint, sort, and seal a matrix without executing it."""

    resolver = scenario_resolver or FileOrLibraryScenarioResolver()
    revision = source_revision or read_source_revision()
    contracts = {
        reference: resolver.resolve(reference) for reference in spec.axes.scenario
    }
    cells = [
        ExperimentCell.build(
            combination=combination,
            scenario=contracts[combination.scenario],
            experiment_id=spec.matrix_id,
            matrix_spec_hash=spec.contract_hash(),
            source_revision=revision,
            estimated_cost_usd=spec.cost_per_model_usd[combination.model],
        )
        for combination in spec.combinations()
    ]
    return ResolvedMatrix.build(spec=spec, source_revision=revision, cells=cells)


def load_and_resolve_matrix(
    path: Path | str,
    *,
    source_revision: str | None = None,
) -> ResolvedMatrix:
    """Load a matrix and resolve relative scenarios against the matrix directory."""

    matrix_path = Path(path).resolve()
    return resolve_matrix(
        load_matrix_file(matrix_path),
        scenario_resolver=FileOrLibraryScenarioResolver(base_dir=matrix_path.parent),
        source_revision=source_revision,
    )


__all__ = [
    "FileOrLibraryScenarioResolver",
    "MAX_MATRIX_YAML_BYTES",
    "ScenarioResolver",
    "load_and_resolve_matrix",
    "load_matrix_file",
    "resolve_matrix",
]
