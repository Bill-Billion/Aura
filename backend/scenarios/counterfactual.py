"""Structural validation for static/dynamic AuraBench scenario pairs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from backend.scenarios.spec_v2 import ScenarioSpecV2


class CounterfactualPairError(ValueError):
    def __init__(self, group_id: str, reason: str) -> None:
        self.group_id = group_id
        self.reason = reason
        super().__init__(f"counterfactual group {group_id!r}: {reason}")


@dataclass(frozen=True)
class CounterfactualPair:
    group_id: str
    static: ScenarioSpecV2
    dynamic: ScenarioSpecV2

    @property
    def fingerprint(self) -> str:
        payload = {
            "group_id": self.group_id,
            "base": counterfactual_base_projection(self.static),
            "factor": self.static.counterfactual.factor,
            "dynamic_perturbations": [
                item.model_dump(mode="json") for item in self.dynamic.perturbations
            ],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def counterfactual_base_projection(spec: ScenarioSpecV2) -> dict[str, object]:
    """Fields that must be identical for causal attribution within a pair."""

    return spec.model_dump(
        mode="json",
        exclude={"id", "name", "description", "counterfactual", "perturbations"},
    )


def validate_counterfactual_pairs(
    specs: Iterable[ScenarioSpecV2],
    *,
    require_complete: bool = True,
) -> list[CounterfactualPair]:
    """Validate pair cardinality and the single-intervention invariant."""

    grouped: dict[str, list[ScenarioSpecV2]] = {}
    for spec in specs:
        grouped.setdefault(spec.counterfactual.group_id, []).append(spec)

    pairs: list[CounterfactualPair] = []
    for group_id in sorted(grouped):
        group = grouped[group_id]
        by_variant: dict[str, ScenarioSpecV2] = {}
        for spec in group:
            variant = spec.counterfactual.variant
            if variant in by_variant:
                raise CounterfactualPairError(group_id, f"duplicate {variant} variant")
            by_variant[variant] = spec

        missing = sorted({"static", "dynamic"} - set(by_variant))
        if missing:
            if require_complete:
                raise CounterfactualPairError(
                    group_id, "missing variant(s): " + ", ".join(missing)
                )
            continue
        if len(group) != 2:
            raise CounterfactualPairError(
                group_id, "a pair must contain exactly two scenarios"
            )

        static = by_variant["static"]
        dynamic = by_variant["dynamic"]
        if static.id == dynamic.id:
            raise CounterfactualPairError(
                group_id, "static and dynamic ids must differ"
            )
        if static.counterfactual.factor != dynamic.counterfactual.factor:
            raise CounterfactualPairError(
                group_id, "variants declare different factors"
            )
        if static.perturbations:
            raise CounterfactualPairError(
                group_id, "static variant contains perturbations"
            )
        if not dynamic.perturbations:
            raise CounterfactualPairError(
                group_id, "dynamic variant has no perturbation"
            )
        if counterfactual_base_projection(static) != counterfactual_base_projection(
            dynamic
        ):
            raise CounterfactualPairError(
                group_id,
                "variants differ outside id/name/description/variant/perturbations",
            )

        pairs.append(CounterfactualPair(group_id, static, dynamic))

    return pairs


__all__ = [
    "CounterfactualPair",
    "CounterfactualPairError",
    "counterfactual_base_projection",
    "validate_counterfactual_pairs",
]
