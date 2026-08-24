"""Typed, deterministic experiment-matrix contracts for AuraBench.

The matrix is deliberately a finite Cartesian product.  Every exclusion names
one complete cell; there are no wildcard rules whose meaning can silently grow
when a new axis value is added.
"""

from __future__ import annotations

import hashlib
import json
import math
from itertools import product
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from backend.engine.rng import MAX_JSON_SAFE_SEED

MATRIX_SCHEMA_VERSION = "1.0"
CELL_ID_PREFIX = "cell-"
MAX_MATRIX_AXIS_VALUES = 256
MAX_MATRIX_CELLS = 10_000
MAX_MATRIX_STRING_LENGTH = 512


def canonical_json(value: Any) -> str:
    """Return the single canonical JSON representation used by matrix hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MatrixAxes(_StrictModel):
    """All seven finite dimensions in the AuraBench experiment contract."""

    scenario: list[str] = Field(min_length=1, max_length=MAX_MATRIX_AXIS_VALUES)
    seed: list[StrictInt] = Field(min_length=1, max_length=MAX_MATRIX_AXIS_VALUES)
    model: list[str] = Field(min_length=1, max_length=MAX_MATRIX_AXIS_VALUES)
    topology: list[str] = Field(min_length=1, max_length=MAX_MATRIX_AXIS_VALUES)
    governance: list[str] = Field(min_length=1, max_length=MAX_MATRIX_AXIS_VALUES)
    observation: list[str] = Field(min_length=1, max_length=MAX_MATRIX_AXIS_VALUES)
    repetition: list[StrictInt] = Field(
        min_length=1,
        max_length=MAX_MATRIX_AXIS_VALUES,
    )

    @field_validator("scenario", "model", "topology", "governance", "observation")
    @classmethod
    def _unique_nonempty_strings(cls, values: list[str]) -> list[str]:
        if not values:
            raise ValueError("matrix axes must not be empty")
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("matrix axis values must be non-empty strings")
        if any(len(value) > MAX_MATRIX_STRING_LENGTH for value in normalized):
            raise ValueError(
                f"matrix axis strings must not exceed {MAX_MATRIX_STRING_LENGTH} characters"
            )
        if len(normalized) != len(set(normalized)):
            raise ValueError("matrix axis values must be unique")
        return sorted(normalized)

    @field_validator("seed")
    @classmethod
    def _unique_seeds(cls, values: list[int]) -> list[int]:
        if not values:
            raise ValueError("matrix axes must not be empty")
        if any(value < 0 or value > MAX_JSON_SAFE_SEED for value in values):
            raise ValueError(
                f"seed must be between 0 and {MAX_JSON_SAFE_SEED}"
            )
        if len(values) != len(set(values)):
            raise ValueError("matrix axis values must be unique")
        return sorted(values)

    @field_validator("repetition")
    @classmethod
    def _unique_repetitions(cls, values: list[int]) -> list[int]:
        if not values:
            raise ValueError("matrix axes must not be empty")
        if any(value < 0 for value in values):
            raise ValueError("repetition must be non-negative")
        if len(values) != len(set(values)):
            raise ValueError("matrix axis values must be unique")
        return sorted(values)


class ExactExclusion(_StrictModel):
    """One exact seven-axis combination to omit from the resolved matrix."""

    scenario: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    seed: StrictInt = Field(ge=0, le=MAX_JSON_SAFE_SEED)
    model: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    topology: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    governance: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    observation: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    repetition: StrictInt = Field(ge=0)

    def key(self) -> tuple[str, int, str, str, str, str, int]:
        return (
            self.scenario,
            self.seed,
            self.model,
            self.topology,
            self.governance,
            self.observation,
            self.repetition,
        )


class MatrixSpec(_StrictModel):
    """Author-written matrix plus explicit safety budgets."""

    matrix_schema_version: Literal["1.0"] = MATRIX_SCHEMA_VERSION
    matrix_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,95}$")
    axes: MatrixAxes
    exclude: list[ExactExclusion] = Field(
        default_factory=list,
        max_length=MAX_MATRIX_CELLS,
    )
    max_cells: StrictInt = Field(default=256, ge=1, le=MAX_MATRIX_CELLS)
    max_total_cost_usd: float = Field(default=0.0, ge=0.0)
    cost_per_model_usd: dict[str, float] = Field(
        default_factory=lambda: {"rule_based": 0.0, "mocked": 0.0},
        max_length=MAX_MATRIX_AXIS_VALUES,
    )

    @field_validator("max_total_cost_usd")
    @classmethod
    def _finite_cost_limit(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("max_total_cost_usd must be finite")
        return value

    @field_validator("cost_per_model_usd")
    @classmethod
    def _valid_model_costs(cls, values: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for raw_model, raw_cost in values.items():
            model = raw_model.strip()
            cost = float(raw_cost)
            if not model:
                raise ValueError("cost_per_model_usd keys must be non-empty")
            if len(model) > MAX_MATRIX_STRING_LENGTH:
                raise ValueError(
                    f"model names must not exceed {MAX_MATRIX_STRING_LENGTH} characters"
                )
            if not math.isfinite(cost) or cost < 0:
                raise ValueError("model costs must be finite and non-negative")
            normalized[model] = cost
        return normalized

    @model_validator(mode="after")
    def _validate_exclusions_and_costs(self) -> "MatrixSpec":
        declared = {
            "scenario": set(self.axes.scenario),
            "seed": set(self.axes.seed),
            "model": set(self.axes.model),
            "topology": set(self.axes.topology),
            "governance": set(self.axes.governance),
            "observation": set(self.axes.observation),
            "repetition": set(self.axes.repetition),
        }
        keys = [entry.key() for entry in self.exclude]
        if len(keys) != len(set(keys)):
            raise ValueError("exclude entries must be unique")
        for index, exclusion in enumerate(self.exclude):
            for field_name, allowed in declared.items():
                value = getattr(exclusion, field_name)
                if value not in allowed:
                    raise ValueError(
                        f"exclude[{index}].{field_name}={value!r} is not declared "
                        "on that axis"
                    )

        self.exclude = sorted(self.exclude, key=lambda entry: entry.key())

        missing_costs = sorted(set(self.axes.model) - set(self.cost_per_model_usd))
        if missing_costs:
            raise ValueError(
                "cost_per_model_usd must price every model: "
                + ", ".join(missing_costs)
            )
        return self

    def combinations(self) -> list[ExactExclusion]:
        """Expand exclusions first, then enforce global cell and cost caps."""

        excluded = {entry.key() for entry in self.exclude}
        raw_count = math.prod(
            (
                len(self.axes.scenario),
                len(self.axes.seed),
                len(self.axes.model),
                len(self.axes.topology),
                len(self.axes.governance),
                len(self.axes.observation),
                len(self.axes.repetition),
            )
        )
        resolved_count = raw_count - len(excluded)
        if resolved_count > self.max_cells:
            raise ValueError(
                f"resolved matrix has {resolved_count} cells, "
                f"exceeding max_cells={self.max_cells}"
            )
        cells = [
            ExactExclusion(
                scenario=scenario,
                seed=seed,
                model=model,
                topology=topology,
                governance=governance,
                observation=observation,
                repetition=repetition,
            )
            for (
                scenario,
                seed,
                model,
                topology,
                governance,
                observation,
                repetition,
            ) in product(
                self.axes.scenario,
                self.axes.seed,
                self.axes.model,
                self.axes.topology,
                self.axes.governance,
                self.axes.observation,
                self.axes.repetition,
            )
            if (
                scenario,
                seed,
                model,
                topology,
                governance,
                observation,
                repetition,
            )
            not in excluded
        ]
        total_cost = sum(self.cost_per_model_usd[cell.model] for cell in cells)
        if total_cost > self.max_total_cost_usd + 1e-12:
            raise ValueError(
                f"resolved matrix estimated cost ${total_cost:.6f} exceeds "
                f"max_total_cost_usd=${self.max_total_cost_usd:.6f}"
            )
        return cells

    def contract_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class ScenarioContract(_StrictModel):
    reference: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    scenario_id: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    scenario_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentCell(_StrictModel):
    """One fully resolved, immutable experimental condition."""

    matrix_schema_version: Literal["1.0"] = MATRIX_SCHEMA_VERSION
    experiment_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,95}$")
    matrix_spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scenario_reference: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    scenario_id: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    scenario_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: StrictInt = Field(ge=0, le=MAX_JSON_SAFE_SEED)
    model: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    topology: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    governance: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    observation: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    repetition: StrictInt = Field(ge=0)
    source_revision: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    estimated_cost_usd: float = Field(ge=0.0)
    cell_id: str = Field(pattern=r"^cell-[0-9a-f]{32}$")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "matrix_schema_version": self.matrix_schema_version,
            "scenario_id": self.scenario_id,
            "scenario_contract_hash": self.scenario_contract_hash,
            "seed": self.seed,
            "model": self.model,
            "topology": self.topology,
            "governance": self.governance,
            "observation": self.observation,
            "repetition": self.repetition,
            "source_revision": self.source_revision,
        }

    @model_validator(mode="after")
    def _validate_cell_id(self) -> "ExperimentCell":
        expected = CELL_ID_PREFIX + sha256_json(self.identity_payload())[:32]
        if self.cell_id != expected:
            raise ValueError("cell_id does not match the resolved condition")
        return self

    @classmethod
    def build(
        cls,
        *,
        combination: ExactExclusion,
        scenario: ScenarioContract,
        experiment_id: str,
        matrix_spec_hash: str,
        source_revision: str,
        estimated_cost_usd: float,
    ) -> "ExperimentCell":
        identity: dict[str, Any] = {
            "matrix_schema_version": MATRIX_SCHEMA_VERSION,
            "scenario_id": scenario.scenario_id,
            "scenario_contract_hash": scenario.scenario_contract_hash,
            "seed": combination.seed,
            "model": combination.model,
            "topology": combination.topology,
            "governance": combination.governance,
            "observation": combination.observation,
            "repetition": combination.repetition,
            "source_revision": source_revision,
        }
        cell_id = CELL_ID_PREFIX + sha256_json(identity)[:32]
        return cls(
            **identity,
            experiment_id=experiment_id,
            matrix_spec_hash=matrix_spec_hash,
            scenario_reference=scenario.reference,
            estimated_cost_usd=estimated_cost_usd,
            cell_id=cell_id,
        )

    def contract_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


class ResolvedMatrix(_StrictModel):
    """Stable, sorted execution manifest persisted before any cell runs."""

    matrix_schema_version: Literal["1.0"] = MATRIX_SCHEMA_VERSION
    matrix_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,95}$")
    source_revision: str = Field(min_length=1, max_length=MAX_MATRIX_STRING_LENGTH)
    spec_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    total_estimated_cost_usd: float = Field(ge=0.0)
    cells: list[ExperimentCell] = Field(max_length=MAX_MATRIX_CELLS)
    matrix_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(
        cls,
        *,
        spec: MatrixSpec,
        source_revision: str,
        cells: list[ExperimentCell],
    ) -> "ResolvedMatrix":
        ordered = sorted(cells, key=lambda cell: cell.cell_id)
        payload = {
            "matrix_schema_version": MATRIX_SCHEMA_VERSION,
            "matrix_id": spec.matrix_id,
            "source_revision": source_revision,
            "spec_hash": spec.contract_hash(),
            "total_estimated_cost_usd": sum(
                cell.estimated_cost_usd for cell in ordered
            ),
            "cells": [cell.model_dump(mode="json") for cell in ordered],
        }
        return cls(**payload, matrix_hash=sha256_json(payload))

    @model_validator(mode="after")
    def _validate_order_and_hash(self) -> "ResolvedMatrix":
        ids = [cell.cell_id for cell in self.cells]
        if ids != sorted(ids):
            raise ValueError("resolved cells must be sorted by cell_id")
        if len(ids) != len(set(ids)):
            raise ValueError("resolved cells must have unique cell_id values")
        for cell in self.cells:
            if cell.experiment_id != self.matrix_id:
                raise ValueError("cell experiment_id does not match matrix_id")
            if cell.matrix_spec_hash != self.spec_hash:
                raise ValueError("cell matrix_spec_hash does not match spec_hash")
            if cell.source_revision != self.source_revision:
                raise ValueError("cell source_revision does not match matrix source_revision")
        expected_cost = sum(cell.estimated_cost_usd for cell in self.cells)
        if not math.isclose(
            self.total_estimated_cost_usd,
            expected_cost,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("matrix total cost does not match its cells")
        payload = self.model_dump(mode="json", exclude={"matrix_hash"})
        expected = sha256_json(payload)
        if self.matrix_hash != expected:
            raise ValueError("resolved matrix hash does not match its contents")
        return self


__all__ = [
    "CELL_ID_PREFIX",
    "MAX_MATRIX_AXIS_VALUES",
    "MAX_MATRIX_CELLS",
    "MAX_MATRIX_STRING_LENGTH",
    "MATRIX_SCHEMA_VERSION",
    "ExactExclusion",
    "ExperimentCell",
    "MatrixAxes",
    "MatrixSpec",
    "ResolvedMatrix",
    "ScenarioContract",
    "canonical_json",
    "sha256_json",
]
