"""Deterministic, dependency-free statistics for validated experiment pairs.

This module deliberately does not load artifacts or choose cohorts.  Callers
must first construct complete, fairness-valid pairs; the functions below then
apply the pre-declared estimator and test without deleting either side.
"""

from __future__ import annotations

import math
import random
import statistics as std_statistics
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from backend.engine.rng import MAX_JSON_SAFE_SEED

from .spec import sha256_json


STATISTICS_SCHEMA_VERSION = "1.0"
DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
MAX_BOOTSTRAP_RESAMPLES = 100_000
MAX_BOOTSTRAP_DRAWS = 50_000_000
MAX_STATISTICAL_PAIRS = 10_000
MAX_HOLM_HYPOTHESES = 256
EXACT_WILCOXON_MAX_NONZERO = 50


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class BootstrapConfig(_StrictModel):
    root_seed: StrictInt = Field(default=0, ge=0, le=MAX_JSON_SAFE_SEED)
    resamples: StrictInt = Field(
        default=DEFAULT_BOOTSTRAP_RESAMPLES,
        ge=1,
        le=MAX_BOOTSTRAP_RESAMPLES,
    )
    confidence_level: StrictFloat = Field(default=0.95, gt=0.0, lt=1.0)


class BinaryPair(_StrictModel):
    pair_id: str = Field(min_length=1, max_length=256)
    treatment: StrictBool
    reference: StrictBool

    @field_validator("pair_id")
    @classmethod
    def _non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pair_id must not be blank")
        return value


class ContinuousPair(_StrictModel):
    pair_id: str = Field(min_length=1, max_length=256)
    treatment: StrictFloat
    reference: StrictFloat

    @field_validator("pair_id")
    @classmethod
    def _non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pair_id must not be blank")
        return value


class ConfidenceInterval(_StrictModel):
    confidence_level: StrictFloat = Field(gt=0.0, lt=1.0)
    lower: StrictFloat
    upper: StrictFloat
    method: Literal["paired_percentile_type7", "wilson_score"]

    @model_validator(mode="after")
    def _ordered_bounds(self) -> "ConfidenceInterval":
        if self.lower > self.upper:
            raise ValueError("confidence interval lower bound exceeds upper bound")
        return self


class BootstrapResult(_StrictModel):
    method: Literal["paired_percentile_type7"] = "paired_percentile_type7"
    statistic: Literal["mean_pair_difference", "median_pair_difference"]
    resamples: StrictInt = Field(ge=1, le=MAX_BOOTSTRAP_RESAMPLES)
    derived_seed: StrictInt = Field(ge=0, le=MAX_JSON_SAFE_SEED)
    confidence_interval: ConfidenceInterval


class McNemarResult(_StrictModel):
    method: Literal["exact_binomial_two_sided"] = "exact_binomial_two_sided"
    treatment_only_successes: StrictInt = Field(ge=0)
    reference_only_successes: StrictInt = Field(ge=0)
    discordant_pairs: StrictInt = Field(ge=0)
    statistic: StrictInt = Field(ge=0)
    p_value: Decimal = Field(ge=Decimal(0), le=Decimal(1))

    @model_validator(mode="after")
    def _consistent_counts(self) -> "McNemarResult":
        if self.discordant_pairs != (
            self.treatment_only_successes + self.reference_only_successes
        ):
            raise ValueError("discordant pair count is inconsistent")
        if self.statistic != min(
            self.treatment_only_successes,
            self.reference_only_successes,
        ):
            raise ValueError("McNemar statistic is inconsistent")
        if not self.p_value.is_finite():
            raise ValueError("p_value must be finite")
        return self


class BinaryPairedResult(_StrictModel):
    statistics_schema_version: Literal["1.0"] = STATISTICS_SCHEMA_VERSION
    analysis_kind: Literal["binary_paired"] = "binary_paired"
    analysis_id: str = Field(min_length=1, max_length=256)
    effect_direction: Literal["treatment_minus_reference"] = (
        "treatment_minus_reference"
    )
    pair_count: StrictInt = Field(gt=0, le=MAX_STATISTICAL_PAIRS)
    treatment_successes: StrictInt = Field(ge=0)
    reference_successes: StrictInt = Field(ge=0)
    risk_difference: StrictFloat = Field(ge=-1.0, le=1.0)
    bootstrap: BootstrapResult
    mcnemar: McNemarResult

    @model_validator(mode="after")
    def _counts_fit_sample(self) -> "BinaryPairedResult":
        if self.treatment_successes > self.pair_count:
            raise ValueError("treatment successes exceed pair count")
        if self.reference_successes > self.pair_count:
            raise ValueError("reference successes exceed pair count")
        expected_difference = (
            self.treatment_successes - self.reference_successes
        ) / self.pair_count
        if not math.isclose(
            self.risk_difference,
            expected_difference,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("risk difference is inconsistent with success counts")
        if (
            self.mcnemar.treatment_only_successes
            - self.mcnemar.reference_only_successes
            != self.treatment_successes - self.reference_successes
        ):
            raise ValueError("McNemar discordance is inconsistent with success counts")
        if self.bootstrap.statistic != "mean_pair_difference":
            raise ValueError("binary result requires mean-difference bootstrap")
        return self


class WilcoxonResult(_StrictModel):
    method: Literal[
        "exact_sign_permutation",
        "normal_approximation",
        "degenerate_all_zero",
    ]
    pair_count: StrictInt = Field(gt=0, le=MAX_STATISTICAL_PAIRS)
    nonzero_pair_count: StrictInt = Field(ge=0)
    zero_difference_count: StrictInt = Field(ge=0)
    positive_rank_sum: StrictFloat = Field(ge=0.0)
    negative_rank_sum: StrictFloat = Field(ge=0.0)
    statistic: StrictFloat = Field(ge=0.0)
    tie_group_sizes: list[StrictInt] = Field(default_factory=list)
    normal_variance: StrictFloat | None = Field(default=None, gt=0.0)
    z_value: StrictFloat | None = Field(default=None, ge=0.0)
    p_value: Decimal = Field(ge=Decimal(0), le=Decimal(1))

    @model_validator(mode="after")
    def _consistent_shape(self) -> "WilcoxonResult":
        if self.nonzero_pair_count + self.zero_difference_count != self.pair_count:
            raise ValueError("Wilcoxon zero/nonzero counts are inconsistent")
        if not math.isclose(
            self.statistic,
            min(self.positive_rank_sum, self.negative_rank_sum),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("Wilcoxon statistic is inconsistent")
        if not self.p_value.is_finite():
            raise ValueError("p_value must be finite")
        expected_rank_total = self.nonzero_pair_count * (
            self.nonzero_pair_count + 1
        ) / 2.0
        if not math.isclose(
            self.positive_rank_sum + self.negative_rank_sum,
            expected_rank_total,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("Wilcoxon rank sums are inconsistent")
        if any(size < 2 for size in self.tie_group_sizes):
            raise ValueError("Wilcoxon tie groups must contain at least two ranks")
        if self.method == "normal_approximation":
            if self.normal_variance is None or self.z_value is None:
                raise ValueError("normal Wilcoxon result requires variance and z")
            if self.nonzero_pair_count <= EXACT_WILCOXON_MAX_NONZERO:
                raise ValueError("normal Wilcoxon result is below exact threshold")
        elif self.method == "exact_sign_permutation":
            if self.nonzero_pair_count == 0:
                raise ValueError("exact Wilcoxon result requires nonzero differences")
            if self.nonzero_pair_count > EXACT_WILCOXON_MAX_NONZERO:
                raise ValueError("exact Wilcoxon result exceeds exact threshold")
            if self.normal_variance is not None or self.z_value is not None:
                raise ValueError("exact Wilcoxon result forbids normal fields")
        elif self.nonzero_pair_count != 0:
            raise ValueError("degenerate Wilcoxon result requires all-zero differences")
        if self.method != "normal_approximation" and (
            self.normal_variance is not None or self.z_value is not None
        ):
            raise ValueError("exact/degenerate Wilcoxon result forbids normal fields")
        return self


class ContinuousPairedResult(_StrictModel):
    statistics_schema_version: Literal["1.0"] = STATISTICS_SCHEMA_VERSION
    analysis_kind: Literal["continuous_paired"] = "continuous_paired"
    analysis_id: str = Field(min_length=1, max_length=256)
    effect_direction: Literal["treatment_minus_reference"] = (
        "treatment_minus_reference"
    )
    pair_count: StrictInt = Field(gt=0, le=MAX_STATISTICAL_PAIRS)
    median_difference: StrictFloat
    bootstrap: BootstrapResult
    wilcoxon: WilcoxonResult

    @model_validator(mode="after")
    def _consistent_method(self) -> "ContinuousPairedResult":
        if self.bootstrap.statistic != "median_pair_difference":
            raise ValueError("continuous result requires median-difference bootstrap")
        if self.wilcoxon.pair_count != self.pair_count:
            raise ValueError("continuous and Wilcoxon pair counts differ")
        return self


class ProportionResult(_StrictModel):
    statistics_schema_version: Literal["1.0"] = STATISTICS_SCHEMA_VERSION
    analysis_kind: Literal["proportion"] = "proportion"
    status: Literal["ok", "unevaluable"]
    successes: StrictInt = Field(ge=0)
    total: StrictInt = Field(ge=0)
    estimate: StrictFloat | None = Field(default=None, ge=0.0, le=1.0)
    confidence_interval: ConfidenceInterval | None = None

    @model_validator(mode="after")
    def _consistent_shape(self) -> "ProportionResult":
        if self.successes > self.total:
            raise ValueError("successes exceed total")
        if self.total == 0:
            if (
                self.status != "unevaluable"
                or self.estimate is not None
                or self.confidence_interval is not None
            ):
                raise ValueError("zero-total proportion must be unevaluable")
        elif (
            self.status != "ok"
            or self.estimate is None
            or self.confidence_interval is None
        ):
            raise ValueError("positive-total proportion requires estimate and interval")
        if (
            self.confidence_interval is not None
            and self.confidence_interval.method != "wilson_score"
        ):
            raise ValueError("proportion result requires a Wilson score interval")
        return self


class HypothesisPValue(_StrictModel):
    hypothesis_id: str = Field(min_length=1, max_length=256)
    p_value: Decimal = Field(ge=Decimal(0), le=Decimal(1))

    @field_validator("hypothesis_id")
    @classmethod
    def _non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("hypothesis_id must not be blank")
        return value

    @field_validator("p_value")
    @classmethod
    def _finite_p_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("p_value must be finite")
        return value


class HolmAdjustment(_StrictModel):
    hypothesis_id: str = Field(min_length=1, max_length=256)
    raw_p_value: Decimal = Field(ge=Decimal(0), le=Decimal(1))
    adjusted_p_value: Decimal = Field(ge=Decimal(0), le=Decimal(1))
    reject_null: StrictBool


class HolmFamilyResult(_StrictModel):
    statistics_schema_version: Literal["1.0"] = STATISTICS_SCHEMA_VERSION
    method: Literal["holm_bonferroni"] = "holm_bonferroni"
    status: Literal["complete"] = "complete"
    family_id: str = Field(min_length=1, max_length=256)
    alpha: Decimal = Field(gt=Decimal(0), lt=Decimal(1))
    hypothesis_count: StrictInt = Field(gt=0)
    adjustment_order: list[str] = Field(min_length=1)
    adjustments: list[HolmAdjustment] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_family(self) -> "HolmFamilyResult":
        adjusted_ids = [item.hypothesis_id for item in self.adjustments]
        if self.hypothesis_count != len(adjusted_ids):
            raise ValueError("Holm hypothesis count is inconsistent")
        if len(adjusted_ids) != len(set(adjusted_ids)):
            raise ValueError("Holm adjustments contain duplicate hypotheses")
        if adjusted_ids != sorted(adjusted_ids):
            raise ValueError("Holm adjustments must be sorted by hypothesis_id")
        if len(self.adjustment_order) != self.hypothesis_count or len(
            self.adjustment_order
        ) != len(set(self.adjustment_order)):
            raise ValueError("Holm adjustment order must be complete and unique")
        if set(self.adjustment_order) != set(adjusted_ids):
            raise ValueError("Holm adjustment order is incomplete")
        return self


def _validate_identifier(
    value: str,
    *,
    field: str,
    max_length: int = 256,
) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(
            f"{field} must be a non-blank string of at most {max_length} characters"
        )
    return value


def _validate_pair_ids(pair_ids: Sequence[str]) -> list[str]:
    if not pair_ids:
        raise ValueError("paired analysis requires at least one pair")
    if len(pair_ids) > MAX_STATISTICAL_PAIRS:
        raise ValueError(f"paired analysis exceeds {MAX_STATISTICAL_PAIRS} pairs")
    ordered = sorted(
        _validate_identifier(value, field="pair_id") for value in pair_ids
    )
    if len(ordered) != len(set(ordered)):
        raise ValueError("pair_id values must be unique")
    return ordered


def derive_bootstrap_seed(
    *,
    root_seed: int,
    namespace: str,
    pair_ids: Sequence[str],
) -> int:
    """Derive one stable named RNG substream independent of input order."""

    if isinstance(root_seed, bool) or not isinstance(root_seed, int):
        raise TypeError("root_seed must be an integer")
    if root_seed < 0 or root_seed > MAX_JSON_SAFE_SEED:
        raise ValueError(f"root_seed must be between 0 and {MAX_JSON_SAFE_SEED}")
    _validate_identifier(namespace, field="namespace", max_length=512)
    ordered_ids = _validate_pair_ids(pair_ids)
    digest = sha256_json(
        {
            "statistics_schema_version": STATISTICS_SCHEMA_VERSION,
            "root_seed": root_seed,
            "namespace": namespace,
            "pair_ids": ordered_ids,
        }
    )
    return int(digest[:13], 16)


def type7_quantile(values: Sequence[float], probability: float) -> float:
    """Return the Hyndman-Fan type-7 sample quantile used by bootstrap CIs."""

    if not values:
        raise ValueError("quantile requires at least one value")
    if len(values) > MAX_BOOTSTRAP_RESAMPLES:
        raise ValueError(f"quantile exceeds {MAX_BOOTSTRAP_RESAMPLES} values")
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        raise TypeError("probability must be numeric")
    probability = float(probability)
    if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
        raise ValueError("probability must be finite and between 0 and 1")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in values
    ):
        raise TypeError("quantile values must be numeric and non-boolean")
    ordered = sorted(float(value) for value in values)
    if any(not math.isfinite(value) for value in ordered):
        raise ValueError("quantile values must be finite")
    position = (len(ordered) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (
        ordered[upper_index] - ordered[lower_index]
    )


def _decimal_ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0 or numerator < 0:
        raise ValueError("probability ratio must be non-negative")
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return Decimal(numerator) / Decimal(denominator)


def exact_mcnemar(
    treatment_only_successes: int,
    reference_only_successes: int,
) -> McNemarResult:
    """Return the exact two-sided binomial McNemar test."""

    for name, value in (
        ("treatment_only_successes", treatment_only_successes),
        ("reference_only_successes", reference_only_successes),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must be non-negative")

    discordant = treatment_only_successes + reference_only_successes
    if discordant > MAX_STATISTICAL_PAIRS:
        raise ValueError(f"McNemar test exceeds {MAX_STATISTICAL_PAIRS} pairs")
    statistic = min(treatment_only_successes, reference_only_successes)
    if discordant == 0:
        p_value = Decimal(1)
    else:
        term = 1
        lower_tail_numerator = 1
        for k in range(statistic):
            term = term * (discordant - k) // (k + 1)
            lower_tail_numerator += term
        denominator = 1 << discordant
        numerator = min(denominator, 2 * lower_tail_numerator)
        p_value = _decimal_ratio(numerator, denominator)

    return McNemarResult(
        treatment_only_successes=treatment_only_successes,
        reference_only_successes=reference_only_successes,
        discordant_pairs=discordant,
        statistic=statistic,
        p_value=p_value,
    )


def _paired_bootstrap(
    differences: Sequence[float],
    *,
    pair_ids: Sequence[str],
    namespace: str,
    config: BootstrapConfig,
    statistic_name: Literal["mean_pair_difference", "median_pair_difference"],
    statistic: Callable[[Sequence[float]], float],
) -> BootstrapResult:
    if len(differences) * config.resamples > MAX_BOOTSTRAP_DRAWS:
        raise ValueError(
            f"paired bootstrap exceeds {MAX_BOOTSTRAP_DRAWS} total draws"
        )
    derived_seed = derive_bootstrap_seed(
        root_seed=config.root_seed,
        namespace=namespace,
        pair_ids=pair_ids,
    )
    rng = random.Random(derived_seed)
    size = len(differences)
    replicates: list[float] = []
    for _ in range(config.resamples):
        sample = [differences[rng.randrange(size)] for _ in range(size)]
        replicates.append(float(statistic(sample)))

    tail = (1.0 - config.confidence_level) / 2.0
    interval = ConfidenceInterval(
        confidence_level=config.confidence_level,
        lower=type7_quantile(replicates, tail),
        upper=type7_quantile(replicates, 1.0 - tail),
        method="paired_percentile_type7",
    )
    return BootstrapResult(
        statistic=statistic_name,
        resamples=config.resamples,
        derived_seed=derived_seed,
        confidence_interval=interval,
    )


def _canonical_binary_pairs(pairs: Sequence[BinaryPair]) -> list[BinaryPair]:
    if any(not isinstance(pair, BinaryPair) for pair in pairs):
        raise TypeError("binary pairs must be BinaryPair instances")
    ordered_ids = _validate_pair_ids([pair.pair_id for pair in pairs])
    by_id = {pair.pair_id: pair for pair in pairs}
    return [by_id[pair_id] for pair_id in ordered_ids]


def analyze_binary_pairs(
    pairs: Sequence[BinaryPair],
    *,
    analysis_id: str,
    bootstrap: BootstrapConfig | None = None,
) -> BinaryPairedResult:
    """Estimate a paired binary risk difference and exact McNemar p-value."""

    analysis_id = _validate_identifier(analysis_id, field="analysis_id")
    config = bootstrap or BootstrapConfig()
    if not isinstance(config, BootstrapConfig):
        raise TypeError("bootstrap must be a BootstrapConfig")
    ordered = _canonical_binary_pairs(pairs)
    pair_ids = [pair.pair_id for pair in ordered]
    differences = [
        float(int(pair.treatment) - int(pair.reference)) for pair in ordered
    ]
    treatment_successes = sum(pair.treatment for pair in ordered)
    reference_successes = sum(pair.reference for pair in ordered)
    treatment_only = sum(
        pair.treatment and not pair.reference for pair in ordered
    )
    reference_only = sum(
        pair.reference and not pair.treatment for pair in ordered
    )
    risk_difference = float(sum(differences) / len(differences))
    bootstrap_result = _paired_bootstrap(
        differences,
        pair_ids=pair_ids,
        namespace=f"binary:{analysis_id}",
        config=config,
        statistic_name="mean_pair_difference",
        statistic=lambda values: sum(values) / len(values),
    )
    return BinaryPairedResult(
        analysis_id=analysis_id,
        pair_count=len(ordered),
        treatment_successes=treatment_successes,
        reference_successes=reference_successes,
        risk_difference=risk_difference,
        bootstrap=bootstrap_result,
        mcnemar=exact_mcnemar(treatment_only, reference_only),
    )


def _canonical_continuous_pairs(
    pairs: Sequence[ContinuousPair],
) -> list[ContinuousPair]:
    if any(not isinstance(pair, ContinuousPair) for pair in pairs):
        raise TypeError("continuous pairs must be ContinuousPair instances")
    ordered_ids = _validate_pair_ids([pair.pair_id for pair in pairs])
    by_id = {pair.pair_id: pair for pair in pairs}
    return [by_id[pair_id] for pair_id in ordered_ids]


def _paired_differences(pairs: Sequence[ContinuousPair]) -> list[float]:
    differences: list[float] = []
    for pair in pairs:
        difference = float(pair.treatment) - float(pair.reference)
        if not math.isfinite(difference):
            raise ValueError(f"pair {pair.pair_id!r} has a non-finite difference")
        differences.append(0.0 if difference == 0.0 else difference)
    return differences


def _rank_nonzero_differences(
    differences: Sequence[float],
) -> tuple[list[tuple[float, float]], list[int]]:
    ordered = sorted((abs(value), value) for value in differences if value != 0.0)
    ranked: list[tuple[float, float]] = []
    tie_sizes: list[int] = []
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][0] == ordered[index][0]:
            end += 1
        average_rank = ((index + 1) + end) / 2.0
        ranked.extend((average_rank, value) for _, value in ordered[index:end])
        if end - index > 1:
            tie_sizes.append(end - index)
        index = end
    return ranked, tie_sizes


def _exact_signed_rank_p_value(scaled_ranks: Sequence[int], observed: int) -> Decimal:
    total = sum(scaled_ranks)
    counts = [0] * (total + 1)
    counts[0] = 1
    reachable = 0
    for rank in scaled_ranks:
        for subtotal in range(reachable, -1, -1):
            count = counts[subtotal]
            if count:
                counts[subtotal + rank] += count
        reachable += rank
    threshold = min(observed, total - observed)
    one_tail = sum(counts[: threshold + 1])
    denominator = 1 << len(scaled_ranks)
    numerator = min(denominator, 2 * one_tail)
    return _decimal_ratio(numerator, denominator)


def _two_sided_normal_p_value(z_value: float) -> Decimal:
    p_value = math.erfc(z_value / math.sqrt(2.0))
    if p_value > 0.0:
        return Decimal(repr(p_value))

    # erfc underflows for extreme but valid statistics.  A short Mills-ratio
    # expansion is stable in precisely that large-z region and avoids p=0.
    inverse_square = 1.0 / (z_value * z_value)
    correction = (
        1.0
        - inverse_square
        + 3.0 * inverse_square**2
        - 15.0 * inverse_square**3
        + 105.0 * inverse_square**4
    )
    log_p = (
        0.5 * math.log(2.0 / math.pi)
        - math.log(z_value)
        - 0.5 * z_value * z_value
        + math.log(correction)
    )
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return Decimal(repr(log_p)).exp()


def wilcoxon_signed_rank(differences: Sequence[float]) -> WilcoxonResult:
    """Return a two-sided paired Wilcoxon signed-rank test."""

    if not differences:
        raise ValueError("Wilcoxon test requires at least one pair")
    if len(differences) > MAX_STATISTICAL_PAIRS:
        raise ValueError(f"Wilcoxon test exceeds {MAX_STATISTICAL_PAIRS} pairs")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in differences
    ):
        raise TypeError("Wilcoxon differences must be numeric and non-boolean")
    normalized = [float(value) for value in differences]
    if any(not math.isfinite(value) for value in normalized):
        raise ValueError("Wilcoxon differences must be finite")

    ranked, tie_sizes = _rank_nonzero_differences(normalized)
    nonzero = len(ranked)
    zero_count = len(normalized) - nonzero
    positive = float(sum(rank for rank, value in ranked if value > 0.0))
    negative = float(sum(rank for rank, value in ranked if value < 0.0))
    statistic = min(positive, negative)
    if nonzero == 0:
        return WilcoxonResult(
            method="degenerate_all_zero",
            pair_count=len(normalized),
            nonzero_pair_count=0,
            zero_difference_count=zero_count,
            positive_rank_sum=0.0,
            negative_rank_sum=0.0,
            statistic=0.0,
            tie_group_sizes=[],
            p_value=Decimal(1),
        )

    if nonzero <= EXACT_WILCOXON_MAX_NONZERO:
        scaled_ranks = [int(round(rank * 2.0)) for rank, _ in ranked]
        observed_scaled = sum(
            rank for rank, (_, value) in zip(scaled_ranks, ranked, strict=True)
            if value > 0.0
        )
        return WilcoxonResult(
            method="exact_sign_permutation",
            pair_count=len(normalized),
            nonzero_pair_count=nonzero,
            zero_difference_count=zero_count,
            positive_rank_sum=positive,
            negative_rank_sum=negative,
            statistic=statistic,
            tie_group_sizes=tie_sizes,
            p_value=_exact_signed_rank_p_value(scaled_ranks, observed_scaled),
        )

    mean = nonzero * (nonzero + 1) / 4.0
    variance = nonzero * (nonzero + 1) * (2 * nonzero + 1) / 24.0
    variance -= sum(size**3 - size for size in tie_sizes) / 48.0
    if variance <= 0.0 or not math.isfinite(variance):
        raise ValueError("Wilcoxon normal variance is not positive and finite")
    z_value = max(0.0, (abs(positive - mean) - 0.5) / math.sqrt(variance))
    return WilcoxonResult(
        method="normal_approximation",
        pair_count=len(normalized),
        nonzero_pair_count=nonzero,
        zero_difference_count=zero_count,
        positive_rank_sum=positive,
        negative_rank_sum=negative,
        statistic=statistic,
        tie_group_sizes=tie_sizes,
        normal_variance=float(variance),
        z_value=float(z_value),
        p_value=_two_sided_normal_p_value(z_value),
    )


def analyze_continuous_pairs(
    pairs: Sequence[ContinuousPair],
    *,
    analysis_id: str,
    bootstrap: BootstrapConfig | None = None,
) -> ContinuousPairedResult:
    """Estimate median paired difference and Wilcoxon signed-rank evidence."""

    analysis_id = _validate_identifier(analysis_id, field="analysis_id")
    config = bootstrap or BootstrapConfig()
    if not isinstance(config, BootstrapConfig):
        raise TypeError("bootstrap must be a BootstrapConfig")
    ordered = _canonical_continuous_pairs(pairs)
    pair_ids = [pair.pair_id for pair in ordered]
    differences = _paired_differences(ordered)
    median_difference = float(std_statistics.median(differences))
    bootstrap_result = _paired_bootstrap(
        differences,
        pair_ids=pair_ids,
        namespace=f"continuous:{analysis_id}",
        config=config,
        statistic_name="median_pair_difference",
        statistic=lambda values: float(std_statistics.median(values)),
    )
    return ContinuousPairedResult(
        analysis_id=analysis_id,
        pair_count=len(ordered),
        median_difference=median_difference,
        bootstrap=bootstrap_result,
        wilcoxon=wilcoxon_signed_rank(differences),
    )


def wilson_interval(
    successes: int,
    total: int,
    *,
    confidence_level: float = 0.95,
) -> ProportionResult:
    """Return a Wilson score interval, or unevaluable for a zero denominator."""

    for name, value in (("successes", successes), ("total", total)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value < 0:
            raise ValueError(f"{name} must be non-negative")
    if successes > total:
        raise ValueError("successes must not exceed total")
    if total > MAX_STATISTICAL_PAIRS:
        raise ValueError(f"Wilson interval exceeds {MAX_STATISTICAL_PAIRS} samples")
    if isinstance(confidence_level, bool) or not isinstance(
        confidence_level, (int, float)
    ):
        raise TypeError("confidence_level must be numeric")
    confidence_level = float(confidence_level)
    if (
        not math.isfinite(confidence_level)
        or confidence_level <= 0.0
        or confidence_level >= 1.0
    ):
        raise ValueError("confidence_level must be finite and between 0 and 1")
    if total == 0:
        return ProportionResult(
            status="unevaluable",
            successes=0,
            total=0,
        )

    estimate = successes / total
    z_value = std_statistics.NormalDist().inv_cdf(
        0.5 + confidence_level / 2.0
    )
    z_squared = z_value * z_value
    denominator = 1.0 + z_squared / total
    center = (estimate + z_squared / (2.0 * total)) / denominator
    half_width = (
        z_value
        / denominator
        * math.sqrt(
            estimate * (1.0 - estimate) / total
            + z_squared / (4.0 * total * total)
        )
    )
    interval = ConfidenceInterval(
        confidence_level=confidence_level,
        lower=max(0.0, center - half_width),
        upper=min(1.0, center + half_width),
        method="wilson_score",
    )
    return ProportionResult(
        status="ok",
        successes=successes,
        total=total,
        estimate=float(estimate),
        confidence_interval=interval,
    )


def holm_adjust(
    *,
    family_id: str,
    planned_hypothesis_ids: Sequence[str],
    observed: Sequence[HypothesisPValue],
    alpha: Decimal = Decimal("0.05"),
) -> HolmFamilyResult:
    """Adjust one complete, pre-registered family with Holm-Bonferroni."""

    family_id = _validate_identifier(family_id, field="family_id")
    planned = sorted(
        _validate_identifier(value, field="hypothesis_id")
        for value in planned_hypothesis_ids
    )
    if not planned:
        raise ValueError("Holm family must pre-register at least one hypothesis")
    if len(planned) > MAX_HOLM_HYPOTHESES:
        raise ValueError(f"Holm family exceeds {MAX_HOLM_HYPOTHESES} hypotheses")
    if len(planned) != len(set(planned)):
        raise ValueError("planned Holm hypothesis IDs must be unique")
    if not isinstance(alpha, Decimal) or not alpha.is_finite():
        raise TypeError("alpha must be a finite Decimal")
    if alpha <= 0 or alpha >= 1:
        raise ValueError("alpha must be between 0 and 1")
    if len(observed) > MAX_HOLM_HYPOTHESES:
        raise ValueError(f"Holm family exceeds {MAX_HOLM_HYPOTHESES} hypotheses")
    if any(not isinstance(item, HypothesisPValue) for item in observed):
        raise TypeError("observed values must be HypothesisPValue instances")
    observed_by_id = {item.hypothesis_id: item for item in observed}
    if len(observed_by_id) != len(observed):
        raise ValueError("observed Holm hypothesis IDs must be unique")
    observed_ids = set(observed_by_id)
    planned_ids = set(planned)
    if observed_ids != planned_ids:
        missing = sorted(planned_ids - observed_ids)
        extra = sorted(observed_ids - planned_ids)
        raise ValueError(
            f"Holm family is incomplete; missing={missing}, extra={extra}"
        )

    ordered = sorted(
        observed,
        key=lambda item: (item.p_value, item.hypothesis_id),
    )
    adjusted_by_id: dict[str, Decimal] = {}
    family_size = len(ordered)
    precision = max(
        50,
        max(len(item.p_value.as_tuple().digits) for item in ordered) + 8,
    )
    with localcontext() as context:
        context.prec = precision
        context.rounding = ROUND_HALF_EVEN
        running = Decimal(0)
        for index, item in enumerate(ordered):
            candidate = item.p_value * Decimal(family_size - index)
            running = max(running, min(Decimal(1), candidate))
            adjusted_by_id[item.hypothesis_id] = +running

    adjustments = [
        HolmAdjustment(
            hypothesis_id=hypothesis_id,
            raw_p_value=observed_by_id[hypothesis_id].p_value,
            adjusted_p_value=adjusted_by_id[hypothesis_id],
            reject_null=adjusted_by_id[hypothesis_id] <= alpha,
        )
        for hypothesis_id in planned
    ]
    return HolmFamilyResult(
        family_id=family_id,
        alpha=alpha,
        hypothesis_count=len(planned),
        adjustment_order=[item.hypothesis_id for item in ordered],
        adjustments=adjustments,
    )


__all__ = [
    "DEFAULT_BOOTSTRAP_RESAMPLES",
    "EXACT_WILCOXON_MAX_NONZERO",
    "MAX_BOOTSTRAP_RESAMPLES",
    "MAX_BOOTSTRAP_DRAWS",
    "MAX_HOLM_HYPOTHESES",
    "MAX_STATISTICAL_PAIRS",
    "STATISTICS_SCHEMA_VERSION",
    "BinaryPair",
    "BinaryPairedResult",
    "BootstrapConfig",
    "BootstrapResult",
    "ConfidenceInterval",
    "ContinuousPair",
    "ContinuousPairedResult",
    "HolmAdjustment",
    "HolmFamilyResult",
    "HypothesisPValue",
    "McNemarResult",
    "ProportionResult",
    "WilcoxonResult",
    "analyze_binary_pairs",
    "analyze_continuous_pairs",
    "derive_bootstrap_seed",
    "exact_mcnemar",
    "holm_adjust",
    "type7_quantile",
    "wilcoxon_signed_rank",
    "wilson_interval",
]
