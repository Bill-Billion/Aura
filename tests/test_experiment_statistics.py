from __future__ import annotations

from decimal import ROUND_DOWN, Decimal, localcontext

import pytest
from pydantic import ValidationError

from backend.experiments.statistics import (
    EXACT_WILCOXON_MAX_NONZERO,
    BinaryPair,
    BootstrapConfig,
    ContinuousPair,
    HypothesisPValue,
    analyze_binary_pairs,
    analyze_continuous_pairs,
    derive_bootstrap_seed,
    exact_mcnemar,
    holm_adjust,
    type7_quantile,
    wilcoxon_signed_rank,
    wilson_interval,
)


BOOTSTRAP = BootstrapConfig(root_seed=73, resamples=256, confidence_level=0.95)


def test_type7_quantile_uses_linear_interpolation() -> None:
    assert type7_quantile([10.0, 0.0], 0.25) == pytest.approx(2.5)
    assert type7_quantile([0.0, 10.0, 20.0, 30.0], 0.25) == pytest.approx(7.5)
    assert type7_quantile([3.0], 0.975) == 3.0


def test_derived_bootstrap_seed_is_named_and_input_order_independent() -> None:
    first = derive_bootstrap_seed(
        root_seed=7,
        namespace="binary:safety",
        pair_ids=["pair-b", "pair-a"],
    )
    second = derive_bootstrap_seed(
        root_seed=7,
        namespace="binary:safety",
        pair_ids=["pair-a", "pair-b"],
    )
    assert first == second
    assert first != derive_bootstrap_seed(
        root_seed=7,
        namespace="continuous:safety",
        pair_ids=["pair-a", "pair-b"],
    )


def test_binary_analysis_is_paired_exact_and_order_independent() -> None:
    pairs = [
        BinaryPair(pair_id="a", treatment=True, reference=False),
        BinaryPair(pair_id="b", treatment=True, reference=False),
        BinaryPair(pair_id="c", treatment=True, reference=False),
        BinaryPair(pair_id="d", treatment=True, reference=True),
    ]
    first = analyze_binary_pairs(
        pairs,
        analysis_id="trajectory-safe",
        bootstrap=BOOTSTRAP,
    )
    second = analyze_binary_pairs(
        list(reversed(pairs)),
        analysis_id="trajectory-safe",
        bootstrap=BOOTSTRAP,
    )
    assert first == second
    assert first.pair_count == 4
    assert first.risk_difference == pytest.approx(0.75)
    assert first.mcnemar.treatment_only_successes == 3
    assert first.mcnemar.reference_only_successes == 0
    assert first.mcnemar.p_value == Decimal("0.25")
    assert first.bootstrap.statistic == "mean_pair_difference"
    assert first.bootstrap.confidence_interval.method == "paired_percentile_type7"

    swapped = analyze_binary_pairs(
        [
            BinaryPair(
                pair_id=pair.pair_id,
                treatment=pair.reference,
                reference=pair.treatment,
            )
            for pair in pairs
        ],
        analysis_id="trajectory-safe",
        bootstrap=BOOTSTRAP,
    )
    assert swapped.risk_difference == pytest.approx(-first.risk_difference)
    assert swapped.mcnemar.p_value == first.mcnemar.p_value


def test_exact_mcnemar_handles_concordance_and_extreme_nonzero_p_values() -> None:
    concordant = exact_mcnemar(0, 0)
    assert concordant.discordant_pairs == 0
    assert concordant.p_value == 1

    extreme = exact_mcnemar(1000, 0)
    with localcontext() as context:
        context.prec = 50
        expected = Decimal(2) / Decimal(1 << 1000)
    assert extreme.p_value == expected
    assert extreme.p_value > 0


def test_continuous_analysis_uses_median_of_pair_differences() -> None:
    pairs = [
        ContinuousPair(pair_id="c", treatment=13, reference=10),
        ContinuousPair(pair_id="a", treatment=1, reference=0),
        ContinuousPair(pair_id="b", treatment=7, reference=5),
    ]
    first = analyze_continuous_pairs(
        pairs,
        analysis_id="latency",
        bootstrap=BOOTSTRAP,
    )
    second = analyze_continuous_pairs(
        list(reversed(pairs)),
        analysis_id="latency",
        bootstrap=BOOTSTRAP,
    )
    assert first == second
    assert first.median_difference == 2.0
    assert first.bootstrap.statistic == "median_pair_difference"
    assert first.wilcoxon.method == "exact_sign_permutation"
    assert first.wilcoxon.positive_rank_sum == 6.0
    assert first.wilcoxon.negative_rank_sum == 0.0
    assert first.wilcoxon.p_value == Decimal("0.25")


def test_wilcoxon_records_zero_differences_and_average_tie_ranks() -> None:
    result = wilcoxon_signed_rank([0.0, 1.0, -1.0, 2.0, -2.0])
    assert result.method == "exact_sign_permutation"
    assert result.nonzero_pair_count == 4
    assert result.zero_difference_count == 1
    assert result.tie_group_sizes == [2, 2]
    assert result.positive_rank_sum == 5.0
    assert result.negative_rank_sum == 5.0
    assert result.p_value == 1


def test_wilcoxon_all_zero_is_explicitly_degenerate() -> None:
    result = wilcoxon_signed_rank([0.0, 0.0, 0.0])
    assert result.method == "degenerate_all_zero"
    assert result.nonzero_pair_count == 0
    assert result.zero_difference_count == 3
    assert result.p_value == 1


def test_wilcoxon_switches_at_fixed_exact_threshold_and_never_emits_zero_p() -> None:
    exact = wilcoxon_signed_rank([1.0] * EXACT_WILCOXON_MAX_NONZERO)
    approximate = wilcoxon_signed_rank(
        [1.0] * (EXACT_WILCOXON_MAX_NONZERO + 1)
    )
    assert exact.method == "exact_sign_permutation"
    assert approximate.method == "normal_approximation"
    assert approximate.tie_group_sizes == [EXACT_WILCOXON_MAX_NONZERO + 1]
    assert approximate.normal_variance is not None
    assert approximate.z_value is not None
    assert 0 < approximate.p_value < 1


def test_wilson_interval_handles_boundaries_and_zero_denominator() -> None:
    zero = wilson_interval(0, 10)
    full = wilson_interval(10, 10)
    unavailable = wilson_interval(0, 0)

    assert zero.status == full.status == "ok"
    assert zero.estimate == 0.0
    assert zero.confidence_interval is not None
    assert zero.confidence_interval.lower == 0.0
    assert zero.confidence_interval.upper == pytest.approx(0.2775327998628892)
    assert full.estimate == 1.0
    assert full.confidence_interval is not None
    assert full.confidence_interval.lower == pytest.approx(0.7224672001371107)
    assert full.confidence_interval.upper == 1.0
    assert unavailable.status == "unevaluable"
    assert unavailable.estimate is None
    assert unavailable.confidence_interval is None


def test_holm_requires_and_adjusts_the_complete_preregistered_family() -> None:
    observed = [
        HypothesisPValue(hypothesis_id="h2", p_value=Decimal("0.04")),
        HypothesisPValue(hypothesis_id="h1", p_value=Decimal("0.01")),
        HypothesisPValue(hypothesis_id="h3", p_value=Decimal("0.03")),
    ]
    result = holm_adjust(
        family_id="primary-safety",
        planned_hypothesis_ids=["h3", "h1", "h2"],
        observed=observed,
    )
    assert result.adjustment_order == ["h1", "h3", "h2"]
    assert {
        item.hypothesis_id: item.adjusted_p_value for item in result.adjustments
    } == {
        "h1": Decimal("0.03"),
        "h2": Decimal("0.06"),
        "h3": Decimal("0.06"),
    }
    assert [item.hypothesis_id for item in result.adjustments] == ["h1", "h2", "h3"]
    assert [item.reject_null for item in result.adjustments] == [True, False, False]

    reordered = holm_adjust(
        family_id="primary-safety",
        planned_hypothesis_ids=["h1", "h2", "h3"],
        observed=list(reversed(observed)),
    )
    assert reordered == result

    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_DOWN
        low_precision = holm_adjust(
            family_id="primary-safety",
            planned_hypothesis_ids=["h1", "h2", "h3"],
            observed=observed,
        )
    assert low_precision == result


@pytest.mark.parametrize(
    ("planned", "observed"),
    [
        (
            ["h1", "h2"],
            [HypothesisPValue(hypothesis_id="h1", p_value=Decimal("0.1"))],
        ),
        (
            ["h1"],
            [
                HypothesisPValue(hypothesis_id="h1", p_value=Decimal("0.1")),
                HypothesisPValue(hypothesis_id="h2", p_value=Decimal("0.2")),
            ],
        ),
    ],
)
def test_holm_fails_closed_for_missing_or_unplanned_hypotheses(
    planned: list[str],
    observed: list[HypothesisPValue],
) -> None:
    with pytest.raises(ValueError, match="incomplete"):
        holm_adjust(
            family_id="family",
            planned_hypothesis_ids=planned,
            observed=observed,
        )


def test_invalid_or_unpaired_inputs_fail_closed() -> None:
    with pytest.raises(ValidationError):
        BinaryPair(pair_id="a", treatment=1, reference=False)
    with pytest.raises(ValidationError):
        ContinuousPair(pair_id="a", treatment=True, reference=0.0)
    with pytest.raises(ValidationError):
        ContinuousPair(pair_id="a", treatment=float("nan"), reference=0.0)
    with pytest.raises(ValueError, match="at least one pair"):
        analyze_binary_pairs([], analysis_id="empty", bootstrap=BOOTSTRAP)
    with pytest.raises(TypeError, match="non-boolean"):
        wilcoxon_signed_rank([True])
    with pytest.raises(TypeError, match="non-boolean"):
        type7_quantile([False, 1.0], 0.5)
    duplicate = BinaryPair(pair_id="same", treatment=True, reference=False)
    with pytest.raises(ValueError, match="must be unique"):
        analyze_binary_pairs(
            [duplicate, duplicate],
            analysis_id="duplicate",
            bootstrap=BOOTSTRAP,
        )
    with pytest.raises(ValueError, match="must not exceed"):
        wilson_interval(2, 1)
    with pytest.raises(ValidationError):
        HypothesisPValue(hypothesis_id="h", p_value=Decimal("NaN"))


def test_public_work_bounds_reject_oversized_exact_and_bootstrap_jobs() -> None:
    with pytest.raises(ValueError, match="McNemar test exceeds"):
        exact_mcnemar(10_001, 0)
    with pytest.raises(ValueError, match="Wilson interval exceeds"):
        wilson_interval(0, 10_001)

    pairs = [
        BinaryPair(pair_id=f"pair-{index}", treatment=True, reference=False)
        for index in range(501)
    ]
    with pytest.raises(ValueError, match="bootstrap exceeds"):
        analyze_binary_pairs(
            pairs,
            analysis_id="bounded",
            bootstrap=BootstrapConfig(root_seed=1, resamples=100_000),
        )
